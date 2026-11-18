#!/usr/bin/env python3
"""把 srt 的时间轴强制单调：任一 cue 起点晚于上一条终点，零时长补到 0.2s。

解析按时间戳锚定，不再依赖「块第一行是序号」——这是旧版漏修跨块重叠的原因。

用法：python3 scripts/fix_srt.py 文件或目录...
"""
import re
import sys
from pathlib import Path

TS = re.compile(r"(\d+):(\d\d):(\d\d),(\d+) --> (\d+):(\d\d):(\d\d),(\d+)")


def to_sec(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def fmt(sec):
    sec = max(sec, 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    ms = int(round((s - int(s)) * 1000))
    if ms == 1000:                     # 舍入进位，不能让毫秒写成 1000
        s += 1
        ms = 0
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def parse_cues(text: str) -> list[list]:
    """时间戳锚定解析：容忍缺序号、序号错位、CRLF、BOM、时间戳前后有杂行。"""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    cues = []
    i = 0
    while i < len(lines):
        m = TS.search(lines[i])
        if not m:
            i += 1
            continue
        a = to_sec(*m.groups()[:4])
        e = to_sec(*m.groups()[4:])
        body, j = [], i + 1
        while j < len(lines) and not TS.search(lines[j]):
            raw = lines[j].rstrip()
            nxt = j + 1
            while nxt < len(lines) and not lines[nxt].strip():
                nxt += 1
            if raw.strip().isdigit() and nxt < len(lines) and TS.search(lines[nxt]):
                j += 1                 # 这是下一条的序号，不是正文
                continue
            if raw.strip():
                body.append(raw.strip())
            j += 1
        cues.append([a, e, body])
        i = j
    return cues


def fix(path: Path) -> int:
    cues = parse_cues(path.read_text(encoding="utf-8"))
    out, prev, fixed = [], 0.0, 0
    for a, e, body in cues:
        na = max(a, prev + 0.05)
        ne = max(e, na + 0.20)
        if abs(na - a) > 0.001 or abs(ne - e) > 0.001:
            fixed += 1
        out.append(f"{len(out) + 1}\n{fmt(na)} --> {fmt(ne)}\n" + "\n".join(body) if body
                   else f"{len(out) + 1}\n{fmt(na)} --> {fmt(ne)}\n")
        prev = ne
    path.write_text("\n\n".join(out) + "\n", encoding="utf-8")
    return fixed


if __name__ == "__main__":
    target = Path(sys.argv[1])
    files = sorted(target.rglob("*.srt")) if target.is_dir() else [target]
    for f in files:
        print(f"{f.name}: 修正 {fix(f)} 条")
