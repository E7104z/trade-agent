"""TRADERS_LOG_SPEC_v1 Phase 2 -- slot rendering and the validator. One rule
per test, each with a minimal draft."""
from __future__ import annotations

import random
import string

import pytest

from agent import traders_log as tl


def _slot(name, value, scope):
    return {"name": name, "value": value, "scope": scope, "source": "test", "label": name}


FACTS = {
    "slots": {s["name"]: s for s in (
        _slot("day.net_r", "+0.40R", "one_session"),
        _slot("deficit.Q3", "Q3 (question): 5/8", "count"),
        _slot("calibration.conviction_3", "conviction 3: n 9, 56% winners", "cumulative"),
        _slot("verdict.calibration_q3", "Calibration verdict (computed, Q3): not yet computable.",
              "computed_verdict"),
        _slot("menu.Q3", "Q3", "count"),
        _slot("menu.cell", "morning:x=y", "count"),
    )},
    "menu": {"menu.Q3": "Q3", "menu.cell": "morning:x=y"},
    "menu_item_ids": ["Q3", "morning:x=y", "no change"],
}

CANT, HAPPENED, BELIEF = ("I still owe the scoreboard {{deficit.Q3}}.",
                          "Net {{day.net_r}}. One session, not evidence. So far {{calibration.conviction_3}}. "
                          "The system says {{verdict.calibration_q3}}",
                          "I suspect the open was noisy.")
TOMORROW = "Tomorrow: {{menu.Q3}} — take the cleanest real setup that feeds it."


def draft(cant=CANT, happened=HAPPENED, belief=BELIEF, tomorrow=TOMORROW, heads=tl.HEADINGS):
    parts = [heads[0], "", cant, "", heads[1], "", happened, "", heads[2], "", belief, ""]
    return "\n".join(parts + ([tomorrow] if tomorrow is not None else []))


def _bad(raw, needle):
    rendered, v = tl.render(raw, FACTS)
    assert rendered == "" and v, raw
    assert any(needle in x for x in v), v


# ------------------------------------------------------------------ good path

def test_a_well_formed_log_renders_byte_stable():
    a, va = tl.render(draft(), FACTS)
    b, vb = tl.render(draft(), FACTS)
    assert va == vb == [] and a == b
    assert "Net +0.40R (one session)." in a                 # renderer adds the label
    assert "So far conviction 3: n 9, 56% winners." in a    # cumulative: no suffix
    assert "(one session) (one session)" not in a
    assert '"Calibration verdict (computed, Q3): not yet computable."' in a
    assert "Q3 (question): 5/8" in a and a.endswith("Tomorrow: Q3 — take the cleanest real setup that feeds it.")
    assert "{{" not in a


def test_no_change_is_a_valid_tomorrow():
    assert tl.render(draft(tomorrow="Tomorrow: no change"), FACTS)[1] == []


def test_a_code_fence_and_curly_apostrophe_are_tolerated():
    raw = "```\n" + draft().replace("can't", "can’t") + "\n```"
    assert tl.render(raw, FACTS)[1] == []


# ---------------------------------------------------------------------- slots

def test_unknown_slot():
    _bad(draft(happened="Net {{day.gross_r}}."), "unknown slot {{day.gross_r}}")


def test_malformed_slot_token():
    _bad(draft(happened="Net {{ day net }}."), "malformed slot token")


@pytest.mark.parametrize("num", ["13", "3/20", "0.5", "1R", "2026", "10:30"])
def test_digit_outside_a_slot(num):
    _bad(draft(happened=f"Net {{{{day.net_r}}}} after {num} of it."), "digit outside a slot")


@pytest.mark.parametrize("word", ["thirteen", "a dozen", "the third session", "Twenty"])
def test_number_words_past_two(word):
    _bad(draft(happened=f"Net {{{{day.net_r}}}} with {word} misses."), "number written in words")


def test_one_and_two_in_prose_are_fine():
    assert tl.render(draft(happened="Net {{day.net_r}}. One or two names moved."), FACTS)[1] == []


# --------------------------------------------------------------------- belief

def test_digit_in_the_belief_section():
    _bad(draft(belief="I suspect 3 names faded."), "belief section may contain no digit")


def test_slot_in_the_belief_section():
    _bad(draft(belief="I suspect {{day.net_r}} was luck."), "belief section may contain no slot")


# ------------------------------------------------------------------- Tomorrow

def test_missing_tomorrow_line():
    _bad(draft(tomorrow=None), "last line must start with 'Tomorrow:'")


def test_tomorrow_naming_a_slot_that_is_not_on_the_menu():
    _bad(draft(tomorrow="Tomorrow: {{day.net_r}}"), "may name only a menu item")


def test_tomorrow_naming_two_menu_items():
    _bad(draft(tomorrow="Tomorrow: {{menu.Q3}} and {{menu.cell}}"), "exactly one menu item")


def test_tomorrow_with_neither_item_nor_no_change():
    _bad(draft(tomorrow="Tomorrow: hunt harder."), "exactly one menu item or 'no change'")


def test_tomorrow_with_both_item_and_no_change():
    _bad(draft(tomorrow="Tomorrow: {{menu.Q3}}, otherwise no change"), "AND 'no change'")


def test_tomorrow_clause_must_be_short():
    _bad(draft(tomorrow="Tomorrow: {{menu.Q3}} " + "and then more " * 20), "one short clause")


def test_two_tomorrow_lines():
    _bad(draft(belief="I suspect noise.\n\nTomorrow: no change"), "exactly one 'Tomorrow:' line")


# ------------------------------------------------------------------- sections

def test_missing_section():
    _bad(draft(heads=(tl.HEADINGS[0], "## Notes", tl.HEADINGS[2])), "sections must be exactly")


def test_sections_out_of_order():
    _bad(draft(heads=(tl.HEADINGS[1], tl.HEADINGS[0], tl.HEADINGS[2])), "sections must be exactly")


def test_extra_heading():
    _bad(draft(happened="### Detail\nNet {{day.net_r}}."), "sections must be exactly")


def test_text_before_the_first_section():
    _bad("Dear log,\n\n" + draft(), "text before the first section heading")


def test_length_cap():
    rendered, v = tl.render(draft(), FACTS, max_chars=50)
    assert rendered == "" and "the cap is 50" in v[0]


def test_empty_draft():
    assert tl.render("", FACTS) == ("", ["empty draft"])


# ----------------------------------------------------------------------- fuzz

def test_fuzz_slot_free_digit_free_text_never_crashes():
    rng = random.Random(7)
    alphabet = string.ascii_letters + " .,;:'\n#-—"
    for _ in range(300):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 400)))
        rendered, v = tl.render(raw, FACTS)
        assert isinstance(rendered, str) and isinstance(v, list)
        if not v:
            assert rendered == tl._normalize(raw)
