# Architecture

Everything here describes the code as committed. File references are
relative to `src/bookocr/`.

## 1. The contract

`ocr convert book.pdf [--output DIR]` produces `DIR/book.txt` and
`DIR/book.md`, and nothing else user-facing. The tool must work on any PDF
at any path. It knows nothing about the novels, knowledge bases, or any
other project. Default output directory is `./<pdf-stem>/` in the current
working directory (`cli/main.py: _default_output_dir`).

Accuracy and page traceability outrank speed. The tool must never silently
rewrite text, so there is deliberately **no automatic text correction**;
uncertainty is recorded internally and pages are flagged instead.

## 2. Pipeline workflow

`pipeline.py: BookPipeline.convert` runs one book in two sweeps.

```
PDF
 |  register book + pages in <out>/.ocr_internal/state.db  (all pages PENDING)
 v
SWEEP 1: for every page not yet COMPLETED/BLANK/REVIEW_REQUIRED
   rasterize (PyMuPDF, 300 dpi PNG, temp dir)
   -> analyze + conditionally preprocess (blank? skew? noise? contrast?)
   -> PaddleOCR: detect lines + recognise text (CPU)         [Pass 1]
   -> Surya layout: typed regions + block reading order (GPU) [layout]
   -> assign each OCR line to the layout block containing it,
      order lines right-to-left within rows                   [reading order]
   -> quality score 0-100 -> tier HIGH/MEDIUM/LOW/CRITICAL
   -> append record to pages.jsonl, update state.db
   pages scoring LOW/CRITICAL are remembered for sweep 2
 |
SWEEP 2 (only if ocr.escalation.enabled, default OFF):
   QARI-OCR re-reads the remembered pages                     [Pass 2]
   accept if the new score is MEDIUM/HIGH, else REVIEW_REQUIRED
 |
FINALIZE
   read pages.jsonl (latest record per page wins)
   detect recurring watermark lines across the whole book
   write book.txt, book.md, manifest.json, qc_report.json
```

Blank pages (almost no ink) are recorded as `BLANK` and skipped by OCR.
A page whose rasterization or OCR raises is marked `FAILED` with the error
and retry count; the book continues. `FAILED` pages are retried on the next
run.

### Page states (`core/types.py: PageStatus`)

`PENDING, PROCESSING, COMPLETED, LOW_CONFIDENCE, REPROCESSING, FAILED,
REVIEW_REQUIRED, BLANK`. `StateStore.pending_pages` excludes only
`COMPLETED`, `BLANK`, `REVIEW_REQUIRED`, so a page left in `PROCESSING` by a
crash, or `FAILED`, or `LOW_CONFIDENCE`, is picked up again on resume.

## 3. Components

| Component | File | Notes |
|---|---|---|
| Rasterizer | `rasterize/pymupdf_rasterizer.py` | Per-page render at `rasterize.dpi` (300). A corrupt page raises `CorruptedPageError`, caught per page. |
| Preprocessor | `preprocess/adaptive.py` | `analyze()` measures ink ratio, contrast, skew, noise; `apply()` only does what is needed (see section 4). |
| Primary OCR | `engines/paddle_engine.py` | PaddleOCR 3.7, `PP-OCRv5_server_det` + `arabic_PP-OCRv5_mobile_rec`, CPU. Returns one region per detected line with bbox and score. |
| Layout | `layout/surya_layout.py` | Surya 2 via a local llama.cpp server; maps its labels to ours. |
| Reading order | `layout/reading_order.py` | Row clustering + right-to-left ordering. No model needed. |
| Escalation OCR | `engines/qari_engine.py` | QARI v0.3 (Qwen2-VL-2B fine-tune). Optional. |
| Quality scorer | `quality/scorer.py` | Heuristic ensemble (section 6). |
| Watermark filter | `postprocess/watermark.py` | Book-level recurring-line detector (section 7). |
| Output writer | `output/writer.py` | Append-only `pages.jsonl`; derives txt/md/qc at finalize. |
| State store | `core/state.py` | SQLite, one database per output directory. |
| Metrics | `eval/metrics.py` | CER/WER (strict and loose), punctuation recall. |
| Interfaces | `core/interfaces.py` | Abstract base classes for every replaceable stage. Partly unused, see section 10. |

## 4. Preprocessing

Every step is conditional; nothing runs blindly.

- `analyze`: `ink_ratio` (share of pixels < 200), `contrast_std`, `skew_deg`
  (`minAreaRect` on thresholded ink, clamped to +-15 deg), `noise_score`
  (Laplacian variance / 10000), `blank` (ink below
  `triage.blank_page_ink_ratio_threshold`).
- `apply`: deskew if |skew| >= `preprocess.deskew.min_angle_deg`; denoise
  (`fastNlMeansDenoising`) if noise > threshold; adaptive-threshold
  binarize if contrast_std < `binarize.low_contrast_threshold`. If nothing
  applies, the original raster is passed through untouched.
- In practice this library's pages are clean digital-looking rasters, so
  preprocessing almost never changes anything.

Declared in config but **not implemented**: `preprocess.upscale`,
`preprocess.binarize.method`, `triage.orientation_check` (needs Tesseract,
which is not installed), and `rasterize.image_format` (always PNG).

## 5. Reading order (the part that took real debugging)

Two stages, in `layout/`.

1. **Layout blocks (Surya).** Each Surya block carries a `position`
   (0-indexed reading order) and a label. `assign_lines_to_layout` puts each
   PaddleOCR line into the block whose box contains the line's centre,
   copies the block's kind and order, and orders blocks by `position`. Lines
   inside no block become `body` and sort after all typed content, so text is
   never dropped, only demoted. A short digit-only line inside a header or
   footer is reclassified as `page_number`.
2. **Row clustering, right to left (`reading_order.py`).** Within one block,
   lines are grouped into visual rows by vertical overlap, rows are ordered
   top to bottom, and inside a row lines are ordered by **descending x**
   (Arabic reads right to left).

Why stage 2 exists: PaddleOCR's detector sometimes splits one justified
Arabic line into two or three boxes with slightly different baselines. An
ascending-y sort scrambles them. This was first mistaken for a layout
problem; the real cause was found by inspecting raw box coordinates (see
`PROBLEMS_AND_SOLUTIONS.md` #6). Stage 2 does not depend on Surya, so it is
also applied when `layout.engine` is `none` or a layout call fails.

Layout label mapping (`surya_layout.py: _LABEL_MAP`): `Text/ListGroup/...`
-> `body`; `SectionHeader/Title` -> `heading`; `PageHeader` -> `header`;
`PageFooter` -> `footer`; `Caption` -> `caption`; `Footnote` -> `footnote`;
`Table` -> `table`; `Picture/Figure/Diagram` -> `illustration`. Unknown
labels map to `body` rather than being dropped.

Kinds excluded from the public output (`writer.py: _EXCLUDED_REGION_KINDS`):
`header`, `footer`, `page_number`, `watermark`. Footnotes, captions, tables
and headings stay in.

## 6. Quality scoring

`quality/scorer.py: HeuristicQualityEvaluator` combines five signals into a
0-100 score with configured weights (engine confidence 0.5, Arabic-character
ratio 0.2, function-word hit rate 0.15, length 0.1, garbage-symbol ratio
0.05). Tiers: HIGH >= 80, MEDIUM >= 60, LOW >= 30, else CRITICAL.

Warnings recorded: `low_arabic_content`, `high_garbage_symbol_density`,
`near_empty_page`, `repeated_line_detected`, `degenerate_repetition`.
The last two are a **hard override to CRITICAL**: a page that repeats a line
(3+ times) or a 4-word cluster (6+ times) is never acceptable regardless of
the other signals. This was added after a real QARI run looped a cluster of
real Arabic names dozens of times, which the other signals scored as fine.

**Limits, stated plainly.** The score is a routing heuristic, not accuracy.
It cannot see dropped punctuation, dropped digits/Latin words, reading-order
errors, or a page that is fluent but wrong. On the synthetic benchmark the
engine reported confidence in the 90s while character error was 4.3% and
punctuation recall 50%. `dictionary_hit_rate` is a function-word list, not a
morphological analyser.

## 7. Watermark and non-content filtering

Two independent layers.

1. Surya classifies the bottom band as `footer`/`page_number`, which the
   writer excludes. This works on a single page.
2. `postprocess/watermark.py: detect_watermark_lines` runs once per book.
   A line is a watermark only if **both** hold: it sits in the bottom
   `watermark_filter.bottom_band_fraction` (15%) of the page **and** it
   recurs, identically after normalisation, on at least
   `max(min_page_occurrences=3, min_page_fraction=0.3 x pages)` pages.
   Position alone or repetition alone is not enough, so a repeated line of
   real dialogue is safe. Matching lines are removed from `book.txt/md` only;
   the raw text stays in `pages.jsonl`.

Known gap: pages read by QARI carry the whole page, footer included, and
OCR noise makes the footer text differ per page, so layer 2 does not catch
it there (see FUTURE_WORK).

## 8. State, resumability and outputs on disk

```
<output>/
  book.txt                  public
  book.md                   public
  .ocr_internal/
    pages.jsonl             append-only raw record per page (source of truth for text)
    state.db                SQLite: per-page status, tier, confidence, engine, retries, error
    manifest.json           book id/title, source path, page count, pipeline version, config hash, engine versions
    qc_report.json          tier distribution, mean/min confidence, flagged pages, filtered watermark lines
```

- **Page is the unit of work.** A record is appended the moment a page is
  done; a crash loses at most the page in flight. Verified by killing a real
  536-page book mid-run and resuming from the right page with no duplicate
  records.
- **pages.jsonl is append-only**, so a page can appear twice (Pass 1 then
  Pass 2). `output/writer.py: _read_pages` keeps the last record per page.
  book.txt/md are always regenerable from it without re-running OCR.
- **SQLite, one file per output directory**, not one global database, so the
  tool has no shared state and works from any location.
- **Reproducibility.** `manifest.json` stores the config hash, pipeline
  version and engine versions. Determinism is as good as the engines allow.
- Rasterized page images live only in a temp directory for the run.

### Record schema (one JSON object per line of pages.jsonl)

`page, text, confidence, tier, status, engine_used, processing_pass,
page_width, page_height, regions[{kind,bbox,text,confidence}], warnings[],
engine_versions{}, processed_at, duration_s`.

## 9. Output formats

- **book.txt**: for each page, the marker line `===== PAGE N =====`, a
  blank line, the page's body lines joined with `\n`, blank lines between
  pages. Marker text is `output.txt_page_marker`.
- **book.md**: `# <pdf stem>` then per page `## Page N`, then each region as
  its own paragraph; regions of kind `heading` are rendered `### text`.
  Marker text is `output.md_page_heading`.

**Known limitation:** lines are emitted as recognised. There is no
paragraph reconstruction, so a novel paragraph appears as several lines
(txt) or several one-line paragraphs (md). See FUTURE_WORK.

## 10. Interfaces and what is actually wired

`core/interfaces.py` defines the replaceable-component contract:
`DocumentSource, Rasterizer, Preprocessor, LayoutDetector, OCREngine,
QualityEvaluator, EscalationPolicy, OutputWriter, Validator, JobManager`.
Used today: `DocumentSource, Rasterizer, Preprocessor, LayoutDetector,
OCREngine, QualityEvaluator, OutputWriter`. **Defined but not used:**
`EscalationPolicy` (the accept/escalate decision is inlined in
`pipeline.py`), `Validator` (no whole-book validation pass), `JobManager`
(no queue; one book per invocation).

## 11. Configuration

`config/default.yaml`, validated by pydantic (`config.py`). Override with
`--config other.yaml` (deep-merged) or `--set dotted.key=value` before the
subcommand, e.g. `ocr --set ocr.escalation.enabled=true convert book.pdf`.
`--set` values are coerced to int/float/bool where they look like one (a
real bug otherwise). Declared but unused keys: `preprocess.upscale`,
`preprocess.binarize.method`, `triage.orientation_check`,
`rasterize.image_format`, `concurrency.*`, `ocr.cloud_fallback.*`.

## 12. CLI (`cli/main.py`)

| Command | Purpose |
|---|---|
| `ocr convert PDF [--output DIR] [--force]` | The product. Resumes automatically; `--force` reprocesses every page. |
| `ocr status DIR` | Per-status page counts and failed pages for a converted book. |
| `ocr inspect DIR [--page N]` | The qc report, or one page's raw record. Debug only. |
| `ocr benchmark DIR [--limit N --pages-per-book N]` | Samples PDFs under a directory and prints throughput/quality. Console only. |

Global options before the subcommand: `--config PATH`, `--set key=value`.
The entry point sets `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
before anything imports torch.

## 12b. Batch use

There is no directory mode for `convert`; loop in the shell. Each book gets
its own output directory and its own state, so books are independent.
Every `ocr convert` starts its own Surya server (about 10-13 s startup),
paid once per book.

## 13. Key decisions and the reasons behind them

| Decision | Reason |
|---|---|
| Standalone tool, two outputs only | Owner's explicit scope: OCR is infrastructure, analysis lives elsewhere. |
| Page as unit of work, append-only records | Crash safety and resume without redoing pages; audit trail of Pass 1 vs Pass 2. |
| SQLite per output dir | Atomic per-page updates, zero shared state, works from any path. |
| pages.jsonl is truth; txt/md derived | Output format or filter changes never require re-OCR. |
| Layout before ordering | Reading order is a correctness property, not polish. |
| Row clustering independent of Surya | The defect is in PaddleOCR's detector, not the layout model; the fix must survive Surya being off. |
| Two sweeps, not per-page escalation | The escalation set is unknown until the book is read; it also keeps GPU-touching stages from overlapping. |
| Never silently downgrade | A Pass 2 result is accepted only if it scores MEDIUM/HIGH; otherwise `REVIEW_REQUIRED`, not a guess. |
| Do not auto-correct text | Owner requirement: no invented or "fixed" content. |
| Conservative watermark rule (position AND repetition) | Must never delete real dialogue to be safe. |
| Local-first, cloud fallback optional and off | Cost, privacy, reproducibility; the cloud tier was never built. |
| paddlepaddle pinned to 3.1.0 | 3.3.1 crashes on CPU (PROBLEMS #1). |
| Surya through a bundled llama.cpp build | Docker's NVIDIA runtime was not configured and system-wide installs were not wanted (PROBLEMS #7). |
| Escalation OFF by default | HTML output unconverted and CPU too slow (ENGINES.md). |
