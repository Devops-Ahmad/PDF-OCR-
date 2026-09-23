"""Conservative recurring-watermark detection.

A line is only ever classified as a non-content watermark/scan-artifact when
BOTH conditions hold:

1. Position: it sits within the bottom band of the page (scanner/distributor
   stamps and channel handles are placed at the foot of the page).
2. Repetition: a near-identical line recurs across a large share of the
   book's pages.

Position alone would risk stripping a legitimate final line of dialogue that
happens to sit low on the page. Repetition alone would risk stripping a
recurring in-story refrain. Requiring both, and requiring the recurrence
count to scale with book length, is the "conservative rule" the product spec
calls for -- this must never delete real content to be safe.

This runs once, after all pages of a book are OCR'd (it needs to see the
whole book to know what "recurring" means), not per-page.
"""

from __future__ import annotations

import re
import unicodedata


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text.strip())
    return re.sub(r"\s+", " ", text)


def detect_watermark_lines(
    pages: list[dict],
    *,
    bottom_band_fraction: float = 0.15,
    min_page_occurrences: int = 3,
    min_page_fraction: float = 0.3,
) -> set[str]:
    """`pages` are the parsed records from pages.jsonl (must include
    `page_height` and `regions` with `bbox`/`text`). Returns the set of
    normalized line strings judged to be recurring non-content artifacts.
    """
    total_pages = len(pages)
    if total_pages == 0:
        return set()

    bottom_line_pages: dict[str, set[int]] = {}

    for page in pages:
        height = page.get("page_height") or 0
        if not height:
            continue
        threshold_y = (1 - bottom_band_fraction) * height
        for region in page.get("regions", []):
            text = _normalize(region.get("text", ""))
            if not text:
                continue
            bbox = region.get("bbox") or [0, 0, 0, 0]
            y0 = bbox[1] if len(bbox) > 1 else 0
            if y0 >= threshold_y:
                bottom_line_pages.setdefault(text, set()).add(page["page"])

    min_required = max(min_page_occurrences, round(min_page_fraction * total_pages))
    return {text for text, page_set in bottom_line_pages.items() if len(page_set) >= min_required}
