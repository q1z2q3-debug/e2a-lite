"""写谱与落位的回归用例：参数取错、落位错位都会静默毁成品。"""
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import perform as pf
import score_chapter as sc


class ScoreTest(unittest.TestCase):
    def test_segments_carry_full_params(self):
        segs = sc.build_score(["色不异空，空不异色；受想行识，亦复如是。"])
        self.assertTrue(segs)
        for k in ("text", "rate", "pitch", "volume", "pause"):
            self.assertIn(k, segs[0])
        for s in segs:
            self.assertGreaterEqual(s["pause"], 0.0)

    def test_rate_and_pitch_keep_sign(self):
        # 曾把 +4% 格式化成 4%，Edge 直接拒绝，42 句整批失败
        segs = sc.build_score(["这一点很重要，须记住。"])
        for s in segs:
            self.assertRegex(s["rate"], r"^[+-]\d+%$")
            self.assertRegex(s["pitch"], r"^[+-]\d+Hz$")

    def test_clause_split_shortens_long_sentence(self):
        long = "这一句很长，" * 6 + "收尾。"
        self.assertGreater(len(sc.split_clauses(long)), 1)

    def test_pause_scaling_is_monotone(self):
        base = sc.build_score(["甲提出一个问题，乙给出答案。"])
        tight = json.loads(json.dumps(base))
        for s in tight:
            s["pause"] = round(s["pause"] * 0.62, 3)
        self.assertTrue(all(t["pause"] <= b["pause"] + 1e-9 for t, b in zip(tight, base)))


class AssembleTest(unittest.TestCase):
    def _wav(self, p: Path, sec: float):
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(pf.SR)
            w.writeframes(b"\x00\x00" * int(sec * pf.SR))

    def test_timeline_matches_pauses(self):
        with tempfile.TemporaryDirectory() as d:
            w1, w2 = Path(d) / "1.wav", Path(d) / "2.wav"
            self._wav(w1, 1.0)
            self._wav(w2, 2.0)
            segs = [
                {"text": "甲", "_wav": str(w1), "pause": 0.3, "sid": 1},
                {"text": "乙", "_wav": str(w2), "pause": 0.5, "sid": 2},
            ]
            pcm, cues = pf.assemble(segs, head=0.5)
            self.assertEqual(len(cues), 2)
            self.assertAlmostEqual(cues[0][1], 0.5, places=2)
            self.assertAlmostEqual(cues[1][1], 0.5 + 1.0 + 0.3, places=2)
            total = len(pcm) / 2 / pf.SR
            self.assertAlmostEqual(total, 0.5 + 1.0 + 0.3 + 2.0 + 0.5, places=2)

    def test_cache_key_separates_params(self):
        a = {"text": "同文", "rate": "+4%", "pitch": "-2Hz", "volume": "+0%"}
        b = dict(a, rate="+6%")
        self.assertEqual(pf.key_of(a, "v"), pf.key_of(dict(a), "v"))
        self.assertNotEqual(pf.key_of(a, "v"), pf.key_of(b, "v"))

    def test_srt_ts_format(self):
        self.assertEqual(pf.srt_ts(3725.25), "01:02:05,250")


if __name__ == "__main__":
    unittest.main()
