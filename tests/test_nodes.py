import unittest

import numpy as np

from _load import load

video = load("pro-video")
ali = load("pro-ali")
image = load("pro-image")


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


class LabelPlace(unittest.TestCase):
    def test_corners(self):
        self.assertEqual(image.place((1000, 800), (100, 50), "左上", 10), (10, 10))
        self.assertEqual(image.place((1000, 800), (100, 50), "右下", 10), (890, 740))

    def test_centered(self):
        self.assertEqual(image.place((1000, 800), (100, 50), "上中", 10), (450, 10))
        self.assertEqual(image.place((1000, 800), (100, 50), "正中", 10), (450, 375))


if __name__ == "__main__":
    unittest.main()
