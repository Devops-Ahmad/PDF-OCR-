from bookocr.core.types import Region
from bookocr.layout.reading_order import order_lines_rtl


def test_order_lines_rtl_with_no_layout_model():
    # Same real-page bug as test_layout_assignment's RTL test, but exercised
    # through the layout-independent path used when layout.engine == "none"
    # or a layout-model call fails mid-page.
    lines = [
        Region(kind="body", bbox=(258, 309, 1200, 383), text="حاجياتها وتوجهت لجبلآريان وصلت أفسار"),
        Region(kind="body", bbox=(130, 310, 258, 385), text="للجبل"),
        Region(kind="body", bbox=(50, 325, 122, 379), text="بعد"),
    ]
    result = order_lines_rtl(lines)
    assert [r.text for r in result] == ["حاجياتها وتوجهت لجبلآريان وصلت أفسار", "للجبل", "بعد"]


def test_order_lines_rtl_preserves_normal_top_to_bottom_paragraph():
    lines = [
        Region(kind="body", bbox=(0, 100, 500, 130), text="line two"),
        Region(kind="body", bbox=(0, 0, 500, 30), text="line one"),
        Region(kind="body", bbox=(0, 200, 500, 230), text="line three"),
    ]
    result = order_lines_rtl(lines)
    assert [r.text for r in result] == ["line one", "line two", "line three"]
