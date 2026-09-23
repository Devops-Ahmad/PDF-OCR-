"""Compare PaddleOCR detection/recognition settings on the synthetic
benchmark pages (see synth_benchmark.py, which must have been run first to
produce pages/ and reference.json). Measures the primary engine only --
no layout model, no escalation -- so a setting's effect on raw recognition
quality is isolated from everything downstream. Footer/page-number/
watermark lines are dropped by position (bottom band), the same thing the
layout stage does in the real pipeline, so they don't count as errors.

Usage: python scripts/engine_compare.py --bench DIR [--only NAME ...]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from bookocr.core.types import Region
from bookocr.eval.metrics import page_report
from bookocr.layout.reading_order import order_lines_rtl

DET, REC = "PP-OCRv5_server_det", "arabic_PP-OCRv5_mobile_rec"

CONFIGS = {
    "baseline": {},
    "unclip2.0": {"text_det_unclip_ratio": 2.0},
    "unclip2.5": {"text_det_unclip_ratio": 2.5},
    "thresh_low": {"text_det_thresh": 0.2, "text_det_box_thresh": 0.4},
    "unclip2.0+thresh_low": {"text_det_unclip_ratio": 2.0, "text_det_thresh": 0.2, "text_det_box_thresh": 0.4},
    "big_input": {"text_det_limit_side_len": 2400, "text_det_limit_type": "max"},
    "big+unclip2.0+thresh_low": {"text_det_limit_side_len": 2400, "text_det_limit_type": "max", "text_det_unclip_ratio": 2.0, "text_det_thresh": 0.2, "text_det_box_thresh": 0.4},
}


def run_config(name, params, pages, refs, verbose_pages=False):
    from paddleocr import PaddleOCR

    ocr = PaddleOCR(
        text_detection_model_name=DET,
        text_recognition_model_name=REC,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        **params,
    )
    t0 = time.time()
    rows = []
    for n, path in pages:
        res = list(ocr.predict(str(path)))[0]
        h = res["doc_preprocessor_res"]["output_img"].shape[0] if "doc_preprocessor_res" in res and res["doc_preprocessor_res"] else 2149
        lines = []
        for i, (t, box) in enumerate(zip(res.get("rec_texts", []), res.get("rec_boxes", []))):
            if box[1] > 0.85 * h:  # footer band: page number + watermark stamps
                continue
            lines.append(Region(kind="body", bbox=tuple(float(v) for v in box), text=t, reading_order=i))
        hyp = "\n".join(r.text for r in order_lines_rtl(lines))
        rows.append(page_report(refs[str(n)], hyp))
    dt = time.time() - t0

    def mean(k):
        v = [r[k] for r in rows if r[k] is not None]
        return sum(v) / len(v)

    print(f"{name:<28} CER {mean('cer_strict'):6.2%}  WER {mean('wer_strict'):6.2%}  WERloose {mean('wer_loose'):6.2%}  punct {mean('punct_recall'):6.1%}  ({dt/len(rows):.1f}s/page)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", required=True)
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    bench = Path(args.bench)
    refs = json.loads((bench / "reference.json").read_text(encoding="utf-8"))
    pages = sorted((int(p.name.split("_")[1]), p) for p in (bench / "pages").glob("page_*.png"))
    for name, params in CONFIGS.items():
        if args.only and name not in args.only:
            continue
        run_config(name, params, pages, refs)


if __name__ == "__main__":
    main()
