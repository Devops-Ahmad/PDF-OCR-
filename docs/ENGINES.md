# OCR engines and models

What is integrated, how each behaves on this machine, and everything learned
about QARI. Versions are those installed on 2026-09-23.

Machine: Ubuntu, Ryzen 5 5600H (12 threads), ~30 GB RAM, NVIDIA RTX 3050 Ti
Laptop with 4096 MB VRAM of which **~3.68 GiB is actually usable**, driver
CUDA 13.2, Python 3.12 (via uv).

## Summary

| Engine | Role | Runs on | Status |
|---|---|---|---|
| PaddleOCR 3.7 (PP-OCRv5 det + Arabic mobile rec) | Primary recogniser (Pass 1) | CPU | Integrated, default. Weak on punctuation/digits/Latin. |
| Surya 2 (surya-ocr 0.22.1) via llama.cpp | Layout + block reading order | GPU (~3 GB) | Integrated, default. Works well. |
| QARI-OCR v0.3 (Qwen2-VL-2B, merged) | Pass 2 escalation | CPU (GPU path unverified) | Integrated but **off by default**; unfinished. |
| QARI-OCR v0.2.2.1 (LoRA adapter) | (earlier attempt) | | Abandoned: broken adapter. |
| Surya 2 full-page OCR | (tested as recogniser) | GPU | Unusable as configured. |

## 1. PaddleOCR (primary, Pass 1)

Files: `engines/paddle_engine.py`. Packages: `paddleocr==3.7.x`,
`paddlepaddle==3.1.0` (**pinned**).

- Models: detector `PP-OCRv5_server_det`, recogniser
  `arabic_PP-OCRv5_mobile_rec` (small, ~2M parameters, 747-symbol
  dictionary of which 242 are Arabic; the dictionary does contain
  `. : ! ( ) - - « » ، ؟`). Doc-orientation, unwarping and text-line
  orientation are switched off.
- Language code is `ar`, not `arabic`. When explicit model names are given,
  `lang` is ignored, so the engine passes one or the other.
- **CPU only.** Paddle publishes no `paddlepaddle-gpu` wheel usable here
  (only 2.6.1 for CUDA 11.2-12.0, which predates the PP-OCRv5 format).
- **paddlepaddle 3.3.1 crashes on CPU** (`ConvertPirAttribute2RuntimeAttribute
  not support [pir::ArrayAttribute<pir::DoubleAttribute>]`, an oneDNN/PIR
  bug) on every PP-OCRv5 detector tried. Disabling oneDNN avoids it but is
  ~9x slower (51.7 s vs 5.8 s per page). 3.1.0 has no crash at full speed.
  Re-test before ever bumping the pin.
- Output per page: one region per detected line (`rec_texts`, `rec_scores`,
  `rec_boxes`); page confidence is the mean line score x 100.
- Speed: ~5.6 s/page on synthetic pages, 6-18 s/page on real ones (two books
  ran 2-3x slower and were never explained).
- **Weakness (measured):** drops period, colon, exclamation mark,
  parentheses, and the leading dialogue dash on most fonts; merges adjacent
  words in places ("...اللهطلب"); drops Western digits and Latin words in
  mixed lines; sometimes reads a shadda as a letter; occasionally emits a
  phantom empty box. Its dictionary contains those symbols, so this is a
  recognition-sensitivity limit of the small mobile model, not a missing
  vocabulary. Detector tuning does not fix it (BENCHMARKS.md #4).
- Model list checked: `PP-OCRv6_*_rec` has **zero Arabic characters** in its
  dictionary, so it cannot be used. No server-class Arabic recogniser exists
  in this PaddleOCR release.

## 2. Surya (layout and block reading order)

Files: `layout/surya_layout.py`. Package: `surya-ocr>=0.22` (0.22.1).

- **Surya >= 0.20 is not an in-process torch model.** It runs a vision-
  language model behind a local inference server, via either a Docker+GPU
  `vllm` backend or a `llama-server` (llama.cpp) process. Docker's NVIDIA
  runtime was not configured here, so the project downloads a self-contained
  llama.cpp build (`b11124`, CUDA 12.8 variant, plus the `cudart` bundle) into
  `.tools/llamacpp/` with `scripts/setup_layout_backend.sh`. `surya_layout.py:
  _configure_llamacpp_backend` finds it and sets `SURYA_INFERENCE_BACKEND`,
  `LLAMA_CPP_BINARY` and `LD_LIBRARY_PATH` with `setdefault`, so a variable
  the user already set wins.
- Model: GGUF `datalab-to/surya-ocr-2-gguf` (`surya-2.gguf` ~1.27 GB +
  `surya-2-mmproj.gguf` ~0.2 GB), downloaded on first use.
- **VRAM:** the server holds about **3 GB** while running. Its Python-side
  `stop()` is a no-op (the subprocess is only killed by an `atexit` handler
  when the whole Python process exits), so the memory cannot be released
  mid-process. Anything else that needs the GPU must run in a different
  process, after Surya has exited.
- Speed: ~10-13 s startup once per `ocr convert` run, then ~2-3 s/page.
- Quality seen: correct block typing and order on real pages, including
  separating the footer band (page number and stamps) from body text.
- The model returns a `position` per block (0-indexed reading order) and a
  label; no meaningful per-block confidence (a uniform value is returned).
- Labels: `Caption, Footnote, Equation, ListGroup, PageHeader, PageFooter,
  Picture, SectionHeader, Table, Text, Figure, Code, Form,
  TableOfContents, ChemicalBlock, Diagram, Bibliography, BlankPage`.
- torch installed as a dependency: `torch 2.14.0+cu130`, CUDA available.
  The first install pulled ~7 GB of CUDA libraries and took ~50 minutes on
  this connection.

### Surya-2 full-page OCR (tested, rejected as configured)

`surya.recognition.RecognitionPredictor(full_page=True)` exists and is
described upstream as the more accurate mode. On 2026-09-23 the first
synthetic page had not returned after ~14 minutes with the GPU at 99%
utilisation, so the run was killed. It was not evaluated for accuracy. The
probe is preserved in `scripts/probes/surya_ocr_probe.py`. Untried ideas: cap
`max_tokens`, use block mode per layout block, or check for a generation
loop.

## 3. QARI-OCR

QARI is a family of Arabic OCR fine-tunes of Qwen2-VL from the NAMAA-Space
group (paper: arXiv 2506.02295, reporting on a diacritics-heavy set of 200
scanned pages WER 0.160 / CER 0.061 for v0.2, ahead of the Mistral OCR API).
It reads a whole page image and returns text; it has no boxes and no native
confidence.

### 3.1 v0.2.2.1 (abandoned)

`NAMAA-Space/Qari-OCR-0.2.2.1-VL-2B-Instruct` is the version the paper
benchmarks, but the repository is a **PEFT LoRA adapter only**
(`adapter_config.json` + `adapter_model.safetensors`, no
`model.safetensors`), so loading it directly fails with "does not appear to
have a file named pytorch_model.bin or model.safetensors". It must sit on a
base model. Its config names
`unsloth/qwen2-vl-2b-instruct-unsloth-bnb-4bit` (correct casing:
`unsloth/Qwen2-VL-2B-Instruct-unsloth-bnb-4bit`).

Findings, in order:

1. `device_map="auto"` on the 4-bit base fails: "Some modules are dispatched
   on the CPU or the disk". `device_map={"": 0}` loads it (~3 GB reported).
2. CUDA out-of-memory followed at a constant "tried to allocate 1.36 GiB",
   unchanged by lowering `max_pixels`, `max_new_tokens`, or enabling
   `expandable_segments`. **This was misdiagnosed for a long time as QARI
   being too big.** The error text also said "Process N has 2.99 GiB memory
   in use" while our process held 82 MB: the 3 GB belonged to the Surya
   llama-server. QARI was never the one occupying the VRAM.
3. On CPU with the vanilla `Qwen/Qwen2-VL-2B-Instruct` base, output was
   broken in three different ways depending on the page: **whole-string
   reversed Arabic** (reversing the string recovered mostly correct text),
   **degenerate repetition loops** (the same cluster of words dozens of
   times), and **non-text hallucination** (strings that look like bounding
   box coordinates).
4. Reversing the output was added as a workaround, and
   `repetition_penalty=1.3`/`no_repeat_ngram_size=4` was added against the
   loops. The aggressive values measurably increased letter-level scrambling
   on the other pages (they force the model off its top token). They were
   later softened to 1.1 / 6.
5. Root cause found: loading the adapter logged "Found missing adapter
   keys" for **648 LoRA weights, all in the vision tower**
   (`visual.blocks.*`). PEFT silently skips a LoRA weight whose target
   module does not exist, so the vision-side adaptation never applied. The
   same warning appeared with **both** the vanilla Qwen base and the
   non-quantized `unsloth/Qwen2-VL-2B-Instruct` base, so picking a
   different base repository did not fix it. This looks like an
   incompletely published adapter, not something to patch around.

### 3.2 v0.3 (current)

`NAMAA-Space/Qari-OCR-v0.3-VL-2B-Instruct` ships a merged
`model.safetensors` (4.42 GB, standard `Qwen2VLForConditionalGeneration`),
so there is no adapter and no base-matching. This is what
`engines/qari_engine.py` loads.

Observed on one real page (CPU, float32):

- Correct reading direction with **no reversal hack** (the hack was removed).
- Punctuation preserved: parentheses, guillemets, colons, periods.
- A few letter-level errors remain (for example a letter dropped inside a
  word).
- Output is **HTML**, not plain text: `<h2>` for the paragraph, `<i>` and
  `<b>` for emphasis. The engine does **not** convert it yet, so enabling
  escalation would put raw tags into `book.txt`. A regex converter was
  written for the GPU probe (`scripts/probes/qari_gpu_probe.py:
  html_to_text`) but not moved into the engine.
- The mean-token-probability confidence came out at 100.0, which is not
  informative (the value saturates).
- Speed: the measured 1617 s for that page **includes the 4.4 GB download and
  model load**, so there is no clean CPU seconds-per-page for v0.3. The older
  v0.2 adapter ran 40-340 s per page on CPU.
- Not verified: accuracy on many pages, GPU speed, VRAM peak.

Engine settings now (`qari_engine.py`): prompt is the model card's OCR
prompt; `max_pixels = 1024*28*28`, `min_pixels = 256*28*28`;
`repetition_penalty=1.1`, `no_repeat_ngram_size=6`; `max_new_tokens` from
config (1024); CPU uses float32. **The `device: "cuda"` branch loads bf16
unquantized (4.4 GB) and would not fit in 3.68 GiB**; the intended GPU path
is 4-bit NF4 via bitsandbytes, written only in the unrun probe
(`scripts/probes/qari_gpu_probe.py`).

A v0.4.0 (Qwen3-VL-4B, trained on a books dataset, CER 0.122 / WER 0.256 on
its own card, described as tuned for printed Islamic texts and needing
300+ DPI) exists and was not tried; it is larger (4B).

### 3.3 Why escalation is off

(1) HTML unconverted; (2) CPU takes minutes per page; (3) GPU path
unverified with v0.3; (4) it reads the whole page including the footer and
stamp, and those noisy footer strings differ per page so the recurring-line
watermark filter does not catch them. See FUTURE_WORK.

## 4. Engines evaluated on paper and not used

From the initial research (KITAB-Bench, ACL 2025, and repository review):

| Engine | Outcome |
|---|---|
| Tesseract 5 | ~54% CER on the Arabic benchmark; not installed; only ever considered for orientation detection (unused). |
| EasyOCR | ~58% CER on the same benchmark. |
| PaddleOCR-VL / dots.ocr (1.7B) | Multilingual VLM parsers; no strong Arabic evidence; not tried. |
| AIN (MBZUAI 7B Arabic LMM) | Too large for 4 GB VRAM; weights availability unclear. |
| Kraken / eScriptorium | Built for historical manuscripts with per-font training; wrong tool for printed novels. |
| ABBYY / Azure / AWS Textract | Recurring cost and vendor lock-in; not needed for a local-first design. |
| Gemini Flash (cloud) | Best measured Arabic accuracy in KITAB-Bench (13% CER on a hard set) and cheap, kept as the optional Pass 3 in the design; **never implemented**. |
| GOT-OCR2, Marker, olmOCR | Not Arabic-specialised; not used. |

## 5. Model files on disk (and what is obsolete)

Hugging Face cache (`~/.cache/huggingface`, ~17 GB) and `~/.paddlex` (~170 MB).

In use: `datalab-to/surya-ocr-2-gguf`, `NAMAA-Space/Qari-OCR-v0.3-VL-2B-Instruct`,
PaddleX `PP-OCRv5_server_det` and `arabic_PP-OCRv5_mobile_rec`.

Safe to delete to recover ~13 GB (left in place, nothing was deleted):
`NAMAA-Space/Qari-OCR-0.2.2.1-VL-2B-Instruct`, `Qwen/Qwen2-VL-2B-Instruct`,
`unsloth/Qwen2-VL-2B-Instruct`, `unsloth/Qwen2-VL-2B-Instruct-unsloth-bnb-4bit`,
and the unused PaddleX `PP-OCRv6_medium_rec` / `PP-OCRv5_mobile_det`.
