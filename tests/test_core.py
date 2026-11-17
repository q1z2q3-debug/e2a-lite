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


class TestAssembleGuards(unittest.TestCase):
    def test_natural_order(self):
        from pathlib import Path
        names = ["s10.wav", "s2.wav", "s1.wav"]
        got = sorted(names, key=lambda n: tts._natural(Path(n)))
        self.assertEqual(got, ["s1.wav", "s2.wav", "s10.wav"])

    def test_count_mismatch_returns_2(self):
        import argparse
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.txt"
            src.write_text("甲。乙。丙。", encoding="utf-8")  # 1 段 3 句
            wavs = Path(d) / "wavs"; wavs.mkdir()
            (wavs / "1.wav").write_bytes(b"")
            (wavs / "2.wav").write_bytes(b"")
            args = argparse.Namespace(inp=str(src), wavs=str(wavs), out=str(Path(d) / "out"),
                                      title=None, engine="t", artist="t", formats="mp3",
                                      max_len=70, trim=False, cue_mode="auto", align="silence")
            self.assertEqual(tts.assemble(args), 2)


class TestProbe(unittest.TestCase):
    def test_probe_returns_sane_values(self):
        import subprocess
        p = "/tmp/_probe_test.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi", "-i",
                        "anullsrc=r=40000:cl=stereo", "-t", "2", p], check=True)
        from pathlib import Path
        dur, rate, ch = tts.probe_audio(Path(p))
        self.assertAlmostEqual(dur, 2.0, delta=0.1)
        self.assertEqual((rate, ch), (40000, 2))


class TestAlign(unittest.TestCase):
    def test_single_cue_full_span(self):
        spans, measured = tts._align(10.0, ["一句。"], [])
        self.assertEqual(spans, [(0.0, 10.0)])
        self.assertTrue(measured)

    def test_uses_pauses_when_enough(self):
        sil = [(2.0, 2.4), (5.0, 5.4), (8.0, 8.4)]
        spans, measured = tts._align(10.0, ["甲甲。", "乙乙。", "丙丙。", "丁丁。"], sil)
        self.assertEqual(len(spans), 4)
        self.assertTrue(measured)
        self.assertTrue(all(spans[i][1] <= spans[i + 1][0] + 1e-6 for i in range(len(spans) - 1)))

    def test_fallback_marks_unmeasured(self):
        spans, measured = tts._align(10.0, ["甲。", "乙。", "丙。"], [])
        self.assertEqual(len(spans), 3)
        self.assertFalse(measured)
