from wake_me_when.wakeme.judge import Debouncer, build_judge_input, map_evidence, parse_verdict


def test_high_confidence_fires_once_mode():
    d = Debouncer(min_conf=0.6)
    assert d.judge(True, 0.9, [5]).action == "fire"
    assert d.last_fire_row == 5


def test_low_confidence_below_min_is_ignored():
    d = Debouncer(min_conf=0.6)
    assert d.judge(True, 0.5, [5]).action == "none"
    assert d.pending is None


def test_overlapping_windows_never_count_as_two_positives():
    d = Debouncer(min_conf=0.6)
    assert d.judge(True, 0.7, [10]).action == "pending"
    # the next overlapping window cites the same row again: still pending, never a fire
    assert d.judge(True, 0.7, [10]).action == "pending"
    assert d.judge(True, 0.75, [10]).action == "pending"
    # new evidence (a row the first positive did not cite) confirms
    dec = d.judge(True, 0.7, [10, 11])
    assert dec.action == "fire" and dec.row_ids == [10, 11]


def test_same_rows_confirmed_by_a_window_that_saw_newer_speech():
    # live run (LiveNOW FOX, "taxes or government spending", confirm=twice): the judge cited rows
    # 274-276 nine times at 0.90-0.95 while the window moved on; that must confirm, not stay pending
    d = Debouncer(min_conf=0.6, confirm="twice")
    assert d.judge(True, 0.95, [274, 275, 276], newest_row=278).action == "pending"
    # the same window judged again (cue re-trigger, no new speech) is not a confirmation
    assert d.judge(True, 0.93, [274, 275, 276], newest_row=278).action == "pending"
    dec = d.judge(True, 0.93, [274, 275, 276], newest_row=284)
    assert dec.action == "fire" and dec.row_ids == [274, 275, 276]


def test_negative_between_positives_resets_confirmation():
    d = Debouncer(min_conf=0.6)
    d.judge(True, 0.7, [10])
    d.judge(False, 0.2, [])
    assert d.judge(True, 0.7, [11]).action == "pending"


def test_confirm_twice_requires_second_positive_even_when_confident():
    d = Debouncer(min_conf=0.6, confirm="twice")
    assert d.judge(True, 0.99, [3]).action == "pending"
    assert d.judge(True, 0.99, [4]).action == "fire"


def test_every_mode_never_refires_on_evidence_before_last_fire():
    d = Debouncer(min_conf=0.6)
    assert d.judge(True, 0.95, [20]).action == "fire"
    # judge re-cites old rows (e.g. a window that still overlaps the fired moment)
    assert d.judge(True, 0.95, [18, 20]).action == "none"
    assert d.judge(True, 0.95, [30]).action == "fire"


def test_not_it_suppresses_rows_and_resumes():
    d = Debouncer(min_conf=0.6)
    d.judge(True, 0.95, [7])
    d.reject([7])
    assert d.judge(True, 0.95, [7]).action == "none"
    assert d.judge(True, 0.95, [9]).action == "fire"


def test_stage_a_only_fires_unconfirmed_on_strong_evidence():
    d = Debouncer()
    assert d.stage_a_only(False, [4]).action == "none"
    dec = d.stage_a_only(True, [4])
    assert dec.action == "fire" and dec.unconfirmed


def test_map_evidence_trusts_only_window_ids_then_falls_back_to_quote():
    rows = [(41, "Battery life is great."), (42, "It starts at just seven hundred and ninety nine dollars."),
            (43, "Pre-orders open Friday.")]
    assert map_evidence({"evidence_row_ids": [42, 999, "x"]}, rows) == [42]
    # ids missing or hallucinated -> locate the quote in the window instead of trusting a time
    assert map_evidence({"evidence_row_ids": [7], "evidence_quote": "seven hundred and ninety nine"}, rows) == [42]
    assert map_evidence({"evidence_row_ids": [], "evidence_quote": "completely unrelated words"}, rows) == []


def test_build_judge_input_includes_negatives_and_ocr_and_caps_length():
    rows = [(i, f"line {i} " + "x" * 50) for i in range(500)]
    text = build_judge_input("cond", rows, "ON SCREEN $799", ["the earlier tease"], max_chars=2000)
    assert "[499]" in text and "[0]" not in text
    assert "ON SCREEN $799" in text and "the earlier tease" in text


def test_parse_verdict_clamps():
    assert parse_verdict({"matched": True, "confidence": 3, "evidence_quote": "q"}) == (True, 1.0, "q")
    assert parse_verdict("garbage") == (False, 0.0, "")
