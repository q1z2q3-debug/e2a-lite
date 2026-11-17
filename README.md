# e2a-lite

把一本电子书，做成**带精确字幕、带章节、可增量重跑**的有声书。纯 CPU、无显卡、2 核 3 GB 也能跑完一本。

站点：<https://q1z2q3-debug.github.io/e2a-lite/>　试听样章：《心经》57 秒

## 它和 ebook2audiobook 差在哪

ebook2audiobook 的强项是音色克隆（XTTS 系）与引擎数量；它的字幕是「按段落估」，一旦某章改一个字就得整本重跑，而且默认要显卡才顺。

本项目把力气放在另外几件事上：

| | ebook2audiobook | e2a-lite |
|---|---|---|
| 硬件 | 建议 4 GB VRAM 以上 | 2 核 / 2 GB 内存 / 无显卡（实测 15×实时） |
| 字幕时间轴 | 段落级、估算 | **句级真实时间戳**：逐句合成、逐句计时，零误差 |
| 增量 | 整章或整本重来 | **句子级 hash 缓存**：改一句只重算一句 |
| 质检 | 无 | `check` 子命令：时间轴单调／首尾吻合／重叠／静音／响度／覆盖率，不通过就退非零 |
| 可审计 | 日志 | `manifest.json`：音色、语速、章数、句数、时长、缓存命中、章节时间点 |
| 输入 | 电子书格式为主 | txt／md／html／docx／epub／pdf（pandoc／pdftotext 兜底） |
| 引擎 | 自带多种、绑定整套依赖 | Piper 开箱即用；**外部 TTS 的逐句音频也能直接组装**（`assemble`） |
| 输出 | m4b 等 | mp3／m4b（章节标记）／srt／vtt／manifest |

不承诺的事：**本机不做音色克隆**。3 GB 内存放不下 XTTS，这台机器也带不动它。要克隆音色请去有显卡的机器，本项目不假装能做。

## 快速开始

```bash
pip install -r requirements.txt          # piper-tts[alignment]
python3 tts.py fetch --voice zh_CN-huayan-medium
python3 tts.py build --in examples/心经.txt --out out/
python3 tts.py check --audio out/心经.mp3 --srt out/心经.srt
```

产物在 `out/`：`心经.mp3`、`心经.m4b`（两章）、`心经.srt`、`心经.vtt`、`manifest.json`。

用别的引擎（平台 TTS、云服务、你自己的模型）念的逐句音频，也能套上同一套时间轴：

```bash
python3 tts.py assemble --in 书.md --wavs wavs/ --out out/ --trim
```

常用参数：

```bash
--speed 0.95         # 语速倍数，越小越慢越稳
--jobs 2             # 并行进程数（CPU 核数一半左右最稳）
--max-len 60         # 单句最大字数，过长按逗号断
--formats mp3,srt    # 只出需要的格式
--cache DIR          # 缓存目录，跨书复用同一句
--title "书名" --artist "润泽说"
```

## 实测

| 文本 | 字数 | 音频 | 合成用时 | 倍速 |
|---|---|---|---|---|
| 《心经》全篇 | 260 | 57.7 s | 6.9 s | 8.4× |
| 长文压测（12 段） | 4,052 | 11 min 36 s | 45 s | 15.4× |
| 推算《心经通释》全书 | 71,207 | ≈3.4 h | ≈13 min | — |

机器：2 vCPU（Xeon 8457C）、3 GB 内存、无 GPU、onnxruntime CPU。

## 已知边界

- Piper 的音色是「稳、清楚、没有表演」，不是真人朗读；想更有情绪，需要换引擎或上真人。
- 中文音色目前只有 `zh_CN-huayan-medium`（女声）可直接用；多音字与古文断句按规则切，个别处会读错，靠 `--max-len` 与手动改句缓解。
- 章节识别依赖 Markdown 标题或「第 N 章／篇／回」；无标题的长文按整篇一章处理。
- 每句单独合成，句间会有 0.16 s 停顿；句数特别多时听感偏「一句一顿」，可调 `SILENCE_GAP`。

## 许可

MIT。示例音频为《心经》公有领域文本，由本项目管线生成。
