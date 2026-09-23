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
```

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
library (Phase 0/1/2, partial): rasterization, adaptive preprocessing,
PaddleOCR Pass 1, heuristic quality scoring, page-atomic internal records,
crash-safe resumability, a conservative recurring-watermark filter.

Not yet implemented: real layout/reading-order detection (Surya) — currently
region order is whatever PaddleOCR's box-sort produces, which is known to be
wrong on some pages (see `docs/phase0_findings.md`); Pass 2 escalation
(QARI-OCR) for low-confidence pages; Pass 3 cloud fallback (opt-in only,
disabled by default).
