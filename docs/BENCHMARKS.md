# Benchmarks, measurements and test results

All numbers were measured on 2026-09-23 on the machine described in
`ENGINES.md`. Where something was estimated or eyeballed rather than
measured, it says so. **The only ground-truth accuracy numbers in this
project are the synthetic-benchmark ones (section 3).**

## 1. Library survey (101 PDFs, done with pdfinfo / pdftotext)

| Metric | Value |
|---|---|
| Books / pages / size | 101 / 39,274 / 1.5 GB |
| Pages per book | 94 to 1,602 (median 368) |
| Pure image PDFs (near-zero text layer) | 75 books (74%) |
| Partial or junk text layer | 14 books |
| Text layer over the first pages | 12 books, but even these only cover cover/contents pages, not body text |

Conclusion: classification has to be per page; effectively every page needs
OCR. Producers seen include LuraDocument, PDFTron and ilovepdf. Every sampled
page (12+ pages across 5 series) is a clean, born-digital-looking raster
with no skew or speckle noise, and most carry a distributor watermark and a
library-name stamp in the footer. Whether any of the 101 are true
photographed scans is **unknown**: none was seen in the sample.

## 2. Real pages, primary pipeline only (no ground truth)

8 books, 31 pages sampled, PaddleOCR Pass 1, before layout was added.

| Measure | Result |
|---|---|
| Quality tier | 31/31 HIGH |
| Mean page score per book | 92.0 to 95.9 |
| Seconds per page | 6.3 to 18.2 (six books 6-10 s; two books 17-18 s, cause never found) |

**These are engine/heuristic scores, not accuracy.** A hand comparison of one
clean real page (by eye, 17 lines) suggested roughly 5-7% word errors with
dropped punctuation; that is an eyeball estimate on one page, not a
measurement. The synthetic benchmark below shows the score badly
overestimates quality.

Extrapolated full-library cost, Pass 1 on CPU at ~9-10 s/page: about
100-110 hours (4-5 days), plus layout at ~2-3 s/page. An early single-sample
guess of ~11 hours was wrong by ~10x and was corrected by this benchmark.

## 3. Synthetic ground-truth benchmark

Tool: `scripts/synth_benchmark.py`. It renders original Arabic text (written
for the benchmark) into page images in 5 fonts, wraps them into a PDF, runs
the **real** `ocr convert` pipeline (escalation off), and compares
`book.txt` per page with the rendered text. Pages mimic the real ones:
1254x2149 at 300 dpi, justified RTL paragraphs, dialogue lines with
`-` `:` `«»` `( )`, Arabic-Indic and Western digits, one embedded Latin word,
tashkeel on some words, centred page number, footer watermark in both
corners. **Limits:** fonts, kerning and ink differ from real books; this is a
controlled regression benchmark, not a substitute for real-page checks.

Metrics (`eval/metrics.py`): CER and WER on NFC-normalised, whitespace-
collapsed text; "strict" keeps diacritics, "loose" removes tashkeel/tatweel;
punctuation recall is the share of the reference's punctuation marks found.

Result, 10 pages (2 per font), full pipeline, 147 s total, primary engine:

| Font | CER strict | CER loose | WER strict | WER loose | Punctuation kept |
|---|---|---|---|---|---|
| Noto Naskh Arabic | 3.93% | 3.27% | 15.75% | 12.93% | 36.0% |
| Noto Kufi Arabic | 5.91% | 5.24% | 21.88% | 19.04% | 31.1% |
| Noto Sans Arabic | 5.73% | 5.45% | 21.48% | 19.98% | 45.7% |
| FreeSerif | 4.52% | 3.61% | 18.93% | 14.45% | 43.7% |
| DejaVu Sans | 1.52% | 1.44% | 6.18% | 5.62% | 94.3% |
| **All** | **4.32%** | **3.80%** | **16.84%** | **14.40%** | **50.2%** |

Watermark text leaked into the output on **no** page (layout footer
classification plus the recurring-line filter work).

Punctuation recall per mark, pages 1-8 (the four fonts that fail):
`.` 0/84, `:` 0/23, `-` (dialogue dash) 0/10, `!` 0/8, `(` 0/6, `)` 0/6,
`،` 72/80, `؟` 18/18, `«` 7/11, `»` 3/11.

Observed error types (inspection of a page line by line): parenthesis read
as the letter alef and the following period and space dropped; adjacent
words merged; Western digits ("1927") and the Latin word dropped from a
mixed line, splitting it into separate boxes; leading dialogue dash missing;
one shadda read as a nun; one phantom empty box. The score the engine
reported on these pages was 87-98.

Targets set with the owner and **not met**: CER <= 1%, WER <= 3%,
reading order 100% on the sample (this part holds on the sample),
punctuation preserved, no watermark leakage (holds), no silent bad pages.
Whether the quality scorer flags the bad pages is **not** met: it does not.

## 4. Detector / recogniser settings experiment

Tool: `scripts/engine_compare.py`, primary engine only, same 10 pages,
footer band dropped by position.

| Setting | CER | WER | WER loose | Punct | s/page |
|---|---|---|---|---|---|
| baseline | 4.34% | 16.80% | 14.42% | 50.2% | 5.6 |
| unclip ratio 2.0 | 5.59% | 20.05% | 16.37% | 48.2% | 5.4 |
| unclip ratio 2.5 | 13.14% | 33.03% | 30.52% | 39.9% | 5.3 |
| lower det thresholds (0.2 / 0.4) | 4.49% | 17.31% | 14.75% | 49.5% | 5.4 |
| unclip 2.0 + lower thresholds | 5.78% | 20.37% | 16.78% | 48.2% | 5.5 |
| larger input side limit (2400, max) | 4.34% | 16.80% | 14.42% | 50.2% | 6.6 |
| larger input + unclip 2.0 + lower thresholds | 5.78% | 20.37% | 16.78% | 48.2% | 6.1 |

Conclusion: nothing beat the baseline; wider boxes merge lines and hurt. The
baseline number here (4.34% / 16.80% / 50.2%) matches the full pipeline run
(4.32% / 16.84% / 50.2%), so this harness is a faithful proxy for the primary
engine. The line-by-line dump showed the detector finds the lines and the
recogniser is what drops the marks.

## 5. Engine probes

| Probe | Result |
|---|---|
| QARI v0.2.2.1 on 4 real pages (CPU) | Reversed text, repetition loops, hallucinated coordinates, 648 missing adapter keys. Abandoned. |
| QARI v0.3 on 1 real page (CPU) | Correct direction, punctuation kept, HTML output, a few letter errors. No accuracy number. |
| QARI v0.3 on GPU, 4-bit, synthetic set | Script written, **never run** (`scripts/probes/qari_gpu_probe.py`). |
| Surya-2 full-page OCR on the synthetic set | First page not finished after ~14 min at 99% GPU; killed (`scripts/probes/surya_ocr_probe.py`). |

## 6. Throughput and resources actually observed

| Stage | Observation |
|---|---|
| Rasterize + preprocess | Negligible next to OCR. |
| PaddleOCR (CPU, mkldnn, paddle 3.1.0) | 5.6 s/page synthetic; 6-18 s/page real. 51.7 s/page without oneDNN. |
| Surya layout | ~10-13 s startup per run, ~2-3 s/page. Server holds ~3 GB VRAM. |
| QARI v0.2 on CPU | 40-340 s per page; ~10-11 GB RAM. |
| QARI v0.3 on CPU | Not cleanly measured (first run included a 4.4 GB download). |
| Whole small book (4 pages) primary pipeline | ~25-45 s including startup. |

**Not measured:** CPU%/RAM/VRAM profiles per stage, storage per book,
failure rate over a large run, reprocessing rate on a realistic library.

## 7. Correctness checks on the real library

- **Resume after a hard kill:** a real 536-page book was killed with SIGTERM
  after page 16, restarted with no flags, and resumed at the interrupted
  page; `pages.jsonl` had 20 lines for 20 pages, no duplicates. (Run before
  the standalone-tool refactor; the state-store logic is unchanged and unit
  tested, only its location moved to a per-output-directory database.)
- **Reading order:** a page with scrambled fragments was fixed and matched
  the printed text exactly; the fix is locked in by a test using that page's
  real box coordinates.
- **Watermark/footer/page number:** removed from `book.txt` on the test
  extract (footer classification) and on the synthetic set.
- **Idempotence:** the full `ocr convert` output was byte-identical before and
  after extracting the reading-order module.

## 8. Unit tests

`python -m pytest tests/ -q` -> **26 passed in 0.81 s**.

| File | Tests | Covers |
|---|---|---|
| `test_state.py` | 2 | resume semantics (a page stuck in PROCESSING is re-queued), idempotent registration |
| `test_layout_assignment.py` | 6 | kind inheritance, side-by-side block order, real-bbox RTL row reassembly, unmatched line kept, page-number heuristic |
| `test_reading_order.py` | 2 | RTL ordering without a layout model, normal top-to-bottom paragraph |
| `test_watermark.py` | 4 | recurring bottom line flagged; repeated non-bottom line not; single bottom line not; empty book |
| `test_quality_scorer.py` | 3 | word-cluster repetition forces CRITICAL, repeated line forces CRITICAL, normal prose not flagged |
| `test_cli_config_overrides.py` | 3 | `--set` coerces ints, floats, bools; strings pass through |
| `test_metrics.py` | 6 | CER/WER definitions, whitespace, loose vs strict, punctuation recall |

**Not covered by any test:** the pipeline end to end (needs models), the
Paddle/Surya/QARI engines themselves, the writer's txt/md formatting, and
the rasterizer. The end-to-end checks in section 7 were manual.
