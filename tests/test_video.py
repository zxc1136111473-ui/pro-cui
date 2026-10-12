"""视频类新节点：AI 改视频 / 视频换人（阿里的异步任务）、视频拼接（本地 ffmpeg）、保存视频。

不依赖 ComfyUI：阿里的请求换成假的（只看发出去的内容和各种失败的处理）；要真的解视频 / 跑 ffmpeg 的测试，需要本机有真的 PyAV 和 ffmpeg，
没有就跳过（系统自带的 python 一般没有 av）。**用虚拟环境跑一遍**（`python3 -m venv v && v/bin/pip install av imageio-ffmpeg numpy pillow requests && v/bin/python -m unittest discover -s tests -p test_video.py`）：
装了 `imageio-ffmpeg` 节点就会用它自带的 ffmpeg（和服务器同一路，服务器上是 7.0.2、本机装出来是 7.1），而不是本机 `brew` 装的新版——
拼接转场的 `xfade` 在 ffmpeg 7.x 上要求固定帧率、8.x 不要求，只在本机新版上测会把这个坑漏掉（真机踩过）。
"""
import email.parser
import email.policy
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import numpy as np
import requests

from _load import load

ali = load("pro-ali")
video = load("pro-video")

FFMPEG = shutil.which("ffmpeg")
REAL_AV = not isinstance(sys.modules.get("av"), mock.MagicMock)
MEDIA = REAL_AV and FFMPEG is not None
needs_media = unittest.skipUnless(MEDIA, "需要真的 PyAV 和 ffmpeg（用装了 av 的虚拟环境跑）")

INFO = json.dumps({"apikey": "k", "custom_api_base": "https://dashscope.aliyuncs.com/compatible-mode"})


class Img:
    """假的图片张量：只要 shape = (批, 高, 宽, 通道)，要转 JPEG 时给一张纯色图。"""
    def __init__(self, w, h):
        self.shape = (1, h, w, 3)
        self.arr = np.full((h, w, 3), 0.5, dtype=np.float32)

    def __getitem__(self, i):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.arr


class Vid:
    """假的 VIDEO：和 ComfyUI 的 VideoFromFile 一样用 get_stream_source() 给出路径（或内存里的 BytesIO）。"""
    def __init__(self, src):
        self.src = src

    def get_stream_source(self):
        return self.src


class FakeVideoFromFile:
    def __init__(self, path):
        self.path = path

    def get_stream_source(self):
        return self.path


class FakeFolders:
    """顶替 ComfyUI 的 folder_paths：output / temp 都在一个临时目录里，文件名编号规则和真的一样（前缀_00001_.扩展名）。"""
    def __init__(self, root):
        self.out, self.tmp = os.path.join(root, "output"), os.path.join(root, "temp")
        os.makedirs(self.out)
        os.makedirs(self.tmp)

    def get_output_directory(self):
        return self.out

    def get_temp_directory(self):
        return self.tmp

    def get_save_image_path(self, prefix, out_dir):
        sub, _, name = prefix.rpartition("/")
        folder = os.path.join(out_dir, sub) if sub else out_dir
        os.makedirs(folder, exist_ok=True)
        n = 1 + len([f for f in os.listdir(folder) if f.startswith(name + "_")])
        return folder, name, n, sub, prefix


def parse_form(body, content_type):
    """multipart/form-data 的请求体 → ([(字段名, 内容字节, 文件名)] 按出现顺序)。"""
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    return [(p.get_param("name", header="content-disposition"), p.get_payload(decode=True), p.get_filename()) for p in msg.iter_parts()]


def frame_at(path, t):
    """用 ffmpeg 取 t 秒处的一帧（RGB 数组）。ffmpeg 取帧时会按旋转标记自动转正，拿它当「应该长成什么样」的参照。"""
    from PIL import Image
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", path, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True)
    return np.asarray(Image.open(io.BytesIO(r.stdout)).convert("RGB")).astype(float)


def read(path):
    with open(path, "rb") as f:
        return f.read()


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def make_clip(path, seconds, size, rate=24, audio=True, pattern="testsrc", fmt=None):
    """用本机 ffmpeg 造一段测试视频。"""
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"{pattern}=duration={seconds}:size={size[0]}x{size[1]}:rate={rate}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"] + (["-c:a", "aac", "-shortest"] if audio else []) + ([] if fmt is None else ["-f", fmt]) + [path]
    subprocess.run(args, check=True)
    return path


# ════════════════════════════════════════════════════════════════════════════
# 阿里：上传 / 提交 / 查询 / 下载（全是假的请求）
# ════════════════════════════════════════════════════════════════════════════
class FakeSess:
    """顶替 ali._open 给的会话：get 返回测试给的东西。"""
    def __init__(self, get):
        self.get = get

    def close(self):
        pass


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("不是 JSON")
        return self._body


POLICY = {"data": {"upload_host": "https://dashscope-file-mgr.oss-cn-beijing.aliyuncs.com", "upload_dir": "dashscope-instant/abc/2026-10-12/xyz",
                   "oss_access_key_id": "AK", "signature": "SIG", "policy": "POL", "x_oss_object_acl": "private", "x_oss_forbid_overwrite": "true", "max_file_size_mb": 100}}


class OssUpload(unittest.TestCase):
    def run_upload(self, policy=POLICY, post=None, get_status=200, data=b"abc", name="a.mp4", gets=None):
        gets = gets if gets is not None else []

        def get(url, params=None, headers=None, timeout=None):
            gets.append((url, params, headers))
            return Resp(get_status, policy)
        posts = []

        def default_post(url, data=None, headers=None, timeout=None):
            posts.append((url, data.read(), headers, timeout))
            return Resp(200)
        with mock.patch.object(ali, "_open", lambda base, what, who="阿里": FakeSess(get)), mock.patch.object(ali.requests, "post", post or default_post):
            out = ali._oss_upload("https://dashscope.aliyuncs.com", "k", "wan2.2-animate-mix", data, name, "视频换人")
        return out, posts, gets

    def test_the_policy_is_asked_for_the_model_and_the_form_has_the_file_last(self):
        out, posts, gets = self.run_upload()
        self.assertEqual(out, "oss://dashscope-instant/abc/2026-10-12/xyz/a.mp4")
        url, params, headers = gets[0]
        self.assertEqual((url, params, headers["Authorization"]), ("https://dashscope.aliyuncs.com/api/v1/uploads", {"action": "getPolicy", "model": "wan2.2-animate-mix"}, "Bearer k"))
        host, body, headers, timeout = posts[0]
        self.assertEqual(host, POLICY["data"]["upload_host"])
        form = parse_form(body, headers["Content-Type"])
        self.assertEqual(form[-1], ("file", b"abc", "a.mp4"))                                      # file 必须是最后一个表单域
        self.assertEqual({k: v.decode() for k, v, _ in form[:-1]}, {"OSSAccessKeyId": "AK", "Signature": "SIG", "policy": "POL", "x-oss-object-acl": "private",
                                                                    "x-oss-forbid-overwrite": "true", "key": "dashscope-instant/abc/2026-10-12/xyz/a.mp4", "success_action_status": "200"})
        self.assertEqual(timeout, ali.UPLOAD_TIMEOUT)

    def test_the_api_key_is_never_sent_to_the_upload_host(self):
        out, posts, gets = self.run_upload()
        self.assertNotIn(b"k", [v for k, v, _ in parse_form(posts[0][1], posts[0][2]["Content-Type"]) if k != "file" and v == b"k"])
        self.assertNotIn("Authorization", posts[0][2])
        self.assertNotIn(b"Bearer", posts[0][1])

    def test_an_upload_host_that_is_not_alibaba_https_is_refused_without_sending_the_file(self):
        for host in ("http://dashscope-file-mgr.oss-cn-beijing.aliyuncs.com", "https://evil.example.com", "https://aliyuncs.com.evil.com", "https://oss.aliyuncs.com.cn", ""):
            policy = {"data": dict(POLICY["data"], upload_host=host)}
            posted = []
            with self.assertRaises(RuntimeError) as cm:
                self.run_upload(policy=policy, post=lambda *a, **k: posted.append(1) or Resp(200))
            self.assertIn("上传地址不对", str(cm.exception), host)
            self.assertEqual(posted, [], host)

    def test_a_file_bigger_than_the_policy_limit_is_refused_before_uploading(self):
        policy = {"data": dict(POLICY["data"], max_file_size_mb=1)}
        posted = []
        with self.assertRaises(RuntimeError) as cm:
            self.run_upload(policy=policy, data=b"x" * (2 * 1024 * 1024), post=lambda *a, **k: posted.append(1) or Resp(200))
        self.assertIn("文件太大", str(cm.exception))
        self.assertEqual(posted, [])

    def test_a_rejected_key_is_reported_at_once_without_retrying(self):
        gets = []
        with self.assertRaises(RuntimeError) as cm:
            self.run_upload(policy={"code": "InvalidApiKey", "message": "No API-key provided."}, get_status=401, gets=gets)
        self.assertIn("拿上传凭证失败", str(cm.exception))
        self.assertEqual(len(gets), 1)

    def test_connection_trouble_is_retried_and_after_three_failures_says_nothing_was_charged(self):
        calls = []

        def flaky(url, data=None, headers=None, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise requests.ConnectionError("断了")
            return Resp(200)
        out, posts, gets = self.run_upload(post=flaky)
        self.assertEqual((out.startswith("oss://"), len(calls)), (True, 3))
        calls.clear()
        with self.assertRaises(RuntimeError) as cm:
            self.run_upload(post=lambda *a, **k: (calls.append(1), (_ for _ in ()).throw(requests.ConnectionError("断了")))[1])
        self.assertEqual(len(calls), 3)
        self.assertIn("不会计费", str(cm.exception))

    def test_stopping_during_the_upload_aborts_it_and_is_not_retried(self):
        class InterruptProcessingException(Exception):
            pass

        def stop():
            raise InterruptProcessingException()
        posts = []

        def post(url, data=None, headers=None, timeout=None):
            posts.append(1)
            data.read(100)                                                                    # requests 发请求体时会一块块读：这时「停止」就抛出来
            return Resp(200)
        with mock.patch.object(ali, "_check_interrupt", stop):
            with self.assertRaises(InterruptProcessingException):
                self.run_upload(post=post)
        self.assertEqual(len(posts), 1)

    def test_a_non_200_from_the_storage_is_a_failure(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_upload(post=lambda url, data=None, headers=None, timeout=None: Resp(403, text="Policy expired"))
        self.assertIn("上传到阿里的临时存储失败", str(cm.exception))


class FormBody(unittest.TestCase):
    FIELDS = [("a", "1"), ("key", "dir/文件.mp4")]

    def test_it_reads_back_as_the_same_multipart_whatever_the_chunk_size(self):
        data = os.urandom(5000)
        for n in (1, 7, 100, 4096, 10 ** 6, -1, None):
            b = ali._FormBody(self.FIELDS, "x.mp4", data)
            got, chunk = [], 0
            while True:
                piece = b.read(n)
                if not piece:
                    break
                if n and n > 0:
                    self.assertLessEqual(len(piece), n, n)                                  # 一块不超过要的大小：请求一块块发，每块有自己的超时
                got.append(piece)
                chunk += 1
                if n in (-1, None, 10 ** 6):
                    break
            whole = b"".join(got)
            self.assertEqual(len(whole), len(b), n)                                         # __len__ = 实际字节数：requests 靠它写 Content-Length，不走分块传输
            form = parse_form(whole, b.content_type)
            self.assertEqual([(k, v, f) for k, v, f in form], [("a", b"1", None), ("key", "dir/文件.mp4".encode(), None), ("file", data, "x.mp4")], n)
            self.assertEqual(b.read(10), b"")

    def test_the_stop_hook_runs_before_every_piece_so_an_upload_can_be_aborted(self):
        calls = []
        b = ali._FormBody([], "x.bin", b"abc" * 100, check=lambda: calls.append(1))
        b.read(10)
        b.read(10)
        self.assertEqual(len(calls), 2)

        class Stop(Exception):
            pass

        def stop():
            raise Stop()
        with self.assertRaises(Stop):
            ali._FormBody([], "x.bin", b"x", check=stop).read(5)

    def test_an_empty_file_and_a_big_one_do_not_get_copied(self):
        b = ali._FormBody([], "e.bin", b"")
        self.assertEqual(parse_form(b.read(), b.content_type), [("file", b"", "e.bin")])
        big = bytearray(30 * 1024 * 1024)
        b = ali._FormBody(self.FIELDS, "big.bin", big)
        self.assertIs(b.parts[1].obj, big)                                                  # 用 memoryview 引用原数据，不复制一份

    def test_it_goes_through_requests_with_a_content_length_and_in_small_pieces(self):
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or -1)
                seen["headers"] = dict(self.headers)
                seen["body"] = self.rfile.read(n) if n >= 0 else b"<<no Content-Length>>"
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        class Srv(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):                                  # 客户端发完就关连接，服务端读下一个请求时会报「连接被重置」：不是问题，别往屏幕上打
                pass
        srv = Srv(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            data = os.urandom(300000)
            b = ali._FormBody(self.FIELDS, "v.mp4", data)
            r = requests.post(f"http://127.0.0.1:{srv.server_address[1]}/", data=b, headers={"Content-Type": b.content_type}, timeout=(5, 5))
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("Transfer-Encoding", seen["headers"])                          # OSS 的表单上传不收分块传输
            self.assertEqual(int(seen["headers"]["Content-Length"]), len(b))
            self.assertEqual(parse_form(seen["body"], seen["headers"]["Content-Type"])[-1], ("file", data, "v.mp4"))
        finally:
            srv.shutdown()
            srv.server_close()


class SubmitAndWait(unittest.TestCase):
    def test_the_task_is_submitted_once_with_the_async_and_oss_headers(self):
        seen = {}

        def send(base, key, path, body, timeout, what, who="阿里", headers=None):
            seen.update(path=path, headers=headers, body=body, key=key)
            return Resp(200, {"output": {"task_id": "T1", "task_status": "PENDING"}})
        with mock.patch.object(ali, "_send", send):
            tid = ali._submit_video_task("https://dashscope.aliyuncs.com", "k", "swap", {"model": "m"}, "视频换人")
        self.assertEqual(tid, "T1")
        self.assertEqual(seen["path"], "/api/v1/services/aigc/image2video/video-synthesis")
        self.assertEqual(seen["headers"], {"X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable"})
        with mock.patch.object(ali, "_send", lambda *a, **k: Resp(200, {"output": {}})):
            with self.assertRaises(RuntimeError) as cm:
                ali._submit_video_task("b", "k", "edit", {}, "改视频")
        self.assertIn("没有拿到任务编号", str(cm.exception))
        with mock.patch.object(ali, "_send", lambda *a, **k: Resp(400, {"code": "InvalidParameter", "message": "bad"})):
            with self.assertRaises(RuntimeError) as cm:
                ali._submit_video_task("b", "k", "edit", {}, "改视频")
        self.assertIn("InvalidParameter", str(cm.exception))
        self.assertEqual(ali.VIDEO_API["edit"], "/api/v1/services/aigc/video-generation/video-synthesis")

    def wait(self, replies, **kw):
        """replies：依次给的回复（Resp 或要抛的异常）；返回 (结果或异常, 查了几次, 取消了几次)。"""
        it = iter(replies)
        calls, cancels = [], []

        class Sess:
            def get(self, url, headers=None, timeout=None):
                calls.append(url)
                r = next(it)
                if isinstance(r, Exception):
                    raise r
                return r

            def close(self):
                pass
        with mock.patch.object(ali.requests, "Session", Sess), mock.patch.object(ali, "_cancel_task", lambda *a: cancels.append(a)), mock.patch.object(ali.time, "sleep", lambda s: None):
            try:
                out = ali._wait_task("https://dashscope.aliyuncs.com", "k", "T1", "改视频", poll_s=0, **kw)
            except BaseException as e:
                out = e
        return out, calls, cancels

    @staticmethod
    def st(status, **out):
        return Resp(200, {"output": dict({"task_status": status}, **out), "usage": {"duration": 8}})

    def test_it_keeps_asking_until_the_task_succeeds(self):
        out, calls, _ = self.wait([self.st("PENDING"), self.st("RUNNING"), self.st("SUCCEEDED", video_url="https://x/y.mp4")])
        self.assertEqual(out["output"]["video_url"], "https://x/y.mp4")
        self.assertEqual(calls, ["https://dashscope.aliyuncs.com/api/v1/tasks/T1"] * 3)

    def test_a_failed_task_says_why_in_plain_words_and_that_it_costs_nothing(self):
        out, _, _ = self.wait([self.st("FAILED", code="DataInspectionFailed", message="input image fail")])
        self.assertIsInstance(out, RuntimeError)
        self.assertIn("审核", str(out))
        self.assertIn("不收费", str(out))
        out, _, _ = self.wait([self.st("FAILED", code="IPInfringementSuspect", message="x")])
        self.assertIn("侵权", str(out))
        out, _, _ = self.wait([self.st("FAILED", code="InvalidImage.NoHuman", message="The input image has no human body. Please upload other image with single person.")])
        self.assertIn("没有检测到人", str(out))                                                           # 真跑见过的错误码：整个码先找
        self.assertIn("The input image has no human body", str(out))                                    # 原文也留着
        out, _, _ = self.wait([self.st("FAILED", code="InvalidImage.Whatever", message="x")])
        self.assertIn("人物照片没通过检查", str(out))                                                     # 没见过的同类错误码：按前缀
        out, _, _ = self.wait([self.st("FAILED", code="InvalidVideo.Duration", message="Invalid video duration. The file duration must be 2s ~ 30s.")])
        self.assertIn("视频没通过检查", str(out))
        out, _, _ = self.wait([self.st("FAILED", code="InvalidParameter", message="Field required: input.media")])
        self.assertIn("Field required: input.media", str(out))
        for status, word in (("CANCELED", "取消"), ("UNKNOWN", "不见了")):
            out, _, _ = self.wait([self.st(status)])
            self.assertIn(word, str(out), status)

    def test_a_flaky_connection_is_waited_out_but_eight_failures_in_a_row_give_up_with_the_task_id(self):
        out, calls, _ = self.wait([requests.ConnectionError("x"), Resp(503, {}), self.st("SUCCEEDED", video_url="u")])
        self.assertEqual(out["output"]["video_url"], "u")
        out, calls, _ = self.wait([requests.ConnectionError("x")] * ali.POLL_FAILS)
        self.assertIsInstance(out, RuntimeError)
        self.assertEqual(len(calls), ali.POLL_FAILS)
        self.assertIn("T1", str(out))
        self.assertIn("先别重跑", str(out))
        out, calls, _ = self.wait([requests.ConnectionError("x")] * 7 + [self.st("RUNNING")] + [requests.ConnectionError("x")] * 7 + [self.st("SUCCEEDED", video_url="u")])   # 中间好过一次，计数清零
        self.assertEqual(out["output"]["video_url"], "u")

    def test_a_reply_without_a_task_status_is_not_waited_on_forever(self):
        odd = Resp(200, {"output": {}, "request_id": "r"})
        out, calls, _ = self.wait([odd, self.st("SUCCEEDED", video_url="u")])
        self.assertEqual(out["output"]["video_url"], "u")                                       # 偶尔一次格式不对：当成没查到，下一次好了就好了
        out, calls, _ = self.wait([odd] * ali.POLL_FAILS)
        self.assertIsInstance(out, RuntimeError)
        self.assertEqual(len(calls), ali.POLL_FAILS)
        self.assertIn("T1", str(out))

    def test_other_http_errors_are_not_waited_out(self):
        out, calls, _ = self.wait([Resp(401, {"code": "InvalidApiKey", "message": "No API-key provided."})])
        self.assertIn("InvalidApiKey", str(out))
        self.assertEqual(len(calls), 1)

    def test_stopping_cancels_the_queued_task_and_a_timeout_does_too(self):
        class InterruptProcessingException(Exception):
            pass

        def stop():
            raise InterruptProcessingException()
        it = iter([self.st("RUNNING")] * 3)

        class Sess:
            def get(self, *a, **k):
                return next(it)

            def close(self):
                pass
        cancels = []
        with mock.patch.object(ali.requests, "Session", Sess), mock.patch.object(ali, "_check_interrupt", stop), mock.patch.object(ali.time, "sleep", lambda s: None), \
                mock.patch.object(ali, "_cancel_task", lambda *a: cancels.append(a)):
            with self.assertRaises(InterruptProcessingException):
                ali._wait_task("https://dashscope.aliyuncs.com", "k", "T1", "改视频", poll_s=1)
        self.assertEqual(cancels, [("https://dashscope.aliyuncs.com", "k", "T1")])
        ticks = iter(range(0, 100000, 400))                    # 每次看表都过去 400 秒
        with mock.patch.object(ali.time, "monotonic", lambda: next(ticks)):
            out, calls, cancels = self.wait([self.st("RUNNING")] * 5, timeout_s=600)
        self.assertIsInstance(out, RuntimeError)
        self.assertIn("还没出结果", str(out))
        self.assertIn("T1", str(out))
        self.assertEqual(len(cancels), 1)

    def test_cancel_is_best_effort(self):
        with mock.patch.object(ali.requests, "post", side_effect=requests.ConnectionError("x")):
            ali._cancel_task("https://dashscope.aliyuncs.com", "k", "T1")                       # 出错也不抛


class Download(unittest.TestCase):
    def test_the_result_is_streamed_to_disk_and_a_failed_download_names_the_url(self):
        d = tempfile.mkdtemp()
        try:
            class R:
                def __init__(self, chunks, status=200):
                    self.chunks, self.status = chunks, status

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def raise_for_status(self):
                    if self.status >= 400:
                        raise requests.HTTPError(response=mock.Mock(status_code=self.status))

                def iter_content(self, n):
                    return iter(self.chunks)
            path = os.path.join(d, "a.mp4")
            with mock.patch.object(ali.requests, "get", lambda *a, **k: R([b"ab", b"cd"])):
                ali._download_file("https://x/y.mp4", path, "改视频")
            self.assertEqual(read(path), b"abcd")
            self.assertFalse(os.path.exists(path + ".part"))
            gets = []
            with mock.patch.object(ali.requests, "get", lambda *a, **k: (gets.append(1), R([], 403))[1]):
                with self.assertRaises(RuntimeError) as cm:
                    ali._download_file("https://x/gone.mp4", os.path.join(d, "b.mp4"), "改视频")
            self.assertEqual(len(gets), 1)                                                     # 4xx 重试没用
            self.assertIn("https://x/gone.mp4", str(cm.exception))
            self.assertIn("已经做好并计费", str(cm.exception))
            gets.clear()
            with mock.patch.object(ali.requests, "get", lambda *a, **k: (gets.append(1), R([], 502))[1]):
                with self.assertRaises(RuntimeError):
                    ali._download_file("https://x/y.mp4", os.path.join(d, "c.mp4"), "改视频")
            self.assertEqual(len(gets), 3)                                                     # 5xx 重试
        finally:
            shutil.rmtree(d)

    def test_stopping_while_downloading_leaves_no_half_file(self):
        d = tempfile.mkdtemp()
        try:
            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def raise_for_status(self):
                    pass

                def iter_content(self, n):
                    return iter([b"ab", b"cd", b"ef"])

            class Stop(Exception):
                pass
            seen = []

            def check():
                seen.append(1)
                if len(seen) == 2:
                    raise Stop()
            path = os.path.join(d, "a.mp4")
            with mock.patch.object(ali.requests, "get", lambda *a, **k: R()), mock.patch.object(ali, "_check_interrupt", check):
                with self.assertRaises(Stop):
                    ali._download_file("https://x/y.mp4", path, "改视频")
            self.assertEqual(os.listdir(d), [])                                               # 没下完的 .part 也删掉，更不会留一个残缺的成品
        finally:
            shutil.rmtree(d)

    def test_finish_video_takes_either_result_shape_and_lands_in_the_temp_dir(self):
        d = tempfile.mkdtemp()
        try:
            folders = FakeFolders(d)
            stale = os.path.join(folders.tmp, "pro_ali_1_aaaaaa.mp4")
            other = os.path.join(folders.tmp, "keep.mp4")
            for p in (stale, other):
                write(p, b"x")
            os.utime(stale, (1, 1))                                                          # 很老的旧文件：顺手清掉
            os.utime(other, (1, 1))                                                          # 不是自己留下的：不动
            got = []
            with mock.patch.dict(sys.modules, {"folder_paths": folders}), mock.patch.object(ali, "_download_file", lambda url, path, what: (got.append((url, path)), write(path, b"v"))), \
                    mock.patch.object(ali.InputImpl, "VideoFromFile", FakeVideoFromFile):
                v1 = ali._finish_video({"output": {"video_url": "https://x/1.mp4"}}, "改视频", "T")                         # 改视频：output.video_url
                v2 = ali._finish_video({"output": {"results": {"video_url": "https://x/2.mp4"}}}, "视频换人", "T")           # 换人：output.results.video_url
                with self.assertRaises(RuntimeError) as cm:
                    ali._finish_video({"output": {}}, "改视频", "T9")
            self.assertEqual([u for u, _ in got], ["https://x/1.mp4", "https://x/2.mp4"])
            self.assertTrue(all(os.path.dirname(p) == folders.tmp and os.path.basename(p).startswith("pro_ali_") for _, p in got))
            self.assertNotEqual(got[0][1], got[1][1])
            self.assertEqual((v1.path, v2.path), (got[0][1], got[1][1]))
            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(other))
            self.assertIn("T9", str(cm.exception))
        finally:
            shutil.rmtree(d)


# ════════════════════════════════════════════════════════════════════════════
# 两个阿里节点：发出去的内容（视频整理 / 上传 / 提交 / 查询 / 下载全部换成假的）
# ════════════════════════════════════════════════════════════════════════════
class VideoNodes(unittest.TestCase):
    def patches(self, seen, prepared=None):
        prepared = prepared or (b"VIDEO", "v.mp4", 6.0, [])
        cls = self

        def upload(base, key, model, data, name, what):
            seen.setdefault("uploads", []).append((model, data, name))
            return f"oss://dir/{name}"

        def submit(base, key, api, body, what):
            seen.update(api=api, body=body, base=base, key=key)
            return "T1"
        return [mock.patch.object(ali, "_prepare_video", lambda src, model, what: (seen.setdefault("prepared", []).append((src, model)), prepared)[1]),
                mock.patch.object(ali, "_oss_upload", upload), mock.patch.object(ali, "_submit_video_task", submit),
                mock.patch.object(ali, "_wait_task", lambda base, key, tid, what: {"output": {"video_url": "u"}, "usage": {"duration": 12}}),
                mock.patch.object(ali, "_finish_video", lambda d, what, tid: "VID"),
                mock.patch.object(ali, "_to_data_url", lambda t: "data:image/jpeg;base64,REF")]

    def run_node(self, fn, seen, **kw):
        ps = self.patches(seen)
        for p in ps:
            p.start()
        try:
            return fn(**kw)
        finally:
            for p in reversed(ps):
                p.stop()

    def edit(self, seen=None, **kw):
        seen = seen if seen is not None else {}
        args = dict(video=Vid("/in/v.mp4"), prompt="把背景换成海边", model="wan2.7-videoedit", resolution="720P", audio="保留原视频的声音", duration=0,
                    ratio="跟原视频一样", prompt_extend=True, watermark=False, seed=7, info=INFO)
        args.update(kw)
        return self.run_node(ali.ProAliVideoEdit().run, seen, **args), seen

    def swap(self, seen=None, **kw):
        seen = seen if seen is not None else {}
        args = dict(image=Img(800, 1000), video=Vid("/in/v.mp4"), authorized=True, quality=next(iter(ali.SWAP_QUALITY)), seed=1, info=INFO)
        args.update(kw)
        return self.run_node(ali.ProAliVideoSwap().run, seen, **args), seen

    def test_edit_sends_the_video_then_the_reference_images_and_the_wan27_parameters(self):
        (vid, resp), seen = self.edit(ref_image1=Img(500, 500), ref_image2=Img(600, 400))
        self.assertEqual(vid, "VID")
        self.assertEqual((seen["api"], seen["key"], seen["base"]), ("edit", "k", "https://dashscope.aliyuncs.com"))
        b = seen["body"]
        self.assertEqual(b["model"], "wan2.7-videoedit")
        self.assertEqual(b["input"]["prompt"], "把背景换成海边")
        self.assertEqual([m["type"] for m in b["input"]["media"]], ["video", "reference_image", "reference_image"])
        self.assertEqual(b["input"]["media"][0]["url"], "oss://dir/v.mp4")
        self.assertEqual(b["input"]["media"][1]["url"], "data:image/jpeg;base64,REF")
        self.assertEqual(b["parameters"], {"resolution": "720P", "watermark": False, "audio_setting": "origin", "seed": 7, "prompt_extend": True})
        self.assertEqual(seen["uploads"], [("wan2.7-videoedit", b"VIDEO", "v.mp4")])                  # 只有视频走临时存储；参考图是 base64
        r = json.loads(resp)
        self.assertEqual((r["code"], r["model"], r["task_id"], r["usage"]), ("success", "wan2.7-videoedit", "T1", {"duration": 12}))

    def test_edit_optional_parameters_only_go_out_when_set(self):
        (_, _), seen = self.edit(ratio="9:16", duration=4, audio="让 AI 决定（指令里写了声音才会重新生成）", prompt_extend=False, watermark=True, resolution="1080P")
        self.assertEqual(seen["body"]["parameters"], {"resolution": "1080P", "watermark": True, "audio_setting": "auto", "seed": 7, "prompt_extend": False, "ratio": "9:16", "duration": 4})
        (_, _), seen = self.edit(duration=1)                                                          # 时长要么 0 要么 2~10
        self.assertEqual(seen["body"]["parameters"]["duration"], 2)
        (_, _), seen = self.edit(duration=10)                                                         # 比视频还长就取视频的长度
        self.assertEqual(seen["body"]["parameters"]["duration"], 6)

    def test_happyhorse_gets_no_wan27_only_parameters(self):
        (_, resp), seen = self.edit(model="happyhorse-1.0-video-edit", ratio="9:16", duration=4, ref_image1=Img(500, 500))
        self.assertEqual(seen["body"]["model"], "happyhorse-1.0-video-edit")
        self.assertEqual(seen["body"]["parameters"], {"resolution": "720P", "watermark": False, "audio_setting": "origin", "seed": 7})
        self.assertIn("不能改比例", json.dumps(json.loads(resp)["notes"], ensure_ascii=False))
        self.assertEqual(seen["prepared"], [("/in/v.mp4", "happyhorse-1.0-video-edit")])
        self.assertEqual(seen["uploads"][0][0], "happyhorse-1.0-video-edit")                          # 临时存储的文件只能给同一个模型用

    def test_edit_refuses_bad_input_before_uploading_anything(self):
        for kw, word in (({"prompt": "  "}, "是空的"), ({"model": "wan9"}, "不认识的模型"), ({"ref_image1": Img(100, 100)}, "太小"), ({"ref_image1": Img(3000, 300)}, "太窄长"),
                         ({"ref_image1": Img(300, 300), "ref_image2": Img(300, 300), "ref_image3": Img(300, 300), "ref_image4": Img(300, 300), "model": "happyhorse-1.0-video-edit"}, None)):
            seen = {}
            if word is None:
                self.edit(seen, **kw)                                                                   # 4 张没超过 happyhorse 的 5 张
                continue
            with self.assertRaises(RuntimeError) as cm:
                self.edit(seen, **kw)
            self.assertIn(word, str(cm.exception), kw)
            self.assertNotIn("uploads", seen, kw)
        with mock.patch.dict(ali.VIDEO_LIMITS["wan2.7-videoedit"], {"refs": 1}):                      # 上限是模型的：调小了就按调小的拦
            seen = {}
            with self.assertRaises(RuntimeError) as cm:
                self.edit(seen, ref_image1=Img(300, 300), ref_image2=Img(300, 300))
            self.assertIn("最多 1 张参考图", str(cm.exception))
            self.assertNotIn("uploads", seen)

    def test_swap_needs_the_consent_tick_and_sends_nothing_without_it(self):
        seen = {}
        with self.assertRaises(RuntimeError) as cm:
            self.swap(seen, authorized=False)
        self.assertIn("请先勾选", str(cm.exception))
        self.assertIn("已获授权", str(cm.exception))
        self.assertNotIn("uploads", seen)
        self.assertNotIn("body", seen)

    def test_swap_sends_the_person_image_and_the_video_and_the_chosen_quality(self):
        (vid, resp), seen = self.swap()
        self.assertEqual(vid, "VID")
        self.assertEqual(seen["api"], "swap")
        b = seen["body"]
        self.assertEqual(b["model"], "wan2.2-animate-mix")
        self.assertEqual(b["input"]["video_url"], "oss://dir/v.mp4")
        self.assertTrue(b["input"]["image_url"].startswith("oss://dir/") and b["input"]["image_url"].endswith(".jpg"))
        self.assertEqual((b["input"]["watermark"], b["parameters"]), (False, {"mode": "wan-std", "check_image": True}))
        self.assertEqual([m for m, _, _ in seen["uploads"]], ["wan2.2-animate-mix", "wan2.2-animate-mix"])           # 图和视频都按这个模型传
        self.assertEqual(seen["uploads"][0][1][:2], b"\xff\xd8")                                                        # 人物照片传的是 JPEG
        (_, _), seen = self.swap(quality="专业（wan-pro，更流畅、更贵）", check_image=False, watermark=True)
        self.assertEqual((seen["body"]["input"]["watermark"], seen["body"]["parameters"]), (True, {"mode": "wan-pro", "check_image": False}))

    def test_swap_refuses_a_photo_that_is_too_small_or_too_wide_before_uploading(self):
        for img, word in ((Img(150, 150), "太小"), (Img(2400, 600), "太窄长")):
            seen = {}
            with self.assertRaises(RuntimeError) as cm:
                self.swap(seen, image=img)
            self.assertIn(word, str(cm.exception))
            self.assertIn("人物照片", str(cm.exception))
            self.assertNotIn("uploads", seen)

    def test_the_inputs_and_widgets_are_what_the_workflows_assume(self):
        e, s = ali.ProAliVideoEdit.INPUT_TYPES(), ali.ProAliVideoSwap.INPUT_TYPES()
        self.assertEqual(list(e["required"]), ["video", "prompt", "model", "resolution", "audio", "duration", "ratio", "prompt_extend", "watermark", "seed"])
        self.assertEqual(list(e["optional"]), ["ref_image1", "ref_image2", "ref_image3", "ref_image4", "info"])
        self.assertEqual(e["required"]["model"][0], ["wan2.7-videoedit", "happyhorse-1.0-video-edit"])
        self.assertEqual(list(s["required"]), ["image", "video", "authorized", "quality", "seed"])
        self.assertFalse(s["required"]["authorized"][1]["default"])                                  # 授权确认默认不勾
        self.assertEqual(list(s["optional"]), ["check_image", "watermark", "info"])
        for node in (ali.ProAliVideoEdit, ali.ProAliVideoSwap):
            self.assertFalse(getattr(node, "OUTPUT_NODE", False), node.__name__)                      # 不是输出节点：由「保存视频」存
            self.assertEqual(node.RETURN_TYPES, ("VIDEO", "STRING"))

    def test_the_limits_match_the_documentation(self):
        L = ali.VIDEO_LIMITS
        self.assertEqual(L["wan2.2-animate-mix"]["seconds"], (2, 30))
        self.assertEqual(L["wan2.7-videoedit"]["seconds"], (2, 10))
        self.assertEqual(L["happyhorse-1.0-video-edit"]["seconds"], (3, 15))
        self.assertEqual({m: L[m]["api"] for m in L}, {"wan2.2-animate-mix": "swap", "wan2.7-videoedit": "edit", "happyhorse-1.0-video-edit": "edit"})
        self.assertEqual(set(ali.EDIT_VIDEO_MODELS), {m for m in L if L[m]["api"] == "edit"})


class ImageBytes(unittest.TestCase):
    def test_a_photo_becomes_a_jpeg_within_the_size_limit(self):
        data = ali._image_bytes(Img(4000, 3000))
        self.assertEqual(data[:2], b"\xff\xd8")
        from PIL import Image
        self.assertLessEqual(max(Image.open(io.BytesIO(data)).size), 2048)
        noisy = Img(1000, 1000)
        noisy.arr = np.random.RandomState(0).rand(1000, 1000, 3).astype(np.float32)               # 噪声图压不动：压到最低质量还超限就报错
        with self.assertRaises(RuntimeError) as cm:
            ali._image_bytes(noisy, max_mb=0.01)
        self.assertIn("超过", str(cm.exception))


# ════════════════════════════════════════════════════════════════════════════
# 视频整理（要真的 PyAV + ffmpeg）
# ════════════════════════════════════════════════════════════════════════════
@needs_media
class PrepareVideo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        j = lambda n: os.path.join(cls.d, n)
        cls.ok = make_clip(j("ok.mp4"), 3, (1280, 720))
        cls.short = make_clip(j("short.mp4"), 1, (640, 360))
        cls.long = make_clip(j("long.mp4"), 12, (640, 360), rate=12)
        cls.big = make_clip(j("big.mp4"), 2, (3000, 1500), rate=6, audio=False)
        cls.tiny = make_clip(j("tiny.mp4"), 3, (150, 100))
        cls.narrow = make_clip(j("narrow.mp4"), 3, (2000, 240), rate=6, audio=False)
        cls.mkv = make_clip(j("a.mkv"), 3, (640, 360), fmt="matroska")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d)

    def seconds_of(self, data):
        p = os.path.join(self.d, "probe.mp4")
        write(p, data)
        return ali._probe_video(p, "测")

    def test_the_probe_reads_the_header(self):
        info = ali._probe_video(self.ok, "测")
        self.assertEqual((info["width"], info["height"], info["vcodec"], info["has_audio"]), (1280, 720, "h264", True))
        self.assertAlmostEqual(info["seconds"], 3.0, delta=0.1)
        self.assertIn("mp4", info["container"])
        with self.assertRaises(RuntimeError) as cm:
            ali._probe_video(os.path.join(self.d, "不存在.mp4"), "测")
        self.assertIn("打不开", str(cm.exception))
        junk = os.path.join(self.d, "junk.mp4")
        write(junk, b"not a video at all")
        with self.assertRaises(RuntimeError):
            ali._probe_video(junk, "测")

    def test_a_good_video_goes_out_untouched(self):
        data, name, secs, notes = ali._prepare_video(self.ok, "wan2.7-videoedit", "改视频")
        self.assertEqual(data, read(self.ok))
        self.assertTrue(name.endswith(".mp4") and name.isascii())                                    # 文件名是哈希：中文名会被阿里拒
        self.assertEqual((notes, round(secs)), ([], 3))
        data2, name2, _, _ = ali._prepare_video(io.BytesIO(data), "wan2.7-videoedit", "改视频")        # 内存里的视频也行
        self.assertEqual((data2, name2), (data, name))

    def test_what_cannot_be_fixed_is_refused_with_the_numbers(self):
        for path, word in ((self.short, "太短"), (self.tiny, "太小"), (self.narrow, "太窄长")):
            with self.assertRaises(RuntimeError) as cm:
                ali._prepare_video(path, "wan2.7-videoedit", "改视频")
            self.assertIn(word, str(cm.exception), path)

    def test_a_video_that_is_too_long_keeps_only_the_first_seconds(self):
        data, name, secs, notes = ali._prepare_video(self.long, "wan2.7-videoedit", "改视频")
        self.assertAlmostEqual(secs, 9.9)                                                         # 截到比上限（10 秒）短 0.1 秒：贴着线阿里可能判超
        self.assertLessEqual(self.seconds_of(data)["seconds"], 9.95)                              # 真正的时长也比上限（10 秒）短一点（重新编码按时间精确停；复制截取实测会多出 80 毫秒、贴着线阿里可能判超）
        self.assertGreater(self.seconds_of(data)["seconds"], 9)
        self.assertTrue(any("前 10 秒" in n for n in notes))
        data, _, secs, notes = ali._prepare_video(self.long, "wan2.2-animate-mix", "视频换人")        # 换人最长 30 秒：12 秒原样
        self.assertEqual(notes, [])
        self.assertAlmostEqual(secs, 12, delta=0.1)
        data, _, secs, notes = ali._prepare_video(self.long, "happyhorse-1.0-video-edit", "改视频")   # 这个模型 15 秒封顶：12 秒原样
        self.assertEqual(notes, [])

    def test_a_video_with_a_huge_picture_is_shrunk_and_an_odd_container_becomes_mp4(self):
        data, _, _, notes = ali._prepare_video(self.big, "wan2.2-animate-mix", "视频换人")
        info = self.seconds_of(data)
        self.assertLessEqual(max(info["width"], info["height"]), 2048)
        self.assertEqual((info["width"] % 2, info["height"] % 2), (0, 0))
        self.assertTrue(any("缩小到 2048" in n for n in notes), notes)
        data, _, _, notes = ali._prepare_video(self.mkv, "wan2.7-videoedit", "改视频")
        self.assertIn("mp4", self.seconds_of(data)["container"])
        self.assertTrue(any("重新编码" in n for n in notes))

    def test_a_heavy_file_is_recompressed_before_the_slow_upload(self):
        with mock.patch.object(ali, "HEAVY_MB", 0.01), mock.patch.object(ali, "HEAVY_SIDE", 640):
            data, _, _, notes = ali._prepare_video(self.ok, "wan2.7-videoedit", "改视频")
        info = self.seconds_of(data)
        self.assertEqual((info["width"], info["height"]), (640, 360))
        self.assertTrue(any("长边缩小到 640" in n for n in notes), notes)
        data, _, _, notes = ali._prepare_video(self.ok, "wan2.7-videoedit", "改视频")           # 默认 25MB 的线：这个小文件不受影响
        self.assertEqual(notes, [])

    def test_recompressing_a_phone_video_stored_sideways_does_not_squash_it(self):
        rot = os.path.join(self.d, "rot.mp4")
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90", "-i", self.ok, "-c", "copy", rot], check=True)      # 编码里 1280×720、带旋转标记 → 看起来是 720×1280 的竖屏
        with mock.patch.object(ali, "HEAVY_MB", 0.01), mock.patch.object(ali, "HEAVY_SIDE", 640):
            data, _, _, notes = ali._prepare_video(rot, "wan2.7-videoedit", "改视频")
        info = self.seconds_of(data)
        self.assertEqual((info["width"], info["height"]), (360, 640))                              # 竖屏缩小成 360×640，不是被压成 640×360
        self.assertTrue(any("长边缩小到 640" in n for n in notes), notes)

    def test_a_phone_video_with_a_rotation_mark_is_always_normalized_before_upload(self):
        rot = os.path.join(self.d, "rot2.mp4")
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90", "-i", self.ok, "-c", "copy", rot], check=True)
        self.assertTrue(ali._rotated(rot))
        self.assertFalse(ali._rotated(self.ok))
        data, _, _, notes = ali._prepare_video(rot, "wan2.7-videoedit", "改视频")                  # 小、h264、mp4：本来原样传，但带旋转标记的一律转码
        self.assertNotEqual(data, read(rot))
        out = os.path.join(self.d, "rot2_out.mp4")
        write(out, data)
        self.assertFalse(ali._rotated(out))                                                       # 转正了、标记清掉了
        info = ali._probe_video(out, "测")
        self.assertEqual((info["width"], info["height"]), (720, 1280))
        self.assertLess(abs(frame_at(rot, 0.2) - frame_at(out, 0.2)).mean(), 12)
        self.assertTrue(any("重新编码" in n for n in notes), notes)

    def test_a_file_over_the_size_limit_is_recompressed_or_refused(self):
        with mock.patch.dict(ali.VIDEO_LIMITS["wan2.7-videoedit"], {"mb": 0.0001}):
            with self.assertRaises(RuntimeError) as cm:
                ali._prepare_video(self.ok, "wan2.7-videoedit", "改视频")
        self.assertIn("文件太大", str(cm.exception))


# ════════════════════════════════════════════════════════════════════════════
# 视频拼接
# ════════════════════════════════════════════════════════════════════════════
class ConcatPlan(unittest.TestCase):
    def test_output_size_follows_the_first_clip_or_the_chosen_ratio(self):
        self.assertEqual(video.concat_size({"width": 1280, "height": 720}, "跟第一段", 720), (1280, 720))
        self.assertEqual(video.concat_size({"width": 3840, "height": 2160}, "跟第一段", 720), (1920, 1080))               # 长边最多 1920
        self.assertEqual(video.concat_size({"width": 1281, "height": 721}, "跟第一段", 720), (1280, 720))                 # 编码要偶数：奇数向下取偶
        self.assertEqual(video.concat_size({"width": 1, "height": 1}, "9:16 竖屏", 720), (720, 1280))
        self.assertEqual(video.concat_size({"width": 1, "height": 1}, "16:9 横屏", 1080), (1920, 1080))
        self.assertEqual(video.concat_size({"width": 1, "height": 1}, "9:16 竖屏", 480), (480, 852))
        self.assertEqual(video.concat_size({"width": 1, "height": 1}, "1:1 方形", 720), (720, 720))

    def test_fps_follows_the_first_clip_within_15_to_30(self):
        self.assertEqual([video.concat_fps(x) for x in (23.976, 24, 25, 29.97, 30, 60, 10, 0, None)], [24, 24, 25, 30, 30, 30, 15, 24, 24])

    def test_the_transition_is_never_longer_than_half_of_the_shortest_clip(self):
        clips = [{"seconds": 5}, {"seconds": 1.0}, {"seconds": 4}]
        self.assertAlmostEqual(video.clamp_transition(clips, 0.6), 0.45)
        self.assertEqual(video.clamp_transition([{"seconds": 5}, {"seconds": 5}], 0.6), 0.6)
        self.assertAlmostEqual(video.clamp_transition([{"seconds": 0.5}, {"seconds": 5}], 2.0), 0.2)
        self.assertEqual(video.clamp_transition([{"seconds": 0.1}], 2.0), 0.1)                                      # 再短也至少 0.1

    def test_the_filter_graph_times_every_transition_from_the_clip_lengths(self):
        clips = [{"seconds": 3.0, "has_audio": True}, {"seconds": 2.5, "has_audio": False}, {"seconds": 2.0, "has_audio": True}]
        flt, v, a, total = video.concat_filter(clips, 1280, 720, 24, "fade", 0.5, False, True)
        self.assertAlmostEqual(total, 3.0 + 2.5 + 2.0 - 2 * 0.5)
        self.assertEqual((v, a), ("[xv2]", "[xa2]"))
        self.assertIn("[v0][v1]xfade=transition=fade:duration=0.500:offset=2.500[xv1]", flt)                      # 第一个转场从 3.0 - 0.5 开始
        self.assertIn("[xv1][v2]xfade=transition=fade:duration=0.500:offset=4.500[xv2]", flt)                     # 累计 3.0 + 2.5 - 0.5 = 5.0，再减 0.5
        self.assertIn("[a0][a1]acrossfade=d=0.500:c1=tri:c2=tri[xa1]", flt)
        self.assertIn("[xa1][a2]acrossfade=d=0.500", flt)
        self.assertIn("anullsrc=r=44100:cl=stereo,aformat", flt)                                                  # 没有声音的那一段补静音
        self.assertIn("[0:a]aresample=44100,", flt)
        self.assertIn("trim=end=3.000", flt)
        self.assertIn("scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black", flt)
        flt2, _, _, _ = video.concat_filter(clips, 720, 1280, 24, "fade", 0.5, True, True)
        self.assertIn("scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280", flt2)

    def test_without_a_transition_the_clips_are_joined_end_to_end(self):
        clips = [{"seconds": 3.0, "has_audio": True}, {"seconds": 2.0, "has_audio": True}]
        flt, v, a, total = video.concat_filter(clips, 640, 360, 24, None, 0, False, True)
        self.assertEqual((v, a, total), ("[vout]", "[aout]", 5.0))
        self.assertIn("[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]", flt)
        self.assertNotIn("xfade", flt)
        flt, v, a, total = video.concat_filter(clips, 640, 360, 24, "slideleft", 0.5, False, False)               # 静音：没有声音链
        self.assertEqual((a, ":a]" in flt, "acrossfade" in flt, "anullsrc" in flt), (None, False, False, False))
        self.assertIn("xfade=transition=slideleft", flt)

    def test_the_transition_names_are_real_ffmpeg_ones(self):
        self.assertEqual(set(video.CONCAT_TRANSITIONS.values()), {"fade", "fadeblack", "slideleft", "slideup", "wipeleft", "circleopen"})


@needs_media
class ConcatNode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        j = lambda n: os.path.join(cls.d, n)
        cls.a = make_clip(j("a.mp4"), 3, (1280, 720))
        cls.b = make_clip(j("b.mp4"), 2.5, (720, 1280), rate=30, audio=False, pattern="smptebars")
        cls.c = make_clip(j("c.mp4"), 2, (640, 360), rate=25)
        cls.rotated = os.path.join(cls.d, "rot.mp4")
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90", "-i", cls.a, "-c", "copy", cls.rotated], check=True)
        os.makedirs(j("占位"))
        cls.placeholder = shutil.copyfile(cls.a, j("占位/请上传视频.mp4"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d)

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.folders = FakeFolders(self.root)
        self.patches = [mock.patch.object(video, "folder_paths", self.folders), mock.patch.object(video, "InputImpl", mock.Mock(VideoFromFile=FakeVideoFromFile))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.root)

    def concat(self, *paths, **kw):
        args = dict(transition="淡入淡出", transition_s=0.5, ratio="跟第一段", short_side=720, fit=video.CONCAT_FIT[0], keep_audio=True)
        args.update(kw)
        vids = [Vid(p) for p in paths] + [None] * (4 - len(paths))
        (out,) = video.ProVideoConcat().run(vids[0], vids[1], video3=vids[2], video4=vids[3], **args)
        return out.path, video._probe_clip(out.path, "出片")

    def test_three_clips_with_a_crossfade_come_out_as_one_video_of_the_right_length(self):
        path, info = self.concat(self.a, self.b, self.c)
        self.assertAlmostEqual(info["seconds"], 3.0 + 2.5 + 2.0 - 2 * 0.5, delta=0.2)
        self.assertEqual((info["width"], info["height"], info["has_audio"]), (1280, 720, True))
        self.assertEqual(os.path.dirname(path), self.folders.tmp)                                       # 写在临时目录，由「保存视频」存进 output/
        self.assertTrue(os.path.basename(path).startswith("pro_concat_"))
        self.assertEqual(os.listdir(self.folders.out), [])

    def test_no_transition_gives_the_plain_sum_and_the_chosen_ratio(self):
        path, info = self.concat(self.a, self.b, transition=video.NO_TRANSITION, ratio="9:16 竖屏", short_side=480, fit=video.CONCAT_FIT[1])
        self.assertAlmostEqual(info["seconds"], 5.5, delta=0.2)
        self.assertEqual((info["width"], info["height"]), (480, 852))

    def test_silent_mode_has_no_audio_track(self):
        _, info = self.concat(self.a, self.c, keep_audio=False)
        self.assertFalse(info["has_audio"])

    def test_the_placeholder_video_in_an_unused_slot_is_skipped(self):
        _, info = self.concat(self.a, self.c, self.placeholder, self.placeholder, transition=video.NO_TRANSITION)
        self.assertAlmostEqual(info["seconds"], 5.0, delta=0.2)

    def test_what_is_missing_is_said_in_plain_words(self):
        with self.assertRaises(RuntimeError) as cm:
            self.concat(self.a, self.placeholder)
        self.assertIn("视频 2 还没上传", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            self.concat(self.placeholder, self.a)
        self.assertIn("视频 1 还没上传", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            self.concat(self.a, os.path.join(self.d, "不存在.mp4"))
        self.assertIn("打不开", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            video.ProVideoConcat().run(Vid(self.a), None, **dict(transition="淡入淡出", transition_s=0.5, ratio="跟第一段", short_side=720, fit=video.CONCAT_FIT[0], keep_audio=True))
        self.assertIn("至少要有两段", str(cm.exception))

    def test_a_video_held_in_memory_is_accepted(self):
        mem = io.BytesIO(read(self.c))
        _, info = self.concat(self.a, mem, transition=video.NO_TRANSITION)
        self.assertAlmostEqual(info["seconds"], 5.0, delta=0.2)

    def test_too_long_a_result_is_refused_before_encoding(self):
        with mock.patch.object(video, "CONCAT_MAX_SECONDS", 4):
            with self.assertRaises(RuntimeError) as cm:
                self.concat(self.a, self.c)
        self.assertIn("超过上限", str(cm.exception))
        self.assertEqual(os.listdir(self.folders.tmp), [])

    def test_a_half_written_result_is_removed_when_ffmpeg_fails_midway(self):
        def half(args, timeout=None):
            with open(args[-1], "wb") as f:
                f.write(b"half")
            raise RuntimeError("[视频拼接] ffmpeg 失败：半路出错")
        with mock.patch.object(video, "_run_ffmpeg", half):
            with self.assertRaises(RuntimeError) as cm:
                self.concat(self.a, self.c)
        self.assertIn("半路出错", str(cm.exception))
        self.assertEqual(os.listdir(self.folders.tmp), [])

    def test_a_failed_ffmpeg_leaves_no_half_written_file_and_shows_the_log(self):
        with mock.patch.object(video, "concat_filter", lambda *a, **k: ("nonsense", "[v]", None, 5.0)):
            with self.assertRaises(RuntimeError) as cm:
                self.concat(self.a, self.c)
        self.assertIn("ffmpeg 失败", str(cm.exception))
        self.assertEqual(os.listdir(self.folders.tmp), [])

    def test_stopping_kills_ffmpeg(self):
        class InterruptProcessingException(Exception):
            pass

        def stop():
            raise InterruptProcessingException()
        procs, real = [], subprocess.Popen

        def spy(*a, **k):
            p = real(*a, **k)
            procs.append(p)
            return p
        with mock.patch.object(video, "_check_interrupt", stop), mock.patch.object(video.subprocess, "Popen", spy):
            with self.assertRaises(InterruptProcessingException):
                self.concat(self.a, self.c)
        self.assertIsNotNone(procs[-1].poll())                                                    # 最后起的是拼接的 ffmpeg（前面是查旋转的）：已经被杀掉、等过了，不会在服务器上白白跑完
        self.assertEqual(os.listdir(self.folders.tmp), [])

    def test_a_phone_video_stored_sideways_with_a_rotation_mark_counts_as_portrait(self):
        info = video._probe_clip(self.rotated, "竖拍")
        self.assertEqual((info["width"], info["height"]), (720, 1280))                            # 编码里是 1280×720，转正之后是 720×1280
        self.assertEqual(video.concat_size(info, "跟第一段", 720), (720, 1280))
        path, out = self.concat(self.rotated, self.c, transition=video.NO_TRANSITION)
        self.assertEqual((out["width"], out["height"]), (720, 1280))                              # 成片是竖的，第一段整个画面都在
        self.assertFalse(video._rotated(path))                                                    # 成片上没有旋转标记：不然播放器会再转一次（ffmpeg 7.x 复杂滤镜里不转正、还把标记带到成片上，真机试出来的）
        ref, got = frame_at(self.rotated, 0.2), frame_at(path, 0.2)
        self.assertEqual(ref.shape, got.shape)
        self.assertLess(abs(ref - got).mean(), 12, "成片第一段的画面和转正后的原视频对不上（方向不对？）")
        self.assertEqual([f for f in os.listdir(self.folders.tmp) if "upright" in f], [])           # 转正用的临时文件跑完就删


@needs_media
class SaveVideo(unittest.TestCase):
    def test_it_copies_the_video_into_output_and_reports_it_for_the_player(self):
        root = tempfile.mkdtemp()
        try:
            folders = FakeFolders(root)
            src = make_clip(os.path.join(folders.tmp, "pro_x.mp4"), 1, (320, 180))
            with mock.patch.object(video, "folder_paths", folders):
                r1 = video.ProVideoSave().run(Vid(src), "video/拼接")
                r2 = video.ProVideoSave().run(Vid(src), "video/拼接")
                r3 = video.ProVideoSave().run(Vid(io.BytesIO(read(src))), "video/内存")
            self.assertEqual(r1["ui"], {"images": [{"filename": "拼接_00001_.mp4", "subfolder": "video", "type": "output"}], "animated": (True,)})
            self.assertEqual(r2["ui"]["images"][0]["filename"], "拼接_00002_.mp4")
            self.assertEqual(r3["ui"]["images"][0], {"filename": "内存_00001_.mp4", "subfolder": "video", "type": "output"})
            self.assertEqual(read(os.path.join(folders.out, "video", "拼接_00001_.mp4")), read(src))
            self.assertTrue(os.path.exists(src))                                                      # 复制不是移动：缓存里的结果还要能用
        finally:
            shutil.rmtree(root)

    def test_the_node_is_an_output_node_without_outputs(self):
        self.assertTrue(video.ProVideoSave.OUTPUT_NODE)
        self.assertEqual(video.ProVideoSave.RETURN_TYPES, ())
        self.assertFalse(getattr(video.ProVideoConcat, "OUTPUT_NODE", False))                         # 拼接本身不是输出节点（输出节点会无视选择分支）


if __name__ == "__main__":
    unittest.main()
