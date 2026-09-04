# Log Out — Session Close

The operator is signing off. Execute the full session close protocol from CLAUDE.md now.

## Steps (execute in order)

1. **Compute JSONL-sourced session duration:** Run `bash bin/compute-session-duration.sh`. Capture its four output lines (START / END / WALL / NOTE). These are the canonical times for steps 4 and 5 — **do not** round, fudge, or substitute Claude's recollection. If the helper errors, surface that to the operator before continuing; do not fall back to guesses.

2. **Queue auto-commit EARLY:** Run `bash "$(git rev-parse --show-toplevel)/.claude/hooks/end-session.sh"`. Moved to step 2 (was step 6 prior to 2026-05-22 B3 fix) so the sentinel is dropped BEFORE the reconciler runs. If the reconciler errors or you bail mid-close, the next Stop event still triggers the commit path and the session's work is preserved. The actual commit fires at the END of this turn regardless of step ordering.

3. **Brain sync moved to step 7.5 (Codex round-2 High, 2026-07-24).** The synchronous in-session
   brain sync now runs AFTER reconciliation (step 5) and the memory writebacks (steps 6-7) — so the
   reconciler's edits, the session-log close block, and the current-state narrative are all INSIDE
   what gets extracted and embedded. Running it here (before them) left every close's own memory
   writes as untracked tail. Nothing to do at this step — continue; do NOT spawn any extraction here.

4. **Self-audit:** Was lazy-loading correct? Any lessons to append to `tasks/lessons.md`? Any decisions to append to `memory/decisions-log.md`?

5. **Spawn close-reconciler:** Launch `agents/close-reconciler/` with:
   - Today's session file (`sessions/YYYY-MM-DD.md`)
   - `memory/current-state.md`
   - All active project files in `memory/projects/`
   - Session summary from context
   - **The four duration lines from step 1, verbatim.** Brief instructs the reconciler to use them unchanged in any session-heading or duration text it proposes.
   **Apply by confidence tier (2026-06-09 — "fully update so memory matches"):** the reconciler is read-only by design because it can misclassify WIP as done — so split on its own tier. **CLOSE** items (explicit session-log evidence it's done — high confidence, low misclassification risk) → **auto-apply** them. **AMBIGUOUS / RECLASSIFY** (the misclassification tier) → **surface to the operator**, apply only on their ok. This evolves the original gate (Apr 2026: human-approves-every-edit) into human-approves-only-the-uncertain — the safety rationale is preserved exactly where it applies. PARTIAL: apply the proposed wording when it's a pure staleness update; surface if it implies a status judgment.
   **RECEIPT MOVED TO STEP 7.8 (2026-07-26).** Do NOT run `reconcile-receipt.py write` here. The
   receipt binds to the in-scope content state at write time, and steps 6–7.7 BELOW (session-log
   writeback, current-state narrative, hub banners, decisions-log appends, self-knowledge fixes)
   all modify in-scope files — so a receipt written here is invalidated by the close's own later
   steps. That is exactly what downgraded the 2026-07-26 12:06 close to "(defensive-save — no
   explicit close)" despite a full, clean close: the gate compared the turn-end inventory against
   a step-5 receipt and correctly found post-receipt changes. Apply the reconciler's proposals
   here; write the receipt at 7.8, after the last in-scope write.

6a. **Self-knowledge is auto-maintained — verify, don't redo.** `capabilities.md` ("what this Core does") is regenerated from live config by the close hook (`session-lifecycle.sh` → `gen-capabilities.py`) on every close, so do NOT hand-edit it. If `check-self-knowledge.py` flagged CLAUDE.md prose drift or a stale `core-profile.md` (the unwireable residue), fix that now — it's the one self-knowledge piece a human still owns.

6. **Update session file + stamp state (SCRIPTED — Phase 1):** Compose the 1-2 sentence close summary (judgment), then run the deterministic writeback instead of hand-formatting it:
   ```
   bash bin/close-writeback.sh --summary "<your one-liner>" --start "<START>" --end "<END>" --wall "<WALL>" --tz <TZ> --model "<Model>"
   ```
   This appends the standard close block to `sessions/YYYY-MM-DD.md` (verbatim START/END/WALL format), stamps `current-state.md` line-1 `Last updated:`, and logs a close-cost line for `/core-si` to trend. It **does not** touch the current-state narrative — that stays step 7 (judgment). Add `--dry-run` to preview. If WALL > 3h, note `(includes idle gaps)` in the summary text per the helper's NOTE.

7. **Update current-state.md NARRATIVE (judgment — line-1 stamp already done by step 6's script):** Prepend the new "Latest session" line (same START → END (WALL) format), demote the prior one to "Prior", prune anything older than 2 sessions, reflect deferred-list changes. This is the judgment half — do NOT re-stamp line 1 (the script did it).

7.5. **Brain sync — SYNCHRONOUS, IN-SESSION, MANDATORY (2026-07-24: in-session extraction, NO headless, NO API key).**
   `/close-core` IS the full close — graph extraction + embedding finish NOW, inside the session, on the
   session's OWN subscription auth (`Agent()` subagents — never `ANTHROPIC_API_KEY`), so the next start has
   NOTHING to do. Positioned HERE — after reconcile (5) and the writebacks (6-7) — so this close's own edits
   are captured too. Two parts, in order:

   **ORDER (Codex rounds 1-2): CAPTURE → EXTRACT → EMBED.** The current session's vault `.md` is written by
   the CAPTURE exporter (`export.py`, via `capture_worker.py`) — NOT by `run-brain-update fast` (its
   `extract-core-sessions.py` only READS existing vault md). And capture normally runs at the Stop hook, AFTER
   this step. So capture must be run EXPLICITLY here, FIRST, or extraction never sees the current session.

   **(1) CAPTURE — export this session's JSONL → vault markdown (deterministic, no LLM):**
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" \
     CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" bash -c '
       cd "$CORE_INSTANCE" && python3 scheduling/brain-pg/discover.py && python3 scheduling/brain-pg/capture_worker.py'
   ```
   This makes the current session's `$CORE_BRAIN/projects/<org>/sessions/*.md` exist before extraction scans it.
   Idempotent (skip-existing + drain), so the Stop-hook capture later is a no-op. If it errors, surface + re-run.

   **(2) GRAPH extraction — in-session (the LLM part):** run
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" \
     CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" \
     bash scheduling/graphify-brain/extract-pending.sh --phase close
   ```
   Read its LAST line — `EXTRACT-STATUS: …` disambiguates the outcome (Codex #2):
   - `none-pending` → detection ran, genuinely nothing pending → OK to proceed to the marker.
   - `skipped (…)` → prereq absent (no brain/prompt/python) = **couldn't check**, NOT "nothing pending" →
     **do NOT mark synced**; surface to the operator (this Core's extraction machinery is missing/misconfigured).
   - `detect-error` (exit 2) → detection FAILED → **do NOT mark synced**; surface to the operator, re-run.
   - a **MANDATORY dispatch directive** (pending evidence) → **follow it exactly**: spawn the **Haiku**
     subagent(s) (`model: haiku`, `run_in_background: false`, Read+Write, one per `pending-batch-NN.txt`); run
     its **VERIFY-BEFORE-MERGE** gate; on MISSING/UNPARSEABLE do the **repair-retry** (Haiku, "re-emit valid
     JSON, escape quotes"), then a **Sonnet** fallback for any file still bad; only then `merge.py` →
     `embed.py --graph-nodes`. YOU (the live session) are the parent that writes/verifies.

   **(2b) ASSERTION extraction — in-session (the recall/supersession layer; MANDATORY, 2026-07-24).**
   Graph nodes are NOT enough — `recall_similar` serves the **assertion** layer, and it goes stale unless
   decisions become assertions every close (this was orphaned pre-07-24; recall served reversed decisions).
   Run it in-session, same pattern as (2):
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" \
     python3 scheduling/brain-pg/decisions_segment.py            # deterministic: tag new decisions + enqueue jobs
   ```
   Then list pending `semantically_interpreted` jobs. The exact query, because two details in it are
   load-bearing and both bit on 2026-07-30 — **`status` is lowercase `pending`** (a `WHERE status='PENDING'`
   returns zero rows and reads exactly like "nothing to do", silently skipping the whole assertion layer),
   and `stage_jobs` joins to `sources` **through `source_revisions`**, not directly:
   ```sql
   SELECT j.id, s.source_key FROM stage_jobs j
     JOIN source_revisions r ON r.id = j.source_revision_id
     JOIN sources s ON s.id = r.source_id
   WHERE j.stage='semantically_interpreted' AND lower(j.status)='pending' AND j.org_id=<org>;
   ```
   Pull each decision's body from `decisions-log.md` by its `<!-- core-decision-id: d_<hex> -->` comment —
   note the id in the comment CARRIES the `d_` prefix, and `source_key` is that same prefixed form. If
   any are pending, spawn a **Sonnet** subagent (Read-only, RETURNS JSON — NOT write) that emits, per decision,
   `{"decision_id","assertions":[{subject_key,predicate,object,applicability:{target_org_ids:[1]},confidence,
   reverses?}]}`. **subject_key AND predicate MUST BOTH be reused across decisions about the same thing** so a reversal links. Supersession keys on the PAIR — assertions_ingest._find_stale_same_subject matches `subject_key=%s AND predicate=%s`, because a (subject_key, predicate) pair is single-valued by construction and a newer row replaces the older one on recency alone. Reusing the subject_key while writing a NEW predicate for the changed state — which is the natural thing to write, and what this instruction used to invite — creates no competition, supersedes nothing, and leaves BOTH rows active so recall serves the reversed decision as current. `reverses.explicit` does NOT rescue it: that block is vestigial since the 2026-07-25 auto-supersession change and zero rows in the live DB carry it. Found by core-business 2026-08-28, live, on two decisions five minutes apart about the same hook. (e.g.
   a new "close cycle is synchronous" decision reuses the prior "close cycle is detached" subject_key with
   `reverses.explicit=true`). YOU (parent) write the returned JSON to a file, `json.loads`-verify it, then:
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" \
     python3 scheduling/brain-pg/assertions_ingest.py <file>
   ```
   Verify `jobs_done == pending count`. (Note: a reversal only links if the prior assertion already exists from a
   PRIOR ingest — a first-time backlog ingests old+new together and won't mark the old superseded; that's cosmetic,
   the new one still ranks + serves.) If this step is skipped/fails, DO NOT set the marker.

   **(2c) CONSOLIDATION — the success half of learning; in-session, same pattern as (2) and (2b).
   MANDATORY (Phase C/D3, 2026-08-05).**
   Everything above learns from FAILURE: a correction happened, an artifact got minted. This step is
   the only one that learns from what WORKED. It reads whole sessions, keeps the stretches that ended
   in acceptance rather than a correction, and turns a sequence seen in **two independent sessions**
   into a behaviour that actually fires. Without it the brain accumulates workflows nothing reads —
   which is exactly the state this was in until 2026-08-05, when `MIN_SESSIONS_TO_GENERATE` was
   referenced only by a print statement.
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" \
     python3 scheduling/brain-pg/consolidate_sessions.py --prepare --limit 5
   ```
   - Prints `nothing accepted-and-unconsolidated` → nothing to do, proceed.
   - Writes `.claude/state/consolidate-pending.json` → **you are the extraction pass.** Read
     `payload['brief']` and apply it to each session's `windows`, then write the result and run
     `--apply <file>`. The brief's hard rules matter: minimum 2 steps, only steps that ACTUALLY
     happened in that window, and `trigger_prompts` copied **verbatim** — a paraphrase fails the
     installer's positive test and the workflow is silently refused.
   - `{"workflows": []}` is a correct and common answer. Inventing one puts a fabricated procedure in
     the brain that later fires as durable instruction, which is worse than capturing nothing.
   - Then mint any workflow that has now reached two sessions:
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" python3 -c \
     "import sys;sys.path.insert(0,'scheduling/claude-si');import friction_loop as fl,json;\
      print(json.dumps(fl.generate_from_workflows(__import__('_env').get_org_id(__import__('pathlib').Path('scheduling/claude-si')),dry=False),indent=1))"
   ```
   Read `skipped` — a refusal is informative, not a failure. "no stored trigger prompt" means Phase C
   captured the workflow before trigger prompts existed; "no real corpus neighbour" means the trigger
   could not be grounded. Neither blocks the close.
   - Finally, keep already-installed workflow behaviours current with the brain (D1/D2):
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" python3 -c \
     "import sys;sys.path.insert(0,'scheduling/claude-si');import friction_loop as fl,json,os;\
      print(json.dumps(fl.refresh_workflow_payloads(__import__('_env').get_org_id(__import__('pathlib').Path('scheduling/claude-si'))),indent=1))"
   ```
   Re-renders and RE-PINS the payload hash for any artifact whose brain steps changed, so an
   installed behaviour follows the workflow instead of freezing at mint time.
   - And flag workflows whose steps have stopped working (Phase W):
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" \
     python3 scheduling/brain-pg/consolidate_sessions.py --flag-stale-steps
   ```
   A workflow that FIRED in a session which then produced a correction is flagged: the procedure was
   offered and the outcome still needed fixing. It flags rather than rewriting — choosing which step
   was wrong is a judgement pass, and a deterministic guess would corrupt the sequence more cheaply
   than it would fix it. The next 2c extraction re-derives from the newer session. Any `errors` entry
   saying a brain Workflow "no longer exists" means a behaviour is still firing for something the
   brain has dropped — surface that to the operator rather than retiring it here.

   **(2d) ASK DISTILLATION — without this the loop is blind to most of its own evidence.
   MANDATORY, and check the backlog number every close (2026-08-05).**
   A recorded correction is useless to the loop until it has a `canonical_ask`: `ask_miner.ask_cases()`
   clusters on that column and `friction_router` refuses any case without it. Producing one needs a
   model, so it can only run from here — a hook cannot call Claude, and the headless API path was
   retired 2026-07-24. Nothing called it until this step existed, and the cost was not small:
   **916 of 1,203 corrections on life (76%) had never been distilled**, so every "healthy" number the
   pipeline reported was computed over a quarter of the corpus. `bin/core-si-close.py` now measures
   this backlog and warns at ≥25%, but only THIS step can clear it.
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" python3 -c \
     "import sys,json;sys.path.insert(0,'scheduling/claude-si');sys.path.insert(0,'scheduling/brain-pg');\
      import ask_miner as am,os;\
      print(json.dumps(am.extract_pending(__import__('_env').get_org_id(__import__('pathlib').Path('scheduling/claude-si')),200),indent=1))"
   ```
   - Empty list → backlog clear, proceed.
   - Otherwise **you are the extraction pass.** Batch the rows (~120 each), and for each batch spawn a
     **Haiku** subagent (pin `model: "haiku"` — this is mechanical extraction, not judgement) with the
     contract below. Haiku in-session is only safe with the three conditions from `subagents.md`:
     the parent validates every output file exists and `json.loads`-parses, repairs or re-runs an
     invalid one, and falls back to Sonnet if a batch fails twice.
   - The extraction contract, which the downstream router enforces and will silently discard
     violations of: imperative mood; **≤160 chars**; **no first- or second-person pronouns**; no
     profanity; no pasted output, paths or code; `type` is exactly one of `constraint` / `procedure` /
     `none`; and `ask: null` + `type: "none"` for pure frustration, one-off factual corrections,
     approvals, and anything where the durable want cannot be read without guessing.
   - **A high null rate is correct.** Roughly half of real corrections carry no durable ask. A batch
     of confident-sounding asks for every row is a bad batch — a fabricated ask pollutes a cluster and
     can cause a wrong rule to be built and then fired at the operator, whereas a null costs nothing but marks
     the row processed so it is not re-extracted forever.
   - Then write the results back (the PARENT writes, per the dispatch-verify standard):
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" python3 -c \
     "import sys,json,os;sys.path.insert(0,'scheduling/claude-si');sys.path.insert(0,'scheduling/brain-pg');\
      import ask_miner as am;pairs=json.load(open('<merged-output.json>'));\
      print('cached',am.cache_asks(__import__('_env').get_org_id(__import__('pathlib').Path('scheduling/claude-si')),pairs))"
   ```
   `cache_asks` re-validates the closed type vocabulary itself and stores an unrecognised label as
   NULL rather than trusting it, so a bad `type` degrades to the router's default instead of
   corrupting routing. Confirm the cached count equals the number of rows you sent.

   **(3) Evidence embed (deterministic) — RESUMABLE, so a timeout is not a failure (2026-07-25).**
   `embed.py --incremental` processes in batches and resumes where it stopped, so a Bash timeout loses
   NO work — it just means the drain didn't finish yet. Treat a timeout as **"run it again"**, never as a
   terminal error, and re-run until it exits 0 (typically 1-2 passes; each pass starts where the last ended).

   This matters because the step blocks on the shared brain lock with NO timeout (Nick's 2026-07-24 call:
   all Cores queue until the queue clears) *inside* a capped Bash call. If a peer Core or the nightly holds
   the lock, the step can spend its whole budget WAITING and exit 143 having done nothing wrong — which is
   precisely what produced the misleading `NO sync marker` on 2026-07-24 (exit 143 at "batch 78/78", an
   embed that was nearly done). A capped call around an uncapped wait is a false-failure generator; the
   re-run loop is what makes it correct without breaking the queue semantics Nick asked for.

   Run (20-min Bash timeout — `timeout: 1200000`), and re-run on timeout until exit 0:
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" \
     CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" \
     bash "$(git rev-parse --show-toplevel)/.claude/hooks/run-brain-update.sh" fast
   ```
   `fast` = frontmatter-node extraction + evidence embed. No Anthropic key. (It DOES use `VOYAGE_API_KEY` for
   the embed — always did; unrelated to the retired extraction key.) Non-zero → surface, re-run once.

   **(4) MARKER — minted by a VERIFIER SCRIPT, never by you (2026-07-25).** Do NOT `touch` this marker.
   It used to be a bare touch you issued after judging your own prior steps successful — the one close
   artifact nothing could contradict, unlike `.reconcile-ran` which a hook mints. On 2026-07-24 the close
   told Nick "everything's certified current" while its own log recorded `NO sync marker`. Run:
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" \
     CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" \
     python3 "$(git rev-parse --show-toplevel)/bin/verify-brain-synced.py" --mint
   ```
   It ignores every claim about what happened and RE-RUNS the detectors: extraction must report
   `none-pending` (`skipped`/`detect-error` = couldn't-check = NOT a pass), zero NULL embeddings on
   entities+evidence, and `brain_status` READY. Exit 0 = verified + marker minted; exit 1 = not synced,
   marker withheld. **If it exits 1, say so plainly to the operator and do NOT work around it** — a false "not
   synced" costs one catch-up next close; a false "synced" silently loses brain currency.

   Notes: the shared brain lock QUEUES (blocking, no timeout — waits its turn behind a peer/nightly). A
   defensive save (walk-away) does NONE of 7.5 — the debt drains at the next `/close-core`.

7.6. **HUB REFRESH — compiled truth (2026-07-25; closes the "all the hubs are up to date" gap).**
   The operator's standing bar, paraphrased: everything reconciled, embedded, and up to date —
   **hubs included** — with nothing left to do when the next session starts.
   Until now `compile-truth-refresh.py` was called ONLY from `session-start-truth-drift.sh` at SessionStart
   — so drifted hubs survived a perfectly clean close and greeted the next session as a "blocker" the close
   could never have fixed. Runs HERE, after extraction (7.5), so it sees this session's own new evidence.

   **Detect first — this is drift-GATED, never unconditional:**
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" \
     CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" \
     python3 scheduling/brain-pg/compile-truth-refresh.py --detect --partition
   ```
   - **0 drifted → say "hubs current" and go to 7.7.** No spend, no fan-out. This is the common case.
   - **1-8 drifted → refresh now.** Spawn ONE **Sonnet** subagent per `refresh-batch-NN.json`
     (`run_in_background: false`, **Read-only** — the subagent RETURNS raw JSON, YOU write the out-file;
     dispatch-verify standard).

     **Each brief MUST OPEN with the exact words `Compiled-truth hub refresh worker`** — nothing
     before them. This is not style. Worker transcripts are kept out of brain extraction by matching
     that opening against `scheduling/graphify-brain/pipeline-exhaust.json`; a refresh transcript
     that leaks in gets indexed as new evidence ON THE HUB IT JUST REFRESHED, so that hub instantly
     re-drifts and the next close refreshes it again — a treadmill, observed 3x in 2 days on
     `sentinel-security-review`. Until 2026-07-28 this step named no phrase at all, so every close
     invented one; three phrasings shipped, the filter matched none, and **24 leaked transcripts
     were 57% of the entire pending backlog**. If you must reword the opening, add the new wording
     to `pipeline-exhaust.json` in the SAME edit — never one without the other.

     Then `json.loads`-verify every out-file and confirm each `(kind, name)`
     matches its batch input BEFORE ingesting:
     ```
     CORE_BRAIN="$HOME/AI Projects/core-brain" CORE_INSTANCE="$(git rev-parse --show-toplevel)" \
       python3 scheduling/brain-pg/compile-truth-refresh.py --ingest
     ```
     **Read the APPLIED count, and treat any `⚠ SHORTFALL` line as a stop.** `--ingest` now prints  <!-- privacy-ok: generic engineering vocabulary -->
     `Refresh-ingested <applied> entities (from <submitted> submitted)` — those two numbers are not the  <!-- privacy-ok: generic engineering vocabulary -->
     same thing, and until 2026-07-30 only the submitted one was printed. The instruction here used to be
     "the ingest count MUST equal the drifted-hub count"; on 2026-07-30 that check read 3 == 3 and passed
     while one hub was written and one Sonnet refresh was discarded, because `--partition` had emitted a
     `(kind, name)` pair that exists in no DB row. Both halves are fixed in code (partition matches on the
     pair; ingest reports `cur.rowcount`) and guarded by `bin/tests/test_hub_refresh_identity.py` — but the
     number to check is the applied one. Note also that the out-file `(kind, name)`-matches-batch check  <!-- privacy-ok: generic engineering vocabulary -->
     CANNOT catch this class: a worker handed a bad pair returns that same bad pair, and matches.
   - **>8 drifted, or estimated cost >$1 → TELL NICK THE SIZE FIRST and get an explicit go.** This is a
     Sonnet fan-out against his capped window; the standing rule is warn-before-multi-agent-spend
     (`.claude/rules/subagents.md`). Never silently fan out a large refresh at close.

   If the refresh fails or is deferred, say so in step 9 — do NOT let 7.5(4) mint a "synced" marker while
   claiming hubs are current; they are separate claims.

7.7. **SELF-KNOWLEDGE — the map vs the live system (2026-07-25).**
   `gen-capabilities.py` already regenerates `memory/capabilities.md` on every close (deterministic, in the
   controller). But `tasks/system-rundown.md` and the Core Atlas are HAND-AUTHORED — `check-self-knowledge.py`
   only *detects* their drift at SessionStart and nothing ever repairs it, so on 2026-07-24 the map was
   missing 5 live hooks (`friction-watchdog`, `deliverable-format-gate`, `friction-dispatch`,
   `session-presence`, `stay-scoped`) and had been for weeks. Run:
   ```
   CORE_INSTANCE="$(git rev-parse --show-toplevel)" python3 bin/check-self-knowledge.py
   ```
   - **No drift → say so, move on.**
   - **Drift reported → FIX IT NOW, in this close.** It names the exact missing/stale hooks; edit
     `tasks/system-rundown.md` (and the Atlas source if a hook row is absent there too) to match live
     `settings.json`. This is a small prose edit, not a rebuild — do it rather than deferring, because
     deferring is exactly how it reached weeks of drift. Re-run the check to confirm it clears.

7.8. **RECEIPT — LAST in-scope act of the close (moved from step 5, 2026-07-26).** Run
   `python3 "$(git rev-parse --show-toplevel)/bin/reconcile-receipt.py" write`. This writes the
   reconcile receipt the close controller (`session-lifecycle.sh close full`) REQUIRES — without a
   receipt matching the turn-end inventory, the Stop-hook close **downgrades to a defensive save**
   (carrying the delta via `.reconcile-pending`) and stamps "(defensive-save — no explicit close)".
   It sits HERE because it binds to the in-scope content state at write time: every in-scope write
   the close makes (steps 6–7.7) must come BEFORE it, and NOTHING in-scope may be edited after it
   this turn (steps 8–9 are read-only + prose to the operator; the controller's own prune/generators run
   inside the gate-checked close and don't count). If it prints **REFUSED**, the close-reconciler
   never actually ran (SubagentStop minted no evidence) — spawn it (step 5), then re-run. If
   SessionStart warned of **UNRECONCILED WORK carried**, that must have been reconciled in step 5
   (the gate blocks until `.reconcile-pending` clears).

8. **Brain status check (Plan A 2026-07-23 — replaces freshness-gate.py):** Run
   `CORE_INSTANCE="$(git rev-parse --show-toplevel)" CORE_BRAIN="${CORE_BRAIN:-$HOME/AI Projects/core-brain}" python3 scheduling/brain-pg/brain_status.py`.
   `brain_status` compares the ledger (what's captured) against DISK reality + the job queue — it SEES the
   whole-session lag the old brain-vs-brain freshness-gate never could. Under the synchronous-close model
   (step 3 ran foreground and you verified exit 0), the expected verdict here is **READY** — say so to the operator.
   **LAGGING/FAILED at this point is NOT normal anymore** (there is no detached worker still finishing):
   it means step 3 didn't fully land — surface the detail, re-run step 3, re-check. The only acceptable
   residual lag is the tail of THIS close conversation itself (captured by the trailing autosave, drained
   at the next close).

9. **Give the operator a closing response:** Brief summary of what was done, what's deferred, and any open items. Any time-claim ("we worked Nh", "started at X") must reuse the helper's output — no rephrasing into different numbers.
