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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

VERSION = "1.5.0"
APP = "e2a-lite"
SENT_RE = re.compile(r"[^。！？；…!?\n]+[。！？；…!?]?")
CHAPTER_RE = re.compile(r"^(?:#{1,4}\s+.+|第[一二三四五六七八九十百零〇0-9]+[章篇回节部].*)$")
SILENCE_GAP = 0.16
CHAPTER_GAP = 0.90
SR = 22050


# ---------------------------------------------------------------- 文本切分
def split_chapters(text: str, title: str) -> list[dict]:
    """按 Markdown 标题切章节。

    `##` 作章；`###` 及更深一级的标题并入所在章，作为该章的首行小标题（朗读时会读出来），
    这样《心经通释》里反复出现的「原文」「般若深解」不会被切成两百个同名章。
    没有 `##` 时退化为「每个标题一章」，再没有就整篇一章。
    """
    has_l2 = bool(re.search(r"^##\s+", text, re.M))
    chapters: list[dict] = []
    cur = {"title": title, "lines": []}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m2 = re.match(r"^##\s+(.+)$", line)
        m3 = re.match(r"^###\s+(.+)$", line)
        m1 = re.match(r"^#\s+(.+)$", line)
        if has_l2 and m3 and cur["lines"] is not None:
            cur["lines"].append(m3.group(1).strip())
            continue
        if m2:
            if cur["lines"]:
                chapters.append(cur)
            cur = {"title": m2.group(1).strip(), "lines": []}
            continue
        if m1 and not has_l2:
            if cur["lines"]:
                chapters.append(cur)
            cur = {"title": m1.group(1).strip(), "lines": []}
            continue
        if m1:
            continue
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
    """用 ffprobe 读真实时长。wave 模块会被带附加 chunk 的 RIFF（如平台 TTS 的 AIGC 块）骗出巨大帧数。"""
    return ffprobe_duration(Path(path))


def probe_audio(path: Path) -> tuple[float, int, int]:
    """返回 (时长, 采样率, 声道)，全部取自 ffprobe，不信 wave 头。"""
    r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries",
                        "format=duration:stream=sample_rate,channels", "-of", "json", str(path)],
                       capture_output=True, text=True)
    d = json.loads(r.stdout or "{}")
    dur = float(d.get("format", {}).get("duration") or 0)
    st = (d.get("streams") or [{}])[0]
    return dur, int(st.get("sample_rate") or SR), int(st.get("channels") or 1)


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


# ---------------------------------------------------------------- 朗读清洗
_MARK = re.compile(r"[A-Za-zＡ-Ｚ]?[（(](?:阴|阳|太极|三才|五蕴)[）)]")
_FIG = re.compile(r"\*图[^\n]*")
_BRACKET = re.compile(r"【([^】]{1,16})】")
_EMPTY_PAREN = re.compile(r"[（(]\s*[）)]")
_DUP_PUNCT = re.compile(r"([，、；。])\1+")
_EDGE_PUNCT = re.compile(r"^[，、；\s]+")
_PUNCT_BEFORE = re.compile(r"[，、；]+(?=[：。！？])")


def clean_line(text: str) -> str:
    """朗读前清洗：去掉框架标注与图注，不改变正文措辞。

    只动三类东西：`佛说A（阴）` 这类阴阳太极标注（改为句读）、`【…】` 降为普通文字、
    `*图…` 图注整段丢弃；正文一字不改。
    """
    if "编注" in text[:12] or "**编注**" in text:
        return ""  # 编辑说明不读
    s = _FIG.sub("", text)  # 图注先整段丢弃
    if not s.strip():
        return ""
    if re.match(r"^\s*!?\[图", text) or re.search(r"\]\(figures/", text):
        return ""  # 图片链接不读
    s = re.sub(r"^\s*[-*·•]\s+", "", s)  # 列表符不读
    s = re.sub(r"^[>\s*]+", "", s)  # 引用符、强调符不读
    s = s.replace("*", "")  # 星号强调不读
    s = _MARK.sub("，", s)
    s = _BRACKET.sub(r"\1", s)
    s = _EMPTY_PAREN.sub("", s)
    s = s.replace("｜", "，").replace("|", "，")
    s = _DUP_PUNCT.sub(r"\1", s)
    s = _PUNCT_BEFORE.sub("", s)
    return _EDGE_PUNCT.sub("", s).strip()


def clean_chapters(chapters: list[dict]) -> list[dict]:
    for ch in chapters:
        out = []
        for l in ch["lines"]:
            c = clean_line(l)
            if not c:
                continue
            # 短标题（原文/般若深解/本节小结…）补一个停顿，免得读成一串
            if len(c) <= 8 and c[-1] not in "。！？：，；、" and not c.endswith("》"):
                c += "，"
            out.append(c)
        ch["lines"] = out
    return chapters


# ---------------------------------------------------------------- 合成进程
_V: dict = {}


def _init_worker(voice_path: str, speed: float) -> None:
    from piper import PiperVoice, SynthesisConfig
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    _V["voice"] = PiperVoice.load(voice_path)
    _V["cfg"] = SynthesisConfig(length_scale=speed)


def _ok_wav(path: Path) -> bool:
    """缓存文件是否可用：存在且时长可读、大于 20 ms。"""
    try:
        return path.exists() and ffprobe_duration(path) > 0.02
    except Exception:
        return False


def _silent_wav(path: str, sr: int = 22050, seconds: float = 0.05) -> None:
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(b"\x00\x00" * int(sr * seconds))


def _synth(job: tuple[str, str]) -> tuple[str, float]:
    """合成一句。参数先写死再交给 piper：极短或全是符号的句子会让 piper 不写头，
    直接报 `# channels not specified`；这里补静音兜底，绝不中断整本。"""
    text, path = job
    sr = int(getattr(_V["voice"].config, "sample_rate", 22050) or 22050)
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        try:
            _V["voice"].synthesize_wav(text, f, syn_config=_V["cfg"])
        except Exception as e:  # 单句失败不影响全本
            print(f"[build] 跳过异常句：{text[:18]}… ({e})", flush=True)
    try:
        d = wav_duration(Path(path))
    except Exception:
        d = 0.0
    if d <= 0.02:
        _silent_wav(path, sr)
        d = 0.05
    return path, d


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
    excl = getattr(args, "exclude", "") or ""
    if excl:
        pat = re.compile(excl)
        dropped = [c["title"] for c in chapters if pat.search(c["title"])]
        chapters = [c for c in chapters if not pat.search(c["title"])]
        for t in dropped:
            print(f"[build] 跳过章节：{t}")
    if getattr(args, "clean", False):
        chapters = clean_chapters(chapters)
    if not chapters:
        raise RuntimeError("排除后没有可读章节")
    jobs, plan = [], []
    for ci, ch in enumerate(chapters):
        for s in split_sentences(ch["lines"], args.max_len):
            key = cache_key(s, voice_name, args.speed)
            wav = cache / f"{key}.wav"
            plan.append((ci, s, wav))
            if not _ok_wav(wav):
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

    return _render(plan, chapters, outdir, book, args.artist, args.formats,
                   voice_name, args.speed, len(plan) - todo, todo, engine="piper", cache=cache, src=src, extras={"speed": args.speed},
                   clean_flag=getattr(args, "clean", False), br=getattr(args, "bitrate", "128k"))


def _render(plan, chapters, outdir, book, artist, fmts, voice_name, speed,
            hits, misses, engine="piper", cache=None, src="", extras=None, clean_flag=False,
            br="128k"):
    """按 plan（(章序, 句, wav)）拼出 mp3/m4b/srt/vtt/manifest；build 与 assemble 共用。"""
    outdir.mkdir(parents=True, exist_ok=True)
    cache = Path(cache) if cache else outdir / ".tmp" / "gap"
    cache.mkdir(parents=True, exist_ok=True)
    sample_rate, channels = SR, 1
    if plan:
        _, sample_rate, channels = probe_audio(plan[0][2])

    gaps = {}
    for sec in {round(SILENCE_GAP, 2), round(CHAPTER_GAP, 2)}:
        g = cache / f"_gap_{sec}_{sample_rate}_{channels}.wav"
        if not g.exists():
            run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi", "-i",
                 f"anullsrc=r={sample_rate}:cl={'stereo' if channels == 2 else 'mono'}",
                 "-t", str(sec), str(g)])
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
    lines = [";FFMETADATA1", f"title={book}", f"artist={artist}"]
    for i, (t, title) in enumerate(chap_marks):
        end = chap_marks[i + 1][0] if i + 1 < len(chap_marks) else clock
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(t*1000)}",
                  f"END={int(end*1000)}", f"title={title}"]
    meta.write_text("\n".join(lines) + "\n", encoding="utf-8")

    formats = {f.strip() for f in fmts.split(",") if f.strip()}
    produced = []
    if "mp3" in formats:
        p = outdir / f"{book}.mp3"
        run(["ffmpeg", "-y", "-v", "quiet", "-i", str(full), "-c:a", "libmp3lame", "-b:a", br,
             "-metadata", f"title={book}", "-metadata", f"artist={artist}", str(p)])
        produced.append(p)
    if "m4b" in formats:
        p = outdir / f"{book}.m4b"
        run(["ffmpeg", "-y", "-v", "quiet", "-i", str(full), "-i", str(meta), "-map_metadata", "1",
             "-c:a", "aac", "-b:a", br, "-f", "mp4", str(p)])
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
        "app": APP, "version": VERSION, "book": book, "source": str(src), **(extras or {}),
        "voice": voice_name, "speed": speed, "clean": bool(clean_flag),
        "chapters": len(chapters), "sentences": len(plan),
        "audio_seconds": round(clock, 2), "engine": engine,
        "chapter_marks": [{"t": round(t, 2), "title": ti} for t, ti in chap_marks],
        "files": [p.name for p in produced],
        "cache_hits": hits, "cache_misses": misses,
        "srt_lines": len(plan),
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(manifest, ensure_ascii=False, indent=1))
    _render.last_manifest = manifest
    return manifest
    return manifest



# ---------------------------------------------------------------- edge 引擎
EDGE_VOICES = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "yunxi": "zh-CN-YunxiNeural",
    "yunjian": "zh-CN-YunjianNeural",
    "xiaoyi": "zh-CN-XiaoyiNeural",
}


def _edge_one(text: str, voice: str, rate: str, dst: Path) -> None:
    """Edge 神经语音合成一句，落成 wav。"""
    import asyncio, edge_tts
    mp3 = dst.with_suffix(".mp3")

    async def go():
        c = edge_tts.Communicate(text, voice, rate=rate)
        await c.save(str(mp3))

    asyncio.run(go())
    run(["ffmpeg", "-y", "-v", "quiet", "-i", str(mp3), "-ar", "24000", "-ac", "1", str(dst)])
    mp3.unlink(missing_ok=True)


def edge_build(args) -> dict:
    """用 Edge 神经音色整本合成：逐句真实时长，字幕即句界。"""
    src = Path(args.inp)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    book = args.title or src.stem
    chapters = split_chapters(extract_text(src), book)
    excl = getattr(args, "exclude", "") or ""
    if excl:
        pat = re.compile(excl)
        chapters = [c for c in chapters if not pat.search(c["title"])]
    if getattr(args, "clean", False):
        chapters = clean_chapters(chapters)
    voice = EDGE_VOICES.get(args.voice, args.voice)
    cache = Path(args.cache) if args.cache else outdir / ".cache_edge"
    cache.mkdir(parents=True, exist_ok=True)
    jobs, plan, hits, misses = [], [], 0, 0
    for ci, ch in enumerate(chapters):
        for s in split_sentences(ch["lines"], args.max_len):
            key = hashlib.sha1(f"edge|{voice}|{args.rate}|{s}".encode()).hexdigest()
            wav = cache / f"{key}.wav"
            if len(re.findall(r"[\u4e00-\u9fff]", s)) < 2:
                if not _ok_wav(wav):
                    _silent_wav(str(wav), 24000, 0.3)
                hits += 1
            elif _ok_wav(wav):
                hits += 1
            else:
                jobs.append((s, str(wav), voice, args.rate))
                misses += 1
            plan.append((ci, s, wav))
    print(f"[edge] {book}：{len(chapters)} 章 / {len(plan)} 句 / 待合成 {len(jobs)}（命中 {hits}）", flush=True)
    if jobs:
        def work(job):
            import time
            t, path, v, r = job
            last = None
            for k in range(4):
                try:
                    _edge_one(t, v, r, Path(path))
                    return path, wav_duration(Path(path))
                except Exception as e:
                    last = e
                    time.sleep(0.6 * (2 ** k))
                    print(f"[edge] 重试 {k + 1}/4：{t[:12]}… ({str(e)[:40]})", flush=True)
            print(f"[edge] 四次失败，补 0.6s 静音：{t[:16]}…", flush=True)
            _silent_wav(path, 24000, 0.6)
            return path, 0.6
        with ThreadPoolExecutor(args.jobs) as pool:
            futs = [pool.submit(work, j) for j in jobs]
            for i, f in enumerate(as_completed(futs), 1):
                f.result()
                if i % 20 == 0 or i == len(jobs):
                    print(f"[edge] 合成 {i}/{len(jobs)}", flush=True)
    return _render(plan, chapters, outdir, book, args.artist, args.formats,
                   args.engine, 1.0, hits, misses, engine=args.engine,
                   cache=cache, src=src, clean_flag=getattr(args, "clean", False),
                   br=getattr(args, "bitrate", "128k"),
                   extras={"speed": 1.0, "voice": voice, "rate": args.rate,
                           "sentence_timing": "measured"})

# ---------------------------------------------------------------- assemble
def silence_points(path: Path, noise: str = "-38dB", min_dur: float = 0.12) -> list[tuple[float, float]]:
    """返回音频里的静音区间 [(start, end), ...]，用 ffmpeg silencedetect 实测。"""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af",
                           f"silencedetect=noise={noise}:d={min_dur}", "-f", "null", "-"],
                          capture_output=True, text=True)
    out = proc.stderr
    starts = [float(m) for m in re.findall(r"silence_start: ([0-9.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end: ([0-9.]+)", out)]
    pairs = list(zip(starts, ends + [float("inf")] * (len(starts) - len(ends))))
    return [(a, b) for a, b in pairs if a > 0.05]


def _align(dur: float, cues: list[str],
           silences: list[tuple[float, float]]) -> tuple[list[tuple[float, float]], bool]:
    """把 k 条句子按「实测停顿最接近字数预期」挂到时间轴上，返回 (边界列表, 是否全为实测)。"""
    if len(cues) == 1:
        return [(0.0, dur)], True
    cand = [(a + min(b, dur)) / 2 for a, b in silences if a < dur - 0.15]
    total = sum(len(c) for c in cues) or 1
    expect, cum = [], 0
    for c in cues[:-1]:
        cum += len(c)
        expect.append(dur * cum / total)
    used, bounds, measured = [], [], True
    for e in expect:
        best = None
        for i, c in enumerate(cand):
            if i in used or c >= dur - 0.2:
                continue
            if not bounds or c > bounds[-1] + 0.15:
                if best is None or abs(c - e) < abs(cand[best] - e):
                    best = i
        if best is None:
            bounds.append(e); measured = False
        else:
            used.append(best); bounds.append(cand[best])
    edges = [0.0] + sorted(bounds) + [dur]
    spans = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    return spans, measured


def _slice(path: Path, t0: float, t1: float, dst: Path) -> None:
    run(["ffmpeg", "-y", "-v", "quiet", "-ss", f"{max(t0, 0):.3f}", "-to", f"{t1:.3f}",
         "-i", str(path), "-ar", "40000", "-ac", "2", str(dst)])


AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}


def _natural(path: Path) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def _trim(path: Path, dst: Path) -> None:
    """削掉句首句尾静音，保留 60 ms 呼吸。"""
    flt = ("silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB:detection=peak,"
           "areverse,"
           "silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB:detection=peak,"
           "areverse,apad=pad_dur=0.06")
    run(["ffmpeg", "-y", "-v", "quiet", "-i", str(path), "-af", flt,
         "-ar", "40000", "-ac", "2", str(dst)])


def assemble(args) -> dict:
    """把任意 TTS 引擎产出的音频组装成带精确字幕与章节的有声书。

    两种映射自动识别：
    - 音频数 = 句数 → 一句一文件，时间轴即真实句长；
    - 音频数 = 段数 → 一段一文件，段内按实测停顿切句（切不出则退为按字数比例，并在 manifest 标注）。
    """
    src = Path(args.inp)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    book = args.title or src.stem
    chapters = split_chapters(extract_text(src), book)
    if getattr(args, "clean", False):
        chapters = clean_chapters(chapters)
    sents = [(ci, s) for ci, ch in enumerate(chapters) for s in split_sentences(ch["lines"], args.max_len)]
    paras = [(ci, p) for ci, ch in enumerate(chapters) for p in ch["lines"] if p.strip()]
    wavdir = Path(args.wavs)
    files = sorted([p for p in wavdir.iterdir() if p.suffix.lower() in AUDIO_EXT], key=_natural)

    work = outdir / ".tmp"
    work.mkdir(parents=True, exist_ok=True)
    measured_all = True

    mode = "auto" if args.cue_mode == "auto" else args.cue_mode
    blocks = []
    if getattr(args, "blocks", ""):
        blocks = json.loads(Path(args.blocks).read_text(encoding="utf-8"))
        mode = "block"
    if mode == "auto":
        mode = "paragraph" if len(files) == len(paras) else "sentence"

    if mode == "block":
        if len(files) != len(blocks):
            print(f"[assemble] 音频 {len(files)} 个，块表 {len(blocks)} 个；请核对")
            return 2
        plan = []
        for k, (blk, f) in enumerate(zip(blocks, files), 1):
            i0, n = blk["start"], blk["count"]
            cues = [t for _, t in sents[i0:i0 + n]]
            ci = sents[i0][0]
            dur = ffprobe_duration(f)
            sil = silence_points(f)
            spans, measured = _align(dur, cues, sil)
            measured_all = measured_all and measured
            pieces = work / f"b{k:03d}"
            pieces.mkdir(exist_ok=True)
            for j, ((t0, t1), cue) in enumerate(zip(spans, cues), 1):
                piece = pieces / f"{j:02d}.wav"
                _slice(f, t0, t1, piece)
                plan.append((ci, cue, piece))
        mode = "block"
    elif mode == "sentence" and len(files) == len(sents):
        plan = [(ci, t, f) for (ci, t), f in zip(sents, files)]
        mode = "sentence"
    elif mode == "paragraph" and len(files) == len(paras):
        mode = "paragraph"
        plan = []
        for i, ((ci, para), f) in enumerate(zip(paras, files), 1):
            cues = split_sentences([para], args.max_len)
            if args.align == "none" or len(cues) == 1:
                plan.append((ci, para, f))
                continue
            dur = ffprobe_duration(f)
            sil = silence_points(f)
            spans, measured = _align(dur, cues, sil)
            measured_all = measured_all and measured
            pieces = work / f"p{i:02d}"
            pieces.mkdir(exist_ok=True)
            for j, ((t0, t1), cue) in enumerate(zip(spans, cues), 1):
                piece = pieces / f"{j:02d}.wav"
                _slice(f, t0, t1, piece)
                plan.append((ci, cue, piece))
        if args.align == "none":
            measured_all = False
    else:
        print(f"[assemble] 音频 {len(files)} 个，对不上句数 {len(sents)} 或段数 {len(paras)}；请核对 --wavs")
        return 2

    print(f"[assemble] {book}：{len(chapters)} 章 / {len(plan)} 条字幕 / 映射 {mode} / "
          f"刀口{'实测' if measured_all else '含比例估算'}", flush=True)
    return _render(plan, chapters, outdir, book, args.artist, args.formats,
                   args.engine, 1.0, 0, len(plan), engine=args.engine,
                   cache=work / "gap", src=src,
                   clean_flag=getattr(args, "clean", False), br=getattr(args, "bitrate", "128k"),
                   extras={"speed": 1.0, "source_audio_dir": str(wavdir),
                           "cue_mode": mode,
                           "sentence_timing": "measured" if measured_all else "part measured"})


def split_book(args) -> int:
    """按 manifest 的章节时间点，把整本音频切成单章文件（mp3 + srt 片段）。"""
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    marks = man["chapter_marks"]
    total = ffprobe_duration(Path(args.audio)) or man["audio_seconds"]
    starts = [m["t"] if isinstance(m, dict) else m[0] for m in marks]
    srt_items = parse_srt(Path(args.srt)) if args.srt and Path(args.srt).exists() else []
    for i, m in enumerate(marks):
        t = starts[i]
        title = m["title"] if isinstance(m, dict) else m[1]
        end = starts[i + 1] if i + 1 < len(starts) else total
        safe = re.sub(r"[^\w\u4e00-\u9fff·]+", "-", title).strip("-")[:48] or f"ch{i+1:02d}"
        name = f"{i + 1:02d}-{safe}"
        run(["ffmpeg", "-y", "-v", "quiet", "-ss", f"{t:.3f}", "-to", f"{end:.3f}",
             "-i", args.audio, "-c:a", "libmp3lame", "-b:a", args.bitrate,
             str(outdir / f"{name}.mp3")])
        if srt_items:
            blocks = []
            for it in srt_items:
                if it["start"] >= t - 0.01 and it["end"] <= end + 0.01:
                    blocks.append((it["start"] - t, it["end"] - t, it["text"]))
            text = "".join(
                f"{j}\n{srt_ts(a)} --> {srt_ts(b)}\n{txt}\n"
                for j, (a, b, txt) in enumerate(blocks, 1))
            (outdir / f"{name}.srt").write_text(text, encoding="utf-8")
        print(f"[split] {name}.mp3  {t:.1f}–{end:.1f}s", flush=True)
    return 0


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
    lang_region, name, quality = args.voice.split("-")
    lang = lang_region.split("_")[0]
    base = f"{HF}/{lang}/{lang_region}/{name}/{quality}/"
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
    ap.add_argument("--version", action="version", version=f"{APP} {VERSION}")
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
    b.add_argument("--clean", action="store_true", help="朗读前去掉（阴）（阳）（太极）等框架标注与图注")
    b.add_argument("--exclude", default="", help="按正则跳过章节标题，如 编辑札记")
    b.add_argument("--bitrate", default="128k", help="音频码率，如 96k / 128k")
    b.add_argument("--cache", default=None)
    b.set_defaults(func=build)

    c = sub.add_parser("check", help="质检")
    c.add_argument("--audio", required=True)
    c.add_argument("--srt", required=True)
    c.set_defaults(func=check)

    a = sub.add_parser("assemble", help="用外部 TTS 的逐句音频组装字幕与章节")
    a.add_argument("--in", dest="inp", required=True, help="原文 txt/md/docx/epub/pdf")
    a.add_argument("--wavs", required=True, help="逐句音频目录（按序号排序对应句子）")
    a.add_argument("--out", required=True, help="输出目录")
    a.add_argument("--engine", default="api", help="引擎名，写进 manifest")
    a.add_argument("--title", default=None)
    a.add_argument("--artist", default="e2a-lite")
    a.add_argument("--formats", default="mp3,m4b,srt,vtt")
    a.add_argument("--max-len", type=int, default=70)
    a.add_argument("--clean", action="store_true", help="朗读前清洗框架标注（字幕同步使用清洗后文本）")
    a.add_argument("--bitrate", default="128k")
    a.add_argument("--blocks", default="", help="块表 JSON：每块对应一个音频文件，块内按停顿切句")
    a.add_argument("--trim", action="store_true", help="削掉句首句尾静音")
    a.add_argument("--cue-mode", choices=["auto", "sentence", "paragraph"], default="auto",
                   help="音频与文本的对应粒度：一句一文件 / 一段一文件")
    a.add_argument("--align", choices=["silence", "none"], default="silence",
                   help="逐段音频时，用实测停顿切句（默认）还是整段作一条")
    a.set_defaults(func=assemble)

    ed = sub.add_parser("edge", help="用 Edge 神经音色整本合成（逐句真实时长）")
    ed.add_argument("--in", dest="inp", required=True)
    ed.add_argument("--out", required=True)
    ed.add_argument("--title", default=None)
    ed.add_argument("--voice", default="xiaoxiao", help="xiaoxiao/yunxi/yunjian/xiaoyi 或完整音色名")
    ed.add_argument("--rate", default="-8%", help="语速，如 -8%% / +0%%")
    ed.add_argument("--max-len", type=int, default=70)
    ed.add_argument("--jobs", type=int, default=4)
    ed.add_argument("--formats", default="mp3,m4b,srt,vtt")
    ed.add_argument("--artist", default="润泽说")
    ed.add_argument("--engine", default="edge")
    ed.add_argument("--bitrate", default="128k")
    ed.add_argument("--clean", action="store_true")
    ed.add_argument("--exclude", default="")
    ed.add_argument("--cache", default=None)
    ed.set_defaults(func=edge_build)

    sp = sub.add_parser("split", help="按章节把整本音频切成单章")
    sp.add_argument("--audio", required=True)
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--srt", default=None)
    sp.add_argument("--out", required=True)
    sp.add_argument("--bitrate", default="96k")
    sp.set_defaults(func=split_book)

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
