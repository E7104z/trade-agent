"""The production config trades every setup found, and every trade reaches the broker."""
from __future__ import annotations

import io
import json


def test_production_config_turns_the_test_lanes_on():
    cfg = json.loads(io.open("config.json", encoding="utf-8").read())
    lane = cfg["coverage_lane"]
    assert lane["enabled"] is True and lane["size_r"] == cfg["min_size_r"]
    assert set(lane["blocking_kills"]) == {"grounding", "entry_type", "event_risk"}
    assert cfg["daily_floor"]["enabled"] is True
    # Every approval plus every coverage trade can reach the paper broker.
    assert cfg["live_paper"]["max_picks_per_day"] >= cfg["max_picks"] + lane["max_picks"]
