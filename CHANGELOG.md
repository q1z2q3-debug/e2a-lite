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
