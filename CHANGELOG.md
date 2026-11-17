# 变更记录

## v1.0.0 · 2026-11-17

首发。

- `tts.py build`：txt／md／html／docx／epub／pdf → mp3、m4b（章节标记）、srt、vtt、manifest.json。
- 句级合成：字幕时间轴由真实音频长度累加，逐句计时，不靠字数估算。
- 句子级 `sha1(voice|speed|text)` 缓存：改一句只重算一句；跨书复用同句。
- 多进程合成（`--jobs`），单 worker 内 onnxruntime 单线程，避免 2 核争抢。
- `tts.py check`：时间轴单调、首尾吻合、重叠、零时长、长静音、响度、字幕覆盖率；不通过退 1。
- `tts.py voices` / `tts.py fetch`：音色列示与 HuggingFace 下载。
- 实测：2 vCPU / 3 GB / 无 GPU，15.4× 实时（4,052 字 → 11 分 36 秒音频，45 秒算完）。
- 站点 `docs/`：试听样章、特性表、与 ebook2audiobook 的差异、边界说明。

## v1.2.0 · 2026-11-18

- `assemble` 支持**逐段音频**：一段一文件时，用 `silencedetect` 实测段内停顿，把每段切回句子，再套同一套时间轴；切不够就按字数比例补，并在 manifest 写 `sentence_timing: measured / part measured`，不假装精确。
- 新增 `--cue-mode {auto,sentence,paragraph}`：音频与文本的对应粒度；auto 优先按段匹配，避免「段数与句数恰好相同」时错位。
- 修时长探测：平台 TTS 的 RIFF 带 AIGC 附加块，Python `wave` 会读出一个荒谬帧数（实测把 10 秒音频读成 7 小时）。改用 ffprobe 取时长、采样率与声道。
- 实测：平台音色逐段念《心经》10 段 → 108.7 秒、18 条字幕、2 个章节，刀口全为实测停顿。
