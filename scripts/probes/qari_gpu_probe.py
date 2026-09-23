"""EXPLORATORY, NEVER RUN. QARI-OCR v0.3 on the GPU (4-bit NF4) over the synthetic pages,
reporting CER/WER/punctuation and seconds/page. Written 2026-09-23 to test the
corrected hypothesis that QARI fits in VRAM once the Surya layout server is NOT
running at the same time. Run it in a process where Surya has not been started.
See docs/ENGINES.md and docs/FUTURE_WORK.md.

Usage: SYNTH_BENCH_DIR=<dir> python scripts/probes/qari_gpu_probe.py <max_pixels_in_28x28_units, e.g. 1024> <n_pages>
"""
import os
import json, re, sys, time, html
from pathlib import Path
import torch
from PIL import Image
from transformers import AutoProcessor, BitsAndBytesConfig, Qwen2VLForConditionalGeneration
from qwen_vl_utils import process_vision_info
from bookocr.eval.metrics import page_report

B = Path(os.environ["SYNTH_BENCH_DIR"])
MAXPX = int(sys.argv[1]) * 28 * 28
N = int(sys.argv[2])
NAME = "NAMAA-Space/Qari-OCR-v0.3-VL-2B-Instruct"
PROMPT = ("Below is the image of one page of a document. Just return the plain text representation of this document "
          "as if you were reading it naturally. Do not hallucinate.")

def html_to_text(s):
    s = re.sub(r"(?i)<br\s*/?>|</(p|h[1-6]|div|li|tr)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()

t0 = time.time()
model = Qwen2VLForConditionalGeneration.from_pretrained(
    NAME, device_map={"": 0},
    quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16))
proc = AutoProcessor.from_pretrained(NAME, min_pixels=256*28*28, max_pixels=MAXPX)
print(f"LOADED in {time.time()-t0:.0f}s, vram={torch.cuda.memory_allocated()/1e9:.2f}GB", flush=True)

refs = json.load(open(B / "reference.json"))
pages = sorted(B.glob("pages/page_*.png"))[:N]
rows, raw = [], {}
for p in pages:
    n = int(p.name.split("_")[1])
    msgs = [{"role": "user", "content": [{"type": "image", "image": Image.open(p).convert("RGB")}, {"type": "text", "text": PROMPT}]}]
    text = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    img, vid = process_vision_info(msgs)
    inp = proc(text=[text], images=img, videos=vid, padding=True, return_tensors="pt").to(model.device)
    t1 = time.time()
    with torch.inference_mode():
        out = model.generate(**inp, max_new_tokens=1400)
    dt = time.time() - t1
    gen = proc.batch_decode(out[:, inp.input_ids.shape[1]:], skip_special_tokens=True)[0]
    hyp = html_to_text(gen)
    raw[n] = gen
    r = page_report(refs[str(n)], hyp); r.update(page=n, secs=dt, peak_gb=torch.cuda.max_memory_allocated()/1e9)
    rows.append(r)
    print(f"page {n}: {dt:.1f}s  CER {r['cer_strict']:.2%}  WER {r['wer_strict']:.2%}  punct {r['punct_recall']}  peakVRAM {r['peak_gb']:.2f}GB", flush=True)

mean = lambda k: sum(x[k] for x in rows if x[k] is not None) / len([x for x in rows if x[k] is not None])
print(f"ALL  CER {mean('cer_strict'):.2%}  CERloose {mean('cer_loose'):.2%}  WER {mean('wer_strict'):.2%}  WERloose {mean('wer_loose'):.2%}  punct {mean('punct_recall'):.1%}  {mean('secs'):.1f}s/page", flush=True)
(B / f"qari_gpu_raw_{sys.argv[1]}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
print("DONE_MARKER", flush=True)
