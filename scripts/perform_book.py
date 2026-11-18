#!/usr/bin/env python3
"""全书表演版：逐章生成表演谱 → 逐章合成 → 逐章质检 → PCM 精确合订。

用法：
  python3 scripts/perform_book.py --in 书.md --out 目录 --voice zh-CN-YunjianNeural [--jobs 3]

产出：
  分章/NN-章名.mp3 + .srt + .m4b
  全书.m4b（35 章标记）/ 全书.srt / 全书.mp3
  表演质检.json（每章句数、时长、覆盖率、响度、结论）
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import perform as pf  # noqa: E402
import score_chapter as sc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tts  # noqa: E402

SR = pf.SR
GAP = 0.8


def run_soft(cmd: list[str]) -> tuple[str, int]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.stdout + p.stderr, p.returncode


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"命令失败：{' '.join(cmd)}\n{p.stderr[-500:]}")
    return p.stdout + p.stderr


def probe(path: Path) -> float:
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                          "format=duration", "-of", "default=nw=1:nk=1",
                                          str(path)]).decode().strip() or 0)


def srt_ts(sec: float) -> str:
    h, r = divmod(max(sec, 0), 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


def mono(lines: list[tuple[float, str, float]]) -> list[tuple[float, str, float]]:
    """字幕时间轴归一：任一 cue 的起点必须晚于上一条终点。"""
    out, prev = [], 0.0
    for a, txt, b in lines:
        a = max(a, prev + 0.05)
        b = max(b, a + 0.20)
        out.append((a, txt, b))
        prev = b
    return out


def chapter_cues(segs: list[dict], head: float) -> tuple[bytes, list[tuple[str, float, float]]]:
    """返回 PCM 与按整句合并后的字幕（时间相对章首）。"""
    pcm, cues = pf.assemble(segs, head)
    lines, cur, last_end = [], None, -1.0
    for sid, a, text, b in cues:
        a = max(a, last_end + 0.001)          # 时间轴严格递增，避免字幕重叠
        if cur and cur[0] == sid:
            cur[2] += text
            cur[3] = max(b, a + 0.05)
            last_end = cur[3]
        else:
            if cur:
                lines.append(cur[1:])
                last_end = cur[3]
            cur = [sid, a, text, max(b, a + 0.05)]
            last_end = cur[3]
    if cur:
        lines.append(cur[1:])
    return pcm, lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default=None)
    ap.add_argument("--voice", default="zh-CN-YunjianNeural")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--bitrate", default="96k")
    ap.add_argument("--exclude", default="编辑札记")
    ap.add_argument("--head-gap", type=float, default=0.4)
    args = ap.parse_args()

    src = Path(args.inp)
    out = Path(args.out)
    work = out / "_pieces"
    chdir = out / "分章"
    for d in (work, chdir):
        d.mkdir(parents=True, exist_ok=True)
    book = args.title or src.stem
    chs = tts.clean_chapters(tts.split_chapters(tts.extract_text(src), book))
    if args.exclude:
        pat = re.compile(args.exclude)
        chs = [c for c in chs if not pat.search(c["title"]) and c["title"].strip() != book]

    big: bytearray = bytearray()
    srt_lines, offsets, qa = [], [], []
    cursor = 0.0

    for i, ch in enumerate(chs, 1):
        title = ch["title"].strip()
        safe = re.sub(r"[^\w\u4e00-\u9fff·]+", "-", title).strip("-")[:40] or f"ch{i:02d}"
        name = f"{i:02d}-{safe}"
        sents = tts.split_sentences(ch["lines"], 70)
        segs = sc.build_score(sents)
        pf.synth_all(segs, args.voice, work, args.jobs)
        pcm, lines = chapter_cues(segs, args.head_gap)
        dur = len(pcm) / 2 / SR

        chw = work / f"{name}.wav"
        with wave.open(str(chw), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm)
        mp3 = chdir / f"{name}.mp3"
        run(["ffmpeg", "-y", "-v", "quiet", "-i", str(chw),
             "-af", "loudnorm=I=-17:TP=-1.5:LRA=11", "-c:a", "libmp3lame",
             "-b:a", args.bitrate, str(mp3)])
        srt = chdir / f"{name}.srt"
        lines = mono(lines)
        srt.write_text("\n".join(f"{k}\n{srt_ts(a)} --> {srt_ts(b)}\n{txt}\n"
                                 for k, (a, txt, b) in enumerate(lines, 1)), encoding="utf-8")
        meta = [";FFMETADATA1", f"title={title}", "artist=润泽说", "[CHAPTER]",
                "TIMEBASE=1/1000", "START=0", f"END={int(dur * 1000)}", f"title={title}"]
        mf = work / f"{name}.meta"
        mf.write_text("\n".join(meta) + "\n")
        run(["ffmpeg", "-y", "-v", "quiet", "-i", str(chw), "-i", str(mf), "-map", "0:a",
             "-map_metadata", "1", "-c:a", "aac", "-b:a", args.bitrate, "-f", "mp4",
             str(chdir / f"{name}.m4b")])

        chk, rc = run_soft([sys.executable, str(ROOT / "tts.py"), "check",
                            "--audio", str(mp3), "--srt", str(srt)])
        cover = next((l for l in chk.splitlines() if "覆盖率" in l), "").strip("· ")
        passed = rc == 0 and "全部通过" in chk
        bad = sum(1 for s in segs if s.get("_fail"))

        srt_lines += [f"{len(srt_lines) + k + 1}\n{srt_ts(a + cursor)} --> {srt_ts(b + cursor)}\n{txt}\n"
                      for k, (a, txt, b) in enumerate(mono(lines))]
        big += pcm + b"\x00" * int(GAP * SR) * 2
        offsets.append((cursor, title))
        cursor += dur + GAP

        qa.append({"章": title, "文件": name, "句": len(lines), "小句": len(segs),
                  "时长s": round(dur, 1), "覆盖率": cover,
                  "失败小句": bad, "结论": "通过" if passed and not bad else "未通过"})
        print(f"[{i}/{len(chs)}] {name} {dur:.1f}s {len(lines)}句 {'通过' if passed else '未通过'}"
              + (f" 失败{bad}" if bad else ""), flush=True)

    fullw = work / "full.wav"
    with wave.open(str(fullw), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(big))
    total = len(big) / 2 / SR

    run(["ffmpeg", "-y", "-v", "quiet", "-i", str(fullw), "-c:a", "libmp3lame",
         "-b:a", args.bitrate, str(out / f"{book}-表演版.mp3")])

    meta = [";FFMETADATA1", f"title={book}·表演版", "artist=润泽说", f"voice={args.voice}"]
    for k, (t0, title) in enumerate(offsets):
        t1 = offsets[k + 1][0] if k + 1 < len(offsets) else total
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(t0 * 1000)}",
                 f"END={int(t1 * 1000)}", f"title={title}"]
    mf = work / "chapters.txt"
    mf.write_text("\n".join(meta) + "\n")
    run(["ffmpeg", "-y", "-v", "quiet", "-i", str(fullw), "-i", str(mf), "-map", "0:a",
         "-map_metadata", "1", "-c:a", "aac", "-b:a", args.bitrate, "-f", "mp4",
         str(out / f"{book}-表演版.m4b")])
    (out / f"{book}-表演版.srt").write_text("\n".join(srt_lines), encoding="utf-8")

    passed_n = sum(1 for q in qa if q["结论"] == "通过")
    json.dump({"book": book, "voice": args.voice, "章节": len(chs),
               "总时长s": round(total, 1), "通过": passed_n, "细则": qa},
              open(out / "表演质检.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[done] {book} 表演版：{len(chs)} 章 / {total / 60:.1f} 分钟 / 通过 {passed_n}/{len(qa)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
