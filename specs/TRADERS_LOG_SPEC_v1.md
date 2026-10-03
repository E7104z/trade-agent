# TRADER'S LOG SPEC v1 — SLOT-BOUND, WRITE-ONLY

Status: draft for implementation
Scope: `agent/traders_log.py` (new), `prompts/traders_log.md` (new), ledger schema (one new table), `agent/loops/evening` (one guarded hook), `agent/delivery.py` (evening email sections), `run.py` (one command), `api/index.py` (table whitelist), `agent/config.py`, tests.
Builds on: `PERSONA_CHARACTER_SHEET_SPEC_v1.md` (must be through Gate 3), EVIDENCE_BACKLOG_SPEC v1, RESEARCH_INTEGRITY_SPEC v4, the runner exit-arms work (migration v28).
Read first: `PERSONA_OVERHAUL_INDEX.md` (standing rules and order of work apply to every phase here).

## 0. The problem and the design

The agent should sound like a person who knows what he has and has not proven. That voice is for **the human reading it**. It must never become a learning channel for the machine.

The log is a first-person daily entry, written after the evening grading. Its job is to be loud and human without lying to the system.

**Design rules:**

- **L-1 Write-only.** No loop, prompt, memory builder, miner, selector or grader reads `traders_log`. The human reads it (email, table, CLI). The agent never does. This is enforced by a static test (§6), not by convention.
- **L-2 Numbers by reference, never by generation.** The model is given a closed set of named **slots** and writes `{{slot_name}}` wherever a fact belongs. The renderer substitutes the value. The raw model output may contain **no digit characters** outside slot tokens. A model cannot mint a number it was never given, and a number it was given cannot be altered. This replaces "generate then validate" with "cannot generate".
- **L-3 No new look at the data.** The facts handed to the log are things the system **already computes and reports** (day outcomes, shadow-ledger forgone R, backlog counts, calibration cells at n>=8, runner arm means for the day, computed verdict strings). The log builder computes **no new comparison, effect size, split or test**.
- **L-4 Beliefs are labeled and inert.** One section is explicitly a belief ("not evidence, not acted on"). It may contain no digits and no slot that carries a result. Nothing in it is persisted anywhere except the log row.
- **L-5 The log ends on an action chosen from a closed menu.** The final line names either a DATA-STARVED item id from the injected list, or the literal `no change`. The model cannot invent a hypothesis to hunt.
- **L-6 Failure is silent to the loops and loud to the human.** If generation or validation fails, the evening completes normally and the email carries a deterministic facts-only fallback, labeled as such. The log can never block grading, the playbook, or an email.

## Phase 0 — Verification (HARD BLOCKER; no code until the report is posted)

1. **Baseline and order.** Run the full suite; record the pass count. Confirm migration v28 is live and the persona spec is through Gate 3 (`persona_version` columns present). If either is missing, stop.
2. **Evening loop.** Locate the actual evening orchestration module (`agent/loops/evening.py` vs `agent/loops/evening_loop.py`; the repo has used both names in specs). Identify the exact point **after** grading, shadow-grading, runner grading, backlog compute and the evening email assembly where a guarded hook can run. List the data already in scope there (day's outcomes, picks, rejections, runner candidates and arms, backlog snapshot, calibration, computed question verdicts).
3. **Stand-down reasons.** Determine whether the system now distinguishes a data-infrastructure outage from a genuine market stand-down in what is stored (Bug B in `principles`). Record the stored field(s). The log must state which it was; if the distinction is not stored, the facts builder emits `stand_down_reason = "not distinguished by the system"` rather than guessing.
4. **Closed-menu source.** Confirm `backlog.latest(con)` / `morning_selection` return the item ids and counts that the log will cite, and that using them in the evening (after `backlog.compute`) is consistent with the morning block.
5. **Read paths.** Grep the whole repo for every consumer of tables the log could be confused with. Confirm `_QUERYABLE_TABLES` in `api/index.py` is read by the dashboard/Arby/browser only, and that **no LLM loop** calls `/api/table/...` or `ledger` helpers that would let it read a new table. List the files that will be on the write/read allowlist in §6.
6. **LLM call pattern.** Read `agent/llm.py::LLMClient.call`: model selection, cost accounting into `runs`, JSON repair, offline mock behavior, and how `kind` is logged. The log is one Sonnet-class call per session via this same client.
7. **Email assembly.** Read `agent/delivery.py::evening_email_html` and its caller; note how sections are appended so the new sections are additive and optional.

**Gate 0:** report posted, no contradictions, owner acknowledges.

## Phase 1 — Schema and the facts builder (no LLM yet)

### 1.1 Migration (additive)

Next free migration number, `CREATE TABLE IF NOT EXISTS`, appended to `_MIGRATIONS`. No sequences, no `DEFAULT nextval()`, no literal `DOUBLE PRECISION` (existing portability rules; the migration tests enforce them).

```
traders_log(
  session_date  DATE,
  lane          VARCHAR,     -- 'combined' in v1
  generated_at  TIMESTAMP,
  persona_version VARCHAR,   -- from the persona stamp; NULL when persona off
  model         VARCHAR,
  status        VARCHAR,     -- 'ok' | 'fallback_facts_only' | 'skipped' | 'error'
  facts_json    VARCHAR,     -- the exact injected facts (slots), as stored
  raw_text      VARCHAR,     -- model output BEFORE substitution (slot tokens intact)
  rendered_text VARCHAR,     -- AFTER substitution; what the human sees
  violations_json VARCHAR,   -- what the validator found, if anything
  tokens_in INTEGER, tokens_out INTEGER, cost_usd DOUBLE,
  PRIMARY KEY (session_date, lane)
)
```

One row per session; re-running the evening replaces the row for that date (idempotent). Every INSERT names its columns.

### 1.2 `agent/traders_log.py::build_facts(con, session_date) -> dict`

Pure read of existing ledger data and already-computed results. Returns an ordered dict of **slots**, each:

```python
{"name": str, "value": str, "scope": "one_session" | "cumulative" | "computed_verdict" | "count", "source": str}
```

`value` is a **pre-formatted string** (the renderer never formats). `source` names the table/function it came from (auditable).

Allowed slot families (nothing else):

- **Day facts (scope one_session):** picks taken, triggered count, wins/losses by `r_planned` (never `r_multiple`), exit reasons, per-pick tickers and planned R. Invalid fills are **excluded and counted** (`invalid_fills`), never narrated.
- **Shadow ledger (one_session):** rejections scored today, forgone R today.
- **Runner (one_session):** candidates, selected, per-industry cap drops; arms A/B/C and plan mean R and win % for the day **as already printed by `run.py runner-exits`**.
- **Proof deficits (count):** the top backlog items exactly as `backlog.morning_block` shows them: item id, `n`, `target`, `eta`, `stalled`. Counts only.
- **Calibration (cumulative):** only cells that pass the n>=8 display rule in `memory._stats_table` / the calibration table. Cells under n=8 are **omitted**, not shown as "suppressed".
- **Computed verdict strings (computed_verdict):** the system's own verdict lines (e.g. Q3 calibration verdict, the runner stop-condition status including the "too early" label) copied verbatim.
- **Stand-down (day fact):** `stand_down_reason` per Phase 0 item 3.
- **Closed menu:** `menu_item_ids` = the backlog item ids shown, plus the literal `no change`.

Explicitly **not** allowed: any winner-vs-loser split, any new bucket comparison, any mean or rate the system does not already report, any p/q value other than a copied verdict string, `r_multiple` as a headline.

Exception-guarded: on any error, return what was built plus `errors`; the caller treats an empty facts dict as `status='skipped'`.

### 1.3 Deterministic fallback renderer

`render_facts_only(facts) -> str`: a plain, labeled, non-narrative list of the same facts (no persona voice). This is what the email shows whenever the model output is absent or fails validation. Every value is labeled with its `scope` (e.g. `(one session)`).

### 1.4 Tests (`tests/test_traders_log_facts.py`)

- Facts builder on a fixture ledger: slot set is exactly the allowed families; every slot has a `source`; no slot name or value contains `p =`, `q =`, `mean`, `significant` unless the scope is `computed_verdict`.
- Invalid fills are counted and never appear in per-pick slots.
- Calibration cells under n=8 are absent.
- Empty ledger, no-trade day, runner-only day, and outage-day all yield a valid (possibly small) facts dict without raising.
- `render_facts_only` is deterministic (same facts, same bytes).

**Gate 1:** suite green; sample facts JSON for the most recent real session posted (from the real ledger, read-only); report posted.

## Phase 2 — Slot rendering and the validator (still no LLM)

### 2.1 `render(raw_text, facts) -> (rendered, violations)`

- Find every `{{slot_name}}` in `raw_text`. Unknown slot name = violation. Each valid slot is replaced with `value` plus, for `scope == "one_session"`, the suffix ` (one session)` appended by the renderer (not the model). `cumulative` and `count` get no suffix; `computed_verdict` is wrapped in quotes.
- After removing slot tokens, **any digit character in the remaining text is a violation**. (This catches `13 sessions`, `3/20`, `1R`, `0.5`, years, times. Everything numeric must come through a slot. Spelled-out small words like "one" and "two" are permitted.)
- The final non-empty line must start with `Tomorrow:` and its remainder must contain either `no change` or exactly one slot reference to a `menu_item_ids` entry, and no free-form hypothesis text beyond a single short clause. (Violation otherwise.)
- Sections must appear in the required order with the required headings (§3.2). Missing/extra headings = violation.
- The **Belief** section must contain no slot at all (L-4) and no digit.
- Length cap: configurable `traders_log.max_chars`, default chosen from the Phase 0 email-layout findings; exceeding it truncates nothing, it is a violation.

Return `rendered` only if `violations == []`; otherwise return `("", violations)`.

### 2.2 Tests (`tests/test_traders_log_render.py`)

Table-driven, one test per rule, each with a minimal raw_text:

- unknown slot → violation; digit outside slot → violation (parametrize: `13`, `3/20`, `0.5`, `1R`, `2026`, `10:30`).
- digit inside a belief section → violation; slot inside belief → violation.
- `one_session` slot gets the suffix, `cumulative` does not.
- missing `Tomorrow:` line / menu id not in `menu_item_ids` / two menu ids → violation.
- a perfectly formed log renders with zero violations, byte-stable.
- fuzz: random raw_text containing no `{{` and no digits renders unchanged except for structure checks (no crash).

**Gate 2:** suite green, report posted.

## Phase 3 — Prompt and generation

### 3.1 `prompts/traders_log.md` (new; `<!-- prompt-version: 1 -->`)

Includes `{{PERSONA}}` (same loader; with persona off the log still works, in a plainer voice). Content requirements:

- Role: you are writing the trader's private daily log. First person, plain, short sentences. The reader is the owner of the desk.
- You are given **slots** (`name = value  [scope]`). You may only write a fact by writing its slot token, e.g. `{{day.net_r}}`. **Never write a digit.** Never write a number in words that restates a slot value (e.g. do not write "thirteen"). Spelled-out "one"/"two" in ordinary prose is fine.
- Required sections, in order, with exactly these headings:
  1. `## What I still can't prove` — the proof-deficit slots, as debts. Counts only.
  2. `## What happened` — day facts. Every sentence about a result must be backed by a slot. Say "one session, not evidence" when relying on a one-session slot (the renderer also labels it).
  3. `## What I believe (not evidence, not acted on)` — prose only, no digits, no slots. A belief may be a suspicion about *why*; it must be phrased as a belief, and it must not recommend a trade, a threshold, or a rule.
  4. `Tomorrow:` — a single line: one menu id as a slot (`{{menu.<id>}}`) with at most one short clause, or `no change`.
- Hard prohibitions: do not compare winners to losers beyond what a slot states; do not propose a threshold, a rule, or a filter; do not interpret a single session as a pattern; do not mention the playbook as if it changed; do not address the system or give it instructions.
- Frustration, if any, must name which slot's count did not move and end the section on the `Tomorrow:` action. The last line is never a sigh.

### 3.2 `generate(con, cfg, session_date) -> dict`

1. `facts = build_facts(...)`. If empty → store `status='skipped'`, return.
2. One LLM call through `LLMClient` (model from `traders_log.model`, default a Sonnet-class model from existing config; cost logged to `runs` with `kind='traders_log'`). Input: the slots (name, value, scope), the menu, and the section contract. No playbook text, no lessons, no raw trade tables beyond the slots.
3. `render(raw, facts)`. On violations: **one retry** with the violation list appended ("your previous draft broke these rules: ..."). Second failure → `status='fallback_facts_only'`, `rendered_text = render_facts_only(facts)`, `violations_json` kept.
4. Persist the row (idempotent per date/lane).
5. Everything inside try/except; on unexpected error store `status='error'` with the message in `violations_json` and return. **Never raise into the evening loop.**
6. Offline/mock mode: add a canned response to `mocks/` that renders cleanly, and one that violates, so both paths are testable without a network.

### 3.3 Tests (`tests/test_traders_log_generate.py`)

- With a stub LLM returning a clean draft: row stored with `status='ok'`.
- With a stub LLM returning a digit: retried once; if the retry is also bad: `fallback_facts_only` and the rendered text equals `render_facts_only(facts)`.
- With a stub LLM that raises: `status='error'`, no exception escapes.
- With empty facts: `skipped`, **no LLM call made** (assert on the stub).
- Re-running the same date replaces the row (one row per date).
- Cost row logged to `runs`.

**Gate 3:** suite green, report posted. Include one real generated log, produced with the flag on against a **scratch** DB copy, for the owner to read. Do not touch production.

## Phase 4 — Wiring: evening hook, email, CLI, table access

1. **Hook:** in the evening loop at the point found in Phase 0, add one guarded call, gated on `traders_log.enabled` (default `false`). Nothing else in the loop changes. If the flag is off the loop's behavior and return value are byte-identical.
2. **Email:** `evening_email_html` gets **two optional sections appended at the end**, each rendered only when its data exists, so the email with the flag off is byte-identical:
   - `Trader's log` — `rendered_text` (or the labeled facts-only fallback, with a plain note `facts-only: the written log failed its checks`).
   - `Today's winners and losers (descriptive)` — see §4.1.
3. **CLI:** `python run.py traders-log [--date YYYY-MM-DD] [--show]` regenerates (or shows) a session's log. Regeneration refuses to run for a session whose grading is incomplete.
4. **Table access:** add `traders_log` to `_QUERYABLE_TABLES` so the owner can browse it. No other consumer is added.

### 4.1 The day's winner/loser block (email only; deterministic, no LLM)

`day_contrast(con, session_date)`: for the day's taken trades (and, separately, all triggered runner candidates), list **counts** of winners and losers by each *already-defined* dimension (setup type, direction, regime tag, and the stored bucket dimensions including the index-tape, extension, and staleness buckets with their frozen edges). Counts only: no mean, no rate difference, no sign, no p. Header, verbatim: `One session, descriptive. This table is a count of what happened, not evidence of what separates winners from losers. Only the weekly search, under FDR correction, may say that.` Cells with a count under the system's display floor are not specially hidden here (counts are not rates), but no rate is shown.

This block is not an input to the log's LLM call and is not in any morning prompt.

### 4.2 Tests

- Flag off: evening email HTML identical to a pre-change capture (fixture).
- Flag on, `status='ok'`: email contains the rendered log and the contrast block.
- Flag on, fallback: email contains the facts-only block and the labeled note.
- The contrast block contains no digits-as-statistics words: assert it does not contain `mean`, `avg`, `win rate`, `%`, `p =`, `edge`.
- Evening loop completes and returns the same result with the hook raising an exception (monkeypatch `generate` to raise).

**Gate 4:** suite green, report posted.

## Phase 5 — The firewall tests (the point of the spec)

`tests/test_traders_log_firewall.py` — these are the deliverable that makes the log safe.

1. **Static read-path allowlist.** Walk every `*.py` and `prompts/*.md` in the repo; any occurrence of `traders_log` must be in this allowlist: `agent/traders_log.py`, the migration module, `agent/ledger.py` (`_MIGRATIONS` entry only), `agent/delivery.py`, `api/index.py` (`_QUERYABLE_TABLES` only), `run.py`, the evening hook line, `tests/`. Anything else fails the test with the file path. Add to the allowlist deliberately or not at all.
2. **No memory-builder import.** Assert `memory.py`, `backlog.py`, `research.py`, `integrity.py`, `runner_selection.py`, `runner_research.py`, `grading.py`, and `loops/weekly.py` do not import `agent.traders_log`.
3. **No prompt references.** Assert no prompt in `prompts/` other than `traders_log.md` mentions the log, and the log prompt is not loaded by any loop other than the evening hook.
4. **Rendered morning/weekly/runner/grader prompts** built from a ledger that contains a populated `traders_log` row do not contain any string from that row's `rendered_text` (build the prompts and assert absence of a sentinel phrase planted in the log row).
5. **Belief section never persists elsewhere:** assert the only write of belief text is the `traders_log` INSERT (grep the module for other `execute(` writes).

**Gate 5:** suite green, all five firewall tests present and passing, report posted.

## Phase 6 — Enabling (owner approval required; do not self-start)

Present to the owner in the conversation: the flag lines (`traders_log.enabled`), a sample log from the scratch DB, the fallback behavior, and the cost per session from the `runs` log. Enable only on explicit approval. After the first three live sessions, post the three stored rows' `status` values and `violations_json` so the owner can see whether the model is clearing the validator or falling back; do not tune the prompt without telling the owner what changed.

## Out of scope (do not build; propose as an addendum if wanted)

- Feeding any log content back into any prompt. If the owner wants a belief to reach the agent, it must be registered as an open question or `probe_for` item through the existing counts-only backlog, with its own integrity review.
- A weekly log, a dashboard panel, or an Arby/mascot reading of the log.
- Per-lane logs (v1 is `combined`).
- Any new statistic computed for the log.
