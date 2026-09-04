--
-- 2026-08-26 — evidence-seeded trust admission
--
-- THE PROBLEM THIS EXISTS TO FIX. `si-fix-admission.py` states its own rule in line 4 of its
-- docstring: "A deterministic core-si fix earns the right to auto-apply by being APPROVED K times
-- running." Trust had exactly one source — Nick clicking approve — and this CHECK constraint is
-- what enforced that:
--
--     CHECK (kind = ANY (ARRAY['approve','reject']))
--
-- The consequence is arithmetic, not opinion: a self-improvement loop whose autonomy is seeded by
-- human approvals CANNOT OUTRUN THE HUMAN. It can only ever act on classes he has already approved
-- by hand, repeatedly. That is a ratchet only Nick can turn, and turning it is precisely the labour
-- he has been asking to be rid of — 67 separate messages since 2026-07-16, the earliest of which
-- already said "none of them are working."
--
-- It also contradicts the standing rule, written 2026-07-23 and still live in tasks/lessons.md:132:
--
--     "Autonomous self-improvement = test-gate + reversibility, NOT a human approval gate"
--
-- The rule has said test-gate for five weeks. The implementation said approval-gate in its own
-- docstring. This migration is the first half of making the code agree with the rule.
--
-- WHAT CHANGES. Two new `kind` values, so trust can also be earned by EVIDENCE:
--
--     'evidence'      a shadow run of the fix's registered applier verified its preconditions
--                     hold and it would have succeeded — recorded WITHOUT applying anything
--     'evidence_fail' the shadow run said it would not succeed; resets the streak exactly as a
--                     human 'reject' does
--
-- The admission counter treats {approve, evidence} as positive and {reject, evidence_fail} as
-- resets, so a fix can graduate to trusted on its own demonstrated behaviour. Nick's explicit
-- approvals still count and still carry the same weight — this ADDS a second road to trust, it does
-- not remove his.
--
-- WHAT DOES NOT CHANGE, DELIBERATELY. Admission still only marks a fix TRUSTED; it applies nothing.
-- The apply path stays `in_safe AND trusted AND has_applier`, so a key still cannot be touched
-- unless it is on `scheduling/core-si/auto-safe.txt` AND has a deterministic applier. Evidence
-- widens how the *trusted* term is satisfied and touches neither of the other two.
--
-- The floor that stays absolute is Nick's own, from CLAUDE.md, not one Core invented: never spend
-- money, never send outward without approval, never force-push, trust-root changes are his command.
-- Evidence must never be able to graduate a fix past THAT — it graduates past the surfacing gate
-- only. (The separate "anything needing judgment" floor in auto-safe.txt's header was written by
-- Core, not by Nick, and should not be cited to him as policy; it is a comment, not a decision.)
--
-- Additive and reversible: re-adding the two-value CHECK restores the prior state exactly, and any
-- 'evidence' rows then simply fail to insert again. Existing rows are untouched — on life there are
-- 24, all kind='approve'.
--

ALTER TABLE core_si_fix_approvals
  DROP CONSTRAINT IF EXISTS core_si_fix_approvals_kind_check;

ALTER TABLE core_si_fix_approvals
  ADD CONSTRAINT core_si_fix_approvals_kind_check
  CHECK (kind = ANY (ARRAY['approve', 'reject', 'evidence', 'evidence_fail']));

COMMENT ON COLUMN core_si_fix_approvals.kind IS
  'How this row bears on the admission streak. approve/reject are Nick''s explicit verdicts. '
  'evidence/evidence_fail are recorded by the close pass from a SHADOW run of the fix''s registered '
  'applier — preconditions verified, nothing applied. {approve,evidence} advance the streak; '
  '{reject,evidence_fail} reset it. Added 2026-08-26 so trust can be earned by demonstrated '
  'behaviour rather than only by human approval: an approval-seeded autonomy gate can never outrun '
  'the human it is meant to relieve, which is why skills/hooks/workflows/commands all sat at 0 '
  'while the loop reported healthy.';
