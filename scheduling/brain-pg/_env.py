"""Defensive secret loader for brain-pg scripts.

Background: Stop-hook fires `python3 embed.py` from a bash subprocess. That
bash inherits its env from the Claude Code parent process, which may have
been launched in a context where `~/.zshrc` / `~/.zshenv` was not sourced
(GUI launchd contexts especially). Without a defensive fallback, the script
fails on missing $VOYAGE_API_KEY even though the secret exists on disk.

Canonical secret store: `~/.claude/secrets.env` — `export KEY=value` lines,
chmod 600, user-level (NOT inside any git repo). Mirrored into shell env
by `~/.zshenv` for interactive + non-interactive zsh.

This helper handles the third path: bash subprocesses that never see zshenv.
Idempotent — if a key is already in os.environ, leave it. Only fills gaps.

Usage:
    from _env import load_secrets
    load_secrets()  # call before any os.environ.get(...) of a secret
"""
from __future__ import annotations
import os
import re
from pathlib import Path

SECRETS_PATH = Path.home() / ".claude" / "secrets.env"

# Match `export KEY=value` or `KEY=value`. Value may be quoted (single or
# double) or bare. Whitespace tolerated. Comments after `#` outside quotes
# are stripped.
_LINE_RX = re.compile(
    r"""^\s*(?:export\s+)?
        (?P<key>[A-Za-z_][A-Za-z0-9_]*)
        =
        (?:
            "(?P<dq>[^"]*)"   |
            '(?P<sq>[^']*)'   |
            (?P<bare>[^\s#]*)
        )
        \s*(?:\#.*)?$
    """,
    re.VERBOSE,
)


def load_secrets(path: Path = SECRETS_PATH) -> dict[str, str]:
    """Read shell-style `export KEY=value` lines from `path`.

    Sets any key into `os.environ` ONLY IF that key is not already present
    (so explicitly-exported env wins). Returns the dict of keys it loaded
    (helpful for logging — but never log the values).

    Silently no-ops if the file is missing or unreadable. Callers are
    expected to fail loud on their own when the required env var is still
    absent after this call.
    """
    loaded: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return loaded

    for raw_line in text.splitlines():
        m = _LINE_RX.match(raw_line)
        if not m:
            continue
        key = m.group("key")
        if key in os.environ:
            continue
        value = m.group("dq")
        if value is None:
            value = m.group("sq")
        if value is None:
            value = m.group("bare") or ""
        os.environ[key] = value
        loaded[key] = value
    return loaded


def core_root(start: Path | None = None) -> Path:
    """The Core instance root, anchored to a file on disk — never to the environment.

    Walk up from `start` (default: this file) until a `.claude/identity.json` is found.
    Identity travels WITH the Core on disk; `CORE_ORG_ID`/`CORE_INSTANCE` are exported per
    session and therefore survive a `cd` into a different Core, which is how a peer-shell run
    ends up pairing one Core's data with another's. Anchoring both the org and the transcript
    directory to the same on-disk file is what makes them impossible to mismatch.
    """
    here = (start or Path(__file__)).resolve()
    for cand in [here, *here.parents]:
        # INTENTIONAL hardcode — cannot use core_paths.IDENTITY_JSON here. That constant is THIS
        # Core's absolute path, and this loop's whole job is to DISCOVER which Core the calling script
        # belongs to by walking up from it. Substituting the constant would make the function always
        # answer "life" regardless of caller — precisely the mis-identification the error below
        # refuses to guess at. Flagged by bin/lint-code-paths.py, and that flag blocked core-school's
        # autosave for four days: the lint is right that this is a hardcode and wrong that it is drift.
        if (cand / ".claude" / "identity.json").is_file():  # NOT core_paths.IDENTITY_JSON — see above: this walk DISCOVERS the caller's Core
            return cand
    raise RuntimeError(
        f"no .claude/identity.json found at or above {here} — cannot identify which Core "  # prose; core_paths.IDENTITY_JSON names the wrong Core in this message by construction
        "this is. Refusing to guess: guessing is what pairs one Core's corrections with "
        "another Core's prompts."
    )


def caller_core_root(script: Path, explicit: str | None = None) -> Path:
    """The Core a tool should operate on — refusing when the script and the caller disagree.

    `core_root(Path(__file__))` is CORRECT for normal operation: every Core runs its own synced
    copy, and anchoring to disk is what stops `CORE_INSTANCE` surviving a `cd` from pairing one
    Core's data with another's. It has exactly one blind spot, and it is not an edge case —
    CROSS-CORE REVIEW, where a peer runs THIS Core's file from THEIR seat to check it before a
    sync. That is the operation the fleet's autonomy depends on.

    core-business hit it on 2026-08-09 running life's enforcement-audit.py from its own cwd: it
    got life's registration count, planted three false claims in its own rules, saw three silent
    passes, and was one keystroke from reporting "no unbacked claims on business" — a second read
    of LIFE wearing business's name. A tool that answers confidently about the wrong partition is
    worse than one that refuses, which is the same conclusion correction-rate.py reached four days
    earlier and this function exists so the next tool does not have to reach it a third time.

    Use this instead of `core_root(Path(__file__))` in any tool whose output NAMES a Core.
    """
    if explicit:
        r = Path(explicit).expanduser().resolve()
        if not (r / ".claude" / "identity.json").is_file():
            raise RuntimeError(f"{r} is not a Core (no .claude/identity.json)")  # lint-code-paths: ignore — error-message text, not a path op
        return r
    script_core = core_root(script)
    try:
        cwd_core = core_root(Path.cwd())
    except RuntimeError:
        return script_core          # caller is outside any Core: the script's own is the only answer
    if cwd_core != script_core:
        raise RuntimeError(
            "REFUSING TO GUESS WHICH CORE TO OPERATE ON.\n"
            f"  this script lives in : {script_core}\n"
            f"  you are standing in  : {cwd_core}\n"
            "Using the script's Core would report ITS state under YOUR name. Run that Core's own "
            "copy, or pass an explicit root."
        )
    return script_core


def get_org_id(start: Path | None = None) -> int:
    """This Core's org_id, from its own identity file. Fail loud if it cannot be determined.

    Each Core declares `org_id` in `.claude/identity.json` (1=life, 2=business, 3=school,
    4=finance, 5=ops); `.mcp.json`/settings additionally export `CORE_ORG_ID` into sessions.
    The brain-pg scripts use this to partition reads + writes via PostgreSQL session-scoped
    `app.current_org_id` (see schema.sql + 2026-05-19 migration).

    Identity wins over the environment (2026-08-05). Previously this read `CORE_ORG_ID` only,
    and `bin/correction-rate.py` bypassed it entirely for `os.environ.get("CORE_ORG_ID", "1")`
    — a silent default to life. Running that from a life shell after `cd`-ing into a peer Core
    divided life's corrections by that peer's prompts and printed a confident number; core-school
    rendered 221.1 per 100, wrong only because it happened to exceed 100. Values under 100 were
    never evidence of correctness. A disagreement between identity and env is reported, loudly,
    because both look equally plausible to whoever reads the output.
    """
    import json as _json
    import sys as _sys
    ident = None
    try:
        root = core_root(start)
    except Exception:
        root = None                    # genuinely outside a Core tree — env is the only source
    if root is not None:
        # A PRESENT-BUT-UNREADABLE identity file is not the same as an absent one, and must not
        # quietly fall through to the env. Codex caught this on review: the original except-all
        # silently degraded to exactly the leaked-env behaviour the function exists to prevent,
        # while the docstring claimed fail-loud. A Core whose identity.json is malformed is
        # mis-installed; guessing its org from a variable that survives a `cd` is the worst
        # available answer.
        ip = root / ".claude" / "identity.json"
        try:
            ident = int(_json.loads(ip.read_text())["org_id"])
        except Exception as exc:
            raise RuntimeError(
                f"{ip} exists but its org_id could not be read ({exc.__class__.__name__}: {exc}). "
                "Refusing to fall back to CORE_ORG_ID — a malformed identity file means this Core "
                "is mis-installed, and the env var is exactly what leaks between Cores. Fix the "
                "file."
            ) from exc

    val = os.environ.get("CORE_ORG_ID")
    env = int(val) if val and val.strip().lstrip("-").isdigit() else None

    if ident is not None:
        if env is not None and env != ident:
            import sys as _sys
            print(f"[_env] NOTE: CORE_ORG_ID={env} in the environment but identity.json says "
                  f"org_id={ident} — using {ident}. A session env var leaked across a `cd` into "
                  f"another Core is the usual cause.", file=_sys.stderr)
        return ident
    if env is not None:
        return env
    # The filename below is PROSE in an error message, not a path operation — it tells a human which
    # file to go look for. core_paths.IDENTITY_JSON would print an absolute path for the WRONG Core in
    # a message whose entire subject is "the calling script's Core could not be identified".
    raise RuntimeError(
        "cannot determine org_id: no readable .claude/identity.json at or above the calling "  # prose, not a path op; core_paths.IDENTITY_JSON would name the wrong Core
        "script, and no CORE_ORG_ID set. Refusing to default."
    )


def connect_corebrain():
    """Connect to corebrain Postgres as `brain_app` + set session-scoped org_id.

    Every brain-pg script that opens a Postgres connection should call this
    instead of `psycopg2.connect(...)` directly. The SET makes
    `current_setting('app.current_org_id')::bigint` available to every
    WHERE clause in the same connection, so query/embed/compile-truth all
    stay scoped to their Core without per-call parameter plumbing.

    C0 fix 2026-05-19: connects as `brain_app` (not current OS user). The
    OS user is a Postgres superuser with rolbypassrls=true — connecting as
    superuser bypasses RLS policies, defeating the DB-layer org_id
    enforcement. `brain_app` is a non-superuser role with table-level perms
    only; RLS policies enforce against it properly. Trust auth on local
    socket means no password handling required.

    To override (testing/admin/maintenance): set CORE_BRAIN_DB_USER env var.
    """
    import psycopg2  # imported lazily so _env.py stays cheap to import
    org_id = get_org_id()
    db_user = os.environ.get("CORE_BRAIN_DB_USER", "brain_app")
    # BOUNDED. There was no connect_timeout and no statement_timeout anywhere in scheduling/, so a
    # host that is REACHABLE BUT SLOW hangs indefinitely — the case a hard-down host does not
    # exercise, because hard-down fails fast and refuses loudly.
    #
    # core-business measured it: an unbounded connect to an unresponsive host was still blocked at
    # 8s; the same call with connect_timeout=1 failed in 1.3s. It flagged this against
    # friction_installer's new block-install path, which now REFUSES when the corpus cannot be
    # fetched — correct for a blocking artifact, and dangerous only because the fetch itself could
    # hang forever. finance and ops run a partially-migrated corebrain, which is exactly the
    # slow-not-down shape.
    #
    # Overridable so a genuinely slow legitimate query is not capped by a number chosen here.
    _ct = int(os.environ.get("COREBRAIN_CONNECT_TIMEOUT", "5"))
    _st = os.environ.get("COREBRAIN_STATEMENT_TIMEOUT_MS", "30000")
    conn = psycopg2.connect(dbname=os.environ.get("COREBRAIN_DB", "corebrain"), user=db_user,
                            connect_timeout=_ct)
    with conn.cursor() as cur:
        cur.execute("SET app.current_org_id = %s", (str(org_id),))
        # Session-scoped, so one wedged query cannot hold a close/dispatch path open forever.
        cur.execute("SET statement_timeout = %s", (str(_st),))
    return conn


def describe_db_failure(exc) -> str:
    """Say WHICH failure this was, and name the knob that fixes it.

    WHY THIS EXISTS (2026-08-13, reported by core-finance as an OPERATOR, not by dosing).

    `psycopg2.errors.QueryCanceled` is a subclass of `OperationalError` (verified:
    QueryCanceled -> QueryCanceledError -> OperationalError). So the statement_timeout that
    connect_corebrain() deliberately sets lands in every `except OperationalError` arm that was
    written for a DOWN HOST, and five call sites across this fleet then printed some form of
    "corebrain unreachable". Four of them catch bare `Exception`, so a schema error reports the
    same way.

    What finance measured: `SELECT 1` instant, `brew services` shows postgresql@17 STARTED, and
    the identical query succeeds under COREBRAIN_STATEMENT_TIMEOUT_MS=120000. The database was
    fine. The message sent them to check a service that was demonstrably running.

    The sharper half is the second-order cost. query.py is what the RECALL-FIRST GATE requires, so
    a slow query became "brain unreachable" became a BLOCKED WRITE — and the bound's own comment
    says it is "overridable so a genuinely slow legitimate query is not capped by a number chosen
    here", while nothing in the error named the variable. **The one person who needs that override
    is the one being told to check brew.** A refusal that names the wrong cause also hides its own
    escape hatch.

    Consolidated rather than patched at five message strings: those five will not stay in step,
    and the next caller writes a sixth. One classifier, and callers print what it returns.
    """
    name = exc.__class__.__name__
    text = str(exc).strip()
    first = text.splitlines()[0] if text else name
    low = first.lower()
    ms = os.environ.get("COREBRAIN_STATEMENT_TIMEOUT_MS", "30000")

    if name in ("QueryCanceled", "QueryCanceledError") or "statement timeout" in low:
        return (f"query exceeded the {ms}ms statement bound — the database is REACHABLE, this is a "
                f"SLOW QUERY, not a connectivity failure. Raise it for this run with "
                f"COREBRAIN_STATEMENT_TIMEOUT_MS=<ms>, or narrow the query.")
    if name in ("UndefinedTable", "UndefinedColumn", "UndefinedFunction", "ProgrammingError",
                "InsufficientPrivilege"):
        return (f"the database answered and REFUSED the query ({name}: {first[:100]}) — a schema or "
                f"permission problem, not connectivity. Checking that Postgres is running will not "
                f"resolve it.")
    if name == "OperationalError" or "could not connect" in low or "connection refused" in low:
        return (f"corebrain unreachable ({first[:100]}) — check: "
                f"brew services list | grep postgresql@17; fall back to grep.")
    return f"{name}: {first[:120]}"


def db_absent(exc) -> bool:
    """True only when the failure means THERE IS NO DATABASE TO TALK TO — the one class a test is
    allowed to SKIP on. False for anything a REACHABLE database said back (schema, permission,
    a cancelled statement, a programming error): those are defects, and a test that SKIPs on them
    hides a broken implementation behind "unavailable".

    Codex review of the P0 repair (2026-09-04): three tests SKIPped on bare `except Exception`,
    so a broken query and an absent database looked identical. Same classifier as
    describe_db_failure() above — one place decides what "absent" means, and the message a test
    prints beside its SKIP comes from the same call.
    """
    if isinstance(exc, ModuleNotFoundError) and (getattr(exc, "name", "") or "").split(".")[0] == "psycopg2":
        return True  # no driver on this seat — `pip install -e .` has not run
    name = exc.__class__.__name__
    low = str(exc).strip().lower()
    if name in ("QueryCanceled", "QueryCanceledError") or "statement timeout" in low:
        return False
    if name in ("UndefinedTable", "UndefinedColumn", "UndefinedFunction", "ProgrammingError",
                "InsufficientPrivilege"):
        return False
    return (name == "OperationalError" or "could not connect" in low or "connection refused" in low
            or ("database" in low and "does not exist" in low))


def connect_or_skip(component: str):
    """Connect, or return None after printing a NAMED skip status. Never raises.

    WHY THIS EXISTS
    ---------------
    A Core with no Postgres — which is every Core on its first run, and any Core whose
    database is down — could not complete a session close. discover.py called
    connect_corebrain() unguarded at the top of the CAPTURE step, the uncaught
    OperationalError killed the `&&` chain, and everything downstream never ran. Graph
    extraction had its own graceful "skipped (no brain dir)" branch and never reached it.

    The guarding was inconsistent rather than designed: brain_status.py and
    verify-brain-synced.py wrapped the call, discover.py and compile-truth-refresh.py did
    not, and nothing said which was correct. Roughly a dozen call sites, no convention.

    So the convention lives here. A close-path component that can be skipped calls this
    and checks for None; a component that genuinely must fail hard keeps calling
    connect_corebrain() directly and says why in a comment. The named status matters —
    "CAPTURE: skipped (no database)" is a fact someone can act on, where a traceback in a
    close log is noise nobody reads.
    """
    try:
        return connect_corebrain()
    except Exception as exc:
        print(f"{component}: skipped ({describe_db_failure(exc)})")
        return None


def connect_corebrain_admin():
    """Connect as brain_admin role for system-process cross-org writes.

    Use ONLY for ingestion paths that need to write rows with different
    org_id values per row (embed.py path-aware ingestion, future
    reconcile-cross-org script, future LLM crystallization). User-facing
    recall paths must use connect_corebrain() (brain_app, RLS-respecting).

    brain_admin is NOSUPERUSER but BYPASSRLS — RLS read/write policies are
    bypassed, so the caller is responsible for tagging rows with the
    correct org_id. Created by migrations/2026-05-20-brain-admin-role.sql.

    No session-scoped org_id is SET — callers tag per-row.

    C1 fix 2026-08-31 (Codex, CRITICAL): this hardcoded dbname="corebrain" while
    connect_corebrain() above already resolves os.environ.get("COREBRAIN_DB", "corebrain").
    A `COREBRAIN_DB=their_db bash bin/setup-brain.sh` install pointed every brain_app
    (non-admin) path at their_db and every admin path at the literal `corebrain` —
    on a machine with a live `corebrain`, the admin/cross-org-write path silently
    targeted THAT database instead of the one just provisioned. Reusing the same
    resolver, not inventing a second one.
    """
    import psycopg2
    conn = psycopg2.connect(dbname=os.environ.get("COREBRAIN_DB", "corebrain"), user="brain_admin")
    return conn


_TENANT_VAULT_TO_ORG: dict[str, int] | None = None


def path_to_org_id(file_path) -> int:
    """Map a source-file path under $CORE_BRAIN/projects/<slug>/... to its
    org_id by looking up the slug in the tenants table.

    Falls back to current CORE_ORG_ID for paths NOT under projects/<slug>/
    (e.g. entities/, topics/, _build/ — these are "global" content owned
    by whichever Core ran the heavy job). Also falls back for legacy
    paths like projects/core/ (predates the multi-Core split).

    The tenants table is read once and cached at module level.
    """
    global _TENANT_VAULT_TO_ORG
    import os, re
    p = str(file_path)
    # Try to match /projects/<slug>/ in the path
    m = re.search(r"/projects/([^/]+)/", p)
    if not m:
        return get_org_id()  # not a project file — caller's org
    slug = m.group(1)

    if _TENANT_VAULT_TO_ORG is None:
        import psycopg2
        try:
            # Same COREBRAIN_DB resolver as connect_corebrain()/connect_corebrain_admin() —
            # this was the same hardcode, on a read path (2026-08-31).
            c = psycopg2.connect(dbname=os.environ.get("COREBRAIN_DB", "corebrain"),
                                 user=os.environ.get("CORE_BRAIN_DB_USER", "brain_app"))
            cur = c.cursor()
            cur.execute("SELECT org_id, name FROM tenants")
            _TENANT_VAULT_TO_ORG = {name: org_id for (org_id, name) in cur.fetchall()}
            c.close()
        except Exception:
            # THE FALLBACK WENT STALE AS THE FLEET GREW. finance (4) and ops (5) were added and
            # this literal was not, so it named three of five Cores. core-business caught it on my
            # tree minutes before a baseline push that would have propagated it to all five
            # (bus #1018); byte-identical on both trees, so it is as much mine as its.
            #
            # It bites ONLY when the tenants query above fails, which is why it survived: the DB is
            # normally reachable and the primary path is correct. But the failure mode is the bad
            # kind — an unlisted slug falls through to `get_org_id()`, the CALLER's own org, so a
            # finance or ops lookup would silently resolve to whoever asked rather than erroring.
            # Wrong-tenant-silently is the worst available answer in a multi-tenant brain.
            #
            # Verified against the live table rather than assumed: SELECT org_id, name FROM tenants
            # returns exactly these five.
            _TENANT_VAULT_TO_ORG = {"life": 1, "business": 2, "school": 3,
                                    "finance": 4, "ops": 5}

    # Legacy 'core' slug maps to org_id=1 (pre-rename was just life)
    if slug == "core":
        return 1
    return _TENANT_VAULT_TO_ORG.get(slug, get_org_id())
