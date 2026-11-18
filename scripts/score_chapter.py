#!/usr/bin/env python3
"""为一章正文生成「表演谱」：逐小句定语速、音高、音量与停顿。

谱不是模板填空，是按文体分层：
- 层题（原文/深解/对照/小结一类短标题）：慢、低、停顿长，先沉一拍
- 经文原句：最慢，句尾不催
- 词条训释：干净利落，一句一落
- 引经：让出处自己站出来
- 判语：落定，留白
- 对举：一句一停
- 叙述：常态

小句层面再叠一层「不像机器」的处理：
- 长句按逗号切小句，句中逐句微加速、音高逐级下坠（自然语调的降阶）
- 逗号停顿 0.14–0.20 s 按句内位置抖动
- 句间停顿 0.22–0.32 s 按索引抖动，不再是同一间隔
- 层题之后额外多半拍，章末一句再收
"""
from __future__ import annotations

import re
from typing import Iterable

HEAD_PATTERNS = (
    "原文，", "原文正读，", "般若深解，", "老庄对照，", "易传锚定，", "本节小结，",
    "下一篇：", "承上启下，", "结语，", "小结，",
)

# 文体角色 → (语速, 音高, 句间停顿)
# 小句独立合成会自带起句/收句的拉长，基准语速因此整体上提，
# 让「相对快慢」保留，绝对语速回到接近整句合成的水平（约 3.3 字/秒）。
ROLE = {
    "head": ("-10%", "-4Hz", 0.70),
    "sutra": ("-6%", "-3Hz", 0.55),
    "term": ("+2%", "-2Hz", 0.28),
    "quote": ("-3%", "-3Hz", 0.42),
    "verdict": ("-2%", "-2Hz", 0.55),
    "pair": ("+0%", "-2Hz", 0.35),
    "close": ("-4%", "-3Hz", 0.60),
    "body": ("+4%", "-2Hz", 0.26),
}

SUTRA_HINTS = re.compile(r"^(观自在菩萨|舍利子|是故|故知|以无所得故|菩提萨埵|三世诸佛|依般若|无挂碍|远离颠倒|究竟涅槃|揭谛|故说般若)")
VERDICT_HINTS = re.compile(r"(就是|即是|正是|便是|故名|所以|不是.{0,12}是|方为|此即|这就是|根源|内核|大纲)")
QUOTE_HINTS = re.compile(r"(《|曰：|言“|云：|经云|老子言|易传》)")


def role_of(text: str, idx: int, total: int) -> str:
    t = text.strip()
    if t in HEAD_PATTERNS or (len(t) <= 8 and t.endswith("，") and idx > 1):
        return "head"
    if SUTRA_HINTS.search(t):
        return "sutra"
    if QUOTE_HINTS.search(t):
        return "quote"
    if VERDICT_HINTS.search(t):
        return "verdict"
    if idx >= total - 2:
        return "close"
    if len(t) <= 18 and ("；" not in t) and re.search(r"(为墙|为壁|为底|属天|属地|居中)", t):
        return "pair"
    return "body"


def split_clauses(text: str) -> list[str]:
    parts = re.split(r"(?<=[，、；：])", text)
    out, buf = [], ""
    for p in parts:
        if not p:
            continue
        if len(buf) + len(p) <= 12 and buf:
            buf += p
        else:
            if buf:
                out.append(buf)
            buf = p
    if buf:
        out.append(buf)
    return out or [text]


def shift(rate: str, dx: int) -> str:
    return f"{int(rate.replace('%', '')) + dx:+d}%"


def lower(pitch: str, dz: int) -> str:
    return f"{int(pitch.replace('Hz', '')) + dz:+d}Hz"


def build_score(sentences: Iterable[str]) -> list[dict]:
    """把一章的句子展开成表演谱（小句级）。"""
    sents = [s.strip() for s in sentences if s and s.strip()]
    total = len(sents)
    segs: list[dict] = []
    for i, sent in enumerate(sents, 1):
        kind = role_of(sent, i, total)
        rate, pitch, pause = ROLE[kind]
        clauses = split_clauses(sent) if len(sent) > 16 else [sent]
        n = len(clauses)
        for j, cl in enumerate(clauses):
            last = j == n - 1
            # 句内：中段微提速，音高逐级下坠；末小句回落，读出“句号”
            r = shift(rate, 1 if 0 < j < n - 1 else (-2 if last and n > 1 else 0))
            p = lower(pitch, -1 * min(j, 3))
            if last and n > 1:
                p = lower(p, -1)
            comma_pause = 0.10 + (0.02 if j % 2 else 0.0)
            segs.append({
                "text": cl,
                "sid": i,
                "rate": r,
                "pitch": p,
                "volume": "+0%",
                "pause": (pause + 0.10 * (i % 3)) if last else comma_pause,
                "note": kind + ("·末" if last else "·中"),
            })
        if kind == "head":
            segs[-1]["pause"] = pause + 0.30
    return segs


if __name__ == "__main__":
    import json
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    import tts

    md = sys.argv[1]
    chs = tts.clean_chapters(tts.split_chapters(open(md).read(), "x"))
    print(json.dumps(build_score(tts.split_sentences(chs[0]["lines"], 70))[:6], ensure_ascii=False, indent=1))
