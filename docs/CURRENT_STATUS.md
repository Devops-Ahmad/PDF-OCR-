# Current project status and the 97%+ quality gate

Snapshot date: **2026-10-03**.

This document is the status source of truth. It separates measurements from
heuristics and plans. Historical experiment details remain in
[`BENCHMARKS.md`](BENCHMARKS.md); implementation details remain in
[`ARCHITECTURE.md`](ARCHITECTURE.md); unfinished engineering work remains in
[`FUTURE_WORK.md`](FUTURE_WORK.md). The proposed multi-engine architecture is
documented in [`OCR_PIPELINE_V2.md`](OCR_PIPELINE_V2.md).

No OCR model or accuracy benchmark was executed for this snapshot. The V2
implementation was verified with model-free unit/integration tests and
static byte-code compilation; details and exact commands are in
[`V2_IMPLEMENTATION_LOG.md`](V2_IMPLEMENTATION_LOG.md).

## Executive verdict

The project has a coherent, resumable PDF-to-`book.txt`/`book.md` pipeline
and useful safety work in progress. It **has not demonstrated 97% word
accuracy**. The only checked-in exact ground-truth benchmark reports strict
WER **16.84%**, which corresponds to a simple `1 - WER` word-accuracy proxy
of **83.16%**, on ten synthetic pages. That number is not a real-book score.

The internal 0-100 page `confidence` is a routing heuristic, not accuracy.
It must never be used to claim 97%: the recorded synthetic pages scored
roughly 87-98 while still having 4.32% CER, 16.84% WER, and only 50.2%
punctuation recall.

## Evidence matrix

| Area | What is supported by existing evidence | What is not established |
|---|---|---|
| Synthetic recognition | 10 pages, 5 fonts: strict CER 4.32%, strict WER 16.84%, punctuation recall 50.2%. | Generalisation to real novels, unseen fonts, degraded scans, tables, or multi-column pages. |
| Real-book recognition | 31 pages were processed historically; all received HIGH heuristic tiers. One 17-line visual check estimated roughly 5-7% word errors. | No checked-in human-reference corpus, statistically valid real-book WER/CER, or full-book accuracy. The HIGH tiers are not accuracy evidence. |
| Reading order | A real RTL split-line defect was fixed and represented by regression fixtures; the synthetic sample retained correct order. | Broad real-book coverage, two-column layouts, tables, illustrations, and a measured book-level failure rate. |
| Watermarks and page furniture | No watermark leakage was recorded on the 10-page synthetic set; conservative footer filtering exists. | A representative real-library false-positive/false-negative rate. |
| Resume and output safety | Source identity now includes streamed SHA-256; attempts, atomic JSON artifacts, fsynced events, latest-record semantics, and whole-book validation are implemented and covered by model-free tests. | Hard-kill recovery of the new V2 path and large-book fault injection have not yet been repeated. |
| V2 routing foundation | Registry-backed persistent process workers, optional native PDF text-layer extraction, candidate/evidence records, conservative disagreement routing, review status, and validation are implemented. | Process isolation currently shares one Python environment; layout remains in the main process; new model adapters are not implemented or measured. |
| Automated tests | On 2026-10-03, `PYTHONPATH=src .venv/bin/python -m pytest -q` passed **40 tests**. `compileall` and CLI help also passed. | Ruff is not installed in the current virtual environment, and no remote CI run was inspected. Engine/model inference is intentionally outside these tests. |
| Scale and performance | Historical observations: PaddleOCR 5.6 s/page synthetic and 6-18 s/page real; Surya adds startup and per-page cost. | Current resource profiles, large-run failure/retry rates, storage per book, and an end-to-end 101-book run. |

## Current architecture in one line

`PDF + SHA-256 -> per-page text-layer gate or raster/preprocess -> registry
worker OCR -> layout/RTL -> evidence + conservative routing -> optional
isolated escalation -> append-only canonical records -> atomic outputs ->
whole-book validation`

QARI-OCR v0.3 is wired as an optional second pass but is off by default.
HTML output is now detected and rejected from automatic acceptance; its
footer handling, model-specific environment, and measured accuracy remain
unfinished. Cloud fallback is declared but not implemented.

## Definition of 97%+

For this project, “97%+ word accuracy” should mean **strict WER <= 3%** on a
fixed, human-verified evaluation corpus. Report `1 - WER` only as a readable
accuracy proxy and always publish WER beside it. Do not substitute OCR engine
confidence, the project quality tier, CER, or visual readability.

The release gate should require all of the following on both the synthetic
regression set and a representative real-page set:

1. Strict WER <= 3% overall, reported per book/font as well as in aggregate.
2. Strict CER <= 1%.
3. Punctuation recall near 100%, with per-mark results for Arabic and Western
   punctuation, digits, and embedded Latin text.
4. Correct reading order on every acceptance page.
5. No watermark/footer leakage and no removal of real body text.
6. No materially bad page silently accepted as HIGH; flagged-page precision
   and recall must be measured against the reference set.
7. Reproducible results from a locked environment, plus verified resume and
   output-integrity behavior.

An aggregate result alone is insufficient: a strong font or book must not
hide a failing one.

## Path to the gate

### 1. Freeze the evaluation protocol

- Define exact Unicode normalisation, whitespace, diacritic, punctuation,
  page-boundary, and reading-order rules before changing an engine.
- Preserve strict and loose CER/WER, but use strict WER as the 97% gate.
- Record code revision, config hash, model revisions, hardware, and per-page
  timing with every result.

### 2. Build real ground truth

- Select 8-10 books stratified by series, producer, page dimensions, font,
  and scan quality, with at least one or two representative body pages each.
- Human-transcribe and independently verify the selected lines/pages.
- Keep copyrighted PDFs and reference text outside Git; check in only the
  corpus manifest, hashes/IDs, protocol, and aggregate reports when safe.
- Maintain a development split and an untouched acceptance split so tuning
  cannot overfit the release measurement.

### 3. Reproduce the existing baseline

- Re-run the synthetic benchmark and the new real corpus only when benchmark
  execution is explicitly authorised.
- Treat a mismatch with the documented 4.32% CER / 16.84% WER baseline as an
  environment or reproducibility problem before attempting model changes.

### 4. Measure QARI v0.3 in isolation

- Run the existing probe in a fresh process without Surya occupying VRAM.
- Measure accuracy, punctuation, speed, peak RAM/VRAM, and failure modes.
- Finish HTML-to-text/Markdown conversion and footer masking before allowing
  QARI output into public files.
- Choose its role from evidence: primary recogniser, region-level second
  opinion, escalation engine, or rejection.

PaddleOCR-VL-1.6 and MonkeyOCRv2 should be measured in the same controlled
adapter harness before QARI's role is fixed. Qianfan-OCR and Chandra belong
to a separate complex-layout/large-hardware track, not the default 4 GB GPU
path. See `OCR_PIPELINE_V2.md` for routing and isolation details.

### 5. Improve recognition, then routing

- First try region/line-level engine disagreement for punctuation, digits,
  Latin spans, and suspicious Paddle output.
- If the measured gap remains, fine-tune an Arabic recogniser on labelled
  recurring fonts and real lines; keep the acceptance split untouched.
- Calibrate the quality scorer against actual page errors. It may route pages
  for review, but it must not be presented as accuracy.

### 6. Add reproducible release evidence

- Produce versioned, machine-readable per-page results and a concise report.
- Add an end-to-end fixture test, synthetic-threshold regression, rasterizer
  corruption cases, and interrupted-resume verification.
- Add and commit a lockfile; pin model revisions as well as Python packages.
- Require local and CI checks to pass before recording a release candidate.

## Immediate next work, without claiming quality progress

1. Package each heavy engine in a pinned environment/container and add the
   PaddleOCR-VL/MonkeyOCR adapters without changing the acceptance gate.
2. Isolate layout execution and add timeout/restart/fault-injection coverage.
3. Create the private real-page reference corpus and protocol.
4. Only with explicit benchmark authorisation, reproduce the baseline and
   compare candidates on the development split; do not tune on acceptance.

Until those steps produce measured results, the accurate status is:
**Gate A foundation implemented and model-free verified; production engine
isolation remains incomplete; 97%+ is not demonstrated.**
