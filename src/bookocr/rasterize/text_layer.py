"""Conservative per-page PDF text-layer extraction."""

from __future__ import annotations

import re

import pymupdf

from bookocr.core.types import EngineResult, Region
from bookocr.layout.reading_order import order_lines_rtl

_ARABIC = re.compile(r"[\u0600-\u06ff]")


class PDFTextLayerExtractor:
    name = "pdf-text-layer"
    version = pymupdf.__version__

    def __init__(self, config: dict):
        self.config = config

    def extract(self, source_path: str, page_number: int, *, raster_width: int, raster_height: int) -> tuple[EngineResult, dict]:
        with pymupdf.open(source_path) as document:
            page = document[page_number - 1]
            page_rect = page.rect
            blocks = page.get_text("blocks", sort=True)

        scale_x = raster_width / max(float(page_rect.width), 1.0)
        scale_y = raster_height / max(float(page_rect.height), 1.0)
        regions = []
        for block in blocks:
            x0, y0, x1, y1, text = block[:5]
            cleaned = str(text).strip()
            if not cleaned:
                continue
            regions.append(
                Region(
                    kind="body",
                    bbox=(float(x0) * scale_x, float(y0) * scale_y, float(x1) * scale_x, float(y1) * scale_y),
                    text=cleaned,
                )
            )
        regions = order_lines_rtl(regions)
        text = "\n".join(region.text for region in regions)
        non_space = [char for char in text if not char.isspace()]
        character_count = len(non_space)
        word_count = len(text.split())
        arabic_ratio = sum(1 for char in non_space if _ARABIC.match(char)) / max(character_count, 1)
        replacement_ratio = text.count("\ufffd") / max(character_count, 1)
        usable = (
            bool(self.config.get("prefer_usable_text_layer", True))
            and character_count >= int(self.config.get("text_layer_min_chars", 80))
            and word_count >= int(self.config.get("text_layer_min_words", 12))
            and arabic_ratio >= float(self.config.get("text_layer_min_arabic_ratio", 0.5))
            and replacement_ratio <= float(self.config.get("text_layer_max_replacement_ratio", 0.01))
        )
        analysis = {
            "present": bool(text),
            "usable": usable,
            "character_count": character_count,
            "word_count": word_count,
            "arabic_ratio": round(arabic_ratio, 6),
            "replacement_ratio": round(replacement_ratio, 6),
            "region_count": len(regions),
        }
        result = EngineResult(
            text=text,
            confidence=None,
            engine_name=self.name,
            engine_version=self.version,
            regions=regions,
            raw={"text_layer_analysis": analysis},
            model_revision="native-pdf",
            output_format="regions",
            warnings=[] if usable else ["text_layer_rejected"],
        )
        return result, analysis
