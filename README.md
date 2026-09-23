# mimi-ocr

Permanent OCR infrastructure for the Mimi literary-analysis project. Turns
scanned Arabic book PDFs in `../sourse/` into page-traceable, quality-scored
text in `../processed/`, ready for Claude's literary-analysis stage
(`../distnation/`). See `docs/phase0_findings.md` for why the pipeline is
configured the way it is, and the architecture proposal in the project chat
history for the full design rationale.

## Setup

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e .
```

## Usage

```bash
mimi-ocr process <path-to.pdf>            # process one book (auto-resumes if interrupted)
mimi-ocr process <directory>              # process every PDF under a directory
mimi-ocr status <path-to.pdf-or-book_id>  # per-status page counts + any failures
mimi-ocr inspect <book_id> [--page N]     # qc_report.json, or one page's full record
mimi-ocr benchmark <directory> [--limit N --pages-per-book N]  # Phase 0 sampling, no commitment to a full run
```

Global overrides (before the subcommand): `--config path.yaml` to layer a
config file over `config/default.yaml`, or `--set dotted.key=value` for a
one-off override, e.g. `mimi-ocr --set ocr.primary.device=cpu process book.pdf`.

## Status

Implemented and validated against the real library (Phase 0/1/2, partial):
rasterization, adaptive preprocessing, PaddleOCR Pass 1, heuristic quality
scoring, page-atomic JSONL output, SQLite-backed crash-safe resumability.

Not yet implemented: layout/reading-order detection (Surya), Pass 2
escalation (QARI-OCR), Pass 3 cloud fallback (Gemini, opt-in only). See
`docs/phase0_findings.md` "Open items" for why layout is the next priority,
not optional polish.
