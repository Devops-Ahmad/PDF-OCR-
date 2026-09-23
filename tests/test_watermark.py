from bookocr.postprocess.watermark import detect_watermark_lines


def _page(page_num, height, lines):
    return {
        "page": page_num,
        "page_height": height,
        "regions": [{"text": text, "bbox": [0, y0, 100, y0 + 5]} for text, y0 in lines],
    }


def test_recurring_bottom_line_is_flagged():
    pages = [_page(i, 100, [("t.me/somechannel", 95), (f"unique line {i}", 50)]) for i in range(1, 6)]
    watermark = detect_watermark_lines(pages, bottom_band_fraction=0.15, min_page_occurrences=3, min_page_fraction=0.3)
    assert "t.me/somechannel" in watermark
    assert not any(f"unique line {i}" in watermark for i in range(1, 6))


def test_repeated_line_not_in_bottom_band_is_not_flagged():
    # A refrain that recurs but sits in the middle of the page must survive --
    # position AND repetition are both required, not either alone.
    pages = [_page(i, 100, [("she said nothing", 50)]) for i in range(1, 6)]
    watermark = detect_watermark_lines(pages, bottom_band_fraction=0.15, min_page_occurrences=3, min_page_fraction=0.3)
    assert watermark == set()


def test_bottom_line_appearing_once_is_not_flagged():
    # Position alone (a single page's real closing line sitting low) must
    # not be enough either.
    pages = [_page(1, 100, [("the end", 95)])] + [_page(i, 100, [(f"line {i}", 50)]) for i in range(2, 6)]
    watermark = detect_watermark_lines(pages, bottom_band_fraction=0.15, min_page_occurrences=3, min_page_fraction=0.3)
    assert "the end" not in watermark


def test_empty_book_returns_empty_set():
    assert detect_watermark_lines([]) == set()
