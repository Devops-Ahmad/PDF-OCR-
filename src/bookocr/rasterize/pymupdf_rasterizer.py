"""PDF -> page image rasterization via PyMuPDF.

PyMuPDF was chosen over shelling out to poppler's pdftoppm because it gives
per-page exceptions (a single corrupted page doesn't kill the whole book),
avoids a subprocess per page (39k+ pages across the library), and returns
page counts/metadata without a second tool invocation.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from bookocr.core.interfaces import DocumentSource, Rasterizer
from bookocr.core.types import PageImage


class CorruptedPageError(Exception):
    def __init__(self, page_number: int, reason: str):
        super().__init__(f"page {page_number}: {reason}")
        self.page_number = page_number
        self.reason = reason


class PyMuPDFSource(DocumentSource):
    def page_count(self, source_path: str) -> int:
        with pymupdf.open(source_path) as doc:
            return doc.page_count

    def is_readable(self, source_path: str) -> bool:
        try:
            with pymupdf.open(source_path) as doc:
                return doc.page_count > 0
        except Exception:
            return False


class PyMuPDFRasterizer(Rasterizer):
    def render_page(self, source_path: str, page_number: int, dpi: int, out_dir: str) -> PageImage:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        out_path = out / f"page_{page_number:05d}.png"

        try:
            with pymupdf.open(source_path) as doc:
                if not (1 <= page_number <= doc.page_count):
                    raise CorruptedPageError(page_number, f"out of range (book has {doc.page_count} pages)")
                page = doc[page_number - 1]
                pix = page.get_pixmap(dpi=dpi)
                pix.save(str(out_path))
                return PageImage(
                    book_id="book",
                    page_number=page_number,
                    path=str(out_path),
                    width=pix.width,
                    height=pix.height,
                    dpi=dpi,
                )
        except CorruptedPageError:
            raise
        except Exception as e:  # pymupdf raises plain RuntimeError/ValueError on malformed pages
            raise CorruptedPageError(page_number, f"{type(e).__name__}: {e}") from e
