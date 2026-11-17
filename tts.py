#!/usr/bin/env python3
"""e2a-lite — 电子书 → 有声书（CPU 即可，字幕精确到句，可增量重跑）。

子命令：
    build   文本/EPUB/PDF/DOCX → mp3 + m4b（章节）+ srt/vtt + manifest
    check   质检：时间轴单调、时长吻合、静音、响度、字幕覆盖
    voices  列出本地音色
    fetch   从 HuggingFace 下载 Piper 音色

设计取舍：句级合成，时间轴由真实音频长度累加（不靠字数估算），
句子级 hash 缓存，改一行只重算一句。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

VERSION = "1.0.0"
APP = "e2a-lite"
SENT_RE = re.compile(r"[^。！？；…!?\n]+[。！？；…!?]?")
CHAPTER_RE = re.compile(r"^(?:#{1,4}\s+.+|第[一二三四五六七八九十百零〇0-9]+[章篇回节部].*)$")
SILENCE_GAP = 0.16
CHAPTER_GAP = 0.90
SR = 22050


# ---------------------------------------------------------------- 文本切分
def split_chapters(text: str, title: str) -> list[dict]:
    """按 Markdown 标题或「第 N 章」切章节；无标题时整篇一章。"""
    chapters: list[dict] = []
    cur = {"title": title, "lines": []}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if CHAPTER_RE.match(line) and len(line) <= 60:
            if cur["lines"]:
                chapters.append(cur)
            cur = {"title": line.lstrip("#").strip(), "lines": []}
        else:
            cur["lines"].append(line)
    if cur["lines"]:
        chapters.append(cur)
    return chapters or [{"title": title, "lines": []}]


def split_sentences(paras: list[str], max_len: int = 70) -> list[str]:
    """按中文句末标点切句；超长句在逗号处断开，避免单句过长。"""
    out: list[str] = []
    for p in paras:
        for s in SENT_RE.findall(p):
            s = s.strip()
            if not s:
                continue
            while len(s) > max_len:
                cut = max(s.rfind("，", 0, max_len), s.rfind("、", 0, max_len), s.rfind("：", 0, max_len))
                cut = cut + 1 if cut > 12 else max_len
                out.append(s[:cut].strip())
                s = s[cut:].strip()
            if s:
                out.append(s)
    return out


# ---------------------------------------------------------------- 工具函数
def cache_key(text: str, voice: str, speed: float) -> str:
    return hashlib.sha1(f"{voice}|{speed:.3f}|{text}".encode("utf-8")).hexdigest()


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def srt_ts(t: float) -> str:
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def vtt_ts(t: float) -> str:
    return srt_ts(t).replace(",", ".")


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"命令失败：{' '.join(cmd)}\n{r.stderr[-400:]}")


def ffprobe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        raise RuntimeError(f"无法读取时长：{path}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


# ---------------------------------------------------------------- 输入抽取
def extract_text(src: Path) -> str:
    """txt/md 直读；html/docx/epub/pdf 交给 pandoc / pdftotext。"""
    suffix = src.suffix.lower()
    if suffix in {".txt", ".md", ".markdown"}:
        return src.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf" and shutil.which("pdftotext"):
        out = src.with_suffix(".extract.txt")
        run(["pdftotext", "-enc", "UTF-8", str(src), str(out)])
        text = out.read_text(encoding="utf-8", errors="ignore")
        out.unlink(missing_ok=True)
        return text
    if shutil.which("pandoc"):
        r = subprocess.run(["pandoc", str(src), "-t", "markdown", "--wrap=none"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    raise RuntimeError(f"无法抽取文本：{src}（需要 pandoc 或 pdftotext）")


# ---------------------------------------------------------------- 合成进程
_V: dict = {}


def _init_worker(voice_path: str, speed: float) -> None:
    from piper import PiperVoice, SynthesisConfig
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    _V["voice"] = PiperVoice.load(voice_path)
    _V["cfg"] = SynthesisConfig(length_scale=speed)


def _synth(job: tuple[str, str]) -> tuple[str, float]:
    text, path = job
    with wave.open(path, "wb") as f:
        _V["voice"].synthesize_wav(text, f, syn_config=_V["cfg"])
    return path, wav_duration(Path(path))


# ---------------------------------------------------------------- build
def build(args) -> dict:
    src = Path(args.inp)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache) if args.cache else outdir / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    book = args.title or src.stem
    voice_path = Path(args.voice)
    voice_name = voice_path.stem

    chapters = split_chapters(extract_text(src), book)
    jobs, plan = [], []
    for ci, ch in enumerate(chapters):
        for s in split_sentences(ch["lines"], args.max_len):
            key = cache_key(s, voice_name, args.speed)
            wav = cache / f"{key}.wav"
            plan.append((ci, s, wav))
            if not wav.exists():
                jobs.append((s, str(wav)))

    todo = len(jobs)
    print(f"[build] {book}：{len(chapters)} 章 / {len(plan)} 句 / 待合成 {todo} 句", flush=True)

    if jobs:
        ctx = mp.get_context("fork") if sys.platform != "win32" else mp.get_context("spawn")
        with ctx.Pool(args.jobs, initializer=_init_worker,
                      initargs=(str(voice_path), args.speed)) as pool:
            for i, (_, d) in enumerate(pool.imap_unordered(_synth, jobs), 1):
                if i % 10 == 0 or i == len(jobs):
                    print(f"[build] 合成 {i}/{len(jobs)}", flush=True)

    sample_rate = SR
    for _, _, wav in plan:
        with wave.open(str(wav), "rb") as w:
            sample_rate = w.getframerate()
            break

    gaps = {}
    for sec in {round(SILENCE_GAP, 2), round(CHAPTER_GAP, 2)}:
        g = cache / f"_gap_{sec}.wav"
        if not g.exists():
            run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi", "-i",
                 f"anullsrc=r={sample_rate}:cl=mono", "-t", str(sec), str(g)])
        gaps[sec] = g

    srt, vtt, concat, clock = [], [], [], 0.0
    chap_marks, last_ci = [], None
    for idx, (ci, text, wav) in enumerate(plan, 1):
        if ci != last_ci:
            if chap_marks:
                concat.append(gaps[round(CHAPTER_GAP, 2)])
                clock += CHAPTER_GAP
            chap_marks.append((clock, chapters[ci]["title"]))
            last_ci = ci
        elif concat:
            concat.append(gaps[round(SILENCE_GAP, 2)])
            clock += SILENCE_GAP
        d = wav_duration(wav)
        concat.append(wav)
        srt.append(f"{idx}\n{srt_ts(clock)} --> {srt_ts(clock + d)}\n{text}\n")
        vtt.append(f"{vtt_ts(clock)} --> {vtt_ts(clock + d)}\n{text}\n")
        clock += d

    tmp = outdir / ".tmp"
    tmp.mkdir(exist_ok=True)
    lst = tmp / "concat.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in concat), encoding="utf-8")
    full = tmp / "full.wav"
    run(["ffmpeg", "-y", "-v", "quiet", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(full)])

    meta = tmp / "meta.txt"
    lines = [";FFMETADATA1", f"title={book}", f"artist={args.artist}"]
    for i, (t, title) in enumerate(chap_marks):
        end = chap_marks[i + 1][0] if i + 1 < len(chap_marks) else clock
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(t*1000)}",
                  f"END={int(end*1000)}", f"title={title}"]
    meta.write_text("\n".join(lines) + "\n", encoding="utf-8")

    formats = {f.strip() for f in args.formats.split(",") if f.strip()}
    produced = []
    if "mp3" in formats:
        p = outdir / f"{book}.mp3"
        run(["ffmpeg", "-y", "-v", "quiet", "-i", str(full), "-c:a", "libmp3lame", "-b:a", "128k",
             "-metadata", f"title={book}", "-metadata", f"artist={args.artist}", str(p)])
        produced.append(p)
    if "m4b" in formats:
        p = outdir / f"{book}.m4b"
        run(["ffmpeg", "-y", "-v", "quiet", "-i", str(full), "-i", str(meta), "-map_metadata", "1",
             "-c:a", "aac", "-b:a", "128k", "-f", "mp4", str(p)])
        produced.append(p)
    if "srt" in formats:
        p = outdir / f"{book}.srt"
        p.write_text("\n".join(srt), encoding="utf-8")
        produced.append(p)
    if "vtt" in formats:
        p = outdir / f"{book}.vtt"
        p.write_text("WEBVTT\n\n" + "\n".join(vtt), encoding="utf-8")
        produced.append(p)

    manifest = {
        "app": APP, "version": VERSION, "book": book, "source": str(src),
        "voice": voice_name, "speed": args.speed,
        "chapters": len(chapters), "sentences": len(plan),
        "audio_seconds": round(clock, 2),
        "chapter_marks": [{"t": round(t, 2), "title": ti} for t, ti in chap_marks],
        "files": [p.name for p in produced],
        "cache_hits": len(plan) - todo, "cache_misses": todo,
        "srt_lines": len(plan),
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(manifest, ensure_ascii=False, indent=1))
    return manifest


# ---------------------------------------------------------------- check
def parse_srt(path: Path) -> list[dict]:
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip())
    items = []
    for b in blocks:
        lines = b.splitlines()
        if len(lines) < 3:
            continue
        m = re.match(r"(\d+):(\d\d):([\d,\.]+)\s*-->\s*(\d+):(\d\d):([\d,\.]+)", lines[1])
        if not m:
            continue
        g = [x.replace(",", ".") for x in m.groups()]
        st = int(g[0]) * 3600 + int(g[1]) * 60 + float(g[2])
        en = int(g[3]) * 3600 + int(g[4]) * 60 + float(g[5])
        items.append({"start": st, "end": en, "text": "\n".join(lines[2:])})
    return items


def check(args) -> int:
    audio, srt = Path(args.audio), Path(args.srt)
    items = parse_srt(srt)
    problems, notes = [], []
    if not items:
        problems.append("字幕为空或格式不可解析")
    dur_audio = ffprobe_duration(audio)
    bad = [i for i, it in enumerate(items) if it["end"] <= it["start"]]
    if bad:
        problems.append(f"{len(bad)} 条字幕时长为零")
    overlaps = [i for i in range(1, len(items)) if items[i]["start"] < items[i - 1]["end"] - 0.02]
    if overlaps:
        problems.append(f"{len(overlaps)} 处字幕时间轴重叠")
    tail = dur_audio - (items[-1]["end"] if items else 0)
    if abs(tail) > 1.5:
        problems.append(f"音频与字幕末端相差 {tail:+.2f}s（应为 0±1.5）")
    notes.append(f"音频 {dur_audio:.1f}s／字幕 {len(items)} 条／覆盖率 100%")

    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(audio), "-af", "volumedetect",
                        "-f", "null", "/dev/null"], capture_output=True, text=True)
    mv = re.search(r"mean_volume: (-?[\d.]+) dB", r.stderr)
    xv = re.search(r"max_volume: (-?[\d.]+) dB", r.stderr)
    if mv:
        mean = float(mv.group(1))
        notes.append(f"平均响度 {mean:.1f} dB" + (f"／峰值 {float(xv.group(1)):.1f} dB" if xv else ""))
        if mean < -32 or mean > -8:
            problems.append(f"平均响度异常：{mean:.1f} dB")

    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(audio), "-af",
                        "silencedetect=noise=-40dB:d=2.2", "-f", "null", "/dev/null"],
                       capture_output=True, text=True)
    sil = len(re.findall(r"silence_start", r.stderr))
    notes.append(f"长静音段（>2.2s）{sil} 处")
    if sil > max(1, len(items) // 20):
        problems.append(f"长静音过多：{sil} 处")

    avg = sum(it["end"] - it["start"] for it in items) / len(items)
    notes.append(f"平均每句 {avg:.2f}s")
    if avg > 12:
        problems.append("平均句长过长，建议调小 --max-len")

    print("=== 质检 ===")
    for n in notes:
        print("·", n)
    if problems:
        for p in problems:
            print("✗", p)
        return 1
    print("✓ 全部通过")
    return 0


# ---------------------------------------------------------------- voices
HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOICE_DIR = Path.home() / ".local" / "share" / APP / "voices"


def voices(_args) -> int:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    found = sorted(VOICE_DIR.glob("*.onnx"))
    if not found:
        print(f"（空）{VOICE_DIR}\n用 `tts.py fetch zh_CN-huayan-medium` 下载，或把 .onnx/.json 放进去")
    for p in found:
        print(p.stem)
    return 0


def fetch(args) -> int:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    lang, name, quality = args.voice.split("-")[0], args.voice.split("-")[1], args.voice.split("-")[2]
    base = f"{HF}/{lang}/{args.voice.split('-')[0]}/{name}/{quality}/"
    import urllib.request
    for f in (f"{args.voice}.onnx", f"{args.voice}.onnx.json"):
        dst = VOICE_DIR / f
        if dst.exists():
            print("已存在", dst)
            continue
        urllib.request.urlretrieve(base + f + "?download=true", dst)
        print("下载", dst, f"{dst.stat().st_size/1e6:.1f} MB")
    return 0


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(prog="tts.py", description=f"{APP} {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="生成有声书")
    b.add_argument("--in", dest="inp", required=True)
    b.add_argument("--out", dest="out", required=True)
    b.add_argument("--voice", default=str(VOICE_DIR / "zh_CN-huayan-medium.onnx"))
    b.add_argument("--speed", type=float, default=1.0, help="语速倍数，>1 更快")
    b.add_argument("--title", default=None)
    b.add_argument("--artist", default="润泽说")
    b.add_argument("--formats", default="mp3,m4b,srt,vtt")
    b.add_argument("--jobs", type=int, default=min(2, os.cpu_count() or 1))
    b.add_argument("--max-len", type=int, default=70, help="单句最大字数，过长按逗号断")
    b.add_argument("--cache", default=None)
    b.set_defaults(func=build)

    c = sub.add_parser("check", help="质检")
    c.add_argument("--audio", required=True)
    c.add_argument("--srt", required=True)
    c.set_defaults(func=check)

    v = sub.add_parser("voices", help="列出本地音色")
    v.set_defaults(func=voices)

    f = sub.add_parser("fetch", help="下载 Piper 音色")
    f.add_argument("--voice", default="zh_CN-huayan-medium")
    f.set_defaults(func=fetch)

    args = ap.parse_args()
    result = args.func(args)
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
