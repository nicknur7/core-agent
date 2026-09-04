export const meta = {
  name: 'triad-review-v1',
  description: 'Scope the pending diff, then run independent adversarial reviews before a blast-radius change ships.',
  whenToUse: "Before shipping a system/code change with real blast radius — the operator's most-repeated standing ask (adversarial review before shipping, 17 recorded moments per artifact_typer.py's ORACLE_CATALOG).",
  phases: [
    { title: 'Scope', detail: 'describe the pending diff — read-only, mechanical', model: 'sonnet' },
    { title: 'Review', detail: 'independent adversarial reviewers, each told to find reasons NOT to ship', model: 'fable' },
  ],
}

// CATALOG SCRIPT — reviewed and hash-pinned in workflow_catalog.py's EXPECTED_SCRIPT_HASHES.
// This file is never executed by the SI pipeline itself (friction_dispatch / friction_installer /
// artifact_generator never eval/exec/subprocess anything — see test_static_no_codegen). It is
// executed ONLY by the Workflow tool, and ONLY after the operator has typed the /slug command whose body
// names this exact file and its pinned hash. See workflow_catalog.py's module docstring for the
// empirically-verified reason a subagent's tool calls here still pass through the same PreToolUse
// chain (pretooluse-guard.sh) that gates the interactive session — that is what lets this script
// stay honest about being "read-only": the guard enforces it structurally, not this file's wording.
//
// args: { glob?: string } — every value that reaches this script was validated against
// workflow_catalog.py's closed param schema BEFORE the /slug command that launches this workflow
// was ever generated. Nothing here is free text lifted from a mined correction.
const glob = (args && typeof args.glob === 'string') ? args.glob : '.'

phase('Scope')
const scope = await agent(
  'Run `git status --porcelain` and `git diff --stat -- ' + glob + '` in the current repository. ' +
  'Report, as plain text: the list of changed files and a short, factual summary of what changed. ' +
  'This is a READ-ONLY reconnaissance step: do not Edit, Write, stage, commit, or push anything, ' +
  'and do not run any network command.',
  { model: 'sonnet', label: 'scope' }
)

phase('Review')
const REVIEWERS = 3
const reviews = await parallel(Array.from({ length: REVIEWERS }, (_, i) => () =>
  agent(
    'You are an independent adversarial reviewer for a change about to ship. Here is the scope of ' +
    'the pending change:\n\n' + scope + '\n\nFind the strongest reason this should NOT ship as-is — ' +
    'a correctness risk, an untested edge case, a design that should be simpler, or a step that is ' +
    'not reversible. Read the actual changed files if that helps you (Read/Grep/Glob only). You may ' +
    'NEVER Write, Edit, run git push, curl a non-local URL, send a message, or take any other ' +
    'outward-facing action — this is a review, not a fix. Return your single strongest finding as ' +
    'plain text, or "no blocking finding" if you genuinely see none.',
    { model: 'fable', label: 'review-' + (i + 1), phase: 'Review' }
  )
))

return { scope, reviews: reviews.filter(Boolean) }
