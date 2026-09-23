from bookocr.cli.main import _parse_sets


def test_numeric_overrides_are_coerced_not_left_as_strings():
    # Real bug: `--set quality.thresholds.high=99` used to store the string
    # "99", which later crashed deep in the scorer comparing float >= str.
    out = _parse_sets(("quality.thresholds.high=99", "quality.thresholds.medium=95.5"))
    assert out["quality.thresholds.high"] == 99
    assert isinstance(out["quality.thresholds.high"], int)
    assert out["quality.thresholds.medium"] == 95.5


def test_boolean_overrides_are_coerced():
    out = _parse_sets(("ocr.escalation.enabled=false",))
    assert out["ocr.escalation.enabled"] is False


def test_string_overrides_pass_through():
    out = _parse_sets(("ocr.primary.device=cpu",))
    assert out["ocr.primary.device"] == "cpu"
