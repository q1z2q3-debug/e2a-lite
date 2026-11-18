#!/usr/bin/env python3
"""把 srt 的时间轴强制单调：任一 cue 的起点晚于上一条终点，零时长补到 0.2s。

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
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


def fix(path: Path) -> int:
    blocks = [b for b in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()) if b.strip()]
    out, prev, fixed = [], 0.0, 0
    for b in blocks:
        lines = b.splitlines()
        m = TS.search(lines[1]) if len(lines) > 1 else None
        if not m:
            continue
        a = to_sec(*m.groups()[:4])
        e = to_sec(*m.groups()[4:])
        na = max(a, prev + 0.05)
        ne = max(e, na + 0.20)
        if abs(na - a) > 0.001 or abs(ne - e) > 0.001:
            fixed += 1
        out.append(f"{len(out) + 1}\n{fmt(na)} --> {fmt(ne)}\n" + "\n".join(lines[2:]))
        prev = ne
    path.write_text("\n\n".join(out) + "\n", encoding="utf-8")
    return fixed


if __name__ == "__main__":
    target = Path(sys.argv[1])
    files = sorted(target.glob("*.srt")) if target.is_dir() else [target]
    for f in files:
        print(f"{f.name}: 修正 {fix(f)} 条")
