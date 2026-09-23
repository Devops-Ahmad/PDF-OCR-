"""EXPLORATORY, NOT RUN TO COMPLETION. Surya-2 full-page OCR on the synthetic pages.

On 2026-09-23 the first page had not returned after ~14 minutes with the GPU
pinned at 99% (llama-server), so it was killed. Kept so the experiment can be
repeated with a token cap or a different prompt. See docs/ENGINES.md.

Usage: SYNTH_BENCH_DIR=<dir from scripts/synth_benchmark.py> python scripts/probes/surya_ocr_probe.py <n_pages>
"""
import os
import json, sys, time
from pathlib import Path
from PIL import Image
from bookocr.layout.surya_layout import _configure_llamacpp_backend
_configure_llamacpp_backend()
from surya.recognition import RecognitionPredictor
B = Path(os.environ["SYNTH_BENCH_DIR"])
pages = sorted(B.glob("pages/page_*.png"))[:int(sys.argv[1])]
pred = RecognitionPredictor()
out = {}
for p in pages:
    n = int(p.name.split("_")[1])
    t0 = time.time()
    res = pred([Image.open(p).convert("RGB")], full_page=True)[0]
    if not out:
        print("RESULT TYPE:", type(res).__name__, [a for a in dir(res) if not a.startswith("_")][:30], flush=True)
        blk = res.blocks[0] if getattr(res, "blocks", None) else None
        print("BLOCK ATTRS:", [a for a in dir(blk) if not a.startswith("_")][:30] if blk else None, flush=True)
    out[n] = res.model_dump() if hasattr(res, "model_dump") else str(res)
    print(f"page {n}: {time.time()-t0:.1f}s", flush=True)
(B / "surya_ocr_raw.json").write_text(json.dumps(out, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
print("DONE_MARKER")
