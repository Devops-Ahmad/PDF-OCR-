from bookocr.eval.metrics import cer, page_report, punctuation_recall, wer


def test_identical_text_has_zero_error():
    assert cer("مرحبا بك", "مرحبا بك") == 0.0
    assert wer("مرحبا بك", "مرحبا بك") == 0.0


def test_whitespace_differences_are_ignored():
    assert cer("مرحبا  بك\nيا صديقي", "مرحبا بك يا صديقي") == 0.0


def test_single_character_substitution_is_one_over_length():
    assert abs(cer("abcd", "abxd") - 0.25) < 1e-9


def test_loose_view_ignores_diacritics_but_strict_does_not():
    ref, hyp = "طرقًا خفيفًا", "طرقا خفيفا"
    assert cer(ref, hyp) > 0
    assert cer(ref, hyp, loose=True) == 0.0


def test_punctuation_recall_catches_dropped_marks():
    ref = "قال: (نعم)!"
    assert punctuation_recall(ref, "قال نعم") == 0.0
    assert punctuation_recall(ref, ref) == 1.0
    assert punctuation_recall("بلا ترقيم", "بلا ترقيم") is None


def test_page_report_has_all_fields():
    r = page_report("قال: نعم", "قال نعم")
    assert set(r) == {"cer_strict", "cer_loose", "wer_strict", "wer_loose", "punct_recall", "ref_chars"}
