# Problems found and how they were resolved

Chronological-ish, including wrong turns, because several early diagnoses
were wrong and the corrections matter if this work is picked up again.
Status: **fixed**, **mitigated**, **open**, or **corrected diagnosis**.

## Environment and dependencies

**1. paddlepaddle 3.3.1 crashes on CPU. Fixed (pin).**
`NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support
[pir::ArrayAttribute<pir::DoubleAttribute>]` on every PP-OCRv5 detector.
oneDNN/PIR bug in paddle itself. Monkey-patching oneDNN off worked but was
~9x slower (51.7 s vs 5.8 s per page). Downgrading to `paddlepaddle==3.1.0`
fixed it at full speed. Pinned in `pyproject.toml` with the reason.

**2. PaddleOCR language code. Fixed.** `lang="arabic"` raises
"No models are available"; the code is `ar`. Passing explicit model names
makes PaddleOCR ignore `lang` with a warning, so the engine passes one or the
other.

**3. No GPU paddle for this machine. Accepted.** Paddle's index only has
`paddlepaddle_gpu` 2.6.1 for CUDA 11.2-12.0, older than the PP-OCRv5 format.
PaddleOCR runs on CPU; the GPU is used by Surya instead.

**4. Python 3.14 system interpreter. Avoided.** ML wheels lag it. The project
uses a `uv`-managed Python 3.12 venv.

**5. Surya install is huge. Accepted.** `uv pip install surya-ocr` pulls a
CUDA-13 torch stack (~7 GB, ~50 min here) and downgrades `opencv-python-
headless` and `pillow`; tests still pass.

## Surya

**6. Reading order scrambled on a real page. Fixed (root cause found).**
First diagnosed as a layout problem. Surya layout alone only partly fixed it.
Inspecting raw boxes showed PaddleOCR's detector had split **one justified
line into 2-3 boxes** at slightly different baselines; an ascending-y
tie-break put them in the wrong order, which is exactly wrong for right-to-
left text. Fix: cluster lines into rows by vertical overlap, then order each
row by descending x (`layout/reading_order.py`). Verified on the exact page,
locked in by a test with its real coordinates, and made layout-independent so
it also runs when Surya is off or fails. Note that the page-level quality
score never saw this defect: reading-order damage does not lower any score.

**7. Surya is a served VLM, not an in-process model. Worked around.**
`LayoutPredictor()` spawns Docker with `--runtime nvidia`, which failed
("unknown or invalid runtime name: nvidia"). Its other backend is a
`llama-server` binary. `scripts/setup_layout_backend.sh` downloads a CUDA
llama.cpp build into `.tools/` (gitignored, nothing system-wide), and
`surya_layout.py` points Surya at it.

**8. `SuryaInferenceManager.stop()` does not free the GPU. Documented.**
`LlamaCppBackend.stop()` only clears Python-side handles; the subprocess dies
at interpreter exit. So ~3 GB of VRAM stays held until the process ends.
`surya_layout.shutdown()` is kept but its docstring says it does not free
memory. This is the real reason for problem 12. **A wrong claim in the
first version of the code and docs ("shut down Surya to free VRAM for QARI")
was corrected.**

**9. Surya-2 full-page OCR hangs. Open.** ~14 min on one page, GPU 99%,
nothing returned. Killed. Probe kept in `scripts/probes/`.

## Pipeline and configuration

**10. Config paths resolved against the wrong directory. Fixed, then
removed.** Relative paths in the YAML resolved against `config/`, so
`../processed` landed inside the project. With the standalone-tool
refactor the paths were removed entirely; output location is now the CLI
argument.

**11. `--set` left values as strings. Fixed.** `--set
quality.thresholds.high=99` stored `"99"`, and a `float >= str` comparison
crashed deep in the scorer. `cli/main.py: _coerce` converts int/float/bool.

## QARI (the long one)

**12. GPU out-of-memory blamed on QARI. Corrected diagnosis.** The errors
said "Process N has 2.99 GiB in use" while our process had 82 MB: that was
the Surya server. Days of shrinking `max_pixels`, `max_new_tokens`, and
allocator settings were chasing the wrong cause; none of them moved the
constant "1.36 GiB" allocation. QARI can plausibly fit once Surya is not
running in the same time window (untested).

**13. QARI v0.2.2.1 repository is an adapter. Understood.** No
`model.safetensors`; needs a base.

**14. `device_map="auto"` on a 4-bit base. Worked around.** "Some modules are
dispatched on the CPU or the disk"; forcing `{"": 0}` loaded it.

**15. Wrong hypothesis: "the image is too big". Ruled out.** The failure
happened at model-load time, before any image was processed.

**16. v0.2.2.1 output was reversed / looping / hallucinated. Root cause
found, engine abandoned.** 648 "missing adapter keys" in the vision tower
with both the vanilla and the unsloth non-quantized bases; PEFT silently
skips them. Not fixable by base choice.

**17. Whole-string reversal workaround, then removal. Superseded.** Reversing
the output recovered mostly-correct text from v0.2.2.1 (with residual letter
transpositions), and was added. It is **not** needed for v0.3 and was
removed; a note remains in `ENGINES.md`.

**18. Aggressive anti-repetition settings hurt quality. Corrected.**
`repetition_penalty=1.3`/`no_repeat_ngram_size=4` ended the loop but made
every other page more scrambled (extra and misplaced letters). Softened to
1.1 / 6; the scorer's repetition check is the real backstop.

**19. Switched to the merged v0.3 checkpoint. Fixed the loading, unfinished
integration.** It loads standalone and reads correctly, but emits HTML and has
no measured accuracy or GPU speed. See FUTURE_WORK.

## Quality scoring

**20. Repetition inside one text blob was invisible. Fixed.** The old
repeated-line check needs line breaks; a model looping inside one line
escaped it. Added a repeated 4-gram detector.

**21. Warnings did not affect the tier. Fixed for repetition.** Warnings were
metadata only, so a looping page could still score MEDIUM/HIGH. Repetition
warnings now force CRITICAL.

**22. The score does not track accuracy. Open.** Measured: score 87-98 with
4.3% CER and 50% punctuation recall. Dropped punctuation/digits and reading
order errors are not detected. Any "auto-accept HIGH" policy is unsafe until
the score is calibrated against measured error.

## Measurement

**23. Throughput estimate off by ~10x. Corrected.** ~11 h guessed from one
sample; benchmarking gave 6-18 s/page, i.e. ~100+ h for the library.

**24. Confidence looked fine, accuracy was not. Found by the synthetic
benchmark.** The primary engine loses punctuation, digits and Latin words.
Detector settings do not help (BENCHMARKS.md #4).

**25. Own benchmark bug. Fixed.** An early ad hoc filename lookup failed on
Arabic names containing bidi control characters and different Unicode
normalisation; the surveys use null-delimited iteration instead.

## Tooling and process (avoid repeating)

- `pkill -f <name>` from a shell whose own command line contains `<name>`
  kills that shell (exit 144). Kill by PID.
- A "wait until X exits" loop using `pgrep -f X` inside `bash -c '...X...'`
  matches itself and never ends.
- `uv pip install` piped to `tail` prints nothing until it finishes; long
  installs look hung. Watch the uv cache size instead.
- `tail -f -n +1` on a log that a new run truncates keeps feeding old
  monitors; use a fresh log file per run.
- Progress bars written to a redirected log do not refresh; check file sizes.
- GitHub from this machine: SSH port 22 times out; use
  `ssh.github.com:443` (same key, same repository).
