# Phase 0 findings (2026-09-23)

Empirical results from benchmarking the real `sourse/` library on this
machine, gathered before committing to a full 101-book run. This document is
the record of *why* the pipeline is configured the way it is — update it
whenever a new finding changes a default.

## Library survey (all 101 PDFs)

| Metric | Value |
|---|---|
| Total books / pages | 101 / 39,274 |
| Total size | 1.5 GB |
| Page count range | 94–1,602 (median 368) |
| Books with a usable text layer | 12/101 (12%), and even those only cover cover/TOC pages, not body text |
| Books that are pure scanned images | 75/101 (74%) |
| Books with partial/junk text layer | 14/101 (14%) |

**Conclusion**: PDF-type classification must happen per-page, not per-book —
even the "best" books in this library need OCR for their body text. See
`mimi_ocr/pipeline.py` — there is no book-level "skip OCR" branch.

## Scan quality: not what was assumed

Every sample page inspected so far (from 4 different series, different file
producers: LuraDocument, PDFTron PDFNet, ilovepdf.com) is a clean,
born-digital-looking raster — even margins, no skew, no speckle noise, no
bleed-through. These are not photographed scans; they read as flattened
ebook renders (likely produced to block copy/paste for piracy distribution —
most carry a `t.me/<channel>` watermark and a "مكتبة" stamp in the page
footer).

**Implication**: the adaptive preprocessing stage (deskew/denoise/binarize)
is expected to be a no-op for most of this library, but per-page analysis is
still required rather than skipping preprocessing outright — a subset of the
101 books may still be true photographic scans, not yet sampled.

**Implication for layout**: the recurring watermark text is a real
contamination risk. Confirmed in the Phase 2 test run below: without a
layout stage, `t.me/ktabpdf` and `مكتبة` end up in the narrative body text of
every page of the sample book. Layout detection (Phase "layout", Surya) must
tag these as a `watermark` region, not just `footer`.

## paddlepaddle version pin (load-bearing)

`paddlepaddle==3.3.1` crashes on CPU inference for every PP-OCRv5 text
detection model tested (`PP-OCRv5_server_det`, `PP-OCRv5_mobile_det`), the
moment oneDNN is enabled (the default):

```
NotImplementedError: (Unimplemented) ConvertPirAttribute2RuntimeAttribute
not support [pir::ArrayAttribute<pir::DoubleAttribute>]
```

Disabling oneDNN avoids the crash but drops throughput ~9x (51.7s/page vs
5.8s/page on the same page). `paddlepaddle==3.1.0` does not hit this bug at
all, with oneDNN enabled, at full speed. `pyproject.toml` pins `==3.1.0` for
this reason — do not bump it without re-testing on a real page first.

No `paddlepaddle-gpu` wheel exists for this machine's CUDA 13.2 driver at a
version that supports the PP-OCRv5 model format (Paddle's official index
only publishes `paddlepaddle_gpu` up to 2.6.1 for cu112–cu120, which predates
PP-OCRv5). **PaddleOCR runs on CPU on this machine, full stop, until that
changes upstream.** GPU acceleration for the pipeline instead comes from the
torch-based stages planned for later phases (Surya, QARI-OCR), which do have
working CUDA wheels for this driver (`torch==2.6.0+cu124`, confirmed
installable).

## Phase 1/2 correctness tests (this is not just a benchmark — it ran for real)

Ran the actual pipeline (rasterize → adaptive preprocess → PaddleOCR Pass 1
→ quality score → JSONL write → SQLite state) against a real book
(`بساتين عربستان 1 .pdf`, 536 pages):

- Killed the process mid-run (SIGTERM) after page 16. Re-ran with no flags:
  correctly resumed from page 17, not page 1. Verified via `book.jsonl`:
  20 lines after both runs, page numbers 1–20, **zero duplicates**.
- Full run on a 4-page extract confirmed `finalize_book()` end to end:
  `manifest.json`, `book.md` (page-marker comments), `qc_report.json` all
  produced correctly, book marked `COMPLETED` in state.db.
- **Known gap, not yet caught by the quality scorer**: page 3 of that extract
  came out with scrambled reading order (short indented dialogue lines sorted
  out of sequence by PaddleOCR's naive top-to-bottom box sort). The current
  heuristic scorer (Arabic ratio, dictionary hits, garbage ratio) scored this
  page HIGH anyway, because none of those signals detect *sequence*
  corruption, only *content* corruption. This is exactly the failure mode the
  planned layout/reading-order stage (Surya) exists to fix — it is not
  optional polish, it's covering a real, observed defect.

## Throughput benchmark (8 books, ~4 pages each, Pass 1 only)

| Book | Pages | Avg s/page | Mean confidence | Tier |
|---|---|---|---|---|
| - أنت لي - | 1,602 | 18.2 | 93.0 | HIGH |
| الاسود يليق بك | 332 | 8.5 | 93.4 | HIGH |
| انتهاء 3 (ستيفاني جاربر) | 423 | 6.8 | 95.9 | HIGH |
| أسطورية 2 (ستيفاني جاربر) | 416 | 6.4 | 95.4 | HIGH |
| كرافال 1 (ستيفاني جاربر) | 424 | 6.3 | 94.5 | HIGH |
| قصة اللصوص | 424 | 17.2 | 92.6 | HIGH |
| آزر | 306 | 9.4 | 93.9 | HIGH |
| أحببتك أكثر | 328 | 10.0 | 92.0 | HIGH |

31 pages sampled, 100% HIGH tier at Pass 1 alone — better than the
architecture proposal's original assumption (10–20% needing escalation).
**Caveat**: "HIGH tier" here means the heuristic scorer is satisfied, not
that the text is perfect — see the reading-order gap above, which the scorer
cannot see. Two books ran ~2–3x slower per page (18.2s, 17.2s) than the rest;
not yet root-caused (likely larger rendered page dimensions or denser text
increasing detection-box count) — flag for the full Phase 0 benchmark once
more books are sampled.

**Revised throughput estimate for the full library**: taking a weighted
average across this spread (~9–10s/page realistic, not the earlier
optimistic ~5.8s/page single-sample guess) gives roughly **100–110 hours of
CPU-bound Pass 1** for all 39,274 pages — about 4–5 days of unattended
background processing, not the ~11 hours originally estimated before this
benchmark ran. This is the kind of correction Phase 0 is for.

## Open items for the next phase

1. Root-cause the two slow-outlier books before committing to a full-library
   time estimate.
2. Wire up Surya for layout + reading order — required to fix the watermark
   contamination and reading-order scrambling found above, not just a nice-to-have.
3. Sample more books to find out whether any of the 101 are true photographic
   scans (none seen yet in 12 samples across 5 series).
4. QARI-OCR escalation tier (Pass 2) is stubbed in config
   (`ocr.escalation.enabled: false`) but not implemented yet.
