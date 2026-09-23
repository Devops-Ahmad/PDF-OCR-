# Future work / remaining work

Everything planned but not done, in priority order. The project is frozen;
this is the list to pick up when it is unfrozen. Each item says what is
known, what to do, and how to know it is done.

**The single most important framing:** the pipeline is complete and stable,
but the primary engine misses the accuracy target (CER 4.3% vs <= 1%;
punctuation 50%). Everything in section A exists to close that gap.

## A. Close the accuracy gap (highest priority)

**A1. Benchmark QARI v0.3 with numbers.**
The GPU probe is written and never run: `scripts/probes/qari_gpu_probe.py`
(4-bit NF4, reports CER/WER/punctuation and seconds/page on the synthetic
set). It must run in a process where Surya has **not** been started (Surya's
server holds ~3 GB VRAM until its process exits). Also run it on CPU for a
clean seconds-per-page. Decide from the numbers whether QARI v0.3 becomes the
primary recogniser, a second opinion, or stays escalation-only.
Done when: a table like BENCHMARKS.md #3 exists for QARI v0.3.

**A2. Fix what the primary engine drops.**
Options, roughly cheapest first:
1. Use QARI v0.3 (or another VLM) on lines/regions where Paddle's output
   lacks punctuation, digits or Latin, merged with Paddle's boxes.
2. Second-engine disagreement: run two engines and flag lines where they
   differ, re-reading only those (this also fixes the scorer, see D).
3. Fine-tune the Arabic PaddleOCR recogniser on synthetic plus real
   labelled lines (the library's fonts repeat a lot, so this may give the
   biggest jump; needs the PaddleOCR training stack and labelled lines).
4. Try Surya-2 OCR in block mode or with a token cap (full-page hung).
5. Try QARI v0.4.0 (Qwen3-VL-4B) if VRAM allows.
Done when: synthetic CER <= 1%, WER <= 3%, punctuation recall ~100%, and the
same holds on real pages (see B).

**A3. Mixed-script handling.** Western digits and Latin words are dropped by
the Arabic recogniser and split lines. Needs a second pass for Latin/digit
runs or a VLM.

## B. Real-page ground truth and acceptance criteria

The synthetic benchmark has exact truth but is not real books. Build a small
reference set from real pages: 8-10 books, stratified by series, page size,
scan/producer, one or two pages each, with a **reference transcription of
some lines per page** made by a person or verified against the page image.
Keep it out of the public repository if the source is copyrighted.
Targets already agreed: CER <= 1%, WER <= 3%, reading order 100% on the
sample, punctuation preserved, no watermark leakage, no bad page passing
silently, reproducible, resumable.
Also measure: CPU/RAM/VRAM per stage, storage, failure and reprocessing rate,
and explain the two books that ran at 17-18 s/page.

## C. Finish QARI as an escalation tier

Currently wired in and **off by default** (`ocr.escalation.enabled: false`).
To enable safely:
1. **Convert its HTML output** (`<h2>`, `<i>`, `<b>`, `<br>`) to plain text
   for txt and to Markdown for md. A converter exists in
   `scripts/probes/qari_gpu_probe.py: html_to_text`; move it into
   `engines/qari_engine.py` and keep headings as headings.
2. **GPU path:** the `device: "cuda"` branch loads bf16 unquantized (4.4 GB)
   and will not fit 3.68 GiB. Implement 4-bit NF4 loading as in the probe and
   run it as its own phase after Surya has exited (separate process, or a
   pipeline split into a layout phase and an OCR phase).
3. **Footer/stamp leakage:** QARI reads the whole page including the footer,
   and the footer text differs page to page, so the recurring-line filter
   misses it. Crop or mask the bottom band using the layout regions before
   sending the page, or filter with fuzzy matching.
4. **Real confidence:** mean top-token probability saturates (100.0 on the
   real page). Use disagreement with Pass 1, length ratio, or repetition
   checks instead.
5. Decide the acceptance rule (currently MEDIUM/HIGH accepted, else
   `REVIEW_REQUIRED`) after A1 gives real numbers.
6. Measure CPU and GPU seconds per page for v0.3.

## D. Quality scoring

- The score does not follow accuracy (87-98 on pages with 4-6% CER). Add
  signals that can see the real failures: punctuation density versus
  expected, digit/Latin presence, agreement between two engines, and a
  reading-order sanity check.
- Calibrate tier thresholds against measured error, not against the score's
  own scale, before any auto-accept policy.
- `dictionary_hit_rate` is a function-word list; consider a real Arabic
  morphological lexicon (e.g. CAMeL Tools) to *flag* suspect words. Do not
  auto-correct text.

## E. Output fidelity

- **Paragraph reconstruction.** `book.txt`/`book.md` emit recognised lines;
  a novel paragraph becomes several lines (txt) or several one-line
  paragraphs (md). Join lines into paragraphs using layout blocks,
  indentation and vertical gaps; keep dialogue lines separate; handle words
  split at line ends.
- Heading detection quality (Surya `SectionHeader`) is unverified across
  books; footnotes and captions are kept but not specially formatted.
- Page number is dropped from the body; keeping it as page metadata (for
  the "printed page vs PDF page" mapping) is not done.
- Markdown for emphasis (`<i>`, `<b>`) from QARI, once C1 is done.

## F. Unused or unimplemented configuration and interfaces

Declared in config but not implemented: `preprocess.upscale`,
`preprocess.binarize.method`, `triage.orientation_check` (needs Tesseract,
not installed), `rasterize.image_format`, `concurrency.cpu_workers` and
`gpu_stage_batch_size` (everything is sequential), `ocr.cloud_fallback.*`.
Interfaces defined but unused: `EscalationPolicy`, `Validator`, `JobManager`.
Either implement or delete them; a whole-book `Validator` (page-count match,
gaps, duplicates) is worth having before large runs.

## G. Scale (only when running the full library)

- Not started: the 101-book run (~100+ h of CPU for Pass 1 plus layout).
- Add a directory/batch mode with a job queue, retries with backoff, and
  per-book progress; pay Surya's 10-13 s startup once per batch, not per book.
- Parallelism: CPU Paddle across pages while the GPU serves layout; measure
  contention first.
- Pass 3 **cloud fallback** (Gemini, opt-in, env-var key, never on by
  default) was designed, and never implemented. Pricing at the time was about
  $0.30 per million input tokens for Gemini 2.5 Flash; verify current pricing.
- Housekeeping: a lockfile (`uv lock`) for reproducible installs (the CUDA-13
  torch pulled today is a moving target), and a CI job for the unit tests.

## H. Tests still missing

End-to-end test on a small fixture PDF, writer output-format tests
(`book.txt`/`book.md` structure), rasterizer tests (corrupt page), resume
test after the refactor with a real interrupted run, and a regression test on
the synthetic benchmark thresholds.

## I. Known limitations to keep in mind

- Accuracy is below target and the score hides it (see above).
- Fonts and books not yet seen may behave differently from the synthetic set.
- Only one printed reading direction and layout family was tested: single
  column, right-to-left. Two-column pages, tables and illustrations were not
  exercised on real pages.
- GPU is 4 GB with ~3.68 GiB usable; two large GPU models cannot be loaded at
  once, and Surya's cannot be unloaded mid-process.
- The two large-book slowdowns were never explained.
