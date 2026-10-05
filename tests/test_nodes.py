import http.server
import json
import threading
import time
import unittest
from unittest import mock

import numpy as np

from _load import load

video = load("pro-video")
ali = load("pro-ali")
image = load("pro-image")
poster = load("pro-poster")


def tone(sec, sr=video.SR, amp=0.5):
    return (amp * np.sin(np.arange(int(sec * sr)) * 0.1)).astype(np.float32)


def silence(sec, sr=video.SR):
    return np.zeros(int(sec * sr), dtype=np.float32)


class SubtitleText(unittest.TestCase):
    def test_split_on_punctuation(self):
        self.assertEqual(video._split_cues("你好，世界。再见!", 16), ["你好", "世界", "再见"])

    def test_split_hard_cut(self):
        self.assertEqual(video._split_cues("一二三四五六七", 3), ["一二三", "四五六", "七"])

    def test_split_empty(self):
        self.assertEqual(video._split_cues("，。 \n", 8), [])

    def test_srt_time(self):
        self.assertEqual(video._srt_time(3661.5), "01:01:01,500")
        self.assertEqual(video._srt_time(-1), "00:00:00,000")

    def test_srt_roundtrip(self):
        cues = [(0.0, 1.5, "第一句"), (2.0, 3.25, "第二句")]
        self.assertEqual(video._parse_srt(video._to_srt(cues)), cues)

    def test_parse_srt_crlf_and_dot(self):
        cues = video._parse_srt("1\r\n00:00:01.000 --> 00:00:02.500\r\nhello\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\na\r\nb\r\n")
        self.assertEqual(cues, [(1.0, 2.5, "hello"), (3.0, 4.0, "a\nb")])


class SubtitleTiming(unittest.TestCase):
    def test_pauses_used_as_boundaries(self):
        x = np.concatenate([tone(1), silence(0.5), tone(1), silence(0.5), tone(1)])
        timed, method = video._time_cues(["一", "二", "三"], x)
        self.assertEqual(method, "停顿")
        self.assertEqual(len(timed), 3)
        # 第二句从第一个停顿结束（≈1.5s）开始
        self.assertAlmostEqual(timed[1][0], 1.5, delta=0.05)
        # 后一句开始之前，前一句不消失
        self.assertGreaterEqual(timed[0][1], timed[1][0] - 0.05)

    def test_fallback_by_chars_without_pauses(self):
        timed, method = video._time_cues(["ab", "cdef"], tone(3))
        self.assertEqual(method, "按字数比例")
        self.assertAlmostEqual(timed[0][1] - timed[0][0], 1.0, delta=0.1)

    def test_offset(self):
        a, _ = video._time_cues(["x"], tone(1), 0.0)
        b, _ = video._time_cues(["x"], tone(1), 2.0)
        self.assertAlmostEqual(b[0][0] - a[0][0], 2.0)

    def test_silent_voice_returns_pair(self):
        # run() 里是 `timed, method = _time_cues(...)`，静音时也必须是二元组，才能走到友好报错
        timed, method = video._time_cues(["x"], silence(1))
        self.assertEqual(timed, [])


class AliSize(unittest.TestCase):
    def test_square_1k(self):
        self.assertEqual(ali._size("1:1", "1K"), "1024*1024")

    def test_2k_longest_side(self):
        self.assertEqual(ali._size("16:9", "2K"), "2048*1152")

    def test_multiple_of_16(self):
        for r in ali.RATIOS:
            w, h = (int(v) for v in ali._size(r, "1K").split("*"))
            self.assertEqual((w % 16, h % 16), (0, 0), r)


class FakeSession:
    """顶替 ali._open 返回的会话：post 是测试给的 Mock。"""
    def __init__(self, post):
        self.post = post

    def close(self):
        pass


def fake_session(post):
    return mock.patch.object(ali, "_open", lambda base, what: FakeSession(post))


class PromptWriter(unittest.TestCase):
    INFO = json.dumps({"apikey": "k", "custom_api_base": "https://dashscope.aliyuncs.com/compatible-mode"})

    @staticmethod
    def resp(status=200, body=None):
        r = mock.Mock(status_code=status, text=json.dumps(body))
        r.json.return_value = body
        return r

    def run_node(self, post, idea="一个红苹果", instruction="写提示词"):
        with fake_session(post):
            return ali.ProAliPromptWriter().run(idea, instruction, "", 0, self.INFO)

    def test_success_and_request_shape(self):
        post = mock.Mock(return_value=self.resp(body={"choices": [{"message": {"content": "  完整提示词 \n"}}], "usage": {"total_tokens": 9}}))
        text, status = self.run_node(post)
        self.assertEqual(text, "完整提示词")
        self.assertEqual(json.loads(status)["code"], "success")
        url, kw = post.call_args[0][0], post.call_args[1]
        self.assertEqual(url, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(kw["timeout"], (ali.POST_CONNECT_TIMEOUT, ali.WRITER_TIMEOUT))                # 连接 / 等回复分开
        self.assertEqual(kw["headers"]["Authorization"], "Bearer k")
        self.assertEqual(kw["json"]["messages"], [{"role": "system", "content": "写提示词"}, {"role": "user", "content": "一个红苹果"}])
        self.assertIs(kw["json"]["enable_thinking"], False)

    def test_image_goes_into_user_message(self):
        post = mock.Mock(return_value=self.resp(body={"choices": [{"message": {"content": "好"}}]}))
        with mock.patch.object(ali, "_to_data_url", return_value="data:image/jpeg;base64,AAAA"), fake_session(post):
            ali.ProAliPromptWriter().run("补充信息", "写文案", "qwen3.8-omni-flash", 0, self.INFO, image=object())
        user = post.call_args[1]["json"]["messages"][1]["content"]
        self.assertEqual(user, [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}, {"type": "text", "text": "补充信息"}])

    def test_connection_error_retried_once(self):
        ok = self.resp(body={"choices": [{"message": {"content": "好"}}]})
        post = mock.Mock(side_effect=[ali.requests.ConnectionError("reset"), ok])
        self.assertEqual(self.run_node(post)[0], "好")
        self.assertEqual(post.call_count, 2)

    def test_connection_error_twice_raises(self):
        post = mock.Mock(side_effect=ali.requests.ConnectionError("reset"))
        with self.assertRaisesRegex(RuntimeError, "连不上"):
            self.run_node(post)
        self.assertEqual(post.call_count, 2)

    def test_http_error_not_retried_and_shows_reason(self):
        post = mock.Mock(return_value=self.resp(400, {"error": {"code": "invalid_parameter", "message": "bad model"}}))
        with self.assertRaisesRegex(RuntimeError, "invalid_parameter.*bad model"):
            self.run_node(post)
        self.assertEqual(post.call_count, 1)

    def test_empty_reply_raises(self):
        post = mock.Mock(return_value=self.resp(body={"choices": [{"message": {"content": "  "}}]}))
        with self.assertRaisesRegex(RuntimeError, "没有文字"):
            self.run_node(post)

    def test_empty_inputs_raise_without_request(self):
        post = mock.Mock()
        for idea, ins in (("  ", "x"), ("x", " ")):
            with self.assertRaises(RuntimeError):
                self.run_node(post, idea, ins)
        post.assert_not_called()


class _Srv(http.server.ThreadingHTTPServer):
    """假的阿里：前 drop_first 个连接接受后直接关掉（客户端看到 Connection aborted，和握手被重置一样：什么都没处理）。"""
    daemon_threads = True

    def __init__(self, drop_first=0):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.drop_first, self.conns, self.heads, self.posts, self.gets = drop_first, 0, 0, 0, 0
        self.post_mode, self.get_codes, self.lock = "ok", [], threading.Lock()

    def verify_request(self, request, client_address):
        with self.lock:
            self.conns += 1
            return self.conns > self.drop_first


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"                         # keep-alive：预热的连接才能被请求复用

    def log_message(self, *a):
        pass

    def reply(self, code, body=b"{}"):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        with self.server.lock:
            self.server.heads += 1
        self.reply(404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with self.server.lock:
            self.server.posts += 1
        if self.server.post_mode == "drop":               # 收到了请求，不回就断开
            self.close_connection = True
            return
        if self.server.post_mode == "slow":
            time.sleep(1.0)
        self.reply(200, b'{"output": {"x": 1}}')

    def do_GET(self):
        with self.server.lock:
            self.server.gets += 1
            code = self.server.get_codes.pop(0) if self.server.get_codes else 200
        self.reply(code, b"DATA" if code == 200 else b"err")


class AliConnect(unittest.TestCase):
    """到阿里的连接阶段：预热连接（免费，失败可重试）+ 计费请求只发一次。真走 requests，对着本机的假服务。"""

    def serve(self, **kw):
        srv = _Srv(**kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)                       # 后加先跑：先停再关
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"

    def test_the_connection_is_warmed_up_with_a_free_head_and_reused_by_the_request(self):
        srv, base = self.serve()
        self.assertEqual(ali._post(base, "k", {"a": 1}, 5, "测试", "/x"), {"output": {"x": 1}})
        self.assertEqual((srv.heads, srv.posts, srv.conns), (1, 1, 1))                   # 同一条连接：预热一次、请求一次

    def test_failed_connections_are_retried_before_anything_is_sent(self):
        srv, base = self.serve(drop_first=2)
        self.assertEqual(ali._post(base, "k", {}, 5, "测试", "/x"), {"output": {"x": 1}})
        self.assertEqual((srv.posts, srv.conns), (1, 3))                                  # 前两个连接被掐了，第三个成功；计费请求只发了一次

    def test_gives_up_after_the_warmup_tries_without_sending_the_request(self):
        srv, base = self.serve(drop_first=99)
        with self.assertRaisesRegex(RuntimeError, r"连不上.*没有发出，不会计费"):
            ali._post(base, "k", {}, 5, "生图", "/x")
        self.assertEqual((srv.posts, srv.conns), (0, ali.WARMUP_TRIES))
        with self.assertRaisesRegex(RuntimeError, "没有发出"):                            # 端口根本没人听：同样，而且很快
            ali._post("http://127.0.0.1:1", "k", {}, 5, "生图", "/x")

    def test_the_billable_request_itself_is_never_resent(self):
        srv, base = self.serve()
        srv.post_mode = "drop"                                                           # 连上了、请求发出去了，对方不回就断开
        with self.assertRaisesRegex(RuntimeError, "连不上"):
            ali._post(base, "k", {}, 5, "配音", "/x")
        self.assertEqual(srv.posts, 1)                                                   # 绝不重发：重发会重复计费

    def test_read_timeout_says_the_request_was_already_sent(self):
        srv, base = self.serve()
        srv.post_mode = "slow"
        with self.assertRaisesRegex(RuntimeError, "等了 0.3 秒没有回应：请求已经发出"):
            ali._post(base, "k", {}, 0.3, "生图", "/x")
        self.assertEqual(srv.posts, 1)

    def test_the_key_is_not_sent_in_the_warmup(self):
        seen = []
        orig = ali.requests.Session.head
        with mock.patch.object(ali.requests.Session, "head", lambda self, url, **kw: (seen.append((url, dict(self.headers), kw)), orig(self, url, **kw))[1]):
            srv, base = self.serve()
            ali._post(base, "sk-secret", {}, 5, "测试", "/x")
        self.assertEqual(len(seen), 1)
        self.assertNotIn("sk-secret", json.dumps(seen[0], default=str))
        self.assertEqual(seen[0][2]["timeout"], (ali.CONNECT_TIMEOUT, 6))                # 连接阶段是短超时

    def test_download_retries_server_errors_but_not_client_errors(self):
        srv, base = self.serve()
        srv.get_codes = [500, 502]
        self.assertEqual(ali._download(base + "/f", "生图"), b"DATA")
        self.assertEqual(srv.gets, 3)
        srv.gets, srv.get_codes = 0, [404]
        with self.assertRaisesRegex(RuntimeError, "下载结果失败：HTTPError"):
            ali._download(base + "/f", "生图")
        self.assertEqual(srv.gets, 1)                                                    # 404（链接过期之类）重试没用
        srv.gets, srv.get_codes = 0, [500, 500, 500, 500]
        with self.assertRaisesRegex(RuntimeError, "下载结果失败"):
            ali._download(base + "/f", "生图")
        self.assertEqual(srv.gets, 3)                                                    # 最多三次
        with self.assertRaisesRegex(RuntimeError, "下载结果失败：ConnectionError"):
            ali._download("http://127.0.0.1:1/f", "生图")

    def test_asr_goes_through_the_same_single_send(self):
        calls = []

        def post(url, **kw):
            calls.append((url, kw["timeout"]))
            r = mock.Mock(status_code=200, text="{}")
            r.json.return_value = {"choices": [{"message": {"content": " 你好 "}}], "usage": {"seconds": 2}}
            return r

        info = json.dumps({"apikey": "k"})
        with mock.patch.object(ali, "_audio_to_wav16k", return_value=b"RIFF"), fake_session(post):
            text, status = ali.ProAliASR().run({"waveform": None}, "auto", info)
        self.assertEqual(text, "你好")
        self.assertEqual(calls, [("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", (ali.POST_CONNECT_TIMEOUT, ali.ASR_TIMEOUT))])


class LabelPlace(unittest.TestCase):
    def test_corners(self):
        self.assertEqual(image.place((1000, 800), (100, 50), "左上", 10), (10, 10))
        self.assertEqual(image.place((1000, 800), (100, 50), "右下", 10), (890, 740))

    def test_centered(self):
        self.assertEqual(image.place((1000, 800), (100, 50), "上中", 10), (450, 10))
        self.assertEqual(image.place((1000, 800), (100, 50), "正中", 10), (450, 375))


class PosterFields(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(poster.parse_fields("标题：夏日清凉节\n副标题：全场满199减50\n角标：限时三天\n画面元素：冰饮、柠檬片、水花"),
                         ("夏日清凉节", "全场满199减50", "限时三天", "冰饮、柠檬片、水花"))

    def test_markdown_quotes_and_empty_badge(self):
        out = poster.parse_fields("- **标题**：「鲜果季」\n- **副标题**: 脆甜多汁\n- 角标：无\n- 画面元素：树叶、木托盘")
        self.assertEqual(out, ("鲜果季", "脆甜多汁", "", "树叶、木托盘"))

    def test_subtitle_not_mistaken_for_title(self):
        t, s, _, _ = poster.parse_fields("副标题：只有副标题")
        self.assertEqual((t, s), ("", "只有副标题"))

    def test_elements_continuation_line(self):
        self.assertEqual(poster.parse_fields("标题：甲\n画面元素：树叶\n水珠、晨光")[3], "树叶、水珠、晨光")

    def test_no_fields_raises(self):
        with self.assertRaises(RuntimeError):
            poster.parse_fields("这是一张很好看的海报")


if __name__ == "__main__":
    unittest.main()
