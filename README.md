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
| 引擎 | 自带多种、绑定整套依赖 | Piper 开箱即用；**外部 TTS 的音频也能直接组装**（`assemble`，逐句或逐段都行） |
| 输出 | m4b 等 | mp3／m4b（章节标记）／srt／vtt／manifest |

不承诺的事：**本机不做音色克隆**。3 GB 内存放不下 XTTS，这台机器也带不动它。要克隆音色请去有显卡的机器，本项目不假装能做。

## 快速开始

```bash
pip install -e .                         # 也可以 pip install -r requirements.txt
python3 tts.py fetch --voice zh_CN-huayan-medium
python3 tts.py build --in examples/心经.txt --out out/
python3 tts.py check --audio out/心经.mp3 --srt out/心经.srt
```

产物在 `out/`：`心经.mp3`、`心经.m4b`（两章）、`心经.srt`、`心经.vtt`、`manifest.json`。

用别的引擎（平台 TTS、云服务、你自己的模型）念的逐句音频，也能套上同一套时间轴：

```bash
python3 tts.py assemble --in 书.md --wavs 逐句/ --out out/ --trim
python3 tts.py assemble --in 书.md --wavs 逐段/ --out out/   # 一段一文件：用实测停顿切句
```

`assemble` 的两种映射自动识别：音频数 = 句数时直接对齐；音频数 = 段数时，用 `silencedetect` 找到段内停顿，把一段切回几行字幕，切不够的按字数比例补，并在 `manifest.json` 标 `sentence_timing: measured / part measured`——不假装精确。

常用参数：

```bash
--speed 0.95         # 语速倍数，越小越慢越稳
--jobs 2             # 并行进程数（CPU 核数一半左右最稳）
--max-len 60         # 单句最大字数，过长按逗号断
--formats mp3,srt    # 只出需要的格式
--cache DIR          # 缓存目录，跨书复用同一句
--title "书名" --artist "润泽说"
--clean              # 朗读前去掉（阴）（阳）（太极）等框架标注与图注
--exclude 编辑札记    # 按正则跳过某些章
--bitrate 96k        # 码率（整本用 96k 省一半体积）

python3 tts.py split --audio out/书.mp3 --manifest out/manifest.json --srt out/书.srt --out out/分章
```

## 实测

| 文本 | 字数 | 音频 | 合成用时 | 倍速 |
|---|---|---|---|---|
| 《心经》全篇 | 260 | 57.7 s | 6.9 s | 8.4× |
| 长文压测（12 段） | 4,052 | 11 min 36 s | 45 s | 15.4× |
| 《心经通释》全书 · Piper | 68,521 | 3 h 22 m | ≈28 min（2 进程） | 36 章、2,879 条字幕 |
| **《心经通释》全书 · Edge 神经音色** | 63,894 | **4 h 29 m** | ≈20 min（3 线程） | 35 章、2,815 条逐句字幕 |
| 分释篇十（平台音色逐段） | 2,077 | 10 min 00 s | 拼装 22.6 s（12 次调用不在此列） | 107 条字幕，刀口全为实测停顿 |
| 分释篇十（加 `--clean`） | 2,067 | 9 min 52 s | 同左，仅重录 2 段 | 框架标注不再读出 |

机器：2 vCPU（Xeon 8457C）、3 GB 内存、无 GPU、onnxruntime CPU。

## 已知边界

- Piper 的音色是「稳、清楚、没有表演」，不是真人朗读；想更有情绪，需要换引擎或上真人。
- 中文音色目前只有 `zh_CN-huayan-medium`（女声）可直接用；多音字与古文断句按规则切，个别处会读错，靠 `--max-len` 与手动改句缓解。
- 章节识别依赖 Markdown 标题或「第 N 章／篇／回」；无标题的长文按整篇一章处理。
- 每句单独合成，句间会有 0.16 s 停顿；句数特别多时听感偏「一句一顿」，可调 `SILENCE_GAP`。

## 许可

MIT。示例音频为《心经》公有领域文本，由本项目管线生成。

## 版本

| 版本 | 要点 | 状态 |
|---|---|---|
| v1.0.0 | 句级字幕、缓存、质检、多格式输入 | 保留（tag + Release） |
| v1.1.0 | `assemble`：外部 TTS 逐句音频上字幕 | 保留（tag + Release） |
| v1.2.0 | 逐段音频按实测停顿切句；修 RIFF/AIGC 时长误读 | 保留（tag + Release） |
| v1.3.0 | `--clean` 朗读清洗、`pyproject.toml`、`--version` | 保留（tag + Release） |
| v1.4.0 | 章节层级、`--exclude`、`split` 拆章、句子级容错、缓存自检；整本 36 章跑通 | 当前 |

旧版本一律保留，不覆写历史提交。

## 两个引擎

| 引擎 | 命令 | 特点 |
| --- | --- | --- |
| Piper（本地离线） | `python3 tts.py build …` | 无需联网、无显卡也能跑；音色稳但无表演 |
| Edge 神经音色（在线） | `python3 tts.py edge …` | 音质接近真人；逐句真实时长；`--rate=-8%` 放慢语速 |

```bash
pip install "edge-tts>=6.1"
python3 tts.py edge --in 书.md --out out --voice xiaoxiao --rate=-8% --jobs 3 --clean
```

可选音色：`xiaoxiao`（女声温润，默认）、`yunxi`（男声）、`yunjian`（男声浑厚）、`xiaoyi`（女声年轻）。

## 整本实测

《心经通释》全书 68,521 字，一次跑完：36 章、2,879 句、3 小时 22 分 34 秒音频、2,879 条字幕；合成约 28 分钟（2 进程、2 核 3 GB、无显卡），质检覆盖率 100%、峰值 -0.3 dB、长静音 0 处。
