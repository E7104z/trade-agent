"""
Capture the rendered text of the five LLM system prompts (and the DATA-STARVED
block) as test fixtures.

Why: PERSONA_CHARACTER_SHEET_SPEC_v1 is additive only -- with `persona.enabled`
false, every rendered prompt must be byte-identical to what the desk sent
before the persona existed. tests/test_persona.py renders each prompt with the
SAME replacements used here and compares against these files. Re-run this
script only when a prompt changes on purpose (and say so in the commit).

    python scripts/capture_prompt_renders.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "prompt_renders"

# Fixed, representative placeholder values. They need not match config.json:
# the fixture pins how a prompt FILE renders, not what today's config says.
_COVERAGE_ANALYST = (" The desk trades every candidate that is a valid order: the risk manager's"
                     " approvals at full size, the rest at minimum size as coverage.")
_COVERAGE_SECTION = "# COVERAGE (every setup found gets tested)\n\nCoverage fixture text.\n\n"

RENDER_CASES: dict[str, dict[str, str]] = {
    "morning_analyst": {"COVERAGE_ANALYST": _COVERAGE_ANALYST,
                        "COVERAGE_SECTION": _COVERAGE_SECTION},
    "morning_risk": {"MAX_PICKS": "4", "MIN_SIZE_R": "0.25", "MAX_SIZE_R": "1.5",
                     "MAX_TOTAL_RISK_R": "3.0", "FLOOR_SIZE_R": "0.25",
                     "COVERAGE_ANALYST": _COVERAGE_ANALYST,
                     "COVERAGE_SECTION": _COVERAGE_SECTION},
    "evening_grader": {},
    "weekly_miner": {},
    "runner_selector": {"MAX_PICKS": "10", "MAX_PER_INDUSTRY": "2",
                        "REGIME_TAG": "uptrend/risk_on/lowvol",
                        "PLAYBOOK_SECTION": "## Runner Lane\n\n(no validated runner rules yet"
                                            " — do not assume any)",
                        "CANDIDATE_TABLE": "ticker | industry\nAAA | Banks - Regional"},
}

BACKLOG_ITEMS: list[dict] = [
    {"item_id": "R0123456789ab", "item_type": "rule", "n": 2, "target": 8,
     "eta_sessions": 12, "stalled": False, "what_counts": "a fill-valid morning trade in setup_type=gap_go"},
    {"item_id": "Q3", "item_type": "question", "n": 5, "target": 8,
     "eta_sessions": None, "stalled": True, "what_counts": "conviction calibration"},
    {"item_id": "morning:stop_atr_bucket=0.5-1", "item_type": "cell", "n": 3, "target": 8,
     "eta_sessions": 4, "stalled": False, "what_counts": "a fill-valid morning trade with stop_atr_bucket=0.5-1"},
]


def main() -> None:
    from agent.backlog import morning_block
    from agent.llm import load_prompt

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name, repl in RENDER_CASES.items():
        (FIXTURE_DIR / f"{name}.md").write_text(load_prompt(name, repl), encoding="utf-8",
                                                newline="\n")
    (FIXTURE_DIR / "morning_block.md").write_text(morning_block(BACKLOG_ITEMS), encoding="utf-8",
                                                  newline="\n")
    print(f"wrote {len(RENDER_CASES) + 1} fixtures to {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
