# WRITE-TIME ENFORCEMENT SPEC v1 — integrity checks that the playbook cannot ignore

**Status:** proposed, 2026-09-23 ([SELF_ASSESSMENT_2026-09-23.md](SELF_ASSESSMENT_2026-09-23.md) P4, G4.1)
**Goals served:** 1, 4
**Scope:** `agent/loops/weekly.py`, `agent/memory.py`, `agent/health.py`, `agent/research.py`, `prompts/weekly_miner.md`, `api/index.py`, the next free migration, tests.
**Writer isolation:** unchanged. The weekly loop stays the sole writer of `playbook.md` and `playbook_versions`. Everything below happens inside that loop's existing write path.

---

## 0. The problem

The weekly loop runs eight write-time checkers over the miner's rewrite (`weekly.py:1039-1061`):
- WR suppression;
- the calibration table;
- the selection-edge table;
- the sizing banner;
- the Runner Lane section;
- negative best edge;
- the open-question registry;
- provenance.

Each checker only calls `log("weekly", "WARN …")`. Then `write_playbook` runs unconditionally (`weekly.py:1062`). The checks are advisory, and their output is invisible:
- `util.log` is a `print` (`util.py:83-101`).
- In production that print goes to Vercel runtime logs. On 2026-09-24 a 7-day query returned only about 20 minutes of lines.
- So every violation the integrity layer detects in production is unrecoverable soon after the run.

Meanwhile two consumers act on the text the same day:
- the morning analyst reads the whole playbook;
- the runner selector reads the Runner Lane section and places live orders from it (`weekly.py:738-747`).

A second problem: the miner prompt contradicts itself on what edge *is*.

| Location | Says |
|---|---|
| `weekly_miner.md:83` (§5) and `:110` (§6) | "The bar for calling something edge: (a) n >= 8, (b) avgR beats its naive baseline, (c) t >= 1.5" |
| `weekly_miner.md:95` and `:97` (RESEARCH INTEGRITY) | "Cite `q_value`, never a raw `t_stat`" and "Only VALIDATED rules may appear in Setup Rules or Current Best Edge" |

The `t >= 1.5` bar is what `stats.py:7-12` calls "a false-positive generator". It reaches the morning agent in the playbook's own header (`playbook.md:38-40`). `_check_wr_suppression` then *requires* every avgR claim to cite `t=` (`weekly.py:858`), which enforces the statistic the integrity section says not to rely on.

---

## Phase 0 — production verification (hard blocker; a self-check, not a sign-off)

Data access means an authenticated `/api/table/...` session or a local `DATABASE_URL`. If neither is available, record Phase 0 as blocked and stop. Never search the machine for credentials.

1. Pull `/api/table/playbook_versions?limit=3`. Record the current production version and the date of its latest row. **If production is still at v5 (2026-09-06), say so in the commit.** It means Goal 1 has had no refinement for 17+ days, and it changes how urgent S4 is.
2. Run all eight checkers locally over the latest production playbook text: a one-off script that imports the `weekly.py` checkers and `integrity.check_playbook`, with `states` read from `research_rules`, which S1 Phase 1 exposes. Record the violation count by checker. This is the baseline Phase 2 must drive to zero-blocking.
3. Pull `/api/table/runs?limit=40` for loop `weekly`: the last ok date, and the error text of any failures.
4. Check whether the current production Runner Lane section contains a `runner_*` row with mean R at n < 8. The runner selector has been acting on that text.

If check 2 finds zero violations in production and check 1 shows a fresh version, Phase 2 is still required: the checkers must be able to act, not just happen to pass. Record the result.

---

## Phase 1 — persist every violation (display only)

1. Add a new table, `playbook_violations`, in the next migration:

   | column | notes |
   |---|---|
   | `version` | playbook version |
   | `checker` | which checker fired |
   | `severity` | `block` / `warn` |
   | `section` | playbook section |
   | `line` | the offending line |
   | `action` | `withheld` / `replaced` / `logged` |
   | `created_at` | timestamp |

2. `weekly.run` collects the output of every checker into one list, still before the write, and inserts it with the new version number after `write_playbook` returns.
3. Add `playbook_violations` (date column `created_at`) to `_QUERYABLE_TABLES`.
4. `health.py` gains a signal: violations in the latest rewrite, split by severity.
   - The Weekly mining stage becomes `warn` when any `block`-severity violation was recorded.
   - The runs row stays `ok`, because the run did complete.
5. The weekly email and the dashboard show the count and the first five lines.

Test: a canned miner response containing one violation of each kind produces eight rows, with the right checker names.

## Phase 2 — enforce at the line, not the document (trade-off T2)

Refusing the whole rewrite would freeze the playbook on any single defect, which trades Goal 4 away for Goal 1. So enforcement is **line-level**. Severity per checker:

| Checker | Severity | Action on the offending line |
|---|---|---|
| provenance: edge claim with no `[rule:]` tag, or a tag naming a non-VALIDATED rule | block | replace with `[withheld by integrity check: provenance — <rule id or "untagged">]` |
| WR/avgR shown at n < 8 | block | replace with `[withheld: WR/avgR at n<8]` |
| calibration or selection-edge row with numbers below the floor | block | the system re-renders that table from the deterministic source (`_calibration_md`, `_selection_edge_md`) |
| Current Best Edge cites a negative R, or any blocked line sits in that section | block | the whole section is replaced by a **system-rendered** block: `NO VALIDATED EDGE YET`, plus QUARANTINED rules from `research_rules` with forward n / required, computed |
| Runner Lane: missing, small-n mean, or contradictory tag | block | the section is replaced by `NO_RUNNER_RULES` plus the deterministic counts from `runner_md`. The selector then acts on "no validated rules", which is the safe default. |
| sizing banner missing or duplicated | warn → auto-fix | insert or de-duplicate the banner verbatim |
| open-question threshold drift | warn | logged; the registry in code stays authoritative |

Rules:
- Withheld text is stored in `playbook_violations.line`, so nothing the miner wrote is lost.
- The replacement text is fixed strings or deterministic renders. **No LLM is involved in repairs.**
- After repair, re-run every checker. The written document must produce zero `block` violations. If it can't be repaired to zero, write the prior version unchanged plus a changelog line naming the failure, and record `action='rewrite_rejected'`. This is the only whole-document fallback, and it is logged.

Tests:
- Each blocking shape is repaired and re-checks clean.
- A repaired Runner Lane is exactly what `runner_playbook_section` returns to the selector.
- The unrepairable case keeps the prior version and records it.

## Phase 3 — one definition of edge, owned by the system

1. The `## Reading This File` section becomes **system-owned**. `write_playbook` replaces whatever the miner wrote there with a template kept in code, so the definition of edge cannot drift between rewrites. The template says:
   - Edge = a rule in state VALIDATED (forward-validated; FDR-screened at discovery), cited with its `[rule: R…]` tag.
   - Confidence tiers and n ≥ 8 suppression, as today.
   - No mention of `t >= 1.5`.
2. `weekly_miner.md`, bumped to prompt-version 10:
   - Delete the §6 bar "(c) t >= 1.5" and the §5 "|t| < 1.5 means …" bullet.
   - §6 now reads: Current Best Edge names VALIDATED rules only; otherwise it states NO VALIDATED EDGE YET and lists QUARANTINED candidates.
   - Keep "cite q_value".
3. `_check_wr_suppression`: an avgR claim must cite `n=`, `stdR=` and **either** `q=` or `t=`. It still requires n and a dispersion figure; it stops mandating the uncorrected statistic.
4. Prompt, docs and code change in the same commit. Record the prompt version in the commit message.

Tests:
- The template survives a miner rewrite that edits it.
- A line citing `n=, stdR=, q=` passes.
- A playbook stating "t >= 1.5" as the edge bar is flagged.

---

## Constraints

- No checker is weakened. Phase 2 changes what happens *after* detection, never the detection.
- The weekly loop must still complete after its expensive LLM call. Every repair runs under `integrity._safe`-style guards. If a repair throws, write the prior version and record it; never lose the run.
- Writer isolation: repairs happen inside `weekly.run` → `write_playbook`. No other loop gains a write path.

## Acceptance (production evidence)

- The first weekly run after shipping shows rows in `/api/table/playbook_versions?limit=1` and in `/api/table/playbook_violations`. The latest version has zero `block` violations remaining in its text; re-run check 2 to confirm.
- The dashboard health panel shows the violation count for that rewrite.
- Update G4.1 in the self-assessment, citing the version number and `run_id`.
