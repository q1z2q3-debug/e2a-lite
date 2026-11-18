#!/usr/bin/env python3
"""按「表演谱」合成：每一小句自带语速、音高、音量与句后停顿，引擎只负责执行。

表演谱（JSON）由 `score_chapter.py` 或人逐句写：
[{"text":"…","sid":1,"rate":"-8%","pitch":"-2Hz","volume":"+0%","pause":0.26,"note":"body·末"}]

用法：
  python3 scripts/perform.py --score s.json --voice zh-CN-YunjianNeural --out 目录 --title 章名 [--jobs 3]

产出：章名.mp3 / .srt（小句合回整句）/ .m4b；响度归一到 -17 dB。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import time
import json
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tts  # noqa: E402

SR = 24000


def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"命令失败：{' '.join(cmd)}\n{p.stderr[-600:]}")


def wav_dur(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def say(text: str, voice: str, rate: str, pitch: str, volume: str, dst: Path) -> bool:
    """合成一小句到 24k 单声道 wav；失败返回 False。"""
    import edge_tts
    mp3 = dst.with_suffix(".mp3")

    async def go():
        await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume).save(str(mp3))

    for k in range(4):
        try:
            asyncio.run(go())
            if mp3.exists() and mp3.stat().st_size > 4000:
                break
        except Exception as e:
            if k == 3:
                print(f"[fail] {type(e).__name__}: {str(e)[:60]}", flush=True)
                return False
        mp3.unlink(missing_ok=True)
        time.sleep(1.2 ** (k + 1))
    run(["ffmpeg", "-y", "-v", "quiet", "-i", str(mp3), "-ar", str(SR), "-ac", "1",
         "-c:a", "pcm_s16le", str(dst)])
    mp3.unlink(missing_ok=True)
    return True


def srt_ts(sec: float) -> str:
    h, r = divmod(max(sec, 0), 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


def key_of(seg: dict, voice: str) -> str:
    return hashlib.sha1(f"{voice}|{seg['rate']}|{seg['pitch']}|{seg['volume']}|{seg['text']}"
                        .encode()).hexdigest()[:12]


def synth_all(segs: list[dict], voice: str, work: Path, jobs: int) -> int:
    """按内容去重后并发合成；返回失败条数。"""
    groups: dict[str, list[dict]] = {}
    for seg in segs:
        groups.setdefault(key_of(seg, voice), []).append(seg)

    def one(item: tuple[str, list[dict]]) -> bool:
        key, same = item
        dst = work / f"{key}.wav"
        has_word = bool(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", same[0]["text"]))
        if not has_word:
            # 纯标点句（「——，」「---」）：发不出音，给一拍静默，不算失败
            sil = work / f"{key}.sil.wav"
            if not sil.exists():
                tts._silent_wav(str(sil), SR, 0.15)
            for s in same:
                s["_wav"] = str(sil)
            return True
        if dst.exists() and wav_dur(dst) > 0.2:
            for s in same:
                s["_wav"] = str(dst)
            return True
        if say(same[0]["text"], voice, same[0]["rate"], same[0]["pitch"], same[0]["volume"], dst):
            for s in same:
                s["_wav"] = str(dst)
            return True
        bad = work / f"{key}.fail.wav"
        dst.unlink(missing_ok=True)          # 失败不入缓存，下轮重试
        if not bad.exists():
            tts._silent_wav(str(bad), SR, 0.4)
        print(f"[warn] 合成失败，补 0.4s 静音：{same[0]['text'][:12]}", flush=True)
        for s in same:
            s["_wav"] = str(bad)
            s["_fail"] = True
        return False

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        results = list(ex.map(one, list(groups.items())))
    return sum(1 for r in results if not r)


def assemble(segs: list[dict], head: float) -> tuple[bytes, list[tuple[int, float, str, float]]]:
    """按谱排时间轴：先铺头部静音，逐小句落位，句后按谱留白。"""
    cues, cursor = [], head
    buf = bytearray(int(head * SR) * 2)
    for seg in segs:
        with wave.open(seg["_wav"], "rb") as w:
            frames = w.readframes(w.getnframes())
        d = len(frames) / 2 / SR
        buf += frames
        cues.append((seg.get("sid", 0), cursor, seg["text"], cursor + d))
        cursor += d
        pad = int(round(float(seg.get("pause", 0.2)) * SR)) * 2
        buf += b"\x00" * pad
        cursor += pad / 2 / SR
    return bytes(buf), cues


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="表演版")
    ap.add_argument("--voice", default="zh-CN-YunjianNeural")
    ap.add_argument("--bitrate", default="96k")
    ap.add_argument("--head-gap", type=float, default=0.4)
    ap.add_argument("--jobs", type=int, default=3)
    args = ap.parse_args()

    segs = json.loads(Path(args.score).read_text(encoding="utf-8"))
    out = Path(args.out)
    work = out / "_pieces"
    work.mkdir(parents=True, exist_ok=True)

    synth_all(segs, args.voice, work, args.jobs)
    pcm, cues = assemble(segs, args.head_gap)

    fullw = work / "full.wav"
    with wave.open(str(fullw), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    total = wav_dur(fullw)

    mp3 = out / f"{args.title}.mp3"
    run(["ffmpeg", "-y", "-v", "quiet", "-i", str(fullw),
         "-af", "loudnorm=I=-17:TP=-1.5:LRA=11", "-c:a", "libmp3lame", "-b:a", args.bitrate, str(mp3)])

    meta = [";FFMETADATA1", f"title={args.title}", "artist=润泽说", "[CHAPTER]",
            "TIMEBASE=1/1000", "START=0", f"END={int(total * 1000)}", f"title={args.title}"]
    mf = work / "meta.txt"
    mf.write_text("\n".join(meta) + "\n")
    run(["ffmpeg", "-y", "-v", "quiet", "-i", str(fullw), "-i", str(mf), "-map", "0:a",
         "-map_metadata", "1", "-c:a", "aac", "-b:a", args.bitrate, "-f", "mp4",
         str(out / f"{args.title}.m4b")])

    # 字幕：小句合回整句
    lines, cur = [], None
    for sid, a, text, b in cues:
        if cur and cur[0] == sid:
            cur[2] += text
            cur[3] = b
        else:
            if cur:
                lines.append(cur)
            cur = [sid, a, text, b]
    if cur:
        lines.append(cur)
    (out / f"{args.title}.srt").write_text(
        "\n".join(f"{k}\n{srt_ts(a)} --> {srt_ts(b)}\n{txt}\n"
                  for k, (_, a, txt, b) in enumerate(lines, 1)), encoding="utf-8")

    bad_n = sum(1 for s in segs if s.get("_fail"))
    print(f"[perform] {args.title}：{len(segs)} 小句 / {len(lines)} 句 / "
          f"{total / 60:.2f} 分钟 / 失败 {bad_n}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
