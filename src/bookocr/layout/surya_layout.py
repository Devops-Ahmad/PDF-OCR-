"""Surya-backed layout detection: typed, ordered page regions.

Surya provides layout classification and reading order in a single call --
each detected block carries a `position` field, its 0-indexed reading order
(https://github.com/datalab-to/surya). This module only produces *structure*
(what kind of region, in what order); PaddleOCR still does the actual Arabic
text recognition (Pass 1 in engines/paddle_engine.py). The two are combined
by `assign_lines_to_layout`: each PaddleOCR line is assigned the kind and
reading order of the Surya block whose box contains it, then the whole page
is re-sorted by (block reading order, line y-position) instead of
PaddleOCR's raw top-to-bottom box sort -- which is what produced the
scrambled-dialogue defect documented in docs/phase0_findings.md.

Label mapping is deliberately conservative: anything Surya doesn't have a
clear analog for maps to "body" rather than being invented or dropped, so no
real content is ever lost because of an unfamiliar label.

Operational note (found 2026-09-23): surya-ocr>=0.20 is not a plain
in-process torch model -- it serves its layout model as a VLM through either
a Docker+GPU vllm backend or a locally-run `llama-server` (llama.cpp)
process. Docker's nvidia runtime wasn't configured on this machine, so this
module bundles its own llama.cpp CUDA build under <project-root>/.tools/llamacpp/
(downloaded from ggml-org/llama.cpp releases, not committed to git -- see
.gitignore) and points Surya at it via env vars, rather than requiring a
system-wide Docker/llama.cpp install. If that directory doesn't exist (e.g.
a fresh clone before running the setup step), Surya falls back to its own
auto-detection, which will error with clear instructions.
"""

from __future__ import annotations

import os
from pathlib import Path

from bookocr.core.interfaces import LayoutDetector
from bookocr.core.types import PageImage, Region
from bookocr.layout.reading_order import order_lines_rtl

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LLAMACPP_TOOLS_DIR = _PROJECT_ROOT / ".tools" / "llamacpp"


def _configure_llamacpp_backend() -> None:
    """Point Surya at our bundled, project-local llama.cpp build if present.
    Uses setdefault so an explicit env var the user already set always wins.
    """
    if not _LLAMACPP_TOOLS_DIR.is_dir():
        return

    server_binaries = list(_LLAMACPP_TOOLS_DIR.glob("llama-b*/llama-server"))
    cudart_dirs = list(_LLAMACPP_TOOLS_DIR.glob("cudart-llama-b*"))
    if not server_binaries:
        return

    os.environ.setdefault("SURYA_INFERENCE_BACKEND", "llamacpp")
    os.environ.setdefault("LLAMA_CPP_BINARY", str(server_binaries[0]))

    lib_dirs = [str(server_binaries[0].parent), *[str(d) for d in cudart_dirs]]
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = ":".join(lib_dirs + ([existing] if existing else []))

_LABEL_MAP = {
    "Text": "body",
    "ListGroup": "body",
    "ListItem": "body",
    "TextInlineMath": "body",
    "Title": "heading",
    "SectionHeader": "heading",
    "PageHeader": "header",
    "PageFooter": "footer",
    "Caption": "caption",
    "Footnote": "footnote",
    "Table": "table",
    "TableOfContents": "body",
    "TableCell": "table",
    "Picture": "illustration",
    "Figure": "illustration",
    "Diagram": "illustration",
    "ChemicalBlock": "illustration",
    "Equation": "body",
    "Code": "body",
    "Form": "body",
    "Bibliography": "body",
    "BlankPage": "body",
}

_ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_PAGE_NUM_CHARS = set("0123456789" + _ARABIC_INDIC_DIGITS + "ivxlcIVXLC-–—. ()[]")

_predictor_singleton = None


def _get_layout_predictor():
    global _predictor_singleton
    if _predictor_singleton is None:
        _configure_llamacpp_backend()
        from surya.layout import LayoutPredictor

        _predictor_singleton = LayoutPredictor()
    return _predictor_singleton


def _looks_like_page_number(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 8:
        return False
    return all(c in _PAGE_NUM_CHARS for c in stripped) and any(c.isdigit() or c in _ARABIC_INDIC_DIGITS for c in stripped)


class SuryaLayoutDetector(LayoutDetector):
    name = "surya"

    def __init__(self, config: dict | None = None):
        self.cfg = config or {}
        try:
            import surya

            self.version = getattr(surya, "__version__", "unknown")
        except Exception:
            self.version = "unknown"

    def detect(self, page: PageImage) -> list[Region]:
        from PIL import Image

        predictor = _get_layout_predictor()
        img = Image.open(page.path).convert("RGB")
        results = predictor([img])
        result = results[0]

        regions = []
        for block in result.bboxes:
            kind = _LABEL_MAP.get(block.label, "body")
            regions.append(
                Region(
                    kind=kind,
                    bbox=tuple(float(v) for v in block.bbox),
                    text="",
                    confidence=getattr(block, "confidence", None),
                    reading_order=block.position,
                )
            )
        return regions


def _bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x0, y0, x1, y1 = bbox
    return (x0 + x1) / 2, (y0 + y1) / 2


def _contains(bbox: tuple[float, float, float, float], point: tuple[float, float]) -> bool:
    x0, y0, x1, y1 = bbox
    px, py = point
    return x0 <= px <= x1 and y0 <= py <= y1


def assign_lines_to_layout(lines: list[Region], layout_regions: list[Region]) -> list[Region]:
    """Combine OCR line boxes (kind='body', has text, no reliable reading
    order) with Surya's typed+ordered blocks. A line with no containing
    layout block is kept as 'body' and sorted after all typed content, on
    the conservative principle that unmatched text is never dropped, only
    demoted -- see the module docstring.
    """
    unmatched_order = max((r.reading_order or 0 for r in layout_regions), default=-1) + 1
    groups: dict[int, list[Region]] = {}

    for line in lines:
        center = _bbox_center(line.bbox)
        matched = next((r for r in layout_regions if _contains(r.bbox, center)), None)

        kind = matched.kind if matched is not None else "body"
        order = matched.reading_order if matched is not None else unmatched_order

        if kind in ("header", "footer") and _looks_like_page_number(line.text):
            kind = "page_number"

        assigned = Region(kind=kind, bbox=line.bbox, text=line.text, confidence=line.confidence, reading_order=order)
        groups.setdefault(order, []).append(assigned)

    out: list[Region] = []
    for order in sorted(groups.keys()):
        out.extend(order_lines_rtl(groups[order]))
    return out
