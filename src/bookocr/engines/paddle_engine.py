"""PaddleOCR-backed primary recognition engine (Pass 1).

Two non-obvious things this module works around, both discovered during
Phase 0 benchmarking on 2026-09-23 — see docs/SETUP.md:

1. paddlepaddle>=3.3 crashes on CPU inference with every PP-OCRv5 detection
   model the moment oneDNN is engaged (the default). pyproject.toml pins
   paddlepaddle==3.1.0 to avoid this; do not remove that pin without
   re-verifying on a real page first.
2. `lang='ar'` is the correct PaddleOCR language code; 'arabic' raises
   ValueError. It resolves to text_detection=PP-OCRv5_server_det and
   text_recognition=arabic_PP-OCRv5_mobile_rec.

No GPU wheel of paddlepaddle exists for this machine's CUDA 13.2 driver at
matching Python/CUDA versions (only paddlepaddle_gpu 2.6.1 cu112-cu120
wheels are published, which predate the PP-OCRv5 model format). This engine
therefore runs on CPU. GPU acceleration for the pipeline instead comes from
the torch-based stages (Surya, QARI-OCR) planned for later phases.
"""

from __future__ import annotations

import time

from bookocr.core.interfaces import OCREngine
from bookocr.core.types import EngineResult, PageImage, Region

_engine_singleton = None


def _get_paddle_ocr(cfg: dict):
    global _engine_singleton
    if _engine_singleton is None:
        from paddleocr import PaddleOCR

        det_name = cfg.get("text_detection_model_name")
        rec_name = cfg.get("text_recognition_model_name")
        # PaddleOCR warns and ignores `lang` if explicit model names are also
        # given, so only pass one or the other.
        kwargs = (
            {"text_detection_model_name": det_name, "text_recognition_model_name": rec_name}
            if det_name and rec_name
            else {"lang": cfg.get("lang", "ar")}
        )
        _engine_singleton = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            **kwargs,
        )
    return _engine_singleton


class PaddleOCREngine(OCREngine):
    name = "paddleocr"

    def __init__(self, config: dict):
        self.cfg = config
        import paddleocr

        self.version = paddleocr.__version__

    def recognize(self, page: PageImage, regions: list[Region] | None = None) -> EngineResult:
        ocr = _get_paddle_ocr(self.cfg)
        t0 = time.time()
        results = list(ocr.predict(page.path))
        duration = time.time() - t0

        lines: list[str] = []
        confidences: list[float] = []
        out_regions: list[Region] = []

        for res in results:
            texts = res.get("rec_texts", [])
            scores = res.get("rec_scores", [])
            boxes = res.get("rec_boxes", [])
            for order, (text, score) in enumerate(zip(texts, scores)):
                lines.append(text)
                confidences.append(score)
                bbox = tuple(float(v) for v in boxes[order]) if order < len(boxes) else (0.0, 0.0, 0.0, 0.0)
                out_regions.append(
                    Region(kind="body", bbox=bbox, text=text, confidence=float(score) * 100, reading_order=order)
                )

        avg_conf = (sum(confidences) / len(confidences) * 100) if confidences else 0.0

        return EngineResult(
            text="\n".join(lines),
            confidence=avg_conf,
            engine_name=self.name,
            engine_version=self.version,
            regions=out_regions,
            raw={"duration_s": duration, "line_count": len(lines)},
        )
