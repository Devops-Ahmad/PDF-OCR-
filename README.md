# bookocr

A standalone, portable OCR tool. Converts a scanned book PDF into a
high-fidelity, page-preserved `book.txt` and `book.md`. That is the entire
product: no knowledge base, no analysis, no embeddings, no project-specific
logic. It has no dependency on any other project and works against any PDF
path you give it.

See `docs/phase0_findings.md` for the empirical findings (real bugs found,
real throughput numbers) that shaped the current defaults.

## Setup

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e .
./scripts/setup_layout_backend.sh   # one-time: local llama.cpp CUDA build for layout/reading-order
```

The last step downloads a self-contained llama.cpp build into `.tools/`
(gitignored, nothing system-wide) that Surya's layout model runs on. The
first real `ocr convert` afterwards also downloads its GGUF model weights
(~1.5GB, cached by `huggingface_hub`, one-time). See
`docs/phase0_findings.md` for why this exists instead of a plain pip install.

## Usage

```bash
ocr convert "/path/to/book.pdf"                          # writes ./book/book.txt + book.md
ocr convert "/path/to/book.pdf" --output "/path/to/out"   # explicit output directory
ocr status "/path/to/out"                                 # per-status page counts + any failures
ocr inspect "/path/to/out" [--page N]                     # internal qc report, or one page's raw OCR record
ocr benchmark "/path/to/some/directory" [--limit N --pages-per-book N]  # dev/QC tool, console output only
```

Global overrides (before the subcommand): `--config path.yaml` to layer a
config file over `config/default.yaml`, or `--set dotted.key=value` for a
one-off override, e.g. `ocr --set ocr.primary.device=cpu convert book.pdf`.

## Output contract

Exactly two user-facing files, in the output directory:

- `book.txt` — plain text, page-preserved (`===== PAGE N =====` markers).
- `book.md` — structured Markdown, page-preserved (`## Page N` headings).

Everything else (`<output>/.ocr_internal/`: raw per-page OCR records, quality
report, resumable state) exists only for reliability/debugging and is never
part of the product surface.

## Status

Implemented and validated against a real 100+ book, 39k+ page Arabic scan
library: rasterization, adaptive preprocessing, PaddleOCR Pass 1, Surya
layout detection + reading-order correction (on by default), heuristic
quality scoring, page-atomic internal records, crash-safe resumability, a
conservative recurring-watermark filter.

Reading order is treated as a correctness requirement, not a nice-to-have.
A real defect was found and fixed: PaddleOCR's line detector sometimes
splits one justified Arabic line into several boxes with slightly different
baselines; naive top-to-bottom sorting scrambled these. The fix
(`layout/surya_layout.py`) clusters lines into visual rows by y-overlap,
then orders each row right-to-left by x-position -- verified against the
exact real page that exposed the bug (`docs/phase0_findings.md`).

Not yet implemented: Pass 2 escalation (QARI-OCR) for low-confidence pages;
Pass 3 cloud fallback (opt-in only, disabled by default).
