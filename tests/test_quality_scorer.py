from bookocr.core.types import EngineResult, PageImage, QualityTier
from bookocr.quality.scorer import HeuristicQualityEvaluator

_WEIGHTS = {
    "engine_confidence": 0.5,
    "arabic_char_ratio": 0.2,
    "dictionary_hit_rate": 0.15,
    "length_anomaly": 0.1,
    "garbage_symbol_ratio": 0.05,
}
_PAGE = PageImage(book_id="book", page_number=1, path="/dev/null", width=100, height=100, dpi=300)


def _evaluator():
    return HeuristicQualityEvaluator({"weights": _WEIGHTS, "thresholds": {"high": 80, "medium": 60}})


def test_degenerate_word_repetition_forces_critical_even_with_high_engine_confidence():
    # Reproduces a real failure: a VLM-based OCR engine (QARI-OCR) fell into
    # a repetition loop, cycling three real Arabic names dozens of times.
    # Arabic-char-ratio and dictionary-style signals alone would score this
    # well -- only the repetition check catches it.
    repeated_cluster = "(رارسأ) (العجولا) (أرية) ...؟ " * 15
    result = EngineResult(text=repeated_cluster, confidence=95.0, engine_name="qari-ocr", engine_version="x")
    report = _evaluator().score(result, _PAGE)
    assert report.tier == QualityTier.CRITICAL
    assert "degenerate_repetition" in report.warnings


def test_repeated_line_also_forces_critical():
    text = "\n".join(["نفس السطر بالضبط"] * 5)
    result = EngineResult(text=text, confidence=90.0, engine_name="test", engine_version="x")
    report = _evaluator().score(result, _PAGE)
    assert report.tier == QualityTier.CRITICAL


def test_normal_prose_is_not_flagged_as_repetitive():
    text = (
        "خرج الرجل وفي يده رأس الراعي وقال\n"
        "عودي من حيث أتيت يا امرأة فليس لك مكان بيننا\n"
        "رفعت أفسار رأسها ونظرت لرأس زوجها بيد الرجل وقالت\n"
        "أقسم بعدد النجوم في السماء أني سأمسك برأس سيدك\n"
    )
    result = EngineResult(text=text, confidence=90.0, engine_name="test", engine_version="x")
    report = _evaluator().score(result, _PAGE)
    assert "degenerate_repetition" not in report.warnings
    assert report.tier != QualityTier.CRITICAL
