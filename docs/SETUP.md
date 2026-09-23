# Setup, operation and resuming development

## 1. Requirements

- Linux (developed on Ubuntu). NVIDIA GPU + driver for the Surya layout
  stage (developed on an RTX 3050 Ti Laptop, 4 GB, driver reporting CUDA
  13.2). A CPU-only run is possible by setting `layout.engine: none`, at
  the cost of typed layout (footer/page-number exclusion and block order).
- [`uv`](https://docs.astral.sh/uv/) and internet access for the first run.
- ~30 GB RAM was available; PaddleOCR needs little, QARI on CPU needs
  ~10-11 GB.
- Disk: the uv cache took ~7 GB for the CUDA torch stack; model caches
  ~17 GB in total during development (see `ENGINES.md` section 5 for what is
  obsolete). Plan on ~15 GB for a clean install with only the models in use.

## 2. Install

```bash
cd ocr_pipeline
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e .                    # first time: several GB, tens of minutes
./scripts/setup_layout_backend.sh      # llama.cpp CUDA build into .tools/ (gitignored)
```

`setup_layout_backend.sh` fetches the latest llama.cpp release tag and the
CUDA 12.8 Ubuntu build plus its `cudart` bundle (override the variant with
`LLAMACPP_CUDA_VARIANT`). It is idempotent. The first real conversion then
downloads the Surya GGUF model (~1.5 GB) and PaddleOCR's two models
(small). Nothing is installed system-wide.

If the machine has Docker with the NVIDIA runtime configured, Surya's own
`vllm` backend could be used instead; this project does not.

## 3. Run

```bash
ocr convert "/path/to/book.pdf" --output "/path/to/out"
ocr status  "/path/to/out"
ocr inspect "/path/to/out" --page 12
ocr --set ocr.escalation.enabled=true convert "/path/to/book.pdf"   # QARI pass, see ENGINES.md
ocr --set layout.engine=none convert "/path/to/book.pdf"            # no Surya
```

A long book can be interrupted and re-run with the same command; it resumes
at the first unfinished page. `--force` redoes everything.

Batch several books:

```bash
for f in /library/*.pdf; do ocr convert "$f" --output "/out/$(basename "$f" .pdf)"; done
```

Each `convert` starts and stops its own Surya server (10-13 s).

## 4. Reproducing the measurements

```bash
python scripts/synth_benchmark.py --out /tmp/synth --pages-per-font 2   # full pipeline, exact CER/WER/punctuation
python scripts/engine_compare.py --bench /tmp/synth                     # detector settings, needs the run above
python -m pytest tests/ -q                                              # 26 unit tests
```

The synthetic benchmark needs the Noto Arabic fonts, FreeSerif and DejaVu
Sans installed under `/usr/share/fonts/truetype/` (paths are hard-coded at the
top of `scripts/synth_benchmark.py: FONTS`) and Pillow built with Raqm.
Unfinished experiments: `scripts/probes/` (see `ENGINES.md`).

## 5. Known environment traps

| Trap | What to do |
|---|---|
| `paddlepaddle` newer than 3.1.0 crashes on CPU | Keep the pin; re-test on a real page before changing it. |
| Surya needs a `llama-server` | Run `scripts/setup_layout_backend.sh`; if `.tools/` is missing Surya tries Docker and fails with "unknown or invalid runtime name: nvidia". |
| Surya holds ~3 GB VRAM until the process exits | Never load another large GPU model in the same process. Use a separate process afterwards. |
| GPU reports 4096 MB but ~3.68 GiB is usable | Budget with the smaller number. |
| Hugging Face downloads print "unauthenticated requests" | Harmless; set `HF_TOKEN` for higher limits. |
| Progress bars in redirected logs do not update | Watch file sizes in `~/.cache/huggingface/hub/*/blobs`. |
| SSH to GitHub port 22 times out on this network | Use `ssh.github.com:443` (below). |
| Python 3.14 is the system default | Use the uv-managed 3.12 venv. |

## 6. Git and GitHub

Local repository: `ocr_pipeline/` is its own git repository (branch `main`).
Remote: `origin` = `git@github.com:Devops-Ahmad/PDF-OCR-.git` (note the
trailing hyphen in the repository name). Port 22 was blocked from the dev
machine, so pushes used the same SSH key through port 443:

```bash
GIT_SSH_COMMAND="ssh -o HostName=ssh.github.com -p 443" git push -u origin main
```

`.gitignore` excludes `.venv/`, `.tools/`, `.ocr_work/`, `.env`, `*.key`,
`synth_bench_out/`, caches. No secrets exist in the repository; the only
planned secret is `GEMINI_API_KEY` for the unimplemented cloud tier, read
from the environment (`.env.example` documents it).

## 7. How to resume development

1. Read `README.md` (status and results), then `docs/FUTURE_WORK.md`.
2. Set up the environment (section 2) and confirm `pytest` gives 26 passed.
3. Run the synthetic benchmark to reproduce the baseline
   (CER 4.32% / WER 16.84% / punctuation 50.2%). If that reproduces, the
   environment is healthy.
4. Start with FUTURE_WORK section A1: run
   `SYNTH_BENCH_DIR=/tmp/synth python scripts/probes/qari_gpu_probe.py 1024 10`
   in a fresh process with Surya not running.
5. Keep `docs/BENCHMARKS.md` updated with every new measurement, and add
   any new problem to `docs/PROBLEMS_AND_SOLUTIONS.md`.

## 8. Status of the working tree when frozen (2026-09-23)

- All code committed; `pytest`: 26 passed.
- Default behaviour: Pass 1 (PaddleOCR) + Surya layout + RTL ordering +
  scoring + watermark filter, escalation off.
- No background processes needed; the Surya server is started and killed per
  run.
- Nothing was deleted; obsolete model caches are still on disk.
