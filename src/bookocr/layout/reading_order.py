"""Row-clustering + RTL ordering: layout-model-independent.

Split out from surya_layout.py because this fix has nothing to do with
Surya specifically -- it corrects a PaddleOCR detection artifact (one
justified Arabic line coming back as several boxes with slightly different
baselines) and should apply whenever OCR lines need ordering, including the
`layout.engine: "none"` fallback path that has no typed layout blocks at
all. See docs/phase0_findings.md for how this was root-caused.
"""

from __future__ import annotations

from bookocr.core.types import Region


def cluster_rows(lines: list[Region]) -> list[list[Region]]:
    """Group lines into visual rows by y-overlap. Needed because PaddleOCR's
    detector sometimes splits one justified/RTL line into several boxes with
    slightly different baselines -- those boxes must be treated as one row,
    not sequential lines.
    """
    rows: list[list[Region]] = []
    for line in sorted(lines, key=lambda r: r.bbox[1]):
        y0, y1 = line.bbox[1], line.bbox[3]
        center = (y0 + y1) / 2
        for row in rows:
            row_y0 = min(r.bbox[1] for r in row)
            row_y1 = max(r.bbox[3] for r in row)
            if row_y0 <= center <= row_y1:
                row.append(line)
                break
        else:
            rows.append([line])
    rows.sort(key=lambda row: min(r.bbox[1] for r in row))
    return rows


def order_lines_rtl(lines: list[Region]) -> list[Region]:
    """Order a set of same-block (or whole-page, if no layout blocks exist)
    lines: top-to-bottom by row, then right-to-left (descending x) within a
    row. Verified against a real scrambled page -- this recovers the correct
    fragment order in every row-splitting case found there.
    """
    ordered: list[Region] = []
    for row in cluster_rows(lines):
        row.sort(key=lambda r: r.bbox[0], reverse=True)
        ordered.extend(row)
    return ordered
