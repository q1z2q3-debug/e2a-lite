import subprocess, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tts


class TestSplit(unittest.TestCase):
    def test_chapters_by_heading(self):
        text = "# 书\n\n## 一\n\n甲。乙。\n\n## 二\n\n丙。\n"
        ch = tts.split_chapters(text, "书")
        self.assertEqual([c["title"] for c in ch], ["一", "二"])
        self.assertEqual(ch[0]["lines"], ["甲。乙。"])

    def test_chapters_no_heading(self):
        ch = tts.split_chapters("甲。乙。", "书")
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0]["title"], "书")

    def test_sentences(self):
        self.assertEqual(tts.split_sentences(["甲。乙！丙？"]), ["甲。", "乙！", "丙？"])

    def test_long_sentence_split(self):
        s = "一" * 120 + "。"
        out = tts.split_sentences([s], max_len=40)
        self.assertTrue(all(len(x) <= 40 for x in out))
        self.assertEqual("".join(out).count("一"), 120)


class TestHelpers(unittest.TestCase):
    def test_cache_key_stable(self):
        a = tts.cache_key("同一句", "v", 1.0)
        b = tts.cache_key("同一句", "v", 1.0)
        c = tts.cache_key("同一句", "v", 0.9)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_ts_format(self):
        self.assertEqual(tts.srt_ts(0), "00:00:00,000")
        self.assertEqual(tts.srt_ts(3661.5), "01:01:01,500")
        self.assertEqual(tts.vtt_ts(3661.5), "01:01:01.500")


class TestSrtRoundTrip(unittest.TestCase):
    def test_parse(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.srt"
            p.write_text("1\n00:00:00,000 --> 00:00:02,500\n甲。\n\n2\n00:00:02,600 --> 00:00:05,000\n乙。\n",
                         encoding="utf-8")
            items = tts.parse_srt(p)
            self.assertEqual(len(items), 2)
            self.assertAlmostEqual(items[0]["end"], 2.5)
            self.assertEqual(items[1]["text"], "乙。")


if __name__ == "__main__":
    unittest.main()
