import http.server
import json
import os
import threading
import time
import unittest
from unittest import mock

import numpy as np

from _load import load

video = load("pro-video")
ali = load("pro-ali")
REAL_TTS_REQUEST = ali._tts_request          # 对白节点的测试会把它换成假的；这里留一份真的，测它自己的错误处理
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


class DialogueScript(unittest.TestCase):
    NAMES = ["小美", "阿强", "", ""]

    def parse(self, script, names=None):
        return ali.parse_script(script, names or self.NAMES)

    def test_every_voice_has_gender_and_style_and_its_label_parses_back(self):
        self.assertEqual(set(ali.VOICE_INFO), set(ali.VOICES), "音色说明表要和音色列表一一对应")
        self.assertEqual(len(set(ali.VOICE_LABELS)), len(ali.VOICES))
        for v, label in zip(ali.VOICES, ali.VOICE_LABELS):
            self.assertTrue(label.startswith(v + " · "), label)
            self.assertEqual(ali.voice_id(label), v)
            self.assertIn(ali.VOICE_INFO[v][0], ("男", "女", "未知"))
        self.assertEqual(ali.voice_id("cherry"), "Cherry")                                   # 直接写 id（大小写不计）也认
        self.assertEqual(ali.voice_id("Eldric Sage · 男 · 沉稳睿智的老者"), "Eldric Sage")       # 带空格的音色名
        with self.assertRaises(RuntimeError):
            ali.voice_id("没有这个音色 · 女 · x")

    def test_roles_narrator_markers_and_stage_directions(self):
        out = self.parse("小美：老板，这个苹果怎么卖呀？\n阿强:新到的红富士，（笑）脆甜多汁。\n旁白：两人聊了起来。\n- 小美： 那我先来五斤！\n2. 阿强（得意）：好嘞！\n还有一句没有角色\n\n")
        self.assertEqual(out, [(0, "小美", "老板，这个苹果怎么卖呀？"), (1, "阿强", "新到的红富士，脆甜多汁。"), (None, "旁白", "两人聊了起来。"),
                               (0, "小美", "那我先来五斤！"), (1, "阿强", "好嘞！"), (None, "旁白", "还有一句没有角色")])

    def test_names_ignore_case_and_use_the_table_spelling_for_display(self):
        self.assertEqual(self.parse("amy：hi\nBOB：yo", ["Amy", "bob", "", ""]), [(0, "Amy", "hi"), (1, "bob", "yo")])

    def test_times_and_empty_labels_are_not_roles(self):
        self.assertEqual(self.parse("10:30 开始营业\n：只有冒号\n3:5"), [(None, "旁白", "10:30 开始营业"), (None, "旁白", "只有冒号"), (None, "旁白", "3:5")])

    def test_leading_numbers_of_a_line_are_kept_but_real_list_markers_are_removed(self):
        for line in ("9.9元一斤，欢迎选购", "3.5折起", "100.0%纯果汁", "-5℃低温保存", "2025年新品上市", "12.5斤装"):
            self.assertEqual(self.parse(line), [(None, "旁白", line)], line)                         # 开头的数字是台词本身，不是序号
        self.assertEqual([r[1:] for r in self.parse("1. 小美：你好\n- 阿强：好\n2、小美：行\n3) 旁白：开始\n• 阿强：嗯\n10.小美：十")],
                         [("小美", "你好"), ("阿强", "好"), ("小美", "行"), ("旁白", "开始"), ("阿强", "嗯"), ("小美", "十")])
        self.assertEqual(self.parse("小美：3.5折起，满9.9包邮")[0][2], "3.5折起，满9.9包邮")

    def test_role_names_may_carry_a_parenthesis_note_or_be_plain_numbers(self):
        names = ["小美（店员）", "阿强", "1", "2"]
        out = self.parse("小美（店员）：你好\n小美：你好呀\n小美（笑）：哈哈\n1：数字角色\n2：另一个\n10:30 开始营业", names)
        self.assertEqual([(w, n) for w, n, _ in out], [(0, "小美（店员）"), (0, "小美（店员）"), (0, "小美（店员）"), (2, "1"), (3, "2"), (None, "旁白")])
        self.assertEqual(out[-1][2], "10:30 开始营业")                                               # 角色表里没有「10」，时间整行当旁白
        with self.assertRaises(RuntimeError):
            self.parse("小美：你好", ["小美（店员）", "小美（顾客）", "", ""])                             # 括号里的说明不算：这是两个同名角色

    def test_an_unknown_role_is_an_error_not_a_guess(self):
        with self.assertRaises(RuntimeError) as cm:
            self.parse("小美：你好\n路人：你好")
        msg = str(cm.exception)
        self.assertIn("路人", msg)
        self.assertIn("小美、阿强", msg)                       # 告诉用户现有哪些角色
        self.assertIn("旁白：", msg)                           # 和「如果是旁白怎么写」

    def test_bad_scripts(self):
        for bad in ("", "   \n\n", "小美：（笑）", "（全是动作）"):
            with self.assertRaises(RuntimeError, msg=repr(bad)):
                self.parse(bad)
        with self.assertRaises(RuntimeError):
            self.parse("小美：a", ["小美", "小美", "", ""])                                     # 两个角色同名
        with self.assertRaises(RuntimeError):
            self.parse("\n".join("小美：第%d句" % i for i in range(ali.MAX_DIALOGUE_LINES + 1)))   # 句数上限（防误粘整篇文章）
        with self.assertRaises(RuntimeError):
            self.parse("小美：" + "字" * (ali.MAX_DIALOGUE_CHARS + 1))                           # 字数上限
        self.assertEqual(len(self.parse("\n".join("小美：第%d句" % i for i in range(ali.MAX_DIALOGUE_LINES)))), ali.MAX_DIALOGUE_LINES)


class DialogueTimeline(unittest.TestCase):
    def test_gaps_between_clips_and_exact_spans(self):
        sr = 1000
        audio, spans = ali.build_dialogue([(np.ones(500, np.float32), sr), (np.ones(250, np.float32), sr), (np.ones(1000, np.float32), sr)], 0.4)
        self.assertEqual(len(audio), 500 + 400 + 250 + 400 + 1000)
        self.assertEqual(spans, [(0.0, 0.5), (0.9, 1.15), (1.55, 2.55)])
        self.assertEqual(float(audio[500:900].sum()), 0.0)                                   # 停顿是静音
        self.assertEqual(float(audio[:500].min()), 1.0)

    def test_split_text_like_the_single_voice_subtitles(self):
        self.assertEqual(ali.split_text("你好，世界。再见!", 16), video._split_cues("你好，世界。再见!", 16))
        self.assertEqual(ali.split_text("一二三四五六七", 3), ["一二三", "四五六", "七"])
        self.assertEqual(ali.split_text("，。", 8), [])

    def test_cues_use_the_real_spans_and_name_the_speaker(self):
        lines = [(0, "小美", "老板，这苹果怎么卖"), (1, "阿强", "九块九"), (None, "旁白", "结束")]
        spans = [(0.0, 2.0), (2.4, 3.0), (3.4, 4.0)]
        cues = ali.dialogue_cues(lines, spans, 16, True)
        self.assertEqual([c[2] for c in cues], ["小美：老板", "小美：这苹果怎么卖", "阿强：九块九", "结束"])      # 旁白不写名字
        self.assertAlmostEqual(cues[0][0], 0.0)
        self.assertAlmostEqual(cues[0][1], 2.0 * 2 / 8, delta=0.01)                           # 按字数分配这一句的时间：「老板」2 个字，整句 8 个字
        self.assertAlmostEqual(cues[1][1], 2.36, delta=0.01)                                  # 停顿里字幕不消失，保持到下一句开始前一点
        self.assertAlmostEqual(cues[-1][1], 4.3, delta=0.001)                                 # 最后一条多留 0.3 秒
        self.assertEqual([c[2] for c in ali.dialogue_cues(lines, spans, 16, False)], ["老板", "这苹果怎么卖", "九块九", "结束"])

    def test_srt_is_readable_by_the_video_node(self):
        lines = [(0, "小美", "你好呀，今天天气不错"), (1, "阿强", "是啊")]
        srt = ali.to_srt(ali.dialogue_cues(lines, [(0.0, 1.5), (1.9, 2.4)], 16, True))
        parsed = video._parse_srt(srt)                                                         # 合成成片节点用的就是这个解析器
        self.assertEqual([c[2] for c in parsed], ["小美：你好呀", "小美：今天天气不错", "阿强：是啊"])
        self.assertTrue(all(a < b for a, b, _ in parsed))
        self.assertTrue(all(parsed[k][1] <= parsed[k + 1][0] + 1e-6 for k in range(len(parsed) - 1)))


class DialogueNode(unittest.TestCase):
    """整个节点：每句合成一次（假的接口）、按角色选音色、拼起来、出字幕和说明；已合成的句子会存下来。"""
    SCRIPT = "小美：你好呀\n阿强：你好\n旁白：结束了"

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.calls = []

        def tts(base, key, text, voice, model, language, instructions=""):
            self.calls.append((text, voice, model, language, instructions))
            if self.fail_at is not None and len(self.calls) == self.fail_at:
                raise RuntimeError("[阿里 配音] HTTP 500")
            return ("RAW:" + text).encode(), {}

        class Arr:
            def __init__(self, a):
                self.a = a

            def __getitem__(self, _):
                return self

        self.fail_at = None
        self.saved = []
        self.patches = [mock.patch.object(ali, "_creds", lambda info: ("k", "https://x")), mock.patch.object(ali, "_tts_request", tts),
                        mock.patch.object(ali, "_cache_dir", lambda: self.tmp),
                        mock.patch.object(ali, "_save_srt", lambda prefix, srt: (self.saved.append((prefix, srt)), "subtitles")[1]),
                        mock.patch.object(ali, "_decode_line", lambda raw, speed, db: (np.full(len(raw) * 100, 0.1, np.float32), 24000)),
                        mock.patch.object(ali, "torch", mock.Mock(from_numpy=Arr))]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def run_node(self, script=None, **kw):
        a = dict(script=script or self.SCRIPT, role1_name="小美", role1_voice="Cherry · 女 · 阳光亲切", role2_name="阿强", role2_voice="Ethan · 男 · 朝气温暖，带北方口音",
                 role3_name="", role3_voice="Serena", role4_name="", role4_voice="Ryan", narrator_voice="Neil", model="qwen3-tts-flash", language="Chinese",
                 speed=1.0, gap_s=0.35, show_names=True, max_chars=16, reuse=True, volume_db=0.0, filename_prefix="subtitles/对白字幕")
        a.update(kw)
        res = ali.ProAliDialogue().run(**a)
        self.last_ui = res["ui"]["text"][0]
        return res["result"]

    def test_each_line_is_synthesized_once_with_its_roles_voice(self):
        audio, srt, report = self.run_node()
        self.assertEqual([(c[0], c[1]) for c in self.calls], [("你好呀", "Cherry"), ("你好", "Ethan"), ("结束了", "Neil")])
        self.assertEqual(audio["sample_rate"], 24000)
        self.assertEqual(self.saved, [("subtitles/对白字幕", srt)])                              # 字幕文件存下来了
        self.assertIn("字幕文件已存到 output/subtitles/", self.last_ui)
        self.assertIn(srt, self.last_ui)
        self.assertIn("小美：你好呀", srt)
        self.assertIn("阿强：你好", srt)
        self.assertNotIn("旁白：", srt)
        self.assertIn("共 3 句、3 个声音", report)
        self.assertIn("小美 → Cherry", report)
        self.assertIn("旁白 → Neil", report)
        self.assertNotIn("没有重新计费", report)

    def test_same_lines_again_come_from_the_cache_and_are_not_billed_again(self):
        self.run_node()
        n = len(self.calls)
        _, _, report = self.run_node()
        self.assertEqual(len(self.calls), n)
        self.assertIn("其中 3 句直接用了上次合成的（没有重新计费）", report)

    def test_changing_one_line_or_voice_only_redoes_that_line(self):
        self.run_node()
        self.calls.clear()
        self.run_node(script="小美：你好呀\n阿强：你好吗\n旁白：结束了")
        self.assertEqual([c[0] for c in self.calls], ["你好吗"])
        self.calls.clear()
        self.run_node(role1_voice="Serena · 女 · 温柔")                                          # 换了角色的音色：这个角色的句子重做，别人的不动
        self.assertEqual([(c[0], c[1]) for c in self.calls], [("你好呀", "Serena")])

    def test_reuse_off_synthesizes_everything_again(self):
        self.run_node()
        self.calls.clear()
        self.run_node(reuse=False)
        self.assertEqual(len(self.calls), 3)

    def test_a_failure_in_the_middle_keeps_the_paid_lines(self):
        self.fail_at = 2
        with self.assertRaises(RuntimeError) as cm:
            self.run_node()
        msg = str(cm.exception)
        self.assertIn("第 2/3 句", msg)
        self.assertIn("阿强", msg)
        self.assertIn("前面 1 句已经合成好并存下来了，重新运行不会重复计费", msg)
        self.fail_at = None
        self.calls.clear()
        self.run_node()
        self.assertEqual([c[0] for c in self.calls], ["你好", "结束了"])                          # 第一句用缓存，只付没成功的和后面的

    def test_style_instructions_only_go_out_with_the_instruct_model(self):
        self.run_node(role1_style="语气兴奋")
        self.assertEqual(self.calls[0][4], "")                                                  # flash 模型不带
        self.calls.clear()
        _, _, report = self.run_node(model="qwen3-tts-instruct-flash", role1_style="语气兴奋", role2_style="")
        self.assertEqual([c[4] for c in self.calls], ["语气兴奋", "", ""])                      # 只给有指令的角色；旁白没有
        self.assertNotIn("没用上", report)
        _, _, report = self.run_node(role1_style="语气兴奋", reuse=False)
        self.assertIn("语气指令只有 instruct 模型才有效，这次没用上", report)

    def test_unknown_role_fails_before_any_request(self):
        with self.assertRaises(RuntimeError):
            self.run_node(script="小美：你好\n路人：喂")
        self.assertEqual(self.calls, [])

    def test_a_line_whose_audio_cannot_be_decoded_is_never_cached_and_a_bad_cached_entry_is_redone(self):
        real = ali._decode_line

        def decode(raw, speed, db):
            if "坏".encode() in raw:
                raise ValueError("InvalidData")
            return real(raw, speed, db)
        with mock.patch.object(ali, "_decode_line", decode):
            with self.assertRaises(RuntimeError) as cm:
                self.run_node(script="小美：好\n阿强：坏")
            self.assertIn("解不开", str(cm.exception))
            self.assertIn("可能已经计费", str(cm.exception))
            self.assertEqual(len([f for f in os.listdir(self.tmp) if f.endswith(".audio")]), 1)                # 只有成功的那一句进了缓存
            # 缓存里已经有一条坏的：丢掉、重新合成这一句，不会每次都撞上它
            ali._cache_put(ali._line_key("qwen3-tts-flash", "Cherry", "Chinese", "好呀", ""), "RAW:坏".encode())
            self.calls.clear()
            self.run_node(script="小美：好呀")
            self.assertEqual([c[0] for c in self.calls], ["好呀"])
            self.calls.clear()
            self.run_node(script="小美：好呀")
            self.assertEqual(self.calls, [])                                                       # 这次存的是好的，下次直接用
            # 缓存里有一条坏的、重新合成这一句时又失败了（网络断了）：坏的那条也要丢掉，不留在缓存里
            key = ali._line_key("qwen3-tts-flash", "Cherry", "Chinese", "好呀吗", "")
            ali._cache_put(key, "RAW:坏".encode())
            self.calls.clear()
            self.fail_at = 1
            with self.assertRaises(RuntimeError):
                self.run_node(script="小美：好呀吗")
            self.fail_at = None
            self.assertIsNone(ali._cache_get(key))

    def test_the_stop_button_ends_the_run_before_the_next_line_is_billed(self):
        n = []

        def check():
            n.append(1)
            if len(n) == 2:
                raise KeyboardInterrupt("用户点了停止")
        with mock.patch.object(ali, "_check_interrupt", check):
            with self.assertRaises(KeyboardInterrupt):
                self.run_node()
        self.assertEqual(len(self.calls), 1)                                                       # 第 2 句开始前就停了：只合成（付费）了第 1 句
        self.assertEqual(len(self.saved), 0)

    def test_the_failure_note_is_honest_about_whether_the_paid_lines_will_be_reused(self):
        self.fail_at = 2
        with self.assertRaises(RuntimeError) as cm:
            self.run_node(reuse=False)
        msg = str(cm.exception)
        self.assertIn("前面 1 句已经合成好并存下来了", msg)
        self.assertIn("打开「没改的句子用上次的」", msg)
        self.assertNotIn("重新运行不会重复计费", msg)                                                 # 关着复用时重跑会全部重付，不能说不会

    def test_a_failed_download_after_a_successful_request_says_the_line_may_be_billed(self):
        with mock.patch.object(ali, "_post", lambda *a, **k: {"output": {"audio": {"url": "https://x/a.wav"}}}), \
                mock.patch.object(ali, "_download", mock.Mock(side_effect=RuntimeError("[阿里 配音] 下载结果失败：Timeout"))):
            with self.assertRaises(RuntimeError) as cm:
                REAL_TTS_REQUEST("https://x", "k", "你好", "Cherry", "qwen3-tts-flash", "Chinese")
        self.assertIn("下载结果失败", str(cm.exception))
        self.assertIn("已经合成成功、可能已经计费", str(cm.exception))

    def test_the_instructions_only_go_into_the_request_for_the_instruct_model(self):
        sent = []

        def post(base, key, body, timeout, what, path=None):
            sent.append(body)
            return {"output": {"audio": {"url": "https://x/a.wav"}}}
        with mock.patch.object(ali, "_post", post), mock.patch.object(ali, "_download", lambda url, what: b"A"):
            REAL_TTS_REQUEST("https://x", "k", "你好", "Cherry", "qwen3-tts-flash", "Chinese", "语气兴奋")
            REAL_TTS_REQUEST("https://x", "k", "你好", "Cherry", "qwen3-tts-instruct-flash", "Chinese", "语气兴奋")
            REAL_TTS_REQUEST("https://x", "k", "你好", "Cherry", "qwen3-tts-instruct-flash", "Chinese", "  ")
        self.assertNotIn("instructions", sent[0]["input"])
        self.assertEqual((sent[1]["input"]["instructions"], sent[1]["input"]["optimize_instructions"]), ("语气兴奋", True))
        self.assertNotIn("instructions", sent[2]["input"])
        self.assertEqual((sent[0]["input"]["voice"], sent[0]["input"]["language_type"], sent[0]["model"]), ("Cherry", "Chinese", "qwen3-tts-flash"))

    def test_cache_keys_separate_language_and_style_but_not_speed(self):
        self.run_node(script="小美：好呀")
        self.calls.clear()
        self.run_node(script="小美：好呀", language="English")
        self.assertEqual(len(self.calls), 1)                                                       # 语种不同：另合成一条
        self.calls.clear()
        self.run_node(script="小美：好呀", model="qwen3-tts-instruct-flash", role1_style="语气兴奋")
        self.run_node(script="小美：好呀", model="qwen3-tts-instruct-flash", role1_style="语气低沉")
        self.assertEqual([c[4] for c in self.calls], ["语气兴奋", "语气低沉"])                      # 语气不同：另合成
        self.calls.clear()
        self.run_node(script="小美：好呀", speed=1.5, volume_db=3.0)
        self.assertEqual(self.calls, [])                                                           # 语速 / 音量是合成后在本地处理的，不影响缓存

    def test_every_setting_reaches_its_function_and_all_four_roles_and_the_narrator_get_their_own_voices(self):
        decoded = []

        def decode(raw, speed, db):
            decoded.append((speed, db))
            return np.full(2400, 0.1, np.float32), 24000
        script = "甲：一\n乙：二\n丙：三\n丁：四\n旁白：五，六，七，八，九，十，十一，十二\n"
        with mock.patch.object(ali, "_decode_line", decode):
            _, srt, report = self.run_node(script=script, role1_name="甲", role2_name="乙", role3_name="丙", role4_name="丁",
                                           role1_voice="Cherry", role2_voice="Ethan", role3_voice="Serena", role4_voice="Ryan", narrator_voice="Neil",
                                           speed=1.3, volume_db=-2.5, gap_s=0.0, max_chars=4, show_names=False, filename_prefix="字幕/测试")
        self.assertEqual([(c[0], c[1]) for c in self.calls], [("一", "Cherry"), ("二", "Ethan"), ("三", "Serena"), ("四", "Ryan"), ("五，六，七，八，九，十，十一，十二", "Neil")])
        self.assertTrue(all(d == (1.3, -2.5) for d in decoded), decoded)                           # 语速 / 音量传给了每一句
        self.assertEqual(self.saved[-1][0], "字幕/测试")                                           # 字幕文件的位置
        self.assertNotIn("甲：", srt)                                                              # 不写角色名
        cues = video._parse_srt(srt)
        self.assertEqual(len(cues), 4 + 8)                                                          # 旁白那句按标点切成 8 条（每条不超过 4 个字）
        self.assertAlmostEqual(cues[1][0], 0.1, delta=0.001)                                       # gap_s=0：每句 0.1 秒（2400 个采样 @24k）首尾相接
        self.assertIn("5 个声音", report)

    def test_the_cache_keeps_only_the_newest_files(self):
        with mock.patch.object(ali, "CACHE_KEEP", 2):
            for i in range(5):
                ali._cache_put(f"k{i}", b"x")
                os.utime(os.path.join(self.tmp, f"k{i}.audio"), (1000 + i, 1000 + i))
            ali._cache_put("k5", b"x")
        self.assertEqual(sorted(n for n in os.listdir(self.tmp) if n.endswith(".audio")), ["k4.audio", "k5.audio"])
        self.assertIsNone(ali._cache_get("k0"))
        self.assertEqual(ali._cache_get("k5"), b"x")


def soft_disc(size=200, r=60, soft=14):
    """一个边缘软的圆（模拟模型输出的概率图）：圆内 255，圆外 0，边上 soft 像素宽的渐变。"""
    from PIL import Image
    yy, xx = np.mgrid[0:size, 0:size]
    d = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
    a = np.clip((r + soft / 2 - d) / soft, 0, 1)
    return Image.fromarray((a * 255).astype(np.uint8), "L")


class Matting(unittest.TestCase):
    def test_edge_refinement_tightens_the_soft_rim_and_none_leaves_it_alone(self):
        a = soft_disc()
        self.assertTrue(np.array_equal(np.asarray(image.refine_alpha(a, "不处理（模型原样）")), np.asarray(a)))
        raw, std = np.asarray(a, dtype=np.float32) / 255, np.asarray(image.refine_alpha(a, "标准（去掉白边）"), dtype=np.float32) / 255
        self.assertLess(std.sum(), raw.sum())                                               # 往里缩了：前景面积变小
        self.assertLess(((std > 0.04) & (std < 0.96)).sum(), ((raw > 0.04) & (raw < 0.96)).sum() * 1.3)   # 过渡带没有变宽
        self.assertAlmostEqual(float(std[100, 100]), 1.0, places=2)                         # 里面还是不透明的
        self.assertAlmostEqual(float(std[5, 5]), 0.0, places=2)                             # 外面还是透明的
        strong = np.asarray(image.refine_alpha(a, "强（边缘更紧）"), dtype=np.float32).sum()
        light = np.asarray(image.refine_alpha(a, "轻（只去一点点）"), dtype=np.float32).sum()
        self.assertLess(strong, std.sum() * 255)
        self.assertLess(std.sum() * 255, light)                                             # 轻 < 标准 < 强（越强缩得越多）

    def test_big_images_are_refined_at_a_working_size_and_come_back_the_same_size(self):
        from PIL import Image
        big = Image.new("L", (3000, 2000), 255)
        out = image.refine_alpha(big, "标准（去掉白边）")
        self.assertEqual(out.size, (3000, 2000))

    def test_trim_to_content(self):
        from PIL import Image
        im = Image.new("RGBA", (100, 80), (0, 0, 0, 0))
        im.paste(Image.new("RGBA", (20, 10), (255, 0, 0, 255)), (30, 40))
        t = image.trim_to_content(im)
        self.assertEqual(t.size, (20, 10))
        self.assertEqual(image.trim_to_content(im, pad=5).size, (30, 20))
        with self.assertRaises(RuntimeError):
            image.trim_to_content(Image.new("RGBA", (10, 10), (255, 0, 0, 0)))

    def test_predict_alpha_follows_the_models_preprocessing_and_returns_a_same_size_map(self):
        from PIL import Image

        class Sess:
            def __init__(self):
                self.seen = None

            def get_inputs(self):
                return [mock.Mock(**{"name": "in0"})] if False else [type("I", (), {"name": "in0"})()]

            def run(self, _, feed):
                self.seen = feed["in0"]
                out = np.zeros((1, 1, 8, 8), np.float32)
                out[0, 0, 2:6, 2:6] = 5.0                                                     # 中间一块是前景（原始分数，会被拉到 0~1）
                return [out]
        sess = Sess()
        spec = image.MATTE_MODELS["u2net"]
        m = image.engine.predict_alpha(Image.new("RGB", (60, 40), (200, 30, 30)), spec, sess)
        self.assertEqual(sess.seen.shape, (1, 3, 320, 320))
        self.assertEqual(sess.seen.dtype, np.float32)
        self.assertEqual((m.mode, m.size), ("L", (60, 40)))
        a = np.asarray(m)
        self.assertGreater(a[20, 30], 200)                                                    # 中间前景（假模型只有 8x8，放大到 60x40 会糊，所以不要求到 255）
        self.assertLess(a[0, 0], 40)                                                          # 边角背景
        # u2net 的归一化：整张图除以最大值、再减均值除方差；红色通道最大 → 红通道约 (1 - 0.485) / 0.229
        self.assertAlmostEqual(float(sess.seen[0, 0, 100, 100]), (1.0 - 0.485) / 0.229, places=3)

    def test_matte_image_puts_the_alpha_on_the_original_colors(self):
        from PIL import Image

        def fake_predict(im):                                                                    # 假模型：中间一块是前景
            a = Image.new("L", im.size, 0)
            a.paste(255, (20, 20, 60, 60))
            return a
        src = Image.new("RGB", (80, 80), (10, 200, 30))
        rgba = image.matte_image(src, "u2net", "不处理（模型原样）", fake_predict)
        self.assertEqual((rgba.mode, rgba.size), ("RGBA", (80, 80)))
        self.assertEqual(rgba.getpixel((40, 40)), (10, 200, 30, 255))                            # 颜色还是原图的
        self.assertEqual(rgba.getpixel((2, 2))[3], 0)
        tight = image.matte_image(src, "u2net", "标准（去掉白边）", fake_predict)
        half = tight.split()[3].point(lambda v: 255 if v > 127 else 0).getbbox()                 # 看一半透明度的轮廓
        self.assertTrue(34 <= half[2] - half[0] <= 38, half)                                     # 40 像素宽的前景，标准边缘处理往里各缩 2 像素

    def test_the_worker_process_writes_the_alpha_and_reports_a_missing_onnxruntime(self):
        import tempfile
        from PIL import Image

        class Sess:
            def get_inputs(self):
                return [type("I", (), {"name": "x"})()]

            def run(self, _, feed):
                out = np.zeros((1, 1, 8, 8), np.float32)
                out[0, 0, 2:6, 2:6] = 5.0
                return [out]
        d = tempfile.mkdtemp()
        src, dst = os.path.join(d, "in.png"), os.path.join(d, "out.png")
        Image.new("RGB", (50, 30), (1, 2, 3)).save(src)
        ort = mock.Mock(SessionOptions=mock.Mock, InferenceSession=lambda path, sess_options=None, providers=None: Sess())
        with mock.patch.dict("sys.modules", {"onnxruntime": ort}):
            self.assertEqual(image.engine._worker(["u2net", "/x.onnx", src, dst]), 0)
        with Image.open(dst) as m:
            self.assertEqual((m.mode, m.size), ("L", (50, 30)))
        os.remove(dst)
        with mock.patch.dict("sys.modules", {"onnxruntime": None}):
            self.assertEqual(image.engine._worker(["u2net", "/x.onnx", src, dst]), image.engine.NO_ONNXRUNTIME)
        self.assertFalse(os.path.exists(dst))

    def test_run_worker_turns_every_failure_into_a_plain_message(self):
        import subprocess
        from PIL import Image
        img = Image.new("RGB", (20, 20), (9, 9, 9))

        def fake_run(code, stderr="", write=False):
            def run(cmd, capture_output=True, text=True, timeout=None):
                if write:
                    Image.new("L", (20, 20), 200).save(cmd[-1])
                return subprocess.CompletedProcess(cmd, code, "", stderr)
            return run
        eng = image.engine
        with mock.patch.object(eng, "model_path", lambda name: "/m.onnx"):
            with mock.patch("subprocess.run", fake_run(0, write=True)):
                a = eng.run_worker(img, "u2net")
            self.assertEqual((a.mode, a.size, a.getpixel((3, 3))), ("L", (20, 20), 200))
            for code, stderr, want in ((eng.NO_ONNXRUNTIME, "", "onnxruntime"), (-9, "", "内存不够"), (137, "", "内存不够"), (1, "Traceback...\nValueError: 坏图", "ValueError: 坏图")):
                with mock.patch("subprocess.run", fake_run(code, stderr)):
                    with self.assertRaises(RuntimeError) as cm:
                        eng.run_worker(img, "u2net")
                self.assertIn(want, str(cm.exception), code)
            with mock.patch("subprocess.run", mock.Mock(side_effect=subprocess.TimeoutExpired("x", 5))):
                with self.assertRaises(RuntimeError) as cm:
                    eng.run_worker(img, "u2net", timeout=5)
            self.assertIn("5 秒", str(cm.exception))
            captured = {}

            def spy(cmd, capture_output=True, text=True, timeout=None):
                captured["cmd"] = cmd
                Image.new("L", (20, 20), 1).save(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with mock.patch("subprocess.run", spy):
                eng.run_worker(img, "u2net")
            self.assertEqual(captured["cmd"][2:4], ["u2net", "/m.onnx"])                         # 小进程拿到的是模型名和模型文件

    @staticmethod
    def read(path):
        with open(path, "rb") as f:
            return f.read()

    def test_model_download_is_verified_and_never_leaves_a_half_file(self):
        import hashlib
        import tempfile
        d = tempfile.mkdtemp()
        data = b"fake-onnx-bytes" * 100
        spec = dict(image.MATTE_MODELS["silueta"], md5=hashlib.md5(data).hexdigest())

        class Resp:
            def __init__(self, payload):
                self.payload, self.pos = payload, 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                chunk = self.payload[self.pos:self.pos + n]
                self.pos += n
                return chunk
        with mock.patch.dict(image.MATTE_MODELS, {"silueta": spec}), mock.patch.dict(os.environ, {"PRO_MATTE_MODELS": d}), mock.patch.object(image.engine, "_checked", set()):
            with mock.patch("urllib.request.urlopen", lambda url, timeout=0: Resp(data)):
                p = image.engine.model_path("silueta")
            self.assertEqual(self.read(p), data)
            self.assertEqual(os.listdir(d), ["silueta.onnx"])
            image.engine._checked.clear()
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("已经有了就不该再下载")):
                self.assertEqual(image.engine.model_path("silueta"), p)                               # 已有且校验过：不联网
            image.engine._checked.clear()
            with open(p, "wb") as f:
                f.write(b"broken")                                                             # 文件坏了：重新下
            with mock.patch("urllib.request.urlopen", lambda url, timeout=0: Resp(data)):
                image.engine.model_path("silueta")
            self.assertEqual(self.read(p), data)
            os.remove(p)
            with mock.patch("urllib.request.urlopen", lambda url, timeout=0: Resp(b"wrong")):
                with self.assertRaises(RuntimeError) as cm:
                    image.engine.model_path("silueta")
            self.assertIn("silueta.onnx", str(cm.exception))                                   # 告诉用户去哪下 / 放哪
            self.assertEqual(os.listdir(d), [])                                                 # 校验不过：临时文件清掉，也没有留下坏的模型文件

    def test_big_alphas_are_refined_at_1024_and_small_ones_at_their_own_size(self):
        from PIL import Image
        seen = []
        real = Image.Image.filter

        def spy(self_, f):
            seen.append(self_.size)
            return real(self_, f)
        with mock.patch.object(Image.Image, "filter", spy):
            image.refine_alpha(Image.new("L", (3000, 2000), 255), "标准（去掉白边）")
            image.refine_alpha(Image.new("L", (300, 200), 255), "标准（去掉白边）")
        self.assertEqual(seen, [(1024, 683)] * 2 + [(300, 200)] * 2)                              # 缩边和柔化各一次：大图在 1024 上做，小图原尺寸

    def test_trim_ignores_a_faint_haze_but_keeps_real_pixels(self):
        from PIL import Image
        im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        im.paste(Image.new("RGBA", (10, 10), (255, 0, 0, 5)), (0, 0))                              # 透明度 5：几乎看不见的雾
        im.paste(Image.new("RGBA", (10, 10), (255, 0, 0, 200)), (50, 60))
        self.assertEqual(image.trim_to_content(im).size, (10, 10))                                 # 雾被裁掉，只留真像素
        self.assertEqual(image.trim_to_content(im, threshold=0).size, (60, 70))                    # 阈值设成 0 才会把雾也算上

    def test_matte_node_returns_the_input_untouched_and_a_mask_that_is_one_minus_alpha(self):
        import types

        class T:
            def __init__(self, a):
                self.a = a

            def cpu(self):
                return self

            def numpy(self):
                return self.a
        src = [T(np.full((20, 30, 3), 0.4, np.float32))]
        fake_torch = types.SimpleNamespace(from_numpy=lambda a: a, stack=lambda xs: np.stack(xs))

        def worker(im, name):                                                                     # 假模型：左半是前景
            a = np.zeros((im.height, im.width), np.uint8)
            a[:, : im.width // 2] = 255
            from PIL import Image
            return Image.fromarray(a, "L")
        with mock.patch.object(image, "torch", fake_torch), mock.patch.object(image.engine, "run_worker", worker):
            out, mask = image.ProMatte().run(src, next(iter(image.MATTE_LABELS)), "不处理（模型原样）")
        self.assertIs(out, src)                                                                   # 颜色原样：就是输入本身（不复制、不量化）
        self.assertEqual(mask.shape, (1, 20, 30))
        self.assertAlmostEqual(float(mask[0, 5, 5]), 0.0, places=3)                               # 前景：不透明 → mask 0
        self.assertAlmostEqual(float(mask[0, 5, 25]), 1.0, places=3)                              # 背景：透明 → mask 1

    def test_mask_to_alpha_rounds_instead_of_truncating(self):
        class T:
            def __init__(self, a):
                self.a = a

            def cpu(self):
                return self

            def numpy(self):
                return self.a
        a = image._alpha_from_mask(T(np.array([[0.0, 1.0, 0.5, 0.2]], np.float32)), (4, 1))
        self.assertEqual(list(np.asarray(a)[0]), [255, 0, 128, 204])                              # 0.8*255 = 204.0；0.5 → 127.5 → 128（不是被截成 127）
        b = image._alpha_from_mask(T(np.zeros((8, 8), np.float32)), (16, 16))
        self.assertEqual((b.size, int(np.asarray(b).min())), ((16, 16), 255))                     # 尺寸对不上（LoadImage 对没透明度的图给 64x64 的 MASK）会放大到图的大小

    def test_the_worker_is_started_with_this_python_and_the_given_timeout(self):
        import subprocess
        from PIL import Image
        got = {}

        def run(cmd, capture_output=True, text=True, timeout=None):
            got.update(cmd=cmd, timeout=timeout)
            Image.new("L", (10, 10), 9).save(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(image.engine, "model_path", lambda n: "/m.onnx"), mock.patch("subprocess.run", run):
            image.engine.run_worker(Image.new("RGB", (10, 10)), "u2net", timeout=77)
        import sys
        self.assertEqual(got["cmd"][0], sys.executable)
        self.assertTrue(got["cmd"][1].endswith("matte_engine.py") and os.path.isfile(got["cmd"][1]))
        self.assertEqual(got["timeout"], 77)
        self.assertEqual(image.engine.WORKER_TIMEOUT, 300)

    def test_an_unwritable_model_folder_is_a_plain_error_that_names_the_fix(self):
        with mock.patch.object(image.engine, "_model_dir", mock.Mock(side_effect=PermissionError("denied"))):
            with self.assertRaises(RuntimeError) as cm:
                image.engine.model_path("u2net")
        self.assertIn("PRO_MATTE_MODELS", str(cm.exception))

    def test_every_matting_choice_maps_to_a_known_model_and_edge_preset(self):
        first = next(iter(image.MATTE_LABELS))
        self.assertEqual(image.MATTE_LABELS[first], "u2net")                                    # 默认是最省内存又只抠主体的 u2net
        self.assertTrue(set(image.MATTE_LABELS.values()) <= set(image.MATTE_MODELS))
        for spec in image.MATTE_MODELS.values():
            self.assertEqual(len(spec["md5"]), 32)
            self.assertTrue(spec["file"].endswith(".onnx"))
        self.assertIn("标准（去掉白边）", image.MATTE_EDGES)


class LayerCompose(unittest.TestCase):
    @staticmethod
    def product(w=60, h=40, margin=20, color=(220, 30, 30)):
        from PIL import Image
        im = Image.new("RGBA", (w + 2 * margin, h + 2 * margin), (0, 0, 0, 0))
        im.paste(Image.new("RGBA", (w, h), color + (255,)), (margin, margin))
        return im

    def test_the_product_goes_to_the_chosen_spot_with_its_transparent_margin_ignored(self):
        r = image.compose(self.product(), None, (400, 400), (255, 255, 255), "正中", 50, shadow="没有阴影")
        self.assertEqual(r["final"].size, (400, 400))
        self.assertEqual(r["final"].getpixel((200, 200)), (220, 30, 30))
        self.assertEqual(r["final"].getpixel((5, 5)), (255, 255, 255))
        box = r["product"].split()[3].getbbox()                                                  # 商品在画布上的外框：宽 200（60 的 50% 画面宽 → 放大到 200）
        self.assertEqual((box[2] - box[0]), 200)
        self.assertEqual((box[0] + box[2]) // 2, 200)
        r2 = image.compose(self.product(), None, (400, 400), (255, 255, 255), "左上", 25, margin_pct=5, shadow="没有阴影")
        b2 = r2["product"].split()[3].getbbox()
        self.assertEqual((b2[0], b2[1]), (20, 20))                                                # 靠边：离边 5% = 20 像素
        r3 = image.compose(self.product(), None, (400, 400), (255, 255, 255), "右下", 25, margin_pct=5, shadow="没有阴影")
        b3 = r3["product"].split()[3].getbbox()
        self.assertEqual((400 - b3[2], 400 - b3[3]), (20, 20))

    def test_size_is_a_box_the_product_has_to_fit_in_both_ways(self):
        tall = self.product(30, 120)
        r = image.compose(tall, None, (400, 400), (255, 255, 255), "正中", 80, shadow="没有阴影")
        b = r["product"].split()[3].getbbox()
        self.assertEqual(b[3] - b[1], 320)                                                        # 高的商品：高度顶到 80%，宽度跟着缩
        self.assertEqual(b[2] - b[0], 80)

    def test_offsets_move_the_product_and_it_may_hang_over_the_edge_without_crashing(self):
        base = image.compose(self.product(), None, (400, 400), (255, 255, 255), "正中", 30, shadow="没有阴影")["product"].split()[3].getbbox()
        moved = image.compose(self.product(), None, (400, 400), (255, 255, 255), "正中", 30, offset=(10, -5), shadow="没有阴影")["product"].split()[3].getbbox()
        self.assertEqual((moved[0] - base[0], moved[1] - base[1]), (40, -20))
        out = image.compose(self.product(), None, (400, 400), (255, 255, 255), "正中", 30, offset=(45, 45), shadow="没有阴影")
        self.assertEqual(out["final"].size, (400, 400))

    def test_a_background_picture_sets_the_canvas_and_the_shadow_sits_under_the_product(self):
        from PIL import Image
        bg = Image.new("RGB", (300, 500), (30, 60, 200))
        r = image.compose(self.product(), bg, (999, 999), (255, 255, 255), "下中", 40, shadow="地面接触阴影", strength=0.5)
        self.assertEqual(r["final"].size, (300, 500))                                             # 有背景图：以它的大小为画布
        self.assertEqual(r["background"].getpixel((0, 0)), (30, 60, 200, 255))
        sh = r["shadow"].split()[3]
        pb = r["product"].split()[3].getbbox()
        sb = sh.point(lambda v: 255 if v > 8 else 0).getbbox()
        self.assertIsNotNone(sb)
        self.assertGreaterEqual(sb[3], pb[3] - 2)                                                 # 阴影在商品底部一带（会伸到商品底边下面）
        self.assertLess(sb[1], pb[3])
        self.assertLessEqual(int(np.asarray(sh).max()), 128)                                      # 强度 0.5：最浓也不超过一半
        self.assertEqual(r["shadow"].getpixel((5, 5))[3], 0)
        none = image.compose(self.product(), bg, (1, 1), (255, 255, 255), "下中", 40, shadow="没有阴影")
        self.assertIsNone(none["shadow"].split()[3].getbbox())
        soft = image.compose(self.product(), bg, (1, 1), (255, 255, 255), "正中", 40, shadow="柔和投影", strength=0.6)["shadow"].split()[3]
        self.assertIsNotNone(soft.getbbox())

    def test_the_final_image_is_the_layers_stacked_in_order(self):
        from PIL import Image
        r = image.compose(self.product(), Image.new("RGB", (200, 200), (255, 255, 255)), (1, 1), (0, 0, 0), "正中", 50, shadow="柔和投影")
        stack = r["background"].copy()
        stack.alpha_composite(r["shadow"])
        stack.alpha_composite(r["product"])
        self.assertTrue(np.array_equal(np.asarray(stack.convert("RGB")), np.asarray(r["final"])))

    def test_the_soft_drop_shadow_is_pushed_down_and_right_of_the_product(self):
        from PIL import Image
        r = image.compose(self.product(60, 40, 0), Image.new("RGB", (400, 400), (255, 255, 255)), (1, 1), (0, 0, 0), "正中", 40, shadow="柔和投影", strength=1.0, softness=0.2)
        pb = r["product"].split()[3].getbbox()
        sb = r["shadow"].split()[3].point(lambda v: 255 if v > 100 else 0).getbbox()
        self.assertGreater(sb[2], pb[2])                                                           # 阴影比商品更靠右 / 更靠下
        self.assertGreater(sb[3], pb[3])
        self.assertGreater(sb[0], pb[0])

    def test_a_huge_background_is_scaled_down_to_the_canvas_limit(self):
        from PIL import Image
        r = image.compose(self.product(), Image.new("RGB", (6000, 3000), (10, 20, 30)), (1, 1), (0, 0, 0), "正中", 30, shadow="没有阴影")
        self.assertEqual(r["final"].size, (image.MAX_CANVAS_SIDE, image.MAX_CANVAS_SIDE // 2))      # 最长边缩到 4096，比例不变
        self.assertEqual({im.size for im in (r["background"], r["product"], r["shadow"])}, {r["final"].size})   # 各层还是同样大小
        small = image.compose(self.product(), Image.new("RGB", (300, 200), (10, 20, 30)), (1, 1), (0, 0, 0), "正中", 30, shadow="没有阴影")
        self.assertEqual(small["final"].size, (300, 200))                                         # 小图不动
        self.assertEqual(image.ProLayerCompose.INPUT_TYPES()["required"]["short_side"][1]["max"], 2048)

    def test_an_empty_cutout_is_a_clear_error(self):
        from PIL import Image
        with self.assertRaises(RuntimeError):
            image.compose(Image.new("RGBA", (50, 50), (255, 0, 0, 0)), None, (100, 100), (255, 255, 255), "正中", 50)

    def test_an_opaque_photo_is_refused_with_a_hint_to_cut_it_out_first(self):
        import types

        class T:
            def __init__(self, a):
                self.a = a

            def cpu(self):
                return self

            def numpy(self):
                return self.a
        rgb = T(np.full((40, 60, 3), 0.5, np.float32))
        no_alpha = T(np.zeros((64, 64), np.float32))                                              # LoadImage 对没有透明度的图给的 MASK：全 0（= 全不透明）
        kw = dict(position="正中", scale_pct=50.0, margin_pct=4.0, offset_x_pct=0.0, offset_y_pct=0.0, shadow="没有阴影", shadow_strength=0.3, shadow_softness=1.0,
                  bg_color="#FFFFFF", ratio="1:1", short_side=256, save_layers=False, filename_prefix="x")
        with mock.patch.object(image, "torch", types.SimpleNamespace(from_numpy=lambda a: a, stack=lambda xs: xs)):
            with self.assertRaises(RuntimeError) as cm:
                image.ProLayerCompose().run([rgb], [no_alpha], **kw)
        self.assertIn("商品抠图", str(cm.exception))

    def test_an_unknown_matting_model_is_a_plain_error(self):
        from PIL import Image
        with self.assertRaises(RuntimeError) as cm:
            image.matte_image(Image.new("RGB", (8, 8)), "没有这个模型")
        self.assertIn("没有这个模型", str(cm.exception))

    def test_colors_and_sizes(self):
        self.assertEqual(image.parse_color("#FF8800"), (255, 136, 0))
        self.assertEqual(image.parse_color("f80"), (255, 136, 0))
        self.assertEqual(image.parse_color("乱写"), (255, 255, 255))
        self.assertEqual(image.parse_color(""), (255, 255, 255))
        self.assertAlmostEqual(image.fit_scale((400, 400), (100, 50), 50), 2.0)
        self.assertAlmostEqual(image.fit_scale((400, 200), (100, 100), 50), 1.0)

    def test_the_node_saves_the_final_image_first_then_the_layers_under_one_number(self):
        import tempfile
        import types

        class T:                                                                                  # 假的 torch 张量：只要 cpu().numpy()
            def __init__(self, a):
                self.a = a

            def cpu(self):
                return self

            def numpy(self):
                return self.a
        prod = self.product()
        rgb = T(np.asarray(prod.convert("RGB"), dtype=np.float32) / 255)
        mask = T(1.0 - np.asarray(prod.split()[3], dtype=np.float32) / 255)                       # LoadImage 的 MASK：1 = 透明
        fake_torch = types.SimpleNamespace(from_numpy=lambda a: a, stack=lambda xs: xs)
        out = tempfile.mkdtemp()
        counter = iter(range(1, 10))
        with mock.patch.object(image, "torch", fake_torch), mock.patch.object(image, "_slot", lambda prefix: (out, "商品", next(counter), "图层合成")):
            node = image.ProLayerCompose()
            kw = dict(position="正中", scale_pct=50.0, margin_pct=4.0, offset_x_pct=0.0, offset_y_pct=0.0, shadow="没有阴影", shadow_strength=0.3, shadow_softness=1.0,
                      bg_color="#00FF00", ratio="3:4", short_side=600, filename_prefix="图层合成/商品")
            res = node.run([rgb], [mask], save_layers=False, **kw)
            self.assertEqual([f["filename"] for f in res["ui"]["images"]], ["商品_00001_.png"])
            self.assertEqual(sorted(os.listdir(out)), ["商品_00001_.png"])
            self.assertEqual(res["ui"]["images"][0]["subfolder"], "图层合成")
            centre = res["result"][0][0][400][300]                                                  # 画面正中是商品的红色
            self.assertTrue(abs(centre[0] - 220 / 255) < 0.01 and abs(centre[1] - 30 / 255) < 0.01)
            res = node.run([rgb], [mask], save_layers=True, **kw)
            self.assertEqual([f["filename"] for f in res["ui"]["images"]],                          # 合成图排最前（后面的步骤取「第 1 张图」就是它），再是各层，共用一个序号
                             ["商品_00002_.png", "商品_00002_背景层.png", "商品_00002_商品层.png", "商品_00002_阴影层.png"])
        from PIL import Image
        with Image.open(os.path.join(out, "商品_00001_.png")) as im:
            self.assertEqual((im.mode, im.size), ("RGB", (600, 800)))                              # 3:4、短边 600
        for layer in ("背景层", "商品层", "阴影层"):
            with Image.open(os.path.join(out, f"商品_00002_{layer}.png")) as im:
                self.assertEqual((im.mode, im.size), ("RGBA", (600, 800)), layer)                  # 每层和合成图一样大，能按原位置叠回去

    def test_the_cutout_saver_keeps_the_alpha(self):
        import tempfile
        import types

        class T:
            def __init__(self, a):
                self.a = a

            def cpu(self):
                return self

            def numpy(self):
                return self.a
        prod = self.product()
        rgb = [T(np.asarray(prod.convert("RGB"), dtype=np.float32) / 255)]
        mask = [T(1.0 - np.asarray(prod.split()[3], dtype=np.float32) / 255)]
        out = tempfile.mkdtemp()
        with mock.patch.object(image, "_slot", lambda prefix: (out, "商品", 3, "商品抠图")):
            res = image.ProSaveCutout().run(rgb, mask, True, "商品抠图/商品")
        self.assertEqual(res["ui"]["images"], [{"filename": "商品_00003_.png", "subfolder": "商品抠图", "type": "output"}])
        from PIL import Image
        with Image.open(os.path.join(out, "商品_00003_.png")) as im:
            self.assertEqual((im.mode, im.size), ("RGBA", (60, 40)))                              # 带透明度；裁掉了四周的透明空白（60x40 的商品 + 20 的边）
            self.assertEqual(im.getpixel((30, 20)), (220, 30, 30, 255))
        with mock.patch.object(image, "_slot", lambda prefix: (out, "商品", 4, "商品抠图")):
            image.ProSaveCutout().run(rgb, mask, False, "商品抠图/商品")
        with Image.open(os.path.join(out, "商品_00004_.png")) as im:
            self.assertEqual(im.size, (100, 80))                                                  # 不裁：保持原图大小
            self.assertEqual(im.getpixel((2, 2))[3], 0)


class MatteNodes(unittest.TestCase):
    def test_widgets_and_outputs(self):
        spec = image.ProMatte.INPUT_TYPES()["required"]
        self.assertEqual(list(spec), ["image", "model", "edge"])
        self.assertEqual(spec["model"][1]["default"], next(iter(image.MATTE_LABELS)))
        self.assertEqual(spec["edge"][1]["default"], "标准（去掉白边）")
        self.assertEqual((image.ProMatte.RETURN_TYPES, image.ProSaveCutout.OUTPUT_NODE, image.ProLayerCompose.OUTPUT_NODE, getattr(image.ProMatte, "OUTPUT_NODE", False)), (("IMAGE", "MASK"), True, True, False))
        lc = image.ProLayerCompose.INPUT_TYPES()
        self.assertIn("background", lc["optional"])                                              # 背景图是可选的：不接就用纯色
        self.assertEqual(lc["required"]["position"][1]["default"], "正中")

if __name__ == "__main__":
    unittest.main()
