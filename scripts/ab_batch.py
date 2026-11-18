#!/usr/bin/env python3
"""批量 A/B：把未试改的章逐章按「停顿 ×0.62」重渲一遍，记账只报异常。

用法：
  python3 scripts/ab_batch.py --in 书.md --out 试改批量 --done 心经通释-表演版/分章 --skip "附录一,附录二,...,跋"

产出：每章目录 NN-章名/{章名-结构提速版.mp3,srt,m4b}，以及 ab_summary.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perform as pf  # noqa: E402
import score_chapter as sc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tts  # noqa: E402


def probe(p: Path) -> float:
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                          "format=duration", "-of", "default=nw=1:nk=1",
                                          str(p)]).decode().strip() or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ref", required=True, help="已交付的分章目录，用于取现版时长")
    ap.add_argument("--skip", default="", help="已试改的章名关键字，逗号分隔")
    ap.add_argument("--voice", default="zh-CN-YunjianNeural")
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ref = Path(args.ref)
    skips = [s.strip() for s in args.skip.split(",") if s.strip()]

    chs = tts.clean_chapters(tts.split_chapters(tts.extract_text(Path(args.inp)), "心经通释"))
    chs = [c for c in chs if "编辑札记" not in c["title"] and c["title"].strip() != "心经通释"]

    base_role = dict(sc.ROLE)
    sc.ROLE = {k: (base_role[k][0], base_role[k][1], round(base_role[k][2] * 0.62, 2)) for k in base_role}

    rows = []
    for i, ch in enumerate(chs, 1):
        title = ch["title"].strip()
        if any(s in title for s in skips):
            continue
        safe = re.sub(r"[^\w\u4e00-\u9fff·]+", "-", title).strip("-")[:36]
        name = f"{i:02d}-{safe}"
        cdir = out / name
        cdir.mkdir(exist_ok=True)
        mp3 = cdir / f"{safe}-结构提速版.mp3"
        srt = cdir / f"{safe}-结构提速版.srt"

        ref_files = sorted(ref.glob(f"{i:02d}-*.mp3"))
        ref_dur = probe(ref_files[0]) if ref_files else 0.0

        if not mp3.exists():
            segs = sc.build_score(tts.split_sentences(ch["lines"], 70))
            score_f = cdir / "score.json"
            score_f.write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
            rc = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "perform.py"),
                                 "--score", str(score_f), "--voice", args.voice,
                                 "--out", str(cdir), "--title", f"{safe}-结构提速版",
                                 "--jobs", str(args.jobs)], capture_output=True, text=True)
            for junk in (cdir / "_pieces",):
                if junk.exists():
                    subprocess.run(["rm", "-rf", str(junk)])
            if rc.returncode != 0:
                print(f"[{i}] {name} 渲染失败：{rc.stderr[-160:]}", flush=True)
                rows.append({"章": title, "现版s": round(ref_dur, 1), "提速版s": None, "结论": "渲染失败"})
                continue

        new_dur = probe(mp3)
        chk = subprocess.run([sys.executable, str(ROOT / "tts.py"), "check",
                              "--audio", str(mp3), "--srt", str(srt)], capture_output=True, text=True)
        ok = "全部通过" in chk.stdout and chk.returncode == 0
        delta = (ref_dur - new_dur) / ref_dur * 100 if ref_dur else 0
        rows.append({"章": title, "现版s": round(ref_dur, 1), "提速版s": round(new_dur, 1),
                     "提速%": round(delta, 1), "结论": "通过" if ok else "未通过"})
        print(f"[{i}] {name} {ref_dur:.0f}→{new_dur:.0f}s {delta:+.1f}% {'OK' if ok else 'FAIL'}", flush=True)

    (out / "ab_summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    done = [r for r in rows if r.get("提速%") is not None]
    out_txt = [r for r in done if abs(r["提速%"] - 2.0) > 0.8]
    print(f"[done] 本章批 {len(done)} 章；偏离 2% 超过 0.8 点的：{len(out_txt)} 章", flush=True)
    for r in out_txt:
        print("  异常：", r["章"], r["提速%"], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
