---
name: claude-brain
description: Recall past session context from the Claude Code Brain vault at $CORE_BRAIN. PROACTIVELY ACTIVATE before responding when the user (a) names any person from their memory/relationships/; (b) names any project from their memory/projects/; (c) references past conversations or sessions ("we talked about", "yesterday", "last time", "remember when", "we discussed"); (d) makes strategic planning references that depend on past context (partners, meetings, intros, next steps); OR (e) uses any explicit recall phrase ("let's work on X", "check my brain for Z", "what have we done on X"). Read-only — never write to the vault.
---

# Claude Code Brain — Recall Skill

Surfaces relevant session history from the brain vault BEFORE touching code or making changes.
Paths resolve from `$CORE_BRAIN` (the vault) and `$CORE_INSTANCE`/`$CLAUDE_PROJECT_DIR` (the Core
repo) so this works on any machine, not just the author's.

## When to activate

Activate PROACTIVELY on ANY of these. The bar is low on purpose — when in doubt, query first.

- **Explicit recall:** "let's work on [X]", "last time on [X]", "check my brain for [X]", "where did we leave off on [X]", "pull up [X] history".
- **Named person:** the user references anyone in `$CORE_INSTANCE/memory/relationships/` → query the brain on that name.
- **Named project:** the user references any project in `$CORE_INSTANCE/memory/projects/` → query the brain on it.
- **Past-conversation markers:** "we talked about", "yesterday", "I told them", "we discussed", "we decided", "remember when", "the meeting", "last call".
- **Strategic planning that depends on past context:** partners, co-founders, accelerator, intros, next steps with [person].

**When in doubt:** query. An extra recall call (~5s) is far cheaper than answering without context that exists.

## Recall workflow (do all steps before acting)

### Step 1 — Hybrid RRF query (primary path)

Check Postgres is reachable:
```bash
psql -d corebrain -c 'SELECT 1' >/dev/null 2>&1
```
If not available, skip to Step 2 (grep fallback) immediately.

Run hybrid query:
```bash
python3 "${CLAUDE_PROJECT_DIR:-$CORE_INSTANCE}/scheduling/brain-pg/query.py" --k 10 "<topic from the user's message>"
```
Returns RRF-fused ranked rows with a `legs=` annotation — multi-leg hits (vector+fts+graph) are
stronger signal than vector-only. If it returns useful signal, go to Step 3. If Postgres is down or
zero rows, fall through to Step 2.

### Step 2 — Vault index + grep (fallback)

```bash
# project list:
sed -n '1,40p' "$CORE_BRAIN/README.md"
# grep sessions for the topic:
grep -rliE "<topic>" "$CORE_BRAIN/projects/" --include="*.md" 2>/dev/null | head -20
```

### Step 3 — Open project rollup
Read `$CORE_BRAIN/projects/<name>/_project.md` (session table + open items live here).

### Step 4 — Skim recent sessions
From the rollup's table, read the 2–3 most recent session files with `limit`+`offset` — skim, don't dump.

### Step 5 — Topic hub (optional, narrow topics)
`$CORE_BRAIN/topics/<slug>.md` or `$CORE_BRAIN/tools/<slug>.md` (slug = lowercased, spaces→hyphens).

### Step 6 — Surface findings
Before doing anything else, give a 2–4 sentence summary: what was done last time, what's open, any gotchas.

## Rules
- **Read-only.** Never write to or modify anything under `$CORE_BRAIN/`.
- **Skim, don't read fully.** Use `limit`+`offset`; don't dump whole sessions into context.
- **Summarize before acting.** Say what you found.
- **Scope to the project.** Load only the named project's rollup + recent sessions, not everything.
- **Degrade gracefully.** If Postgres/graphify isn't available, the grep fallback (Step 2) must still work. Never block on the primary path.
- **Writes happen via the Stop hook**, not this skill.

## Vault structure reference
```
$CORE_BRAIN/
├── README.md              ← project index (start here for grep path)
├── projects/<name>/
│   ├── _project.md        ← rollup + session table
│   ├── sessions/*.md      ← main session files (skim 2-3 recent)
│   └── subagents/*.md
├── topics/<slug>.md
├── tools/<slug>.md
└── entities/<slug>.md     ← sessions by person/product/company
```
