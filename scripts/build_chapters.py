#!/usr/bin/env python3
"""逐章制作有声书：一章一合成、一章一质检，全部合格后再合订成整本。

用法：
  python3 scripts/build_chapters.py --in book.md --out 输出目录 --voice yunjian --rate=-8%
产出：
  分章/NN-章名.mp3 + .srt + .m4b   每章独立可听
  全书.m4b / 全书.srt / 全书.mp3    合订版（章标记来自实测时长）
  质检.json                        每章时长、句数、覆盖率、响度、结论
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tts  # noqa: E402

GAP = 0.8  # 章与章之间的呼吸


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"命令失败：{' '.join(cmd)}\n{p.stderr[-800:]}")
    return p.stdout + p.stderr


def probe(path: Path) -> float:
    return float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)]).strip() or 0)


def srt_shift(path: Path, offset: float) -> str:
    """读一章 srt，整体后移 offset 秒，返回拼进全书 srt 的文本。"""
    text = path.read_text(encoding="utf-8")
    out = []
    for block in [b for b in text.split("\n\n") if b.strip()]:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        stamp = lines[1]
        m = re.match(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", stamp)
        if not m:
            continue
        a = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000 + offset
        b = int(m[5]) * 3600 + int(m[6]) * 60 + int(m[7]) + int(m[8]) / 1000 + offset
        out.append(f"{len(out) + 1}\n{srt_ts(a)} --> {srt_ts(b)}\n" + "\n".join(lines[2:]) + "\n")
    return "\n".join(out)


def srt_ts(sec: float) -> str:
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default=None)
    ap.add_argument("--voice", default="yunjian")
    ap.add_argument("--rate", default="-8%")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--bitrate", default="96k")
    ap.add_argument("--exclude", default="编辑札记")
    ap.add_argument("--max-len", type=int, default=70)
    args = ap.parse_args()

    src = Path(args.inp)
    out = Path(args.out)
    work = out / ".work"
    chdir = out / "分章"
    for d in (work, chdir):
        d.mkdir(parents=True, exist_ok=True)
    book = args.title or src.stem
    chapters = tts.split_chapters(tts.extract_text(src), book)
    if args.exclude:
        pat = re.compile(args.exclude)
        chapters = [c for c in chapters if not pat.search(c["title"]) and c["title"].strip() != book]
    cache = out / ".cache_edge"
    cache.mkdir(exist_ok=True)

    qa, offsets, srt_all, pieces = [], [], [], []
    offset = 0.0
    gap_wav = work / "gap.wav"
    if not gap_wav.exists():
        tts._silent_wav(str(gap_wav), 24000, GAP)

    for i, ch in enumerate(chapters, 1):
        title = ch["title"].strip()
        safe = re.sub(r"[^\w\u4e00-\u9fff·]+", "-", title).strip("-")[:40] or f"ch{i:02d}"
        name = f"{i:02d}-{safe}"
        md = work / f"{name}.md"
        md.write_text(f"## {title}\n\n" + "\n\n".join(ch["lines"]) + "\n", encoding="utf-8")
        cdir = work / name
        if not (cdir / f"{title}.mp3").exists():
            print(f"[{i}/{len(chapters)}] 合成 {title}", flush=True)
            run([sys.executable, str(ROOT / "tts.py"), "edge", "--in", str(md), "--out", str(cdir),
                 "--title", title, "--voice", args.voice, f"--rate={args.rate}",
                 "--jobs", str(args.jobs), "--clean", "--cache", str(cache),
                 "--bitrate", args.bitrate, "--formats", "mp3,srt,m4b"])
        raw = cdir / f"{title}.mp3"
        normw = chdir / f"{name}.wav"   # PCM：合订用，时长精确，不受 mp3 编码填充影响
        run(["ffmpeg", "-y", "-v", "quiet", "-i", str(raw),
             "-af", "loudnorm=I=-17:TP=-1.5:LRA=11", "-ar", "24000", "-ac", "1",
             "-c:a", "pcm_s16le", str(normw)])
        norm = chdir / f"{name}.mp3"
        run(["ffmpeg", "-y", "-v", "quiet", "-i", str(normw),
             "-c:a", "libmp3lame", "-b:a", args.bitrate, str(norm)])
        srt = chdir / f"{name}.srt"
        shutil.copy(cdir / f"{title}.srt", srt)
        m4b = chdir / f"{name}.m4b"
        if not m4b.exists():
            shutil.copy(cdir / f"{title}.m4b", m4b)

        chk = run([sys.executable, str(ROOT / "tts.py"), "check", "--audio", str(norm), "--srt", str(srt)])
        sec = probe(normw)
        lines = len([b for b in srt.read_text(encoding="utf-8").split("\n\n") if b.strip()])
        cover = next((l for l in chk.splitlines() if "覆盖率" in l), "")
        loud = next((l for l in chk.splitlines() if "响度" in l), "")
        passed = "全部通过" in chk
        qa.append({"章": title, "文件名": name, "时长s": round(sec, 1), "句数": lines,
                   "覆盖率": cover.strip("· "), "响度": loud.strip("· "), "结论": "通过" if passed else "未通过"})
        print(f"[{i}/{len(chapters)}] {name} {sec:.1f}s {lines} 句 {'通过' if passed else '未通过'}", flush=True)
        srt_all.append(srt_shift(srt, offset))
        pieces.append(normw)
        offsets.append((offset, title))
        offset += sec + GAP

    # 合订：mp3 + 章节标记 + 全书 srt
    concat_list = work / "concat.txt"
    with open(concat_list, "w") as f:
        for k, p in enumerate(pieces):
            f.write(f"file '{p}'\n")
            if k != len(pieces) - 1:
                f.write(f"file '{gap_wav}'\n")
    fullw = work / "full.wav"
    run(["ffmpeg", "-y", "-v", "quiet", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-c:a", "pcm_s16le", str(fullw)])
    total = probe(fullw)
    full = out / f"{book}-{args.voice}.mp3"
    run(["ffmpeg", "-y", "-v", "quiet", "-i", str(fullw),
         "-c:a", "libmp3lame", "-b:a", args.bitrate, str(full)])

    meta = [";FFMETADATA1", f"title={book}", "artist=润泽说", f"voice={args.voice}"]
    for k, (t0, title) in enumerate(offsets):
        t1 = offsets[k + 1][0] if k + 1 < len(offsets) else total
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(t0 * 1000)}",
                 f"END={int(t1 * 1000)}", f"title={title}"]
    meta_f = work / "chapters.txt"
    meta_f.write_text("\n".join(meta) + "\n")
    fm4b = out / f"{book}-{args.voice}.m4b"
    run(["ffmpeg", "-y", "-v", "quiet", "-i", str(fullw), "-i", str(meta_f),
         "-map", "0:a", "-map_metadata", "1", "-c:a", "aac", "-b:a", args.bitrate, "-f", "mp4", str(fm4b)])
    (out / f"{book}-{args.voice}.srt").write_text("\n".join(srt_all), encoding="utf-8")

    json.dump({"book": book, "voice": args.voice, "rate": args.rate,
               "章节数": len(chapters), "总时长s": round(total, 1),
               "通过": sum(1 for q in qa if q["结论"] == "通过"),
               "细则": qa}, open(out / "质检.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[done] {book}：{len(chapters)} 章 / {total / 60:.1f} 分钟 / "
          f"通过 {sum(1 for q in qa if q['结论'] == '通过')}/{len(qa)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
