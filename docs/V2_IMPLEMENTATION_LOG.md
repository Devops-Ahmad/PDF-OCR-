# V2 implementation log

Date: **2026-10-03**

This is the implementation and verification ledger for the routed OCR
foundation described in `OCR_PIPELINE_V2.md`. It distinguishes shipped code,
model-free verification, and work that still needs real OCR measurements.

## Scope and non-scope

Implemented in this change:

- content provenance and output-directory safety;
- per-page native PDF text-layer gating;
- engine registry and persistent process runners;
- canonical result/evidence/routing contracts;
- durable page candidates, decisions, attempts, and lifecycle events;
- conservative two-pass escalation behavior;
- whole-book integrity validation;
- operator commands for audit and validation;
- model-free unit and fake-engine integration coverage.

Deliberately not done:

- no OCR model was downloaded or run;
- no synthetic or real-book benchmark was run;
- no accuracy number was changed;
- no claim of 97%+ accuracy is made;
- no automatic spelling or language-model correction was added.

## Runtime flow now implemented

1. Resolve and open the source PDF.
2. Compute a streamed SHA-256 of its content and the legacy metadata
   fingerprint, then register the book and pages in SQLite.
3. Render each pending page and record raster/preprocessing analysis.
4. Treat a visually blank page as `BLANK` without recognition.
5. Inspect the PDF text layer. Use it only when all configured character,
   word, Arabic-script, and replacement-character gates pass.
6. Otherwise run the primary OCR adapter through `EngineRegistry`; the
   default execution mode is a persistent child process started with
   `multiprocessing` `spawn`.
7. Apply native layout for accepted text layers, or the configured layout
   detector/RTL fallback for OCR output.
8. Store the untouched engine result, independent evidence, quality report,
   and routing decision.
9. Accept safe pages, flag unsafe pages, or queue low pages for Pass 2.
10. Close the primary workers before starting an escalation worker.
11. Compare Pass 2 against Pass 1. HTML leakage or excessive normalized
    disagreement prevents automatic acceptance. The primary output remains
    canonical but is marked `REVIEW_REQUIRED` when adjudication is unsafe.
12. Generate `book.txt` and `book.md`, then validate page coverage, final
    states, and public artifacts before marking the book complete.

## Code changes by responsibility

### Contracts and configuration

- `core/types.py`: added `RoutingAction`, `PageProfile`, `CandidateResult`,
  `DecisionRecord`, and `ValidationReport`; native engine confidence is now
  optional; engine results carry revision, output format, runtime, and
  warnings.
- `core/interfaces.py`: escalation returns an explicit routing action and the
  validator returns a structured report.
- `config.py` and `config/default.yaml`: added conservative text-layer gates,
  process execution/timeouts, and a maximum normalized disagreement.

### Provenance, state, and artifacts

- `core/provenance.py`: streamed content SHA-256 plus the legacy metadata
  fingerprint helper.
- `core/state.py`: migration for `books.source_sha256`; new `attempts` table;
  attempt start/finish/query methods; explicit book status transitions.
  Existing databases are upgraded in place and old callers remain supported.
- `core/artifacts.py`: atomic JSON writes, filesystem-safe candidate names,
  per-page triage/layout/candidate/decision artifacts, a validation report,
  and append-only events followed by `fsync`.

### Engine execution

- `core/engine_registry.py`: adapter specifications, built-in Paddle/QARI
  registrations, in-process runner for tests, persistent spawned-process
  runner for normal execution, timeout handling, and cleanup.
- `engines/paddle_engine.py`: records model revision, output format, and
  runtime.
- `engines/qari_engine.py`: no longer treats token probability as native OCR
  confidence; records output format/runtime and detects HTML output.

### Per-page routing

- `rasterize/text_layer.py`: extracts PDF blocks with coordinates, converts
  them to the raster coordinate space, applies RTL ordering, and accepts the
  layer only after configured completeness/language/corruption gates.
- `quality/evidence.py`: collects counts for text, lines, regions,
  punctuation, digits, Latin spans, HTML tags, native-confidence presence,
  and optional Pass-1/Pass-2 disagreement.
- `quality/routing.py`: implements `ACCEPT`, `REPROCESS_LOCAL`, and
  `FLAG_FOR_REVIEW`. Pass-2 HTML and excessive disagreement are never silently
  accepted.
- `quality/scorer.py`: missing native confidence is neutral routing evidence
  with an explicit warning, not a fabricated zero or accuracy score.
- `pipeline.py`: wires all of the above into resume, Pass 1, Pass 2, artifact
  recording, safe fallback, finalization, and validation.

### Output and operator surface

- `validation.py`: validates expected pages, duplicate/latest canonical
  semantics, allowed final statuses, and the existence of public outputs.
  `REVIEW_REQUIRED` is valid but reported as a warning; missing/corrupt output
  is invalid.
- `cli/main.py`: adds `ocr audit` and `ocr validate`. The benchmark command
  was adapted to obtain engines from the registry after direct pipeline
  engine ownership was removed.
- `output/writer.py`: retains public `book.txt`/`book.md` compatibility and
  uses the latest append-only record for each page.

## Internal artifact contract

```text
<output>/
  book.txt
  book.md
  .ocr_internal/
    state.db
    pages.jsonl
    events.jsonl
    manifest.json
    qc_report.json
    validation_report.json
    pages/000001/
      triage.json
      layout.json
      candidates/<pass>-<engine>.json
      decision.json
```

`manifest.json` uses schema version 2 and records the source SHA-256, page
count, pipeline version, config hash, engine versions, and finalization time.
Candidate files preserve raw adapter output in a JSON-safe representation.

## State migration

The migration is additive and automatic:

- add nullable `books.source_sha256` when missing;
- create `attempts` when missing;
- compare content hashes whenever both the stored and current run have one;
- retain the older metadata fingerprint check for databases created before
  content hashing existed.

No state database is deleted or reset by the migration.

## Commands for operators

```bash
ocr audit /path/to/output
ocr audit /path/to/output --page 12
ocr validate /path/to/output
```

Book-level audit prints manifest, validation, QC, and event counts. Page-level
audit prints triage, layout, candidates, decision, and SQLite attempts.
Validation is read-only with respect to OCR: it does not invoke an engine.

## Verification performed

The following checks were run in this working tree:

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m bookocr.cli.main --help
```

Final test result: **40 passed in 1.33 s**.

Coverage added includes content hashing, state attempts/migration behavior,
artifact serialization, routing safeguards, validator behavior, native PDF
text-layer acceptance/rejection, in-process and persistent process runners,
an end-to-end one-page conversion using a fake engine, and successful
`audit`/`validate` CLI calls against that output.

The first plain `.venv/bin/python -m pytest -q` collection attempt resolved
the editable package from an older checkout. The verified command therefore
uses `PYTHONPATH=src`. This is an environment metadata issue, not a test
failure in the current source; reinstalling the editable package is the
proper cleanup. Ruff was not available in this virtual environment, so no
lint-pass claim is made.

## Known limitations and next gates

1. Process isolation shares the current Python environment. PaddlePaddle,
   Torch/vLLM, Kraken, and parser adapters still need separately pinned
   environments or containers.
2. Layout detection still runs in the parent process; it needs the same
   timeout/restart isolation as recognition.
3. Only existing Paddle and QARI adapters are registered. PaddleOCR-VL,
   MonkeyOCRv2, Qianfan, Chandra, and Kraken adapters are not implemented.
4. `peak_memory`, prompt revision, decoder parameters, and per-worker
   lockfiles are not yet recorded completely.
5. The text-layer thresholds are conservative defaults but are not calibrated
   on the real library.
6. There is no human-review UI; `REVIEW_REQUIRED` is exposed through state,
   artifacts, QC, and audit commands.
7. The current heuristic quality score is still not calibrated accuracy.
8. Hard-kill/fault-injection and large-book V2 recovery need a dedicated run.
9. Gate B/C model comparisons and Gate D 97% acceptance remain unexecuted.

The honest status is therefore: **the auditable Gate A foundation is built
and model-free verified; production dependency isolation and all OCR-quality
claims remain future measured work.**
