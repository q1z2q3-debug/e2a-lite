"""字幕时间轴兜底的回归用例：这四条对应实际踩过的坑。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_srt as fx


def parse(text):
    return fx.parse_cues(text)


class FixSrtTest(unittest.TestCase):
    def test_adjacent_overlap_is_clamped(self):
        # 这一条就是 09-分释篇六 的实际形态：后一句比前一句结束还早
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.srt"
            p.write_text("1\n00:00:01,000 --> 00:00:03,000\n前一句\n\n"
                         "2\n00:00:02,500 --> 00:00:04,000\n后一句\n", encoding="utf-8")
            fx.fix(p)
            cues = parse(p.read_text(encoding="utf-8"))
            self.assertEqual(len(cues), 2)
            self.assertGreaterEqual(cues[1][0], cues[0][1] - 1e-6)

    def test_stray_line_before_timestamp_does_not_drop_cue(self):
        # 旧版只认「块第 1 行是序号」之后的第 2 行，杂行会让整条 cue 被跳过，prev 卡住
        text = ("1\n00:00:01,000 --> 00:00:02,000\n甲\n\n"
                "补注：这一行是杂行\n2\n00:00:01,500 --> 00:00:03,000\n乙\n")
        self.assertEqual(len(parse(text)), 2)

    def test_missing_index_still_parsed(self):
        self.assertEqual(len(parse("00:00:01,000 --> 00:00:02,000\n只有正文\n")), 1)

    def test_crlf_and_bom(self):
        text = "\ufeff1\r\n00:00:01,000 --> 00:00:02,000\r\n行一\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\n行二\r\n"
        self.assertEqual(len(parse(text)), 2)

    def test_round_does_not_emit_1000ms(self):
        self.assertTrue(fx.fmt(1.9995).endswith(",000") or fx.fmt(1.9995) == "00:00:02,000")
        self.assertNotIn(",1000", fx.fmt(59.9996))

    def test_numeric_body_is_kept(self):
        # 「1.」「2、」这类条目是正文；纯数字若紧跟下一条时间戳才当序号丢掉
        cues = parse("1\n00:00:01,000 --> 00:00:02,000\n1\n色不异空\n")
        self.assertEqual(cues[0][2], ["1", "色不异空"])

    def test_zero_length_cue_gets_floor(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.srt"
            p.write_text("1\n00:00:05,000 --> 00:00:05,000\n零时长\n", encoding="utf-8")
            fx.fix(p)
            cues = parse(p.read_text(encoding="utf-8"))
            self.assertGreaterEqual(cues[0][1] - cues[0][0], 0.199)

    def test_renumbers_after_fix(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.srt"
            p.write_text("7\n00:00:01,000 --> 00:00:02,000\n一\n\n9\n00:00:01,900 --> 00:00:03,000\n二\n",
                         encoding="utf-8")
            fx.fix(p)
            lines = p.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "1")
            self.assertIn("2\n", p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
