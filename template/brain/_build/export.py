#!/usr/bin/env python3
"""
Phase 3 Step 1 — Export Every Claude Code Session to a Markdown Vault
Writes to $CORE_BRAIN (default: ~/AI Projects/core-brain/)
Source: ~/.claude/projects/ (read-only)
"""

import json
import os
import re
import glob
import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
from pathlib import Path

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# ─── Config ──────────────────────────────────────────────────────────────────
SOURCE = Path.home() / ".claude" / "projects"
_vault_env = os.environ.get("CORE_BRAIN")
if not _vault_env:
    raise SystemExit(
        "export.py: $CORE_BRAIN env var is required. "
        "Set it before invoking (e.g., export CORE_BRAIN=~/AI\\ Projects/core-brain)."
    )
VAULT = Path(_vault_env).expanduser()
TOOL_RESULT_CAP = 4000  # chars

# Credential patterns to redact
CRED_PATTERNS = [
    re.compile(r'sk-ant-api[0-9A-Za-z_-]{30,}'),
    re.compile(r'sk-[A-Za-z0-9]{32,}'),
    re.compile(r'xai-[A-Za-z0-9_-]{30,}'),
    re.compile(r'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}'),  # JWT
    re.compile(r'ghp_[A-Za-z0-9]{36}'),
    re.compile(r'gho_[A-Za-z0-9]{36}'),
]

# Folder-name → cwd mapping (encoded folder name uses - for /)
def folder_to_cwd(folder_name: str) -> str:
    """Convert '-Users-you-AI-Projects-core' to '/Users/you/AI Projects/core'"""
    # The folders use - as separator, but paths have spaces too
    # We reconstruct by replacing - with / then fixing known path segments
    # Actually the encoding just replaces / with - and spaces with - too
    # We need to do best-effort: replace leading dash with /
    path = folder_name
    if path.startswith('-'):
        path = '/' + path[1:]
    path = path.replace('-', '/')
    # Fix known issues: "AI/Projects" should be "AI Projects"
    path = path.replace('AI/Projects', 'AI Projects')
    path = path.replace('Desktop/AI/Projects', 'Desktop/AI Projects')
    return path

def redact_credentials(text: str) -> str:
    for pat in CRED_PATTERNS:
        text = pat.sub('[REDACTED]', text)
    return text

def slug_from_cwd(cwd: str) -> str:
    """Turn /Users/you/AI Projects/core → core"""
    parts = [p for p in cwd.replace('\\', '/').split('/') if p]
    if parts:
        return re.sub(r'[^a-z0-9]+', '-', parts[-1].lower()).strip('-') or 'unknown'
    return 'unknown'

def full_slug_from_cwd(cwd: str) -> str:
    """Turn /Users/you/AI Projects/core → ai-projects-core"""
    parts = [p for p in cwd.replace('\\', '/').split('/') if p and p != 'Users' and not (len(p) < 15 and p[0].isupper() and p[1:].islower() and p not in ['Desktop', 'Documents', 'Projects'])]
    # Just use last 2 path segments
    seg = parts[-2:] if len(parts) >= 2 else parts
    return re.sub(r'[^a-z0-9]+', '-', ' '.join(seg).lower()).strip('-') or 'unknown'

# Multi-Core routing (spec-multi-core-architecture-2026-05-19.md Phase 4).
# Longest-needle-first ordering matters: 'core-business' must hit before any
# shorter substring like 'core-' would. Cwds with no rule fall through to
# slug_from_cwd (leaf segment).
#
# CORE_SLUG_ALIASES maps OLD vault slugs to NEW domain slugs for the query
# layer. 2026-05-19 brain vault unified: projects/core/ → projects/life/ via
# git mv, alias retained empty as a no-op stub in case downstream query.py
# or recall-similar still imports it (avoid AttributeError). Safe to remove
# the empty dict once we confirm no callers reference it.
# The Core that bare home-dir sessions (~/) belong to. There is no standalone "home" Core,
# so a session opened in $HOME has to land somewhere; it lands in the primary one. Override
# per-machine with CORE_PRIMARY_SLUG if your primary Core is not named "life".
PRIMARY_CORE_SLUG = os.environ.get("CORE_PRIMARY_SLUG", "life")

CWD_PROJECT_RULES: dict[str, str] = {
    'core-business': 'business',
    'core-school':   'school',
    'core-life':     'life',          # post-rename canonical
    'core-finance':  'finance',       # 2026-06-02: was falling through to leaf 'core-finance'
    'core-ops':     'ops',          # 2026-09-03: the fourth Core drifted the same way a third
                                      # time. `_discovered_core_rules()` (below) fixed the sibling-
                                      # layout case on 2026-08-09, but this static map is the
                                      # DOCUMENTED fallback for exactly the layout it degrades to
                                      # {} on — a fork or CI checkout with no sibling core-* dirs on
                                      # disk — and ops was never added here. Any fresh clone hit
                                      # the identical misroute this file's docstring already
                                      # describes finding and fixing twice.
    'core-example':  'example',       # per-vault: map a repo dir name to its brain slug
                                      # catch-all below (substring match) → misrouted to life/org1
                                      # for 2 weeks. Same class as the 06-02 school/finance fixes.
    'core-nick':     'life',          # pre-rename fallback (phases out)
    'career-ops':    'job-hunter',     # 2026-06-02: career-ops repo renamed → job-hunter (Career Hunter). Same project.
    'career_ops':    'job-hunter',
    'core-ui':       'core-ui-server',
    # 2026-06-02 root-cause fix: the repo was renamed ~/AI Projects/core →
    # core-life, but 42 old transcripts still carry cwd=.../AI Projects/core.
    # With no rule, those fell to slug_from_cwd → leaf 'core' → regenerated
    # projects/core/ on EVERY export (the source of the life↔core duplication).
    # This catch-all routes the old repo path (and its subdirs) to life. It is
    # ordered LAST so the specific core-{business,school,life,finance,ui} rules
    # above win first — 'ai projects/core' is a substring of 'core-life' etc.,
    # so it must never be reached for those.
    'ai projects/core': 'life',
}

CORE_SLUG_ALIASES: dict[str, str] = {
    # No active aliases — projects/core/ was unified into projects/life/ on
    # 2026-05-19 via git mv. Kept as empty dict so external imports don't break.
}

def _operator_display_name() -> str:
    """The operator's first name, read from any sibling Core's identity.json — never hardcoded.

    Same fallback contract as `.claude/hooks/lib/coreuser.py`: this exporter runs across every
    Core's sessions (SOURCE is `~/.claude/projects/`, not one Core's tree), so it walks up looking
    for ANY sibling `core-*/.claude/identity.json` rather than anchoring to one Core. One person
    runs every sibling Core, so the first one found is correct; a fresh fork with no identity.json
    yet degrades to the generic label rather than a hardcoded name.
    """
    here = os.path.abspath(__file__)
    for _ in range(8):
        here = os.path.dirname(here)
        if here in ("/", ""):
            break
        try:
            entries = os.listdir(here)
        except OSError:
            continue
        for name in entries:
            if not name.startswith("core-"):
                continue
            idp = os.path.join(here, name, ".claude", "identity.json")
            if os.path.isfile(idp):
                try:
                    u = (json.loads(Path(idp).read_text()) or {}).get("user") or {}
                    n = (u.get("name") or "").strip()
                    if n and not n.upper().startswith("YOUR"):
                        return n
                except Exception:
                    pass
    return "Operator"


_OPERATOR_NAME = _operator_display_name()


def _discovered_core_rules() -> dict[str, str]:
    """Cores DERIVED from disk, so a new one routes itself.

    THE HAND-MAINTAINED MAP HAS DRIFTED THREE TIMES AND ITS OWN COMMENTS RECORD TWO OF THEM —
    the 06-02 school/finance fix, and a two-week misroute to life/org1 annotated four lines above
    the map. core-business found the third on 2026-08-09: `core-ops` is absent, so it matched the
    'ai projects/core' catch-all (a substring of 'ai projects/core-ops') and EVERY ops export
    filed into life's bucket. Measured: 168 org-1 entities with ops source paths — the ops seat's app,
    a vendor, compliance topics — sitting in life's partition.

    A defect that survives being understood, documented, and fixed twice in the same file is not a
    discipline problem. Adding `core-ops` would be the third patch and the fourth Core would drift
    the same way. So the list is derived: any sibling directory holding .claude/identity.json is a
    Core and routes to its own slug.

    Degrades to {} when the layout is absent (a fork, a CI checkout) — the static map still
    applies, and unknown Cores are quarantined by categorize_cwd rather than folded into life.
    """
    import os as _os
    rules: dict[str, str] = {}
    here = _os.path.abspath(__file__)
    for _ in range(8):                       # walk up looking for the Cores' parent directory
        here = _os.path.dirname(here)
        if here in ("/", ""):
            break
        try:
            entries = _os.listdir(here)
        except OSError:
            continue
        found = False
        for name in entries:
            if not name.startswith("core-"):
                continue
            if _os.path.isfile(_os.path.join(here, name, ".claude", "identity.json")):
                rules[name.lower()] = name[len("core-"):].lower()
                found = True
        if found:
            break
    return rules


_CORE_RE = re.compile(r"(^|/)core-([a-z0-9][a-z0-9_-]*)", re.I)


def categorize_cwd(cwd: str) -> str:
    """Return a human-readable project category from cwd.

    Order: DERIVED Core rules, then the static map, then home fallback, then slug_from_cwd —
    with an explicit quarantine for a Core-shaped path nothing recognises.
    """
    cwd_lower = cwd.lower()

    # 1. Derived Cores win. A Core that exists on disk routes to itself, always.
    for needle, slug in _discovered_core_rules().items():
        if needle in cwd_lower:
            return slug

    # 2. AN UNRECOGNISED CORE IS QUARANTINED, NEVER FOLDED INTO life.
    #    This runs BEFORE the static map because that map ends in a substring catch-all
    #    ('ai projects/core') which matches 'ai projects/core-<anything>' — the precise mechanism
    #    that sent ops into life for weeks. Fail toward a visibly wrong bucket, not a plausible
    #    one: 'unrouted-ops' in the vault is a question someone asks, while ops content silently
    #    inside life's partition is a question nobody thinks to ask.
    m = _CORE_RE.search(cwd_lower)
    if m:
        name = m.group(2)
        if not any(needle.lower() in cwd_lower and needle.lower() != "ai projects/core"
                   for needle in CWD_PROJECT_RULES):
            return "unrouted-%s" % name

    for needle, slug in CWD_PROJECT_RULES.items():
        if needle.lower() in cwd_lower:
            return slug
    # 2026-06-02: bare home-dir sessions (~/) fold into the primary Core (org 1) — there is
    # no standalone 'home' Core.
    #
    # DERIVED FROM $HOME, not from a username literal (2026-08-29). The live vault tests
    # `'<username>' in cwd_lower`, and the strip pass that produced this template rewrote that
    # username to the word "you" — leaving a check that matches any path containing "you" and
    # misses every path that does not. A sanitizer cannot rewrite a value into a placeholder and
    # leave the comparison meaningful; the fix is to stop hardcoding the value at all.
    #
    # HOME **AND ITS DIRECT CHILDREN**, matching the original's `cwd.count('/') <= 3`. The first
    # cut of this fix tested equality with $HOME alone, which silently narrowed the rule: on the
    # author's machine `~/Desktop` and `~/Downloads` had folded into the primary Core for months,
    # and equality would have started minting a `desktop` project bucket instead. Caught in
    # adversarial review, not by the routing test — which compared the two implementations on
    # paths two levels deep and never on a direct child.
    _home = os.path.normpath(os.path.expanduser("~"))
    _cwd = os.path.normpath(cwd)
    if _cwd == _home or os.path.dirname(_cwd) == _home:
        return PRIMARY_CORE_SLUG
    return slug_from_cwd(cwd)

def parse_content_block(block, cap=TOOL_RESULT_CAP) -> str:
    """Render a single content block as markdown."""
    if isinstance(block, str):
        return redact_credentials(block)
    if not isinstance(block, dict):
        return str(block)

    btype = block.get('type', '')

    if btype == 'text':
        text = block.get('text', '')
        text = re.sub(r'<system-reminder>.*?</system-reminder>', '[system-reminder omitted]', text, flags=re.DOTALL)
        return redact_credentials(text)

    if btype == 'tool_use':
        name = block.get('name', 'unknown_tool')
        inp = block.get('input', {})
        inp_str = json.dumps(inp, indent=2, ensure_ascii=False)
        inp_str = redact_credentials(inp_str)
        if len(inp_str) > cap:
            inp_str = inp_str[:cap] + f'\n... [truncated, {len(inp_str)} chars total]'
        return (
            f'\n<details>\n<summary>🔧 Tool call: <code>{name}</code></summary>\n\n'
            f'```json\n{inp_str}\n```\n\n</details>\n'
        )

    if btype == 'tool_result':
        tool_id = block.get('tool_use_id', '')
        is_error = block.get('is_error', False)
        content = block.get('content', '')

        if isinstance(content, list):
            # Could be list of blocks (e.g. tool_reference)
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    parts.append(item.get('text', ''))
                elif isinstance(item, dict) and item.get('type') == 'tool_reference':
                    parts.append(f'[tool reference: {item.get("tool_name", "?")}]')
                else:
                    parts.append(str(item))
            content = '\n'.join(parts)

        content = str(content)
        content = redact_credentials(content)
        error_tag = ' ⚠️ ERROR' if is_error else ''
        if len(content) > cap:
            content = content[:cap] + f'\n... [truncated, {len(content)} chars total]'
        return (
            f'\n<details>\n<summary>📤 Tool result{error_tag} <code>{tool_id[:16]}…</code></summary>\n\n'
            f'```\n{content}\n```\n\n</details>\n'
        )

    if btype == 'thinking':
        text = block.get('thinking', '')
        text = redact_credentials(text)
        if len(text) > cap:
            text = text[:cap] + f'\n... [truncated]'
        return f'\n<details>\n<summary>💭 Thinking</summary>\n\n{text}\n\n</details>\n'

    # Fallback
    raw = json.dumps(block, ensure_ascii=False)
    return redact_credentials(raw[:cap])

def render_message(line: dict):
    """Render a JSONL line as markdown. Returns None for non-message lines."""
    mtype = line.get('type')
    if mtype not in ('user', 'assistant'):
        return None

    msg = line.get('message', {})
    role = msg.get('role', mtype)
    content = msg.get('content', '')

    # Build header
    ts = line.get('timestamp', '')
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            ts_str = dt.strftime('%H:%M:%S')
        except:
            ts_str = ts
    else:
        ts_str = ''

    role_label = f'**{_OPERATOR_NAME}**' if role == 'user' else '**Core**'
    header = f'### {role_label}' + (f' `{ts_str}`' if ts_str else '') + '\n\n'

    # Render content
    if isinstance(content, str):
        body = redact_credentials(content)
        # Strip system-reminder blocks from user messages
        body = re.sub(r'<system-reminder>.*?</system-reminder>', '[system-reminder omitted]', body, flags=re.DOTALL)
    elif isinstance(content, list):
        parts = []
        for block in content:
            rendered = parse_content_block(block)
            if rendered:
                parts.append(rendered)
        body = '\n'.join(parts)
    else:
        body = str(content)

    if not body.strip():
        return None

    return header + body + '\n\n'

def extract_session_metadata(lines: list) -> dict:
    """Pull cwd, sessionId, timestamps, first-user-message from lines."""
    meta = {
        'cwd': None,
        'session_id': None,
        'start_ts': None,
        'end_ts': None,
        'first_user_msg': None,
        'message_count': 0,
        'tool_call_count': 0,
    }

    for line in lines:
        if not isinstance(line, dict):
            continue

        sid = line.get('sessionId')
        if sid and not meta['session_id']:
            meta['session_id'] = sid

        cwd = line.get('cwd')
        if cwd and not meta['cwd']:
            meta['cwd'] = cwd

        ts = line.get('timestamp')
        if ts:
            if not meta['start_ts'] or ts < meta['start_ts']:
                meta['start_ts'] = ts
            if not meta['end_ts'] or ts > meta['end_ts']:
                meta['end_ts'] = ts

        ltype = line.get('type')
        if ltype == 'user':
            meta['message_count'] += 1
            if not meta['first_user_msg']:
                msg = line.get('message', {})
                content = msg.get('content', '')
                # Normalize list-shaped content (assistant + user turns can both be lists) to text.
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'text':
                            text_parts.append(block.get('text', ''))
                    content = '\n'.join(text_parts)
                if isinstance(content, str):
                    clean = re.sub(r'<system-reminder>.*?</system-reminder>', '', content, flags=re.DOTALL)
                    clean = re.sub(r'<local-command-(?:caveat|stdout)>.*?</local-command-(?:caveat|stdout)>', '', clean, flags=re.DOTALL)
                    clean = re.sub(r'<command-(?:name|message|args)>.*?</command-(?:name|message|args)>', '', clean, flags=re.DOTALL)
                    clean = clean.strip()
                    if clean and not clean.startswith('<command-name>') and not clean.startswith('<local-command>'):
                        meta['first_user_msg'] = clean[:200]
        elif ltype == 'assistant':
            meta['message_count'] += 1
            msg = line.get('message', {})
            content = msg.get('content', [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'tool_use':
                        meta['tool_call_count'] += 1

    return meta

def session_date_str(start_ts) -> str:
    """Return the local-date (PDT/PST) for a session's start timestamp.
    Filenames use local date so a session that ran 18:01→19:03 PDT on 5/15
    is filed as 2026-05-15_..., not 2026-05-16_... (UTC-shifted).
    Convention: see engine .claude/rules/session.md "consistent timestamps" 2026-05-15."""
    if not start_ts:
        return '1970-01-01'
    try:
        dt = datetime.fromisoformat(start_ts.replace('Z', '+00:00'))
        return dt.astimezone(LOCAL_TZ).strftime('%Y-%m-%d')
    except Exception:
        return '1970-01-01'

def render_session_md(lines: list, meta: dict, subagent_links: list = None, parent_link: str = None) -> str:
    """Render a full session as markdown."""
    date_str = session_date_str(meta['start_ts'])
    sid = meta['session_id'] or 'unknown'
    sid8 = sid[:8]
    cwd = meta['cwd'] or 'unknown'
    # Use categorize_cwd (the file-routing function) for the project: frontmatter
    # so frontmatter matches file location. Audit brain-vault.md #9 (2026-05-13):
    # was slug_from_cwd which gave the leaf cwd (e.g. "graphify-brain") while
    # categorize_cwd routed the file into projects/core/, creating divergence.
    slug = categorize_cwd(cwd)

    # YAML frontmatter
    first_msg = (meta['first_user_msg'] or '').replace('"', "'").replace('\n', ' ')[:150]
    frontmatter = f"""---
session_id: {sid}
date: {date_str}
cwd: {cwd}
project: {slug}
messages: {meta['message_count']}
tool_calls: {meta['tool_call_count']}
start: {meta['start_ts'] or ''}
end: {meta['end_ts'] or ''}
first_message: "{first_msg}"
---
"""

    # Title
    title = f'# Session {date_str} — {slug} `{sid8}`\n\n'

    # Links block
    links_block = ''
    if parent_link:
        links_block += f'**Parent session:** [[{parent_link}]]\n\n'
    if subagent_links:
        links_block += '**Subagent runs:**\n'
        for sl in subagent_links:
            links_block += f'- [[{sl}]]\n'
        links_block += '\n'

    # Metadata table
    meta_table = (
        f'| Field | Value |\n'
        f'|---|---|\n'
        f'| Date | {date_str} |\n'
        f'| Session ID | `{sid}` |\n'
        f'| Working Dir | `{cwd}` |\n'
        f'| Messages | {meta["message_count"]} |\n'
        f'| Tool calls | {meta["tool_call_count"]} |\n\n'
    )

    # Conversation
    conversation_parts = ['## Conversation\n\n']
    for line in lines:
        rendered = render_message(line)
        if rendered:
            conversation_parts.append(rendered)

    if len(conversation_parts) == 1:
        conversation_parts.append('_No conversational messages in this session._\n')

    return frontmatter + title + links_block + meta_table + ''.join(conversation_parts)

def parse_jsonl(path: Path) -> list:
    lines = []
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for raw in f:
                raw = raw.strip()
                if raw:
                    try:
                        lines.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        pass
    return lines

def safe_filename(s: str) -> str:
    return re.sub(r'[^\w\-.]', '_', s)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main(skip_existing: bool = False):
    print(f"Source: {SOURCE}")
    print(f"Vault:  {VAULT}")

    VAULT.mkdir(parents=True, exist_ok=True)
    (VAULT / "_build").mkdir(exist_ok=True)

    # ── Discover all project folders ──
    project_folders = [d for d in SOURCE.iterdir() if d.is_dir()]
    print(f"\nProject folders: {len(project_folders)}")

    # ── Map sessions: sessionId → {main_jsonl, subagent_jsonls, folder, cwd} ──
    # First pass: find all main JSONLs and subagent JSONLs

    all_sessions = {}  # session_id → dict
    subagent_files = []  # list of (path, parent_session_id) tuples
    queue_op_count = 0
    corrupt_count = 0

    for proj_dir in project_folders:
        folder_cwd = folder_to_cwd(proj_dir.name)

        # Main JSONLs in this folder
        for jf in proj_dir.glob('*.jsonl'):
            lines = parse_jsonl(jf)
            if not lines:
                corrupt_count += 1
                continue

            first = lines[0]
            # Queue-operation files are subagent task queues, not main sessions
            if first.get('type') == 'queue-operation':
                queue_op_count += 1
                continue

            meta = extract_session_metadata(lines)
            sid = meta['session_id'] or jf.stem

            # Prefer cwd from file content, fall back to folder-derived
            if not meta['cwd']:
                meta['cwd'] = folder_cwd

            if sid not in all_sessions:
                all_sessions[sid] = {
                    'lines': lines,
                    'meta': meta,
                    'jsonl_path': jf,
                    'folder': proj_dir,
                    'subagents': [],
                    'is_subagent': False,
                }
            # else: duplicate session ID across folders — keep first found

        # Subagent JSONLs in subagents/ subfolders
        subagents_dir = proj_dir  # subagents can be in named subdirs
        for sd in proj_dir.iterdir():
            if sd.is_dir():
                subagents_subdir = sd / "subagents"
                if subagents_subdir.exists():
                    # DELIBERATELY NON-RECURSIVE (2026-07-26, per the operator: leave them invisible).
                    # Workflow-spawned agents live one level deeper (subagents/workflows/
                    # wf_*/agent-*.jsonl) and are EXCLUDED by design, not by accident:
                    # they're mechanical middle-steps (voter panels, per-item pipeline
                    # stages) whose conclusions land in the parent session, which IS
                    # exported. Making this rglob would add hundreds of mostly-redundant
                    # files per heavy session to every close's extraction. Decision:
                    # core-life decisions-log.md 2026-07-26.
                    for sjf in subagents_subdir.glob('*.jsonl'):
                        subagent_files.append((sjf, sd.name, proj_dir))

    print(f"Main sessions found: {len(all_sessions)}")
    print(f"Queue-op files (skipped): {queue_op_count}")
    print(f"Subagent files found: {len(subagent_files)}")

    # ── Group sessions by project (canonical cwd) ──
    projects = defaultdict(list)  # cwd → list of session_ids
    cwd_to_project_slug = {}

    for sid, sdata in all_sessions.items():
        cwd = sdata['meta']['cwd'] or 'unknown'
        projects[cwd].append(sid)
        cwd_to_project_slug[cwd] = categorize_cwd(cwd)

    # Dedupe: merge cwds that map to same canonical project
    # For now, just group by cwd exactly (deduplication by cwd per spec)
    print(f"\nUnique cwds (projects): {len(projects)}")
    for cwd, sids in sorted(projects.items(), key=lambda x: -len(x[1])):
        print(f"  {cwd}: {len(sids)} sessions")

    # ── Write subagent markdown files ──
    subagent_written = 0
    subagent_id_to_vault_path = {}  # agent_id → vault relative path str

    for (sjf, parent_session_id, proj_dir) in subagent_files:
        lines = parse_jsonl(sjf)
        if not lines:
            continue

        meta = extract_session_metadata(lines)
        if not meta['cwd']:
            # Inherit cwd from parent session if available
            parent = all_sessions.get(parent_session_id, {})
            meta['cwd'] = parent.get('meta', {}).get('cwd') or folder_to_cwd(proj_dir.name)

        cwd = meta['cwd']
        proj_slug = categorize_cwd(cwd)
        proj_vault_dir = VAULT / "projects" / proj_slug / "subagents"
        proj_vault_dir.mkdir(parents=True, exist_ok=True)

        agent_id = sjf.stem  # e.g. agent-a934cef44e2720a49
        date_str = session_date_str(meta['start_ts'])

        # Try to get description from meta.json
        meta_json_path = sjf.parent / f"{sjf.stem}.meta.json"
        description = ''
        if meta_json_path.exists():
            try:
                with open(meta_json_path) as f:
                    mj = json.load(f)
                description = mj.get('description', '')
            except:
                pass

        fname = f"{date_str}_{safe_filename(agent_id)}.md"
        vault_rel = f"projects/{proj_slug}/subagents/{fname}"
        out_path = proj_vault_dir / fname

        # Link back to parent
        parent_md = None
        if parent_session_id in all_sessions:
            p_meta = all_sessions[parent_session_id]['meta']
            p_date = session_date_str(p_meta['start_ts'])
            p_slug = slug_from_cwd(p_meta['cwd'] or '')
            p_sid8 = parent_session_id[:8]
            parent_md = f"projects/{proj_slug}/sessions/{p_date}_{p_slug}_{p_sid8}"
            # Register this subagent with the parent
            all_sessions.get(parent_session_id, {}).setdefault('subagents', []).append(vault_rel.replace('.md', ''))

        if skip_existing and out_path.exists():
            subagent_id_to_vault_path[agent_id] = vault_rel.replace('.md', '')
            continue

        content = render_session_md(lines, meta, parent_link=parent_md)
        # Add subagent header note
        header_note = f'> **Subagent run** — `{agent_id}`'
        if description:
            header_note += f' — {description}'
        header_note += '\n\n'
        content = content[:content.index('## Conversation')] + header_note + content[content.index('## Conversation'):]

        out_path.write_text(content, encoding='utf-8')
        subagent_id_to_vault_path[agent_id] = vault_rel.replace('.md', '')
        subagent_written += 1

    print(f"\nSubagent markdown files written: {subagent_written}")

    # ── Write main session markdown files ──
    sessions_written = 0
    project_sessions = defaultdict(list)  # proj_slug → list of session info dicts

    for sid, sdata in all_sessions.items():
        meta = sdata['meta']
        cwd = meta['cwd'] or 'unknown'
        proj_slug = categorize_cwd(cwd)

        proj_sessions_dir = VAULT / "projects" / proj_slug / "sessions"
        proj_sessions_dir.mkdir(parents=True, exist_ok=True)

        date_str = session_date_str(meta['start_ts'])
        # Use proj_slug (routing destination) for the filename slug so the
        # filename matches the project directory it lands in. Previously used
        # slug_from_cwd(cwd) which gave leaf-of-cwd ("core-school") for school
        # sessions even though routing put them in projects/core/. Aligned
        # 2026-05-13 after core-school worktree retirement.
        slug = proj_slug
        sid8 = sid[:8]
        fname = f"{date_str}_{slug}_{sid8}.md"
        out_path = proj_sessions_dir / fname

        # Collect subagent links for this session
        subagent_links = sdata.get('subagents', [])

        if skip_existing and out_path.exists():
            project_sessions[proj_slug].append({
                'date': date_str, 'sid': sid, 'sid8': sid8, 'slug': slug,
                'cwd': cwd, 'messages': meta['message_count'],
                'tool_calls': meta['tool_call_count'],
                'first_msg': (meta['first_user_msg'] or '')[:80],
                'fname': fname, 'start_ts': meta['start_ts'] or '',
            })
            continue

        content = render_session_md(sdata['lines'], meta, subagent_links=subagent_links)
        out_path.write_text(content, encoding='utf-8')
        sessions_written += 1

        project_sessions[proj_slug].append({
            'date': date_str,
            'sid': sid,
            'sid8': sid8,
            'slug': slug,
            'cwd': cwd,
            'messages': meta['message_count'],
            'tool_calls': meta['tool_call_count'],
            'first_msg': (meta['first_user_msg'] or '')[:80],
            'fname': fname,
            'start_ts': meta['start_ts'] or '',
        })

    print(f"Session markdown files written: {sessions_written}")

    # ── Generate _project.md rollups ──
    for proj_slug, sess_list in sorted(project_sessions.items()):
        proj_dir = VAULT / "projects" / proj_slug

        sess_list_sorted = sorted(sess_list, key=lambda x: x['start_ts'], reverse=True)
        total_messages = sum(s['messages'] for s in sess_list)
        total_tools = sum(s['tool_calls'] for s in sess_list)

        rows = []
        for s in sess_list_sorted:
            first = s['first_msg'].replace('|', '\\|').replace('\n', ' ')
            rows.append(f"| [[sessions/{s['fname'].replace('.md','')}\\|{s['date']} {s['sid8']}]] | {s['messages']} | {s['tool_calls']} | {first} |")

        table = '\n'.join(rows)

        # Find all cwds for this project slug
        cwds_for_proj = [cwd for cwd, sids in projects.items() if categorize_cwd(cwd) == proj_slug]
        cwd_list = '\n'.join(f'- `{c}`' for c in sorted(set(cwds_for_proj)))

        rollup = f"""---
project: {proj_slug}
sessions: {len(sess_list)}
total_messages: {total_messages}
total_tool_calls: {total_tools}
last_active: {sess_list_sorted[0]['date'] if sess_list_sorted else 'unknown'}
---

# Project: {proj_slug}

## Working Directories
{cwd_list}

## Summary
- **Sessions:** {len(sess_list)}
- **Total messages:** {total_messages}
- **Total tool calls:** {total_tools}
- **Last active:** {sess_list_sorted[0]['date'] if sess_list_sorted else 'unknown'}

## Session Table
| Session | Messages | Tool Calls | First Message |
|---|---|---|---|
{table}
"""
        (proj_dir / "_project.md").write_text(rollup, encoding='utf-8')

    # ── Generate top-level README.md ──
    readme_rows = []
    for proj_slug, sess_list in sorted(project_sessions.items(), key=lambda x: -len(x[1])):
        last = max((s['date'] for s in sess_list), default='?')
        cwds_for_proj = sorted(set(cwd for cwd, sids in projects.items() if categorize_cwd(cwd) == proj_slug))
        # Prefer canonical (non-retired) paths — core-school worktree retired 2026-05-13
        non_retired = [c for c in cwds_for_proj if 'core-school' not in c.lower()]
        cwd_display = (non_retired or cwds_for_proj)[0] if cwds_for_proj else ''
        readme_rows.append(f"| [[projects/{proj_slug}/_project\\|{proj_slug}]] | {len(sess_list)} | {last} | `{cwd_display}` |")

    readme_table = '\n'.join(readme_rows)
    total_sess = sum(len(v) for v in project_sessions.values())
    # Count subagent files on disk, not subagent_written (which is the THIS-run
    # delta — 0 most runs under --skip-existing). Audit brain-vault.md #6 fix
    # 2026-05-13: README claimed "0 subagents" while filesystem held 436+.
    total_subagents_on_disk = sum(1 for _ in (VAULT / "projects").rglob("subagents/*.md"))

    readme = f"""---
generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}
total_sessions: {total_sess}
total_projects: {len(project_sessions)}
total_subagents: {total_subagents_on_disk}
---

# Claude Code Brain — Session Vault

Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from `~/.claude/projects/`.

**{total_sess} sessions** across **{len(project_sessions)} projects**, plus **{total_subagents_on_disk} subagent runs**.

## Projects

| Project | Sessions | Last Active | Primary CWD |
|---|---|---|---|
{readme_table}

## Vault Structure
```
core-brain/
├── README.md              ← this file (project index)
├── projects/<name>/
│   ├── _project.md        ← rollup + session table
│   ├── sessions/          ← main session markdown files
│   └── subagents/         ← linked subagent runs
└── _build/
    └── export.py          ← rerunnable export script
```

## Notes
- Tool results capped at {TOOL_RESULT_CAP} chars each for readability; full fidelity in `<details>` blocks.
- Credentials redacted (sk-ant-*, sk-*, xai-*, JWTs, ghp_*).
- System-reminder blocks stripped from user/assistant text (kept as `[system-reminder omitted]`).
"""

    (VAULT / "README.md").write_text(readme, encoding='utf-8')

    # ── Final report ──
    print(f"\n{'='*60}")
    print(f"DONE")
    print(f"Sessions written:   {sessions_written}")
    print(f"Subagents written:  {subagent_written}")
    print(f"Projects (by cwd):  {len(project_sessions)}")
    print(f"Queue-op files:     {queue_op_count} (skipped — task queues, not sessions)")
    print(f"Corrupt/empty:      {corrupt_count}")
    print(f"Vault:              {VAULT}")

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-existing', action='store_true',
                        help='Skip session/subagent files that already exist in vault (incremental mode)')
    args = parser.parse_args()
    main(skip_existing=args.skip_existing)
