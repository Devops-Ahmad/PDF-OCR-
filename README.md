# bookocr: standalone Arabic book OCR (PDF -> book.txt + book.md)

> **STATUS VERIFIED 2026-10-03:** the V2 safety and audit foundation is
> implemented and its model-free checks pass (`40 passed`). No OCR model or
> accuracy benchmark was run during this implementation, so the pipeline is
> **not yet accurate enough for unsupervised use** and 97%+ word accuracy has
> not been demonstrated. See [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md)
> and [`docs/V2_IMPLEMENTATION_LOG.md`](docs/V2_IMPLEMENTATION_LOG.md).

## What it is

A standalone, portable command-line tool. It takes a scanned or image-only
book PDF and produces exactly two user-facing files:

- `book.txt`: plain text, page-preserved (`===== PAGE N =====` markers)
- `book.md`: Markdown, page-preserved (`## Page N` headings)

It has no dependency on any other project, directory layout, or downstream
schema. Anything that consumes the text (knowledge bases, analysis, search)
lives outside this repository. Internal files (per-page records, quality
report, resumable state) are written under `<output>/.ocr_internal/` and are
never part of the product surface.

## Where it stands, honestly

Measured on a synthetic benchmark with exact ground truth (10 pages, 5
fonts; method and per-font numbers in [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md)):

| Metric | Measured | Target we set |
|---|---|---|
| Character error rate | **4.3%** | <= 1% |
| Word error rate | **16.8%** | <= 3% |
| Punctuation preserved | **50%** | ~100% |

The main cause is the small Arabic PaddleOCR recognizer dropping `.` `:` `!`
`(` `)` and the dialogue dash, and dropping Western digits and Latin words
inside Arabic lines. Tuning the detector did not help (7 settings tried).
The engine's own confidence score (around 90) does **not** reflect this:
confidence is not accuracy here. The most promising fix found is QARI-OCR
v0.3, which on one real page produced correctly ordered text with
punctuation intact, but it was not benchmarked. See
[`docs/ENGINES.md`](docs/ENGINES.md).

The <= 3% WER target is the project's definition of a 97%+ word-accuracy
proxy. It is a ground-truth metric, not the pipeline's 0-100 confidence
score. The measured synthetic result corresponds to only 83.16% by that
proxy and cannot be extrapolated to real books.

What does work and is verified: right-to-left reading order (a real defect
was found and fixed), watermark/footer/page-number exclusion, crash-safe
resume, deterministic page-atomic output, and a working two-pass structure.

## Quick start

```bash
cd ocr_pipeline
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e .
./scripts/setup_layout_backend.sh      # one-time: local llama.cpp build for the Surya layout model

ocr convert "/path/to/book.pdf"                         # -> ./book/book.txt and ./book/book.md
ocr convert "/path/to/book.pdf" --output "/path/to/out"
ocr status  "/path/to/out"                              # per-status page counts, failures
ocr inspect "/path/to/out" --page 12                    # raw internal record of one page
ocr audit   "/path/to/out" --page 12                    # V2 triage/candidates/decision/attempts
ocr validate "/path/to/out"                             # re-run whole-book integrity checks
```

Use a new output directory for every source PDF. The pipeline records the
canonical source path, page count and file revision; it deliberately refuses
to resume into an output directory created for a different or modified PDF,
because doing so could mix page records from two books.

Full setup, first-run downloads (several GB), hardware notes and known
environment traps: [`docs/SETUP.md`](docs/SETUP.md).

## Documentation map

| Read this | When you want to know |
|---|---|
| [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md) | The current evidence ledger, what remains unmeasured, and the exact path and gate for 97%+ word accuracy |
| [`docs/OCR_PIPELINE_V2.md`](docs/OCR_PIPELINE_V2.md) | The Arabic routed-ensemble design, engine roles, isolated workers, evaluation lab, and implementation gates |
| [`docs/V2_IMPLEMENTATION_LOG.md`](docs/V2_IMPLEMENTATION_LOG.md) | Exactly what was implemented for Gate A, verification evidence, migrations, and remaining limitations |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the pipeline works, every component, the output formats, the CLI, state/resume, and why each design decision was made |
| [`docs/ENGINES.md`](docs/ENGINES.md) | PaddleOCR, Surya and QARI in detail, including everything learned about QARI v0.2.2.1 vs v0.3, and engines evaluated but not used |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | Library survey, real-page results, the synthetic benchmark, the detector-settings experiment, throughput, tests |
| [`docs/PROBLEMS_AND_SOLUTIONS.md`](docs/PROBLEMS_AND_SOLUTIONS.md) | Every problem hit during the build, its root cause and fix, including wrong turns |
| [`docs/FUTURE_WORK.md`](docs/FUTURE_WORK.md) | Everything planned but not done, in priority order, with concrete next steps |
| [`docs/SETUP.md`](docs/SETUP.md) | Installing, running, hardware limits, disk usage, resuming development |
| [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | Automated lint and unit-test checks for every push and pull request |
| [`docs/phase0_findings.md`](docs/phase0_findings.md) | Historical log of the first measurements (superseded where noted) |

## Repository layout

```
config/default.yaml        all tunables (thresholds, engines, filters, output markers)
src/bookocr/
  cli/main.py              `ocr`: convert, status, inspect, audit, validate, benchmark
  pipeline.py              routed orchestrator: text layer, OCR, escalation, validation
  config.py                YAML + --set overrides, validated with pydantic
  core/                    contracts, registry/workers, artifacts, provenance, SQLite state
  rasterize/               PDF page -> image plus conservative native text extraction
  preprocess/              conditional deskew / denoise / binarize
  engines/                 paddle_engine.py (primary), qari_engine.py (Pass 2)
  layout/                  surya_layout.py, reading_order.py (RTL ordering)
  quality/                 heuristic score, evidence collection, conservative routing
  postprocess/watermark.py recurring bottom-band line filter
  output/writer.py         pages.jsonl -> book.txt / book.md / qc_report.json
  eval/metrics.py          CER / WER / punctuation-recall
scripts/
  setup_layout_backend.sh  downloads the llama.cpp build Surya needs
  synth_benchmark.py       ground-truth benchmark through the real pipeline
  engine_compare.py        PaddleOCR detector/recogniser settings comparison
  probes/                  two exploratory, unfinished experiments (see ENGINES.md)
tests/                     model-free unit/integration tests (40 passing)
docs/                      the documentation above
```
