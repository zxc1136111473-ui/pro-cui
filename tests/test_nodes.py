import json
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


class PromptWriter(unittest.TestCase):
    INFO = json.dumps({"apikey": "k", "custom_api_base": "https://dashscope.aliyuncs.com/compatible-mode"})

    @staticmethod
    def resp(status=200, body=None):
        r = mock.Mock(status_code=status, text=json.dumps(body))
        r.json.return_value = body
        return r

    def run_node(self, post, idea="一个红苹果", instruction="写提示词"):
        with mock.patch.object(ali.requests, "post", post):
            return ali.ProAliPromptWriter().run(idea, instruction, "", 0, self.INFO)

    def test_success_and_request_shape(self):
        post = mock.Mock(return_value=self.resp(body={"choices": [{"message": {"content": "  完整提示词 \n"}}], "usage": {"total_tokens": 9}}))
        text, status = self.run_node(post)
        self.assertEqual(text, "完整提示词")
        self.assertEqual(json.loads(status)["code"], "success")
        url, kw = post.call_args[0][0], post.call_args[1]
        self.assertEqual(url, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(kw["json"]["messages"], [{"role": "system", "content": "写提示词"}, {"role": "user", "content": "一个红苹果"}])
        self.assertIs(kw["json"]["enable_thinking"], False)

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
