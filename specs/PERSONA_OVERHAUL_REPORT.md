# PERSONA OVERHAUL — PHASE REPORTS

Running record for `PERSONA_OVERHAUL_INDEX.md`, `PERSONA_CHARACTER_SHEET_SPEC_v1.md` (persona, "P") and
`TRADERS_LOG_SPEC_v1.md` (log, "L"). One section per phase gate. Findings only: decisions about enabling and
deploying are made by the owner in conversation, never by this file.

---

## P Phase 0 / L Phase 0 — Verification (2026-10-03, Saturday)

### Baseline

- HEAD `fa159bb` (Runner trade construction), `origin/master` = `fa159bb`.
- `git status`: untracked only (`_grep_r_multiple*.txt`, `_phase0_check.py`, `_probe.py` — local debug files that
  predate this work — plus the spec files added here).
- Full suite, `.venv` Python 3.14, `DATABASE_URL` unset: **1,297 passed** in 111s. Matches the spec's baseline.

### Is v28 live?

- **No, not at the time of this check.** `fa159bb` (migration v28, arms A/B/C, drift family) is committed and
  pushed; the deployed build predates it. Deploying it, and the follow-up `regrade-runner --from 2026-10-02`, are
  owner steps listed at the end of this report.
- Every commit made for this work sits on top of `fa159bb`, so no deploy of persona or log code can reach
  production without v28 in the same build.
- Production checks (`/api/table/runs?limit=40`, `/api/status`) need the app session; not run here. They are
  listed for the owner. Nothing in the design below depends on their result.

### P-2 Prompt loading (`agent/llm.py`)

- `load_prompt(name, replacements)` reads `prompts/<name>.md` with `read_text` (universal newlines, so the
  three CRLF files render with `\n`) and does a plain `str.replace("{{KEY}}", val)` per key passed.
- **An unfilled placeholder does not raise: it stays in the text verbatim.** So `{{PERSONA}}` needs a
  default-empty rule inside `load_prompt` (the spec's fallback), not edits to every caller.
  `test_selector_prompt_states_the_cap_and_no_false_guarantees` asserts `"{{" not in text` on a selector render
  that passes no PERSONA key, so the default-empty rule is load-bearing for a pinned test.
- `prompt_version(name)`: the first line's `<!-- prompt-version: N -->`, else `"1"`; `"?"` if the file is missing.

### P-3 Where `prompt_version` is read

- Written to `picks.prompt_version` (morning `analyst_vN/risk_vM`, runner `prompt_version("runner_selector")`)
  and `paired_arms.prompt_version`. `daily_plans` has no such column.
- Read: **nowhere** in `ledger.py`, `memory.py`, `research.py`, `integrity.py`, `population.py`, `ablation.py`,
  `backlog.py`, `stats.py`, `runner_research.py`, `loops/weekly.py`, the dashboard or the API. No stat, mining
  cell, FDR batch, ablation or backlog item pools or splits by it. A version bump splits nothing. The persona
  still gets its own column (P §3.1) so it can be stratified.

### P-4 The five prompts

| prompt | version | line endings | role paragraph (first sentence) |
|---|---|---|---|
| `morning_analyst` | 11 | CRLF | "You are the analyst half of a two-stage trading desk." Then "Your mission is singular: **win**." |
| `morning_risk` | 12 | CRLF | "You are the risk manager of a two-stage trading desk — the adversarial half." |
| `evening_grader` | 6 | CRLF | "You are the desk's evening review — a cold-eyed trading coach grading today's plan against what ACTUALLY happened." |
| `weekly_miner` | 16 | LF | "You are the desk's weekly research quant — the pattern miner." |
| `runner_selector` | 2 | LF | "You are selecting which intraday Runner candidates a trading desk should take today." (no `# ROLE` heading) |

Runner selector: `load_prompt("runner_selector", {MAX_PICKS, MAX_PER_INDUSTRY, REGIME_TAG, PLAYBOOK_SECTION,
CANDIDATE_TABLE})` in `runner_selection.select`. It is rendered only when candidates exceed `max_picks`; an
auto-selected session makes no LLM call and so carries no persona.

### P-5 Pinned strings (must pass unmodified)

- `test_coverage_lane.py` 216-227: `**one candidate per setup**`, `"setups_without_instance"`, no `{{COVERAGE`.
- `test_daily_floor.py` 211-219: `The desk trades every session.` (risk, analyst), `"floor_pick": true`,
  `{{FLOOR_SIZE_R}}R`, `no_trade_reason` absent, `must be **>= 2.0** ...`, miner `may not tell the desk to stand down for the day`.
- `test_evidence_backlog.py` 84-89 (`DATA-STARVED`, `3/8`, `STALLED`, `COUNTS toward a threshold, not findings`), 180-184 (risk text).
- `test_runner_playbook_loop.py` 57-69, 229-251: `## Runner Lane`, `dto_runner`, `no validated runner rules yet`, no `{{PLAYBOOK_SECTION}}`.
- `test_runner_pool_integrity.py` 476-486: selector cap sentence, `"{{" not in text`, float wording, **and
  `prompt_version("runner_selector") == "2"`**.

**One conflict:** Phase 2 bumps every prompt's version, and the selector's version is pinned at `"2"`. That pin
exists to force a deliberate test edit whenever the selector prompt changes, which is what the bump is. The
bump goes ahead and that single assertion moves to `"3"`. Every exact-text pin stays unmodified.

### P-6 Paired / ablation

`paired.run_challenger_arm` receives arm A's `analyst_system`/`risk_system` strings and varies only the
**memory context** (`arm_b_context_mode`) and playbook version. It cannot vary prompt text without a third arm,
and arm B is already spent on the context ablation. So:
- No paired persona on/off arm is built (the spec's conditional). The monitor header states the limitation.
- With the persona on, **both** arms carry it, so the existing A/B context comparison stays clean.
- `ablation.py` / `integrity.py` read `picks`/`outcomes` only; nothing there reads prompt text.

### P-7 Existing monitors

The backlog, funnel and friction readings are text blocks (`backlog.digest_text`, `ledger.participation_funnel`,
`friction`) exposed through `run.py` subcommands that print plain text (`runner-exits`, `runner-evidence`,
`runner-status`). The persona monitor follows that pattern: a pure text renderer in its own module plus a
`run.py` subcommand.

### P-8 Stamp location

- INSERTs: `daily_plans` (`ledger.save_daily_plan`), `picks` (`ledger.save_daily_plan`,
  `runner_selection.save_runner_pick`), `runs` (`ledger.log_run`). All name their columns. There is no
  positional `INSERT INTO <table> VALUES` on these tables in `agent/`, `api/` or `run.py`.
- Next free migration numbers: **v29** (persona stamp), **v30** (`traders_log`). Pattern:
  `agent/migrations_vNN.py` exporting `STATEMENTS`, appended in `ledger.py` after v28. The schema fingerprint
  makes the first connection after a deploy run the list once.

### L-2 Evening loop

`agent/loops/evening.py::run`. Order: simulate → shadow → population → runner grading → tapes → coverage →
deterministic events, forward observations, `backlog.compute` → grader LLM → persist grades and lessons →
`log_run("evening")` → console summary → `backlog.daily_digest` → `send_email(evening_email_html)` →
`render_dashboard` → `finish_run_lock`. Hook point: **after `daily_digest`, before `send_email`** (the email must
carry the log). In scope there: `picks`, `outcomes`, `shadow` (scored, net forgone), `runner_summary`, the
stored backlog snapshot, `review`, `stages`. Mock runs return before this point, so a mock evening writes no log.

### L-3 Stand-down reasons (stored)

- `daily_plans.no_trade_category`: `data_incomplete` (degraded data, an outage; always wins), `empty_universe`,
  `all_failed_rr`, `all_failed_regime`, `all_failed_validator`, `llm_error`.
- `daily_plans.session_degraded` / `provider_degraded`; `runner_sessions.provider_degraded`.
- No plan row: the morning's `runs` status (`api_error`, `error`, `skipped`, ...), or no row at all (a
  function-timeout kill logs nothing). The facts builder reports these stored values and guesses nothing.

### L-4 Closed menu

`backlog.morning_selection(backlog.latest(con, d), TOP_N)` is what tomorrow's analyst is shown
(`memory.build_morning_memory` reads `latest(con)`, the snapshot the evening just wrote). Read in the evening
after `backlog.compute(d)`, it is the same list.

### L-5 Read paths

`_QUERYABLE_TABLES` is read only by `/api/table` and `/api/table/<name>` (login-required, browser). No LLM loop
calls `/api/...`. Loops read the ledger through `ledger.*`, `memory.*`, `backlog.*`, `research.*` helpers; none
is generic over table names. Firewall allowlist: `agent/traders_log.py`, `agent/migrations_v30.py`,
`agent/ledger.py` (migration import), `agent/delivery.py`, `api/index.py`, `run.py`, `agent/loops/evening.py`
(hook), `agent/config.py` (config key), `prompts/traders_log.md`, `tests/`.

### L-6 LLM call pattern

`LLMClient.call(system, user, model, mock_name, thinking, web_search, expect_json, max_tokens)`; mock mode reads
`mocks/<mock_name>.json`. The client logs no cost; callers call `ledger.log_run(loop=...)`. `runs` has a `loop`
column, not `kind`: the log's cost row is `loop='traders_log'`. The client retries 3x with 15/30/45s back-off;
inside the evening's 300s budget the log needs **one attempt** with a bounded timeout (optional kwargs on
`call`, defaults unchanged).

### L-7 Email assembly

`evening_email_html(plan_date, review, outcomes_summary)` returns one f-string; the caller appends tape and
digest into `outcomes_summary`. New sections go through a new optional keyword argument whose default renders
nothing, so the flag-off HTML is byte-identical.

### Gate 0

No contradiction with either spec beyond the two recorded items (v28 not yet deployed; the selector version pin).

---

## P Phase 1 — Character sheet and loader (flag off)

Changed:
- `prompts/persona.md` (new, 43 lines): Appendix A verbatim, LF.
- `agent/llm.py` (+94): `load_prompt` substitutes `{{PERSONA}}` first and, when it is absent or empty, removes the
  placeholder together with the one blank line it occupies (`_PERSONA_SLOT`). New `persona_block(cfg, role)`,
  `persona_version(cfg)`, `PERSONA_ROLES`, `_persona_sections()`, `_persona_parts()`.
- `agent/config.py` (+10): default `"persona": {"enabled": False, "version": 1}`, `Config.persona_cfg`.
- `scripts/capture_prompt_renders.py` (new): renders the five prompts and the DATA-STARVED block with fixed
  replacements into `tests/fixtures/prompt_renders/` (captured at Phase 0 HEAD `fa159bb`).
- `tests/test_persona.py` (new, 16 tests).

Design notes:
- Only a literal `True` turns the persona on. `persona.version` in config pins the sheet: a `persona.md` carrying
  another version is not used (a different text is a different treatment), and the loop runs without it.
- Every failure (missing file, missing section, version mismatch, unknown role) returns `""`, logs one line,
  and leaves the prompt exactly as it renders with the flag off.
- `persona_block(cfg, None)` returns the shared sheet alone (used by the trader's log prompt).
- **Spec contradiction, recorded:** Appendix A's required header `<!-- persona-version: 1 -->` contains a digit,
  while §1.1 says "no digits anywhere in the file". The test enforces: header line exactly as specified, no
  digit anywhere below it. The header never reaches a prompt (`persona_block` returns section bodies only).

Gate 1: suite **1,313 passed** (1,297 + 16). Byte-identical off-render passes for all five prompts and the
DATA-STARVED block. Flag still off.

---

## P Phase 2 — Prompt integration (flag-gated)

Order followed as specified: version headers bumped first (analyst 11→12, risk 12→13, grader 6→7, miner 16→17,
selector 2→3), fixtures regenerated (each differs from the Phase 0 render in its header line only, verified),
then `{{PERSONA}}` inserted, then the off-render re-checked against the fixtures.

Placement (one slot per prompt, on its own line, right after the role paragraph):
- `morning_analyst`: after the mission paragraph ("Your mission is singular: **win**"), before "Your job this
  morning". The mission sentence stays; there is no second mission statement.
- `morning_risk`: after the role paragraph, before "**The desk trades every session.**" (untouched).
- `evening_grader`: after the role paragraph, before "Two iron rules".
- `weekly_miner`: after the role paragraph, before the prime-directive paragraph. The "register applies to
  `summary_for_email` only" sentence the spec asks for is already in Appendix A's `Role: miner` section, so the
  miner prompt's own text is unchanged.
- `runner_selector`: after its one-sentence role paragraph.

Wiring: `morning.run` (analyst, risk), `evening.run` (grader), `weekly.run` (miner), `runner_selection.run_selection`
(selector) pass `"PERSONA": persona_block(cfg, role)`. Paired arm B receives arm A's system strings, so with the
persona on both arms carry it and the context-mode comparison is unaffected.

DATA-STARVED: `backlog.morning_block(items, persona=False)`; with `persona=True` one paragraph
(`PERSONA_LEAD_IN`, the spec's suggested text, no digit) goes between the heading and the existing first
paragraph. `memory.build_morning_memory` passes `persona=persona_version(cfg) is not None`, the same predicate
that puts the sheet into the prompts.

Tests: `test_persona.py` +14 (placeholder once per file, versions, on-render = off-render + exactly one block at
the right place, pinned strings intact with the persona on, no digit in any block, lead-in contract, memory
uses the lead-in only when on). `test_runner_pool_integrity.py`: the version-pin assertion moved `"2"`→`"3"`
(recorded in Phase 0); no other existing test was modified.

Gate 2: suite **1,327 passed**. Both flag states verified.

---

## P Phase 3 — Stamp and monitor

Changed:
- `agent/migrations_v29.py` (new): `ALTER TABLE {daily_plans,picks,runs} ADD COLUMN IF NOT EXISTS persona_version VARCHAR`,
  appended to `_MIGRATIONS` after v28.
- `agent/ledger.py`: `save_daily_plan` writes `plan["persona_version"]` to the plan row and every morning pick;
  `log_run(..., persona_version=None)` (new optional kwarg). All INSERTs keep naming their columns.
- `agent/loops/morning.py`: `persona_stamp = persona_version(cfg)` taken when the prompts are built; stored on the
  plan and the `ok`/`degraded` run row. `evening.py` / `weekly.py`: the run row after a persona-carrying call is
  stamped. `runner_selection.py`: picks chosen by the selector call and its run row are stamped; an auto-selected
  session (no call) stays NULL.
- `agent/persona_monitor.py` (new) + `run.py persona-monitor [--sessions N]`: per group (stamp; NULL = "persona
  off"), most recent N sessions: morning sessions, taken vs no-trade days (taken = at least one morning pick;
  the risk manager's `no_trade` flag is reported separately because floor/coverage days carry it while trading),
  stand-down reasons from `no_trade_category` (`data_incomplete` = outage, else genuine), degraded sessions,
  picks, triggered and triggered per session, floor/coverage picks, mean planned size, candidate and pick
  conviction histograms, `probe_for` share; runner sessions, candidates, selected per session, industry-cap drops
  (parsed from the stored selection note). Header verbatim, plus the paired-arm limitation (Phase 0 P-6).
  No paired persona arm was built.
- Tests: `tests/test_persona_stamp.py` (15).

Gate 3: suite **1,341 passed** (+1 monitor test after, see Phase 4 count). Migration applied to a scratch copy of
the local DuckDB ledger (`data/ledger.duckdb`, a sporadic local pipeline, **not production**); columns present on
all three tables; second `connect()` ran no DDL. Monitor on that copy:

```
== persona off ==
  morning sessions: 7 (2026-08-19 .. 2026-09-30)
  taken days: 2; no-trade days: 5 (genuine (no category stored): 5)
  risk manager no_trade flag set: 5 day(s)
  degraded sessions: 0
  picks: 7; triggered: 1; triggered per session: 0.14
  daily_floor picks: 1 (per session 0.14); coverage picks: 5 (per session 0.71)
  mean planned size of picks: 0.25R
  candidates: 48; with probe_for: 1 (share 0.02)
  candidate conviction: 2: 21, 3: 22, 4: 5
  pick conviction: 2: 4, 3: 3
  runner sessions: 8; candidates: 1570; selected: 50 (per session 6.25); industry-cap drops: 0
```
(Local data, labeled as such. The production baseline is an owner step: `python run.py persona-monitor`.)

---

## P Phase 4 — Offline parity and docs

- `tests/test_persona_parity.py` (8): morning, evening and weekly run end to end in mock mode with the persona
  on; the captured system prompts carry the sheet and the right role section; every canned response still
  parses to its contract keys (`market_read`/`candidates`, `picks`/`rejected`, `plan_grade`/`pick_reviews`/`lessons`,
  `summary_for_email`/`playbook_markdown`; the selector mock still parses with `parse_selection`).
- `README.md`: section "Persona (earned hunger)" (flag, P-1..P-5, stamp, monitor); layout row updated.
- The three spec files and this report are in `specs/`.

Gate 4: suite **1,350 passed**. Flag still off in config.json.

---

## L Phase 1 — Schema and the facts builder

- `agent/migrations_v30.py`: `traders_log` exactly as specified (PK `session_date, lane`), appended after v29.
- `agent/traders_log.py::build_facts(con, d)` → `{session_date, slots, menu, menu_item_ids, errors}`. Each slot:
  `name, value (pre-formatted), scope, source, label`. Families (closed): `day.*`, `pick.<lane>.<ticker>`, `shadow.*`,
  `runner.*` (incl. `runner.exit.<policy>` for plan and arms A/B/C via `runner_exits.policy_summary`, the function
  `run.py runner-exits` prints), `deficit.*` + `menu.*` (`backlog.morning_selection(backlog.latest(con, d))`, the
  morning's list), `calibration.conviction_N` (only cells `calibration_aggregate` does not suppress),
  `verdict.calibration_q3` / `verdict.runner_lane` (lines copied verbatim from the weekly-rendered playbook).
- Fill validity is the evening's own definition (`_fill_valid_rows`): triggered, not `invalid_fill`, not an execution
  anomaly. Wins/losses/net by `r_planned` only. A fill-valid trade with no `r_planned` (legacy rows) is counted in
  `day.unmeasured_r`, never backfilled from `r_multiple` (found on the local sample, see below).
- `day.stand_down_reason` (only when the morning lane has no picks), from what is stored: `no_trade_category` /
  `session_degraded` (`data_incomplete` → "data outage"), else the morning `runs` status, else "not distinguished by
  the system".
- `render_facts_only(facts)`: deterministic labeled list, every value tagged with its scope.
- Tests: `tests/test_traders_log_facts.py` (16), seed helper `tests/_traders_log_seed.py`.

Gate 1 sample (local DuckDB copy, **not production**; latest locally graded session 2026-09-21, runner-only):
`day.picks_runner = 10`, `day.triggered = 8`, `day.execution_anomalies = 2`, `day.exits = stop 4, target 4`,
`day.unmeasured_r = 8` (graded before `r_planned`), `runner.candidates = 322`, `runner.selected = 10`,
`day.stand_down_reason = no morning plan and no morning run recorded (not distinguished by the system)`,
`menu_item_ids = ['no change']` (no local backlog snapshot that day).

## L Phase 2 — Rendering and the validator

`render(raw, facts, max_chars=3000) -> (rendered, violations)`. Rules: unknown or malformed slot; any digit outside
a slot; **number words from "three" up and ordinals from "third" up** (added: the spec forbids restating a slot in
words, and "one"/"two" stay legal); headings exactly the three, in order, nothing before the first; the last line
starts `Tomorrow:` and names exactly one menu slot or `no change` (not both), no other slot, one clause of at most
160 characters; belief section has no slot and no digit; rendered length over `traders_log.max_chars` (3000, about
one screen of the single-column email) is a violation, never a cut. `one_session` values get ` (one session)`,
`computed_verdict` values are quoted. A surrounding code fence and curly apostrophes are tolerated.
Tests: `tests/test_traders_log_render.py` (32, incl. the parametrized digits `13`, `3/20`, `0.5`, `1R`, `2026`,
`10:30`, and a seeded fuzz).

## L Phase 3 — Prompt and generation

- `prompts/traders_log.md` (v1): role, the slot rule, the three sections plus the `Tomorrow:` line, the
  prohibitions; `{{PERSONA}}` takes the shared sheet only (no role section). No digit below the header.
- `generate(con, cfg, d, llm_client=None, deadline=None)`: facts → (empty → `skipped`, no call) → one call
  (`attempts=1`, bounded `timeout`, `archive=False`) → render → one retry with the violation list → else
  `fallback_facts_only`. Any exception → `error`, facts-only text kept for the email. Never raises. Re-running a
  date replaces its row. Cost: `runs` row `loop='traders_log'` (status `ok` / `degraded` for a fallback / `error`),
  stamped with `persona_version`. Mock mode uses `mocks/traders_log.json` and stores nothing;
  `mocks/traders_log_violation.json` is the violating twin.
- `LLMClient.call` gained `attempts`, `timeout`, `archive` (defaults = the old behavior).
- Tests: `tests/test_traders_log_generate.py` (11).

Gate 3 sample: one real call (claude-sonnet-4-5, persona on) on the scratch copy, session 2026-09-21. First draft
wrote the word "four" → refused → the retry passed: status `ok`, 4,911 in / 856 out tokens, **$0.028**. Observation
for the owner: the mandated ` (one session)` suffix on every day slot makes sentences read mechanically
("10 (one session) runner-lane picks"). Kept as specified.

## L Phase 4 — Wiring

- Evening hook (`agent/loops/evening.py`), after `backlog.daily_digest`, before `send_email`, only when
  `traders_log.enabled`: `generate(..., deadline=t_start + 300s − reserve_s)` then `day_contrast`; any exception is
  logged and the email goes out without the sections. `stages["traders_log"]` records the status.
- `evening_email_html(..., traders_log=None, day_contrast="")`: two sections appended at the end; with neither, the
  HTML is byte-identical to `2697dd2` (fixture `tests/fixtures/evening_email_before_log.html`). Fallback notes:
  "facts-only: the written log failed its checks" / "...could not be generated" / "...evening ran out of time".
- `day_contrast`: counts of winners and losers (by `r_planned`) per dimension from `buckets.dimensions(lane)` over
  `research.morning_rows(con, on=d)` and `research.runner_rows(con, on=d)`, with the verbatim header. No rate, no sign.
- `run.py traders-log [--date] [--show] [--mock]`; regeneration refuses a date with no `ok`/`degraded` evening run.
- `_QUERYABLE_TABLES["traders_log"] = "session_date"`; one `<option>` in the dashboard's raw-ledger inspector.
- Tests: `tests/test_traders_log_wiring.py` (10), including the real evening loop with the hook off, on, and raising.

## L Phase 5 — The firewall

`tests/test_traders_log_firewall.py` (29 incl. parametrized): (1) every `.py`/`prompts/*.md` line naming
`traders_log` is in a file on the allowlist, with line-level rules for `api/index.py` and `agent/webui.py`, and the
evening names it only inside the hook block; `generate()` is called only by the hook and the CLI. (2) twenty
learning/prompt-building modules neither import nor name it. (3) no other prompt mentions the log; only
`agent/traders_log.py` loads its prompt. (4) with sentinel-filled log rows for today and yesterday, the real morning
and weekly loops (mock), the real evening grader call, morning/weekly memory and the runner selector's playbook
section never carry the sentinel. (5) the module writes only its own table, and the draft skips both the
`llm_archive` and `runs.error` (two leaks found and closed while writing these tests).

Gate 5: suite **1,447 passed**.
