import pytest

from wake_me_when.wakeme import cues, plan


@pytest.mark.parametrize("raw,url,cond,mode", [
    ("demo://keynote when they announce the price", "demo://keynote", "they announce the price", "once"),
    ("when they announce the price https://youtu.be/abc", "https://youtu.be/abc", "they announce the price", "once"),
    ("they say liftoff https://twitch.tv/x --every 3", "https://twitch.tv/x", "they say liftoff", "every"),
    ("<https://a.example/live.m3u8> if the score changes", "https://a.example/live.m3u8", "the score changes", "once"),
])
def test_parse_watch_orders(raw, url, cond, mode):
    req = plan.parse_watch(raw)
    assert (req.url, req.condition, req.mode) == (url, cond, mode)


def test_parse_watch_cooldown_and_errors():
    assert plan.parse_watch("demo://launch when liftoff --every 7").cooldown_min == 7
    with pytest.raises(ValueError, match="stream link"):
        plan.parse_watch("when they announce the price")
    with pytest.raises(ValueError, match="wait for"):
        plan.parse_watch("demo://keynote when")


def test_make_plan_with_stubbed_llm_sanitises_output():
    seen = {}

    def llm(instructions, text, schema):
        seen["text"] = text
        return {"summary": "Price is announced", "cue_phrases": ["Price", "price", "dollars", "  "],
                "ocr_patterns": ["$(", r"\$\d+"], "visual_question": "null", "negative_cues": ["In a moment"],
                "confirm": "twice", "min_confidence": 2}

    p = plan.make_plan(llm, "they announce the price")
    assert "they announce the price" in seen["text"]
    assert p.cue_phrases == ["price", "dollars"]          # lower-cased, deduped, blanks dropped
    assert p.ocr_patterns[1] == r"\$\d+" and p.ocr_patterns[0] == r"\$\("  # invalid regex escaped
    assert p.visual_question is None and p.confirm == "twice" and p.min_confidence == 0.9
    assert p.needs_frames


def test_make_plan_rejects_empty():
    with pytest.raises(RuntimeError):
        plan.make_plan(lambda *a: {"cue_phrases": []}, "x y z")


def test_cue_matching_exact_fuzzy_phonetic_and_negatives():
    rows = [(1, "Acme Phone X starts at just $799."), (2, "We'll get to that in a moment."),
            (3, "the new Nvidea chip"), (4, "nothing here")]
    res = cues.match(rows, ["starts at", "nvidia", "pricing"], ["in a moment"])
    assert res.cues == ["starts at", "nvidia"]
    assert res.row_ids == [1, 3]
    assert res.negatives == ["in a moment"]
    assert not res.strong                       # a negative cue blocks unconfirmed alerts


def test_strong_needs_two_cues_or_ocr():
    one = cues.match([(1, "the price is out")], ["price", "dollars"])
    two = cues.match([(1, "the price is ninety dollars")], ["price", "dollars"])
    ocr = cues.match([], [], None, "SALE $799 TODAY", [r"\$\d+"])
    assert one.hit and not one.strong
    assert two.strong
    assert ocr.strong and ocr.ocr == [r"\$\d+"]


def test_soundex_groups_asr_spellings():
    assert cues.soundex("Kubernetes") == cues.soundex("Kubernetis")
    assert cues.soundex("price") != cues.soundex("prize") or cues.phrase_in("price", "prize")  # fuzzy is fine
