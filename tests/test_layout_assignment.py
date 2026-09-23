from bookocr.core.types import Region
from bookocr.layout.surya_layout import assign_lines_to_layout, _looks_like_page_number


def test_lines_inherit_layout_kind_and_order():
    # A page with a header block above a body block -- OCR lines detected
    # top-to-bottom (PaddleOCR's natural order) already match visual order
    # here, so this mainly checks kind inheritance.
    layout = [
        Region(kind="header", bbox=(0, 0, 100, 20), reading_order=0),
        Region(kind="body", bbox=(0, 20, 100, 200), reading_order=1),
    ]
    lines = [
        Region(kind="body", bbox=(10, 5, 90, 15), text="chapter one", reading_order=0),
        Region(kind="body", bbox=(10, 30, 90, 40), text="once upon a time", reading_order=1),
    ]
    result = assign_lines_to_layout(lines, layout)
    kinds = {r.text: r.kind for r in result}
    assert kinds["chapter one"] == "header"
    assert kinds["once upon a time"] == "body"


def test_fixes_scrambled_reading_order():
    # Two side-by-side layout blocks (e.g. a narrow indented dialogue column
    # split awkwardly): block A should read entirely before block B even
    # though a naive top-to-bottom box sort would interleave their lines.
    layout = [
        Region(kind="body", bbox=(0, 0, 50, 100), reading_order=0),
        Region(kind="body", bbox=(50, 0, 100, 100), reading_order=1),
    ]
    # PaddleOCR's raw order interleaves by y-position across both columns.
    lines = [
        Region(kind="body", bbox=(5, 10, 45, 20), text="A1", reading_order=0),
        Region(kind="body", bbox=(55, 5, 95, 15), text="B1", reading_order=1),
        Region(kind="body", bbox=(5, 30, 45, 40), text="A2", reading_order=2),
        Region(kind="body", bbox=(55, 25, 95, 35), text="B2", reading_order=3),
    ]
    result = assign_lines_to_layout(lines, layout)
    assert [r.text for r in result] == ["A1", "A2", "B1", "B2"]


def test_rtl_line_split_into_multiple_boxes_is_reassembled_in_order():
    # Reproduces a real bug found on a live page: PaddleOCR's detector split
    # one justified Arabic (RTL) line into three boxes with slightly
    # different baselines. A naive ascending-y0 tie-break scrambled them;
    # the fix is row-clustering by y-overlap, then descending-x (RTL) order
    # within the row. Real bbox coordinates from that page.
    layout = [Region(kind="body", bbox=(0, 0, 1254, 2149), reading_order=0)]
    lines = [
        Region(kind="body", bbox=(258, 309, 1200, 383), text="حاجياتها وتوجهت لجبلآريان وصلت أفسار", reading_order=0),
        Region(kind="body", bbox=(130, 310, 258, 385), text="للجبل", reading_order=1),
        Region(kind="body", bbox=(50, 325, 122, 379), text="بعد", reading_order=2),
    ]
    result = assign_lines_to_layout(lines, layout)
    assert [r.text for r in result] == ["حاجياتها وتوجهت لجبلآريان وصلت أفسار", "للجبل", "بعد"]


def test_unmatched_line_is_kept_not_dropped():
    layout = [Region(kind="body", bbox=(0, 0, 50, 50), reading_order=0)]
    lines = [Region(kind="body", bbox=(60, 60, 70, 70), text="stray line", reading_order=0)]
    result = assign_lines_to_layout(lines, layout)
    assert len(result) == 1
    assert result[0].text == "stray line"
    assert result[0].kind == "body"  # demoted to sort last, never discarded


def test_page_number_heuristic_only_fires_inside_header_or_footer():
    layout = [
        Region(kind="footer", bbox=(0, 90, 100, 100), reading_order=0),
        Region(kind="body", bbox=(0, 0, 100, 90), reading_order=1),
    ]
    lines = [
        Region(kind="body", bbox=(45, 92, 55, 98), text="٣١", reading_order=0),
        Region(kind="body", bbox=(10, 10, 90, 20), text="31", reading_order=1),  # "31" as real narrative content
    ]
    result = assign_lines_to_layout(lines, layout)
    by_text = {r.text: r.kind for r in result}
    assert by_text["٣١"] == "page_number"
    assert by_text["31"] == "body"  # same-looking text, but not in a footer -> not reclassified


def test_looks_like_page_number():
    assert _looks_like_page_number("٣١")
    assert _looks_like_page_number("42")
    assert _looks_like_page_number("- 12 -")
    assert not _looks_like_page_number("لكنه رجع")
    assert not _looks_like_page_number("")
