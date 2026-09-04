# Incident history moved out of `.claude/rules/session.md`

The rules stay in the rules file; the dated history explaining why each exists lives here. Verbatim, never deleted.

## Rot warning — threshold history and the archived staleness check (moved 2026-08-27)

- **Rot warning (active signal).** UserPromptSubmit hook `rot-check.py` computes Core-ASI v2 over the last 50 assistant turns. When ASI < τ=0.60, ABA fires (baseline rules re-injected) — the value in `rot-check.py`, lowered from 0.65 on 2026-06-23 (fix RC3). On a `ROT WARNING`, prepend the `[ROT signal: …]` line and continue with refreshed rules. The supply-side `staleness-check.sh` is ARCHIVED as of 2026-05-15 (`.claude/hooks/archive/`). On-demand health readout lives in `/health` (self-contained — reimplements the metrics inline).
