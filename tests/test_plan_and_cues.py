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


@pytest.mark.parametrize("raw,cond,mode,cooldown", [
    ("demo://keynote every time they mention the camera", "they mention the camera", "every", 5),
    ("demo://keynote each time they say price", "they say price", "every", 5),
    ("demo://keynote whenever the crowd cheers", "the crowd cheers", "every", 5),
    ("demo://keynote when they mention the camera, at most every 10 minutes", "they mention the camera", "every", 10),
    ("demo://keynote when they say liftoff --every 3", "they say liftoff", "every", 3),  # old flag still works
    ("demo://keynote when they announce the price", "they announce the price", "once", 5),
])
def test_plain_word_repeat_mode(raw, cond, mode, cooldown):
    req = plan.parse_watch(raw)
    assert (req.condition, req.mode, req.cooldown_min) == (cond, mode, cooldown)
    assert req.mode_set == (mode == "every")


@pytest.mark.parametrize("raw,how", [
    ("demo://keynote when they announce the price", "watch"),
    ("demo://keynote every time they mention the camera", "watch"),
    ("demo://keynote where do they talk about the battery", "search"),
    ("demo://keynote what time do they announce the price?", "search"),
    ("demo://keynote when do they announce the price", "search"),
    ("demo://keynote find the price announcement", "search"),
    ("demo://keynote the price", "unclear"),
])
def test_phrasing(raw, how):
    assert plan.parse_watch(raw).phrasing == how


def test_plan_model_mode_and_cooldown_are_read():
    p = plan.Plan.from_dict({"summary": "s", "cue_phrases": ["camera"], "mode": "every", "cooldown_min": 0.2})
    assert p.mode == "every" and p.cooldown_min == 1.0
    assert plan.Plan.from_dict({"summary": "s", "cue_phrases": ["x"], "mode": "sometimes"}).mode is None
    assert {"mode", "cooldown_min", "screen_label"} <= set(plan.PLAN_SCHEMA["required"])


def test_plan_card_has_no_regex():
    p = plan.Plan.from_dict({"summary": "s", "cue_phrases": ["the price is", "starts at", "dollars", "799", "usd"],
                             "ocr_patterns": [r"\$\s?\d{1,3}(?:[.,]\d{2})?", r"(?i)price"], "screen_label": "prices"})
    card = plan.describe(p)
    assert card == "Listening for: “the price is”, “starts at”, “dollars”, “799”… · Watching the screen for: prices"
    assert "\\" not in card and "$" not in card and "(?" not in card
    unlabeled = plan.Plan.from_dict({"summary": "s", "cue_phrases": ["x"], "ocr_patterns": [r"\d+"]})
    assert plan.describe(unlabeled).endswith("Watching the screen for: related text")
