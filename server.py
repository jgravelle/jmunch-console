"""jMunch Console — local control-plane server.

Zero-dependency (Python stdlib only), offline-safe, localhost-bound. Serves the
static web UI and a small read-only JSON API backed by the installed jMunch CLI
where it emits clean JSON, and by fixtures elsewhere (each response is tagged
``_source: live|fixture`` so the UI can badge un-wired panels).

Phase 1 is intentionally GET-only: no endpoint mutates config, triggers a
reindex, or launches a process, so there is no RCE surface yet. Mutating and
launching actions (Phase 2/3) land behind the bearer-token + two-key-turn model
described in the PRD.

Run:  python server.py            # http://127.0.0.1:8765
Env:  JMUNCH_CONSOLE_PORT         override port (default 8765)
      JMUNCH_CONSOLE_TOKEN        if set, /api/* requires ?token= or Bearer
      JMUNCH_MCP_BIN              jMunch CLI name/path (default jcodemunch-mcp)
      JMUNCH_CONSOLE_FIXTURES=1   force fixtures for every endpoint
      JMUNCH_CONSOLE_RELOAD=1     dev: re-exec on *.py change (no manual restart)
      CLAUDE_ADMIN_KEY            Anthropic Admin API key (sk-ant-admin...) for the
                                  org Usage/Cost panel; also read from ./.env
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
FIXTURES = ROOT / "fixtures"
DATA = ROOT / "data"
HISTORY = DATA / "savings_history.json"
DELIVERY_HISTORY = DATA / "delivery_history.json"
CONSOLE_SETTINGS = DATA / "console_settings.json"


def _read_version() -> str:
    """The console version, read from the ONE source of truth: the `<small>`
    brand in web/index.html. The release workflow tags off that same brand, so
    deriving the HTTP Server header (and /api/meta) from it here means a bump can
    never drift between the UI, the server, and the release tag. Falls back to
    "0.0.0" if the brand can't be read (never crashes startup over a banner)."""
    try:
        m = re.search(r"<small>v([0-9]+\.[0-9]+\.[0-9]+)</small>",
                      (WEB / "index.html").read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    except OSError:
        pass
    return "0.0.0"


VERSION = _read_version()

# Settings edited on the Config screen persist to console_settings.json and are
# loaded here, before the globals below are computed. Precedence: an explicitly
# set environment variable wins (operator intent) and locks its control in the
# UI; the file fills the gaps. _ENV_PINNED remembers which keys the environment
# claimed so /api/console-set can refuse to override them.
_SETTINGS_ENV = {
    "port": "JMUNCH_CONSOLE_PORT",
    "token": "JMUNCH_CONSOLE_TOKEN",
    "mcp_bin": "JMUNCH_MCP_BIN",
    "fixtures": "JMUNCH_CONSOLE_FIXTURES",
    "read_only": "JMUNCH_CONSOLE_READ_ONLY",
    # In-console Help chat. On by default; JMUNCH_CONSOLE_CHAT=0 hard-disables it
    # for offline/air-gapped deployments (the bot makes an outbound call via the
    # user's own `claude` CLI). Read-only by design — it never edits or runs.
    "chat": "JMUNCH_CONSOLE_CHAT",
    # The team-SKU org id. Not a JMUNCH_CONSOLE_* knob — it's the suite's own
    # JCODEMUNCH_ORG_ID env var, which `org()` reads and the `org-rollup`
    # subprocess inherits. Persisting it here lets the org savings rollup be
    # configured from the UI instead of a shell export.
    "org_id": "JCODEMUNCH_ORG_ID",
}
# A key is "pinned" only when its env var was set by the operator, not when the
# console injected it from the settings file (the loop below). The two are
# indistinguishable in os.environ after a re-exec — Config->Restart and the dev
# reload both inherit the parent's mutated environ — so we carry the list of
# self-injected keys forward in a private marker var and exclude them here.
# Without this, restarting with e.g. `"fixtures": "0"` persisted would lock the
# fixtures toggle as if an operator had pinned JMUNCH_CONSOLE_FIXTURES.
_INJECTED_MARKER = "_JMUNCH_CONSOLE_INJECTED"
_self_injected = {p for p in os.environ.get(_INJECTED_MARKER, "").split(",") if p}
_ENV_PINNED = {
    k for k, v in _SETTINGS_ENV.items()
    if os.environ.get(v) is not None and k not in _self_injected
}


def _read_console_settings() -> dict:
    try:
        d = json.loads(CONSOLE_SETTINGS.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_console_settings(d: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    CONSOLE_SETTINGS.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _console_flag(key: str, default: bool = False) -> bool:
    """Read a console-local boolean flag from console_settings.json. Used for UI
    toggle state the console itself drives (e.g. whether it enabled server
    logging) — cheap (a file read), unlike re-deriving it from a jcm CLI call on
    every diagnostics poll. Keys here are NOT in _SETTINGS_ENV, so the startup
    loop never maps them to an env var."""
    return bool(_read_console_settings().get(key, default))


def _set_console_flag(key: str, value: bool) -> None:
    d = _read_console_settings()
    d[key] = bool(value)
    _write_console_settings(d)


_injected_now = []
for _key, _val in _read_console_settings().items():
    _env = _SETTINGS_ENV.get(_key)
    if _env and _key not in _ENV_PINNED and _val is not None:
        os.environ[_env] = str(_val)
        _injected_now.append(_key)
# Record what we injected so a re-exec doesn't mistake it for an operator pin.
os.environ[_INJECTED_MARKER] = ",".join(_injected_now)

MCP_BIN = os.environ.get("JMUNCH_MCP_BIN", "jcodemunch-mcp")
TOKEN = os.environ.get("JMUNCH_CONSOLE_TOKEN", "")
FORCE_FIXTURES = os.environ.get("JMUNCH_CONSOLE_FIXTURES") == "1"

# Actions (config edits, launch/resume, installs, upgrades) are ON by default —
# the console exists to do things. JMUNCH_CONSOLE_READ_ONLY=1 turns them all off
# for cautious or shared deployments (legacy JMUNCH_CONSOLE_ALLOW_LAUNCH=0 is
# honored too). Even when on, every request is allowlisted server-side (agent
# must be a live detected client, repo a live indexed repo, command from a fixed
# map; the filesystem path is resolved here, never taken from the client).
ALLOW_LAUNCH = not (
    os.environ.get("JMUNCH_CONSOLE_READ_ONLY") == "1"
    or os.environ.get("JMUNCH_CONSOLE_ALLOW_LAUNCH") == "0"
)

# Dev convenience: when set, the server re-execs itself on any *.py source change
# so `server.py` edits take effect without a manual restart (web/ files are
# already hot — served fresh from disk each request). Off by default.
RELOAD = os.environ.get("JMUNCH_CONSOLE_RELOAD") == "1"

# In-console Help chat (read-only "Ask" bot). It shells out to the user's own
# `claude` CLI in headless print mode, so there are no API keys for the console
# to manage and the cost lands on the user's own Claude quota. Default model is
# a cheaper tier than whatever their CLI defaults to; JMUNCH_CONSOLE_CHAT_MODEL
# overrides (e.g. 'opus' for harder questions). CHAT_ENABLED gates the feature.
CHAT_ENABLED = os.environ.get("JMUNCH_CONSOLE_CHAT", "1") != "0"
CHAT_MODEL = os.environ.get("JMUNCH_CONSOLE_CHAT_MODEL", "sonnet")
# By default the help bot runs on the user's Claude SUBSCRIPTION, not metered
# API billing: we strip ANTHROPIC_API_KEY/AUTH_TOKEN (and cloud-provider routing)
# from the chat subprocess so `claude` falls through to its OAuth login. Set
# JMUNCH_CONSOLE_CHAT_USE_API=1 for deployments that have an API key but no
# subscription and would rather pay per token.
CHAT_USE_API = os.environ.get("JMUNCH_CONSOLE_CHAT_USE_API") == "1"


# Optional .env at the repo root (gitignored). The one secret the Console reads
# is CLAUDE_ADMIN_KEY — an Anthropic Admin API key (sk-ant-admin...) for the org
# Usage/Cost panel. Process env wins; the file only fills gaps. The key lives in
# module state, is never logged, and no endpoint echoes it back.
def _load_dotenv(path: Path) -> None:
    try:
        # utf-8-sig: Windows editors love BOMs, which would corrupt the first key
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            # standard KEY=VALUE; tolerate KEY:VALUE (values may contain ':')
            sep = "=" if "=" in line else (":" if ":" in line else "")
            if not sep:
                continue
            k, _, v = line.partition(sep)
            k, v = k.strip(), v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


_load_dotenv(ROOT / ".env")
ADMIN_KEY = os.environ.get("CLAUDE_ADMIN_KEY", "")

# The three suite MCPs the console tracks: (id, display name, CLI bin to detect,
# env-var fallback for the key). `id` is the validate.php product namespace.
LICENSES_FILE = DATA / "licenses.json"
VALIDATE_URL = "https://j.gravelle.us/jCodeMunch/validate.php"
# (id, display name, CLI bin, license env-var, pip dist name, GitHub repo)
PRODUCTS = [
    ("jcodemunch", "jCodeMunch-MCP", MCP_BIN,         "JCODEMUNCH_LICENSE_KEY", "jcodemunch-mcp", "jgravelle/jcodemunch-mcp"),
    ("jdocmunch",  "jDocMunch-MCP",  "jdocmunch-mcp", "JDOCMUNCH_LICENSE_KEY",  "jdocmunch-mcp",  "jgravelle/jdocmunch-mcp"),
    ("jdatamunch", "jDataMunch-MCP", "jdatamunch-mcp", "JDATAMUNCH_LICENSE_KEY", "jdatamunch-mcp", "jgravelle/jdatamunch-mcp"),
]
PRODUCT_IDS = {p[0] for p in PRODUCTS}

# jDoc/jData have no rich config file like jcm's — they're configured by a few
# env vars (read across their modules). Rather than tabs over an empty config,
# the Config screen shows this curated reference. (label, env var, default, desc)
SIBLING_SETTINGS = {
    "jDocMunch-MCP": {
        "note": "No rich config file. jDoc is configured by a handful of env vars (read across its modules).",
        "rows": [
            ("meta_fields", "JDOCMUNCH_META_FIELDS", "[]", "Which _meta fields to keep; default strips _meta (token-efficient)"),
            ("embedding_provider", "JDOCMUNCH_EMBEDDING_PROVIDER", "(auto-detect)", "gemini / openai / openai-compatible / sentence-transformers"),
            ("summarizer_provider", "JDOCMUNCH_SUMMARIZER_PROVIDER", "(auto-detect)", "anthropic / gemini / openai / minimax / glm / none"),
            ("git_timeout", "JDOCMUNCH_GIT_TIMEOUT", "10", "Git probe timeout (s) for repo@sha certification; <=0 disables"),
            ("hook_debounce_seconds", "JDOCMUNCH_HOOK_DEBOUNCE_SECONDS", "3", "Auto-reindex hook: per-file leading-edge debounce (coalesces rapid edits) [1.94.0+]"),
            ("hook_max_reindex", "JDOCMUNCH_HOOK_MAX_REINDEX", "2", "Auto-reindex hook: max concurrent reindex workers (cross-process slot cap) [1.94.0+]"),
            ("hook_log", "JDOCMUNCH_HOOK_LOG", "0 (off)", "Auto-reindex hook: 1 writes a breadcrumb log to _hooks/reindex.log [1.94.0+]"),
        ],
    },
    "jDataMunch-MCP": {
        "note": "No rich config file. jData is configured by a few env vars.",
        "rows": [
            ("max_response_tokens", "JDATAMUNCH_MAX_RESPONSE_TOKENS", "8000", "Response token budget (hard ceiling 16000)"),
            ("max_rows", "JDATAMUNCH_MAX_ROWS", "5000000", "Max rows ingested per dataset"),
            ("index_path", "DATA_INDEX_PATH", "~/.data-index", "Base index storage location"),
            ("share_savings", "JDATAMUNCH_SHARE_SAVINGS", "1 (on)", "Anonymous token-savings telemetry"),
            ("meta_fields", "JDATAMUNCH_META_FIELDS", "[]", "Which _meta fields to keep (token-efficient default)"),
            ("use_ai_summaries", "JDATAMUNCH_USE_AI_SUMMARIES", "true", "AI-summarize during indexing"),
        ],
    },
}
AGENT_LAUNCH = {"Claude Code": "claude"}  # display_name -> base command (extend per demand)

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


# --------------------------------------------------------------------------- #
# Data adapters: prefer live CLI JSON, fall back to fixtures. Never raise.
# --------------------------------------------------------------------------- #

def _fixture(name: str) -> dict:
    try:
        return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    except OSError:
        return {}


def _run_cli(args: list[str], timeout: int = 20) -> str | None:
    if FORCE_FIXTURES:
        return None
    try:
        out = subprocess.run(
            [MCP_BIN, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _tag(payload: dict, source: str) -> dict:
    payload["_source"] = source
    return payload


# --------------------------------------------------------------------------- #
# Tiny TTL + single-flight cache for slow live adapters (e.g. agents, which
# shells `claude mcp list` — multi-second CLI startup, esp. on Windows). Keeps
# a hot panel from paying that cost on every request, and serialises concurrent
# requests for the same key so the slow CLI runs once, not N times.
# --------------------------------------------------------------------------- #

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_GUARD = threading.Lock()


def _cache_lock(key: str) -> threading.Lock:
    with _CACHE_GUARD:
        lk = _CACHE_LOCKS.get(key)
        if lk is None:
            lk = _CACHE_LOCKS[key] = threading.Lock()
        return lk


def _cached(key: str, ttl: float, fn):
    """Return a fresh cached value, else compute under a per-key lock so a slow
    adapter runs once rather than once per concurrent request."""
    ent = _CACHE.get(key)
    if ent and time.time() - ent[0] < ttl:
        return ent[1]
    with _cache_lock(key):
        ent = _CACHE.get(key)  # another thread may have refreshed while we waited
        if ent and time.time() - ent[0] < ttl:
            return ent[1]
        val = fn()
        _CACHE[key] = (time.time(), val)
        return val


def _claude_code_connected() -> bool | None:
    """Whether jcodemunch is a *connected* MCP server in Claude Code, via
    `claude mcp list`. None when the `claude` CLI is unavailable.

    install-status only matches its own `jcodemunch-mcp serve` command
    signature, so it misses custom launchers (e.g. the `jmunch-mcp --config
    <name>.toml` multiplexer) and false-negatives "not wired" even when the
    server is live. Connection state is the truth.
    """
    if FORCE_FIXTURES:
        return None
    # Resolve via which() so Windows finds the npm `claude.cmd` shim — a bare
    # ["claude", ...] can't be launched by CreateProcess (no .exe).
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "mcp", "list"],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if "jcodemunch" in line.lower():
            return "connected" in line.lower() or "✓" in line
    return False


def _agents_live() -> dict:
    """Detected MCP clients, from `install-status --json`, with Claude Code's
    wiring corrected against actual `claude mcp list` connection state.

    The two CLIs are the slow part (each is a multi-second process spin-up), so
    run them concurrently — wall time becomes max(t1, t2) instead of t1 + t2."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_status = ex.submit(_run_cli, ["install-status", "--json"])
        f_conn = ex.submit(_claude_code_connected)
        raw = f_status.result()
        cc_connected = f_conn.result()
    if raw:
        try:
            data = json.loads(raw)
            out = []
            for c in data.get("clients", []):
                name = c.get("name", "")
                wired = bool(c.get("configured"))
                method = c.get("method", "")
                # Trust live connection state over the signature-match probe.
                if name == "Claude Code" and cc_connected is not None:
                    wired = cc_connected
                    if cc_connected and not c.get("configured"):
                        method = "mcp list: connected"
                out.append({
                    "agent": name,
                    "wired": wired,
                    "method": method,
                    "config_path": c.get("config_path"),
                    # Only clients with a fixed CLI launch command can be opened
                    # from here. GUI-only clients (e.g. Claude Desktop, the
                    # Electron app) have no `claude`-style entrypoint, so the UI
                    # shows them as detected-but-not-launchable instead of
                    # offering a button that 400s.
                    "launchable": name in AGENT_LAUNCH,
                })
            return _tag({"agents": out}, "live")
        except (ValueError, TypeError):
            pass
    return _tag(_fixture("agents"), "fixture")


def agents() -> dict:
    """Detected MCP clients. Cached briefly — client wiring is near-static, so
    the panel (and its polling) shouldn't re-pay the `claude mcp list` spin-up
    on every request. Warmed at startup; see main()."""
    data = _cached("agents", 60.0, _agents_live)
    # Stamp launchability for any path that skipped it (fixtures), so the client
    # never has to guess which detected clients have a CLI launch command.
    for a in data.get("agents", []):
        a.setdefault("launchable", a.get("agent") in AGENT_LAUNCH)
    return data


def health(repo: str) -> dict:
    """Six-axis radar for a repo, from `health <repo> --radar-only` (live)."""
    if repo:
        raw = _run_cli(["health", repo, "--radar-only"])
        if raw:
            try:
                return _tag({"repo": repo, "radar": json.loads(raw)}, "live")
            except ValueError:
                pass
    return _tag(_fixture("health"), "fixture")


def _record_history(tokens: int, usd: float) -> list[dict]:
    """Append today's rolling-30d savings snapshot to a Console-owned history
    file and return the series. One point per day (today's is updated in place),
    capped at 90 days. This is how the time-series chart becomes real without
    any suite change — the receipt CLI only reports a window total, so the
    Console accumulates its own daily rollup. Best-effort: never raises.
    """
    today = datetime.date.today().isoformat()
    points: list[dict] = []
    try:
        if HISTORY.exists():
            points = json.loads(HISTORY.read_text(encoding="utf-8")).get("points", [])
    except (OSError, ValueError):
        points = []
    snap = {"date": today, "tokens_saved": tokens, "usd": round(usd, 4)}
    if points and points[-1].get("date") == today:
        points[-1] = snap
    else:
        points.append(snap)
    points = points[-90:]
    try:
        DATA.mkdir(exist_ok=True)
        HISTORY.write_text(json.dumps({"points": points}), encoding="utf-8")
    except OSError:
        pass
    return points


def savings() -> dict:
    """Token/$ savings, normalized from `receipt --export <tmp>.json` (live).

    Receipt shape: {totals:{calls,savings_tokens,...}, per_tool:{t:{...}},
    savings_usd}. Folded into the unified `savings` object the UI renders, so
    the panel looks identical on live or fixture data. The 30d receipt window
    drives the headline tiles + per-tool table + the rolling series. The
    All-Time tile instead reads jcm's authoritative lifetime counter from
    `_savings.json` (`_lifetime_savings`) — the receipt only models savings from
    on-disk transcripts (a small subset), whereas the lifetime counter is the
    real cumulative total (the same number the anon community meter tracks).
    """
    if not FORCE_FIXTURES:
        tmp = Path(tempfile.gettempdir()) / "jmunch_console_receipt.json"
        if _run_cli(["receipt", "--days", "30", "--export", str(tmp)]) is not None:
            try:
                data = json.loads(tmp.read_text(encoding="utf-8"))
                tot = data.get("totals", {})
                tools = sorted(
                    (
                        {"tool": k, "tokens": v.get("savings_tokens", 0), "calls": v.get("calls", 0)}
                        for k, v in data.get("per_tool", {}).items()
                    ),
                    key=lambda r: r["tokens"],
                    reverse=True,
                )
                series = _record_history(tot.get("savings_tokens", 0), data.get("savings_usd", 0) or 0)
                return _tag({"savings": {
                    "tokens_saved_30d": tot.get("savings_tokens", 0),
                    "usd_saved_30d": data.get("savings_usd", 0),
                    "tokens_saved_total": _lifetime_savings(),
                    "usd_saved_total": _lifetime_usd(),
                    "calls": tot.get("calls", 0),
                    "tool_breakdown": tools,
                    "series": series,
                    # Soft gate: the UI blurs all but the first per-tool row and
                    # shows a CTA when no valid suite license is on file.
                    "licensed": _any_valid_license(),
                }}, "live")
            except (OSError, ValueError):
                pass
    fx = _fixture("savings")
    if isinstance(fx.get("savings"), dict):
        fx["savings"]["licensed"] = _any_valid_license()
    return _tag(fx, "fixture")


def _lifetime_savings() -> int | None:
    """jcm's authoritative lifetime tokens-saved counter from
    `<CODE_INDEX_PATH>/_savings.json` (`total_tokens_saved`) — the real all-time
    figure, and the same number the anonymous community meter tracks. This is
    distinct from (and far larger than) the receipt window, which only models
    savings present in on-disk transcripts. None if the file is absent/unreadable."""
    root = os.environ.get("CODE_INDEX_PATH") or os.path.join(os.path.expanduser("~"), ".code-index")
    try:
        data = json.loads((Path(root) / "_savings.json").read_text(encoding="utf-8"))
        return int(data["total_tokens_saved"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


# The `receipt` models savings at the Opus rate of $15 / 1M tokens — verified
# against the live receipt output ($2.612055 / 174,137 tok = 15e-6 to the penny).
# The All-Time $ tile multiplies the lifetime counter by the SAME rate so it
# agrees with the receipt-derived 30d dollar figure and ticks live with the
# counter. Update here if jcm reprices the receipt's Opus model.
_OPUS_USD_PER_TOKEN = 15.0 / 1_000_000


def _lifetime_usd() -> float | None:
    """All-time dollars saved = lifetime token counter x the receipt's Opus rate.
    None when the counter is unavailable."""
    tok = _lifetime_savings()
    return None if tok is None else round(tok * _OPUS_USD_PER_TOKEN, 2)


def savings_live() -> dict:
    """Cheap, high-frequency savings signal: jcm's lifetime counter read straight
    from `_savings.json` — no `receipt` subprocess, no transcript scan. Lets the
    Console poll the All-Time tiles in near-real-time at negligible cost: it reads
    the very file the live jcm server writes on each tool call, so there is zero
    load on jcm itself. The expensive 30d receipt stays on `/api/savings`."""
    return {
        "tokens_saved_total": _lifetime_savings(),
        "usd_saved_total": _lifetime_usd(),
        "_source": "live",
    }


def repos(fresh: bool = False) -> dict:
    """Index/watcher cockpit from `list-repos --json` (live). Report shape
    matches the UI repo card directly. Requires jcodemunch-mcp >= 1.108.36.

    Cached (30s) with a generous CLI timeout: `list-repos` computes per-repo
    freshness (a git probe each), so on a machine with many indexed repos it can
    take 60s+ — well past the default _run_cli ceiling. With only the 20s default
    the live call silently lost to the fixture fallback, so the cockpit showed 3
    sample repos and looked like it was 'hiding' the real index. The cache also
    keeps revisiting #index snappy despite the slow underlying call; deletes pop
    it (see delete_index) so a removal reflects without waiting out the TTL.

    `fresh=True` (from /api/repos?fresh=1) bypasses the cache for a guaranteed
    live read — used by the post-index refresh so a just-indexed repo shows up
    without waiting out the TTL or a manual page reload.
    """
    if fresh:
        _CACHE.pop("repos", None)
    def build() -> dict:
        raw = _run_cli(["list-repos", "--json"], timeout=90)
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    for r in data:
                        r["has_source"] = _has_local_source(r.get("source_root"))
                    # Soft gate: unlicensed seats get reindex/copy/delete greyed out.
                    return _tag({"repos": data, "licensed": _any_valid_license()}, "live")
            except ValueError:
                pass
        fix = _fixture("repos")
        for r in fix.get("repos", []):
            r.setdefault("has_source", _has_local_source(r.get("source_root")))
        fix["licensed"] = _any_valid_license()
        result = _tag(fix, "fixture")
        # Distinguish a deliberate fixtures pin (FORCE_FIXTURES) from a live call
        # that was attempted and failed/timed out. The UI shows a clear 'live data
        # unavailable' banner for the latter so sample data is never mistaken for
        # the real index.
        if not FORCE_FIXTURES:
            result["_degraded"] = True
            result["_degraded_reason"] = (
                f"`{MCP_BIN} list-repos` did not respond in time. It can be slow "
                "with many indexed repos; this view retries periodically."
            )
        return result
    return _cached("repos", 30.0, build)


def _has_local_source(source_root) -> bool:
    """True when a repo has a resolvable on-disk source path. Many indexes carry
    an empty `source_root` — remote/URL-indexed benchmark repos, or older index
    formats that never persisted the path — and those can't be launched into or
    re-indexed from here. The launch/reindex pickers filter on this so the UI
    never offers a repo whose action would 404 with 'repo path unavailable'."""
    return bool(source_root) and Path(source_root).is_dir()


def config() -> dict:
    """Effective config from `config --json` (live). The CLI's report already
    matches the UI's key/type/value/default/source shape, so it's a pass-through.
    Requires jcodemunch-mcp >= 1.108.34; older installs fall back to fixtures."""
    raw = _run_cli(["config", "--json"])
    if raw:
        try:
            keys = json.loads(raw)
            if isinstance(keys, list):
                return _tag({"keys": keys}, "live")
        except ValueError:
            pass
    return _tag(_fixture("config"), "fixture")


def _run_cli_json(args: list[str], timeout: int = 20) -> dict:
    """Run a CLI call that emits a JSON object on stdout, capturing it even on a
    non-zero exit (unlike `_run_cli`, which drops stdout when returncode != 0 —
    but `config set/unset --json` reports its errors there with exit 1)."""
    if FORCE_FIXTURES:
        return {"error": "fixtures mode is on", "_status": 409}
    try:
        out = subprocess.run(
            [MCP_BIN, *args], capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"CLI invocation failed: {e}", "_status": 500}
    raw = (out.stdout or "").strip()
    if not raw:
        return {"error": (out.stderr or "").strip() or "no output from CLI", "_status": 500}
    try:
        return json.loads(raw)
    except ValueError:
        return {"error": raw, "_status": 500}


def _config_key_set() -> set:
    """Live set of valid jcm config keys (server-side allowlist for writes)."""
    return {k.get("key") for k in (config().get("keys") or []) if k.get("key")}


def config_set(key, value) -> dict:
    """Write one jCodeMunch config key via `config set` (ALLOW_LAUNCH-gated).

    Mutating config.jsonc is a system change, so it rides the same default-off
    two-key turn as launch/resume/upgrade. All trust is server-side: the key
    must be a real jcm config key (checked against the live `config --json`
    report); the value is forwarded to the CLI, which coerces + type-validates
    it and rolls the file back on any bad write. A bare string passes through;
    every other type is JSON-encoded so the CLI parses it back to that type.
    """
    if not ALLOW_LAUNCH:
        return {"error": "config editing is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    if not isinstance(key, str) or not key:
        return {"error": "missing key", "_status": 400}
    valid = _config_key_set()
    if valid and key not in valid:
        return {"error": f"unknown config key: {key!r}", "_status": 400}
    cli_value = value if isinstance(value, str) else json.dumps(value)
    res = _run_cli_json(["config", "set", key, cli_value, "--json"])
    if res.get("success"):
        if key == "license_key":
            # The jcodemunch dot + pack unlock read this key; refresh both now.
            _CACHE.pop("products", None)
            _CACHE.pop("pack_license", None)
            _CACHE.pop("starter_packs", None)
        return {"status": "set", "key": key, "value": res.get("value")}
    return {"error": res.get("error", "config set failed"), "_status": res.get("_status", 400)}


def config_unset(key) -> dict:
    """Clear one jCodeMunch config key (revert to default) via `config unset`.
    Same ALLOW_LAUNCH gate + server-side key allowlist as config_set."""
    if not ALLOW_LAUNCH:
        return {"error": "config editing is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    if not isinstance(key, str) or not key:
        return {"error": "missing key", "_status": 400}
    valid = _config_key_set()
    if valid and key not in valid:
        return {"error": f"unknown config key: {key!r}", "_status": 400}
    res = _run_cli_json(["config", "unset", key, "--json"])
    if res.get("success"):
        if key == "license_key":
            _CACHE.pop("products", None)
            _CACHE.pop("pack_license", None)
            _CACHE.pop("starter_packs", None)
        return {"status": "unset", "key": key, "changed": bool(res.get("changed"))}
    return {"error": res.get("error", "config unset failed"), "_status": res.get("_status", 400)}


def sibling_config() -> dict:
    """jDoc/jData's env-driven settings as a read-only reference (they have no
    rich config file / `config --json`). Effective value comes from the
    console's own environment; otherwise the documented default."""
    out = []
    for name, spec in SIBLING_SETTINGS.items():
        rows = []
        for label, env, default, desc in spec["rows"]:
            cur = os.environ.get(env)
            rows.append({
                "key": label,
                "env": env,
                "default": default,
                "value": cur if cur is not None else default,
                "source": "env" if cur is not None else "default",
                "description": desc,
            })
        out.append({"name": name, "note": spec["note"], "settings": rows})
    return _tag({"siblings": out}, "live")


def _read_session_meta(path: Path) -> dict:
    """Light, capped parse of a Claude Code session `.jsonl`: first real user
    prompt (the summary), cwd, git branch, first timestamp. Best-effort; bails
    on the first parse error and stops as soon as the fields are known so large
    transcripts don't get fully walked."""
    summary = cwd = branch = first_ts = ""
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(e, dict):
                    continue
                if not first_ts and e.get("timestamp"):
                    first_ts = e["timestamp"]
                if not cwd and e.get("cwd"):
                    cwd = e["cwd"]
                if not branch and e.get("gitBranch"):
                    branch = e["gitBranch"]
                if not summary and e.get("type") == "user" and not e.get("isMeta"):
                    m = e.get("message") or {}
                    c = m.get("content")
                    if isinstance(c, list):
                        c = " ".join(
                            p.get("text", "") for p in c
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                    if isinstance(c, str):
                        c = c.strip()
                        # skip slash-command / system-reminder envelope lines
                        if c and not c.startswith("<"):
                            summary = c
                if summary and cwd and branch:
                    break
    except OSError:
        pass
    return {"summary": summary, "cwd": cwd, "git_branch": branch, "started_at": first_ts}


def sessions() -> dict:
    """Past Claude Code sessions under ~/.claude/projects (live, read-only).

    The resumable artifacts are the per-project `<sessionId>.jsonl` transcripts
    (filename stem == the id `claude --resume` expects). `sessions-index.json`
    is treated as *enrichment only* (summary / messageCount / branch): it drifts
    out of sync with the transcripts on disk, and trusting its `sessionId`s blind
    surfaces sessions that `claude --resume` then rejects ("No conversation found
    with session ID"). So we enumerate the jsonl files as the source of truth and
    fold the index in by id where it still matches.

    Best-effort and version-gated per the PRD coupling policy: if the layout
    isn't present/parseable, falls back to fixtures. This Claude-Code-specific
    coupling lives in the Console, not the suite.
    """
    root = Path(os.path.expanduser(
        os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")
    ))
    idx_meta: dict[str, dict] = {}
    sidechain_ids: set[str] = set()
    out: list[dict] = []
    try:
        for idx in root.glob("*/sessions-index.json"):
            try:
                data = json.loads(idx.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for e in data.get("entries") or []:
                if not isinstance(e, dict):
                    continue
                sid = e.get("sessionId", "")
                if not sid:
                    continue
                if e.get("isSidechain"):
                    sidechain_ids.add(sid)
                else:
                    idx_meta[sid] = e
        for jf in root.glob("*/*.jsonl"):
            sid = jf.stem
            if sid in sidechain_ids:
                continue
            ent = idx_meta.get(sid) or {}
            # parse the transcript only when the index can't fully cover it
            need_parse = not (ent.get("projectPath") and ent.get("summary"))
            meta = _read_session_meta(jf) if need_parse else {}
            try:
                mtime = datetime.datetime.fromtimestamp(
                    jf.stat().st_mtime, datetime.timezone.utc
                ).replace(tzinfo=None).isoformat() + "Z"
            except OSError:
                mtime = ""
            cwd = ent.get("projectPath") or meta.get("cwd") or ""
            repo = os.path.basename(cwd.rstrip("/\\")) or cwd
            out.append({
                "session_id": sid,
                "repo_id": repo,
                "cwd": cwd,
                "started_at": ent.get("created") or meta.get("started_at", ""),
                "modified": ent.get("modified") or mtime,
                "summary": (ent.get("summary") or ent.get("firstPrompt")
                            or meta.get("summary") or "(no summary)"),
                "message_count": ent.get("messageCount", 0),
                "git_branch": ent.get("gitBranch") or meta.get("git_branch", ""),
            })
    except OSError:
        pass
    if out:
        out.sort(key=lambda s: s.get("modified", ""), reverse=True)
        # Soft gate: unlicensed seats can resume only the first three sessions.
        return _tag({"sessions": out[:200], "licensed": _any_valid_license()}, "live")
    fx = _fixture("sessions")
    if isinstance(fx, dict):
        fx["licensed"] = _any_valid_license()
    return _tag(fx, "fixture")


def _spawn_terminal(cwd: str, argv: list, *, hold_on_error: bool = False, hold_on_done: bool = False) -> None:
    """Open *argv* in a new OS terminal at *cwd*. The only platform-specific
    surface in the app. *argv* is a fixed, server-built command list (never
    raw client input) — either one argv (list of strings) or a list of argvs,
    which run in order in the same window (e.g. unwrap-then-pip-uninstall).

    When *hold_on_error* is set, the window is kept open if the final command
    exits non-zero, so a fast-failing process (e.g. `claude --resume <bad-id>`)
    leaves its error on screen instead of vanishing. A clean exit still closes
    normally; earlier commands in a multi-step spawn are best-effort.

    When *hold_on_done* is set, the window ALWAYS waits for a keypress before
    closing — even on a clean exit — so a deliberate one-shot action (e.g.
    indexing) leaves its result on screen for the user to read. Implies the
    hold_on_error behavior.
    """
    multi = bool(argv) and isinstance(argv[0], (list, tuple))
    argvs = [list(a) for a in argv] if multi else [list(argv)]
    argv = argvs[0]
    if sys.platform == "win32":
        if hold_on_error or hold_on_done or multi:
            # The command runs from a temp batch file, NOT an inline
            # `cmd /c "<inner> || pause"` one-liner: each Popen/start/cmd layer
            # re-parses quoting, and list2cmdline's \" escapes mean nothing to
            # cmd — an argv with an embedded quoted string (e.g. powershell
            # -Command "...") arrives mangled and silently never runs (the
            # v0.8.3 Caveman install flashed-and-vanished exactly this way).
            # A batch file is parsed once, so the inner line survives intact.
            lines = [subprocess.list2cmdline(a).replace("%", "%%") for a in argvs]
            fd, script = tempfile.mkstemp(suffix=".cmd", prefix="jmunch-spawn-")
            with os.fdopen(fd, "w", encoding="ascii", errors="replace", newline="\r\n") as fh:
                # `call` so a .cmd/.bat target (npx.cmd) returns control here
                # instead of replacing the batch — without it the pause and
                # self-delete lines below never run. Harmless for .exe targets.
                fh.write("@echo off\n")
                for line in lines:
                    fh.write(f"call {line}\n")
                if hold_on_done:
                    fh.write("echo.\npause\n")            # always wait, even on success
                elif hold_on_error:
                    fh.write("if errorlevel 1 pause\n")
                fh.write('(goto) 2>nul & del "%~f0"\n')  # self-delete on exit
            subprocess.Popen(
                ["cmd", "/c", "start", "", "/D", cwd, "cmd", "/c", script],
                close_fds=True,
            )
        else:
            # `start "" /D <cwd> <argv...>` opens a new console window in cwd.
            subprocess.Popen(["cmd", "/c", "start", "", "/D", cwd, *argv], close_fds=True)
    elif sys.platform == "darwin":
        cmd = "; ".join(" ".join(shlex.quote(x) for x in a) for a in argvs)
        if hold_on_done:
            cmd += '; printf "\\n[done] press Enter to close..."; read _'
        script = f'tell application "Terminal" to do script "cd {shlex.quote(cwd)} && {cmd}"'
        subprocess.Popen(["osascript", "-e", script])
    else:
        term = shutil.which("gnome-terminal") or shutil.which("x-terminal-emulator")
        if not term:
            raise RuntimeError("no terminal emulator found")
        if multi or hold_on_done:
            cmd = "; ".join(" ".join(shlex.quote(x) for x in a) for a in argvs)
            if hold_on_done:
                cmd += '; printf "\\n[done] press Enter to close..."; read _'
            argv = ["bash", "-lc", cmd]
        subprocess.Popen([term, "--working-directory", cwd, "--", *argv])


def _enumerate_processes() -> list[tuple[int, int, str]]:
    """(pid, ppid, command_line) for every visible process — the second and last
    platform-specific surface besides `_spawn_terminal`. The parent pid lets the
    process panel nest a spawned server under the proxy/launcher that owns it.
    Best-effort: returns [] on any enumeration error so a restart degrades to
    'nothing found', never a crash. Windows reads full command lines via CIM;
    POSIX via `ps`. ppid is 0 when unknown."""
    try:
        if sys.platform == "win32":
            ps = ("Get-CimInstance Win32_Process | "
                  "Select-Object ProcessId,ParentProcessId,CommandLine | "
                  "ConvertTo-Csv -NoTypeInformation")
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=25, stdin=subprocess.DEVNULL,
            )
            import csv
            import io
            rows: list[tuple[int, int, str]] = []
            for row in csv.reader(io.StringIO(out.stdout.decode("utf-8", "replace"))):
                if len(row) >= 3 and row[0].isdigit():
                    ppid = int(row[1]) if row[1].isdigit() else 0
                    rows.append((int(row[0]), ppid, row[2] or ""))
            return rows
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,args="], capture_output=True, timeout=25, stdin=subprocess.DEVNULL,
        )
        rows = []
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            parts = line.strip().split(None, 2)  # pid, ppid, args (args may have spaces)
            if len(parts) >= 2 and parts[0].isdigit():
                ppid = int(parts[1]) if parts[1].isdigit() else 0
                args = parts[2].strip() if len(parts) > 2 else ""
                rows.append((int(parts[0]), ppid, args))
        return rows
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


# Bare tokens that are NOT a CLI subcommand: the interpreter / proxy program
# names, and the one subcommand a server itself may carry. Anything else bare in
# a command line (`index-file`, `list-repos`, `config`, `receipt`, …) is a
# subcommand and marks a transient console/agent invocation, not a server.
_NON_SUBCOMMAND_TOKENS = frozenset({"jmunch-mcp", "python", "python3", "py", "serve"})


def _is_server_cmd(needle: str, cmd_low: str) -> bool:
    """Whether a lowercased command line is a *server* invocation for the product
    named by *needle* (also lowercased): it names the product and carries no CLI
    subcommand. A server line is just the binary (optionally under `python`, or
    the `jmunch-mcp --config <toml>` proxy); a CLI line adds a bareword subcommand
    (`index-file` / `list-repos` / `config` / …). So we scan for any such
    bareword — anything that is not a flag (`-…`), a path/file (has `/ \\ .`), a
    bare number, the product binary itself, or a known program / `serve` token.
    Enumerating subcommands missed `index-file`; this inverse rule can't."""
    if needle not in cmd_low:
        return False
    for t in cmd_low.replace('"', " ").replace("'", " ").split():
        if t.startswith("-") or t.isdigit():
            continue  # flag or numeric value
        if "/" in t or "\\" in t or "." in t:
            continue  # path / file (exe, toml, py, …)
        if t == needle or t in _NON_SUBCOMMAND_TOKENS:
            continue  # the product binary, interpreter, proxy, or `serve`
        return False  # a bareword subcommand → transient CLI call, not a server
    return True


def _running_server_pids(binname: str, procs: list | None = None) -> list[int]:
    """PIDs of the running MCP *server* processes for *binname* — the long-lived
    process the MCP client spawned, however it was launched:

    - the product binary directly (`jcodemunch-mcp[.exe]`, no subcommand), or
    - the `jmunch-mcp` proxy whose `--config` names the product
      (`…\\configs\\jcodemunch-mcp.toml`) — how the suite actually runs here.

    The earlier `serve`-subcommand match found nothing because neither shape
    carries `serve`. Instead we match the product name anywhere in the command
    line and exclude the console's own transient CLI calls, which always carry a
    bareword subcommand (`_CLI_SUBCOMMANDS`). Skips our own pid. Best-effort.

    *procs* may be a pre-fetched `_enumerate_processes()` snapshot so a scan over
    several products enumerates the process table once instead of per product."""
    needle = binname.lower()
    me = os.getpid()
    snap = procs if procs is not None else _enumerate_processes()
    return [pid for pid, _ppid, cmd in snap if pid != me and _is_server_cmd(needle, cmd.lower())]


def _kill_pids(pids: list[int]) -> int:
    """Terminate *pids*, best-effort; returns the count successfully signaled."""
    killed = 0
    for pid in pids:
        try:
            if sys.platform == "win32":
                r = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
                )
                if r.returncode == 0:
                    killed += 1
            else:
                os.kill(pid, signal.SIGTERM)
                killed += 1
        except (OSError, subprocess.SubprocessError):
            pass
    return killed


# How each detected client re-spawns a killed stdio server. A killed server
# stays down until the host acts, and the action differs per client — Claude Code
# reconnects from its /mcp command, GUI-only hosts need a full app restart. Names
# match the `agent` field from agents(); anything unlisted gets a generic hint.
_RESTART_HINTS = {
    "Claude Code": "in Claude Code, run /mcp and reconnect (or restart the session)",
    "Claude Desktop": "restart Claude Desktop to relaunch it",
    "Cursor": "in Cursor, toggle the server off/on in the MCP settings (or restart Cursor)",
    "Windsurf": "in Windsurf, restart the server from the MCP panel (or restart Windsurf)",
}


def _respawn_hint() -> str:
    """A respawn instruction tailored to the wired MCP clients the console
    detects, so the restart toast tells the user exactly how to bring the server
    back in *their* host. Joins the hint for each wired client; falls back to a
    generic line when nothing is detected (or detection is unavailable)."""
    wired = [a.get("agent") for a in agents().get("agents", []) if a.get("wired")]
    hints = [_RESTART_HINTS.get(n) or f"reconnect or restart {n}" for n in wired if n]
    # dict.fromkeys dedupes while preserving order (a server wired into one host
    # is the common case; multiple wired hosts each get their own instruction).
    hints = list(dict.fromkeys(hints))
    return "; ".join(hints) or "your MCP client will respawn it on its next reconnect"


def restart_server(product_id: str) -> dict:
    """Terminate the running MCP server process(es) for a product so the MCP
    client respawns a fresh one. The console never owned these processes — the
    client (Claude Code/Desktop) spawns them as stdio subprocesses — so this is a
    terminate-and-let-the-client-respawn, not a managed restart; some clients
    only respawn on their next reconnect/tool call. Rides the same two-key turn
    (ALLOW_LAUNCH) as the other process-touching actions; the matcher excludes
    the console's own transient CLI calls so a restart can't hit one."""
    if not ALLOW_LAUNCH:
        return {"error": "restart is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    prod = next((p for p in PRODUCTS if p[0] == product_id), None)
    if not prod:
        return {"error": f"unknown product: {product_id!r}", "_status": 400}
    pid, name, binname, _envvar, _dist, _gh = prod
    pids = _running_server_pids(binname)
    if not pids:
        return {"status": "not_running", "product": pid,
                "hint": f"no running {name} server found — start it from your MCP client"}
    killed = _kill_pids(pids)
    if not killed:
        return {"error": f"found {len(pids)} {name} server process(es) but couldn't stop them",
                "hint": "they may belong to another user or need elevated rights", "_status": 500}
    return {"status": "restarted", "product": pid, "killed": killed, "hint": _respawn_hint()}


def _proc_kind(cmd_low: str) -> str:
    """Coarse label for a server process line: the `jmunch-mcp` proxy that fronts
    a product vs. a direct product server."""
    return "proxy" if ("jmunch-mcp" in cmd_low and "--config" in cmd_low) else "server"


def _order_process_tree(procs: list[dict]) -> list[dict]:
    """Order a product's matched processes parent-before-child and stamp each with
    `depth` (nesting level) and `descendants` (how many processes it encloses), so
    the panel can nest them visually and make plain that stopping an enclosing
    process takes the enclosed ones with it. A process whose parent isn't in the
    matched set is a root (its real parent is the MCP client, outside our view)."""
    by_pid = {p["pid"]: p for p in procs}
    children: dict[int, list[dict]] = {}
    roots: list[dict] = []
    for p in procs:
        ppid = p.get("ppid") or 0
        if ppid in by_pid and ppid != p["pid"]:
            children.setdefault(ppid, []).append(p)
        else:
            roots.append(p)

    def count_desc(pid: int) -> int:
        return sum(1 + count_desc(k["pid"]) for k in children.get(pid, []))

    ordered: list[dict] = []

    def walk(p: dict, depth: int) -> None:
        p["depth"] = depth
        p["descendants"] = count_desc(p["pid"])
        ordered.append(p)
        for k in sorted(children.get(p["pid"], []), key=lambda x: x["pid"]):
            walk(k, depth + 1)

    for r in sorted(roots, key=lambda x: x["pid"]):
        walk(r, 0)
    return ordered


def processes() -> dict:
    """Live MCP server processes the console can see, grouped by product — the
    read side of the process panel (pairs with /api/kill-process). Each row is
    {pid, ppid, kind (proxy|server), cmd, depth, descendants}, ordered as a
    parent-before-child tree. Enumerates the process table once and matches every
    product against that one snapshot."""
    snap = _enumerate_processes()
    by_pid = {pid: (ppid, cmd) for pid, ppid, cmd in snap}
    groups = []
    for prod_id, name, binname, _envvar, _dist, _gh in PRODUCTS:
        rows = []
        for proc_pid in _running_server_pids(binname, snap):
            ppid, cmd = by_pid.get(proc_pid, (0, ""))
            rows.append({"pid": proc_pid, "ppid": ppid, "kind": _proc_kind(cmd.lower()), "cmd": cmd})
        rows = _order_process_tree(rows)
        groups.append({"product": prod_id, "name": name, "count": len(rows), "procs": rows})
    return _tag({"groups": groups}, "live")


# --------------------------------------------------------------------------- #
# Diagnostics: crash/perf log tail + per-server liveness. Read-only, and every
# read here is a cheap filesystem/process probe (no CLI shell-out), so the panel
# stays fast and can poll. This is the same evidence the customer-facing log
# recipe collects (jcw_<pid>.log watcher logs + JCODEMUNCH_LOG_FILE), surfaced
# live in one screen.
# --------------------------------------------------------------------------- #

def _code_index_dir() -> Path:
    """jcm's index store, where _session_live.json / telemetry.db live."""
    p = os.environ.get("CODE_INDEX_PATH")
    return Path(p) if p else Path.home() / ".code-index"


# Log paths the console manages itself (the two "capture logs" buttons point jcm
# at these), so the panel can always find them regardless of env. Kept in temp so
# they never pollute a repo or the index store.
_WATCHER_LOG = Path(tempfile.gettempdir()) / "jmunch-console-watch.log"
_SERVER_LOG = Path(tempfile.gettempdir()) / "jmunch-console-jcm.log"


def _watcher_pids(procs: list | None = None) -> list[int]:
    """PIDs of running jcm reindex watchers (`watch` / `watch-all` / `watch-claude`),
    so the panel can show one is up and the Start button can refuse to double-spawn.
    Excludes the transient watch-status / watch-install / watch-uninstall verbs."""
    snap = procs if procs is not None else _enumerate_processes()
    me = os.getpid()
    binlow = MCP_BIN.lower()
    out: list[int] = []
    for pid, _ppid, cmd in snap:
        if pid == me:
            continue
        low = cmd.lower()
        if binlow not in low and "jcodemunch" not in low:
            continue
        toks = low.replace('"', " ").replace("'", " ").split()
        if any(t in ("watch", "watch-all", "watch-claude") for t in toks):
            out.append(pid)
    return out


_LOG_TAIL_LINES = 200
_LOG_TAIL_BYTES = 256_000


def _tail_lines(path: Path, n: int = _LOG_TAIL_LINES, max_bytes: int = _LOG_TAIL_BYTES) -> list[str]:
    """Last *n* lines of a possibly-large log, reading only the trailing
    *max_bytes* so a multi-MB watcher log never loads whole."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial first line after the seek
            data = f.read()
        return data.decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def _log_level(line: str) -> str:
    """Coarse severity for a log line, for the panel's colour coding."""
    if "Traceback" in line or "CRITICAL" in line or "ERROR" in line or "Exception" in line:
        return "error"
    if "WARNING" in line or "WARN" in line:
        return "warn"
    return "info"


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def _discover_logs() -> list[dict]:
    """jcm log files we can read: the watcher's auto per-PID logs
    (`<temp>/jcw_<pid>.log`) plus an explicit JCODEMUNCH_LOG_FILE if set. Each
    carries a bounded tail + a warn/error count over that tail; newest first."""
    candidates: list[Path] = sorted(Path(tempfile.gettempdir()).glob("jcw_*.log"))
    candidates += [_WATCHER_LOG, _SERVER_LOG]  # the console-managed capture files
    lf = os.environ.get("JCODEMUNCH_LOG_FILE")
    if lf and lf.lower() != "auto":
        candidates.append(Path(lf))
    out: list[dict] = []
    seen: set[str] = set()
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            st = p.stat()
        except OSError:
            continue
        lines = [{"text": ln, "level": _log_level(ln)} for ln in _tail_lines(p)]
        name_low = p.name.lower()
        out.append({
            "path": key,
            "name": p.name,
            "kind": "watcher" if (name_low.startswith("jcw_") or "watch" in name_low) else "server",
            "size": st.st_size,
            "age_s": round(time.time() - st.st_mtime, 1),
            "errors": sum(1 for ln in lines if ln["level"] == "error"),
            "warnings": sum(1 for ln in lines if ln["level"] == "warn"),
            "lines": lines,
        })
    out.sort(key=lambda f: f["age_s"])  # most recently written first
    return out


def _heartbeat() -> dict | None:
    """jcm's live session-journal mtime = its last tool-call activity (v1.108.57
    writes _session_live.json, throttled). Present only once jcm has served a
    call this run; None when the file isn't there."""
    f = _code_index_dir() / "_session_live.json"
    try:
        st = f.stat()
    except OSError:
        return None
    return {"age_s": round(time.time() - st.st_mtime, 1)}


def _server_state(product_id: str, heartbeat: dict | None) -> tuple[str, str]:
    """(state, detail) for a running server. Heartbeat-derived for jcm (the one
    that writes _session_live.json); the siblings just report 'running' since we
    have no equivalent liveness signal for them yet."""
    if product_id != "jcodemunch" or heartbeat is None:
        return "running", "no heartbeat signal"
    age = heartbeat["age_s"]
    if age < 30:
        return "active", f"last activity {age:.0f}s ago"
    return "idle", f"last activity {_ago(age)} ago"


def _perf_telemetry_cfg():
    """Effective `perf_telemetry_enabled` from jcm's on-disk config, read via the
    CLI (independent of the running server, so it reflects a just-saved toggle).
    True/False, or None if it can't be read. Cached so the Logging poll doesn't
    shell out to `config --json` every tick. Lets the panel tell 'flag off' apart
    from 'flag on but the server hasn't restarted to act on it yet' — the latter
    is exactly why a freshly-toggled setting shows no telemetry.db."""
    def _read():
        for k in (config().get("keys") or []):
            if k.get("key") == "perf_telemetry_enabled":
                return k.get("value")
        return None
    try:
        val = _cached("cfg_perf_telemetry", 30.0, _read)
    except Exception:
        return None
    return None if val is None else bool(val)


def diagnostics() -> dict:
    """Crash/perf log tail + per-server liveness (slice 1 of the Diagnostics
    panel). Read-only. Surfaces the same artifacts the customer log-recipe
    collects so a wedge or crash is visible at a glance instead of buried in a
    temp folder."""
    if FORCE_FIXTURES:
        return _tag(_fixture("diagnostics"), "fixture")
    idx = _code_index_dir()
    logs = _discover_logs()
    heartbeat = _heartbeat()
    snap = _enumerate_processes()
    servers: list[dict] = []
    for prod_id, name, binname, _envvar, _dist, _gh in PRODUCTS:
        for pid in _running_server_pids(binname, snap):
            state, detail = _server_state(prod_id, heartbeat)
            servers.append({"product": prod_id, "name": name, "pid": pid,
                            "state": state, "detail": detail})

    log_file_env = os.environ.get("JCODEMUNCH_LOG_FILE") or None
    watcher_logs = sum(1 for f in logs if f["kind"] == "watcher")
    telemetry_db = (idx / "telemetry.db").is_file()
    perf_cfg = _perf_telemetry_cfg()  # on-disk flag: True / False / None
    signals = {
        "index_dir": str(idx),
        "log_file_env": log_file_env,
        "watcher_logs": watcher_logs,
        "watcher_running": bool(_watcher_pids(snap)),
        "server_logging": _console_flag("server_logging"),
        "heartbeat": heartbeat,
        "perf_telemetry_db": telemetry_db,
        "perf_telemetry_enabled": perf_cfg,
    }

    hints: list[str] = []
    if not log_file_env and not watcher_logs:
        hints.append("File logging is off and no watcher logs were found. Set JCODEMUNCH_LOG_FILE "
                     "(and run watch / watch-all) to capture crashes and indexing wedges.")
    err_logs = [f for f in logs if f["errors"]]
    if err_logs:
        top = err_logs[0]
        hints.append(f"{top['errors']} error line(s) in the recent tail of {top['name']} — "
                     "see the highlighted lines below.")
    if not telemetry_db:
        if perf_cfg:
            # Flag is set on disk but no db yet: the running server loaded config
            # at startup, so it won't persist until it restarts and serves a call.
            hints.append("Perf telemetry is enabled in config, but telemetry.db hasn't been written "
                         "yet. Restart the jCodeMunch server (Processes tab, or let your MCP client "
                         "reconnect) so it picks up the setting; the db is created on the first tool "
                         "call after that.")
        else:
            hints.append("Perf telemetry is off. Turn on perf_telemetry_enabled in Config (or set "
                         "JCODEMUNCH_PERF_TELEMETRY=1), then restart the jCodeMunch server, which writes "
                         "per-tool latency to telemetry.db on its next tool call.")
    # Soft 'possibly wedged' read: a live jcm server whose heartbeat AND watcher
    # logs have all gone quiet for a while. Not asserted as hung — just where to look.
    if any(s["product"] == "jcodemunch" for s in servers) and heartbeat and heartbeat["age_s"] > 120:
        watcher_quiet = (not watcher_logs) or all(
            f["age_s"] > 120 for f in logs if f["kind"] == "watcher")
        if watcher_quiet:
            hints.append(f"A jCodeMunch server is running but has recorded no activity in "
                         f"{_ago(heartbeat['age_s'])}. If a command seems hung, this is where to look.")

    return _tag({"signals": signals, "servers": servers, "logs": logs, "hints": hints}, "live")


def start_watcher() -> dict:
    """Start the jcm background reindex watcher (`watch-all`) with file logging on,
    so an indexing wedge leaves tracks the Diagnostics panel tails. Opens a visible
    terminal (close it to stop). ALLOW_LAUNCH-gated; refuses to double-spawn when a
    watcher is already running. This is the 'run watch / watch-all' half of the
    capture recipe, done from the panel."""
    if not ALLOW_LAUNCH:
        return {"error": "starting the watcher is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    running = _watcher_pids()
    if running:
        return {"status": "already_running", "pids": running,
                "hint": "a jCodeMunch watcher is already running; its log shows in the panel below"}
    if not _product_installed(MCP_BIN, "jcodemunch-mcp"):
        return {"error": "jcodemunch-mcp isn't installed", "hint": "install it from the Products rail first", "_status": 409}
    argv = [MCP_BIN, "watch-all", "--log-file", str(_WATCHER_LOG), "--log-level", "INFO"]
    try:
        _spawn_terminal(str(Path.home()), argv, hold_on_error=True)
    except Exception as e:
        return {"error": f"couldn't start the watcher: {e}", "_status": 500}
    return {"status": "watcher_started", "log": str(_WATCHER_LOG),
            "hint": "watching every indexed repo; reindex activity now logs to the panel below"}


def stop_watcher() -> dict:
    """Stop the running jcm reindex watcher(s) — the counterpart to start_watcher.
    Terminates the `watch`/`watch-all`/`watch-claude` process(es) (which the
    console can identify but didn't necessarily spawn — the user may have opened
    the terminal). Indexed repos stay indexed. ALLOW_LAUNCH-gated."""
    if not ALLOW_LAUNCH:
        return {"error": "stopping the watcher is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    pids = _watcher_pids()
    if not pids:
        return {"status": "not_running", "hint": "no jCodeMunch watcher is running"}
    n = _kill_pids(pids)
    if not n:
        return {"error": "couldn't stop the watcher", "hint": "it may need elevated rights", "_status": 500}
    return {"status": "watcher_stopped", "stopped": n, "pids": pids}


def enable_server_logging() -> dict:
    """Turn on jcm *server* file logging from the panel: write the log_file +
    log_level config keys, then restart the jcm server(s) so the MCP client
    respawns them reading the new config. The server is client-launched, so the
    console can't inject an env var into it — but as of jcodemunch-mcp 1.108.64
    the server honors the log_file config key, which the console CAN set. Older
    servers ignore it (set JCODEMUNCH_LOG_FILE in the MCP config instead).
    ALLOW_LAUNCH-gated. This is the 'set JCODEMUNCH_LOG_FILE' half of the recipe."""
    if not ALLOW_LAUNCH:
        return {"error": "enabling server logging is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    set_file = config_set("log_file", str(_SERVER_LOG))
    if set_file.get("_status"):
        return set_file  # surfaces the config-set error + status as-is
    config_set("log_level", "INFO")  # best-effort; level is secondary to the file
    restart = restart_server("jcodemunch")
    _set_console_flag("server_logging", True)
    note = ("a jCodeMunch server was restarted — it now logs to the file below"
            if restart.get("status") == "restarted"
            else "config set; the running server picks it up the next time it restarts")
    return {"status": "server_logging_enabled", "log": str(_SERVER_LOG),
            "config": {"log_file": str(_SERVER_LOG), "log_level": "INFO"},
            "restart": restart, "note": note,
            "hint": "needs jcodemunch-mcp >= 1.108.64; older servers ignore the log_file config key"}


def disable_server_logging() -> dict:
    """Turn jcm server file logging back off — the counterpart to
    enable_server_logging: unset the log_file (+ log_level) config keys and
    restart the jcm server(s) so they stop writing. ALLOW_LAUNCH-gated."""
    if not ALLOW_LAUNCH:
        return {"error": "disabling server logging is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    unset = config_unset("log_file")
    if unset.get("_status"):
        return unset  # surface the config error + status as-is
    config_unset("log_level")  # best-effort; revert to default
    _set_console_flag("server_logging", False)
    restart = restart_server("jcodemunch")
    note = ("a jCodeMunch server was restarted — it has stopped file logging"
            if restart.get("status") == "restarted"
            else "config cleared; the running server stops logging the next time it restarts")
    return {"status": "server_logging_disabled", "restart": restart, "note": note}


def kill_process(pid) -> dict:
    """Stop one server process by pid. Gated (ALLOW_LAUNCH) and validated against
    the live set of recognized product server pids — the client names a pid, but
    we only kill it when it currently matches a known server, so the panel can't
    be turned into an arbitrary process killer."""
    if not ALLOW_LAUNCH:
        return {"error": "stop is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"error": "invalid pid", "_status": 400}
    if pid <= 0:
        return {"error": "invalid pid", "_status": 400}
    snap = _enumerate_processes()
    valid: set[int] = set()
    for _prod_id, _name, binname, *_rest in PRODUCTS:
        valid.update(_running_server_pids(binname, snap))
    if pid not in valid:
        return {"error": f"pid {pid} is not a recognized jMunch server process",
                "hint": "the list may be stale — refresh the panel", "_status": 400}
    if not _kill_pids([pid]):
        return {"error": f"couldn't stop pid {pid}", "hint": "it may need elevated rights", "_status": 500}
    return {"status": "killed", "pid": pid, "hint": _respawn_hint()}


def clear_logs() -> dict:
    """Housekeeping: delete the jcm log files the Diagnostics panel tails — the
    console-managed capture logs (_WATCHER_LOG / _SERVER_LOG) and the per-PID
    watcher logs (`jcw_*.log`) in the temp dir. A log a live watcher/server still
    holds open can't be removed (Windows file lock) and is left in place rather
    than truncated. NEVER touches the index store, telemetry.db, or savings/
    delivery history. ALLOW_LAUNCH-gated (it deletes files)."""
    if not ALLOW_LAUNCH:
        return {"error": "clearing logs is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    targets: list[Path] = sorted(Path(tempfile.gettempdir()).glob("jcw_*.log"))
    targets += [_WATCHER_LOG, _SERVER_LOG]
    removed: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for p in targets:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not p.exists():
                continue
            p.unlink()
            removed.append(p.name)
        except OSError:
            skipped.append(p.name)  # in use by a live process (or no permission)
    hint = f"{len(skipped)} log(s) still in use were left in place" if skipped else ""
    return {"status": "logs_cleared", "removed": removed, "skipped": skipped, "hint": hint}


def launch(agent: str, repo_id: str) -> dict:
    """Launch a detected agent in an indexed repo. Default-off, allowlisted.

    All trust is server-side: the agent must be in the live detected-clients
    list and have a fixed launch command; the repo must be a live indexed repo;
    its path is resolved here (the client only names a repo_id, never a path).
    """
    if not ALLOW_LAUNCH:
        return {"error": "launch is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    base = AGENT_LAUNCH.get(agent)
    if not base:
        return {"error": f"agent not launchable: {agent!r}", "_status": 400}
    if agent not in {a.get("agent") for a in agents().get("agents", [])}:
        return {"error": f"agent not detected: {agent!r}", "_status": 400}
    repo = next((r for r in repos().get("repos", []) if r.get("repo_id") == repo_id), None)
    if not repo:
        return {"error": f"repo not indexed: {repo_id!r}", "_status": 404}
    cwd = repo.get("source_root", "")
    if not cwd or not Path(cwd).is_dir():
        return {"error": f"repo path unavailable for {repo_id!r}", "_status": 404}
    exe = shutil.which(base)
    if not exe:
        return {"error": f"command not found on PATH: {base!r}", "_status": 404}
    try:
        _spawn_terminal(cwd, [exe])
    except Exception as e:
        return {"error": f"launch failed: {e}", "_status": 500}
    return {"status": "launched", "agent": agent, "repo_id": repo_id, "cwd": cwd}


def resume(session_id: str) -> dict:
    """Resume a Claude Code session via `claude --resume <id>` in its directory.

    Same default-off + server-side allowlist discipline as launch(): the session
    must be in the live sessions list; its cwd is resolved here from that entry,
    never client-supplied.
    """
    if not ALLOW_LAUNCH:
        return {"error": "resume is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    if not session_id:
        return {"error": "missing session_id", "_status": 400}
    sess = next((s for s in sessions().get("sessions", []) if s.get("session_id") == session_id), None)
    if not sess:
        return {"error": f"session not found: {session_id!r}", "_status": 404}
    cwd = sess.get("cwd", "")
    if not cwd or not Path(cwd).is_dir():
        return {"error": f"session repo path unavailable for {session_id!r}", "_status": 404}
    exe = shutil.which("claude")
    if not exe:
        return {"error": "command not found on PATH: 'claude'", "_status": 404}
    try:
        _spawn_terminal(cwd, [exe, "--resume", session_id], hold_on_error=True)
    except Exception as e:
        return {"error": f"resume failed: {e}", "_status": 500}
    return {"status": "resumed", "session_id": session_id, "cwd": cwd}


def upgrade(product_id: str) -> dict:
    """Upgrade a product to its latest GitHub release. System-modifying, so it
    rides the same ALLOW_LAUNCH gate as launch/resume and
    spawns the install in a visible terminal — the user watches pip run.

    Safety rails (server-side, all verdicts trusted here, not from the client):
    - refuses unless an update is actually available;
    - **refuses editable/dev installs** — pip-upgrading those clobbers the
      editable link (and can WinError-32 against a held .exe); `git pull` instead;
    - installs the release's `.whl` asset (the suite ships via GitHub wheels).
    """
    if not ALLOW_LAUNCH:
        return {"error": "upgrade is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    prod = next((p for p in PRODUCTS if p[0] == product_id), None)
    if not prod:
        return {"error": f"unknown product: {product_id!r}", "_status": 400}
    pid, name, binname, _envvar, dist, gh = prod
    import_name = dist.replace("-", "_")
    if _is_editable(import_name):
        return {"error": f"{name} is an editable/dev install",
                "hint": "update it with `git pull` in its repo, not pip", "_status": 409}
    pv, lv = _parse_ver(_installed_version(binname, dist)), _parse_ver(_latest_release(gh))
    if not (pv and lv and pv < lv):
        return {"error": f"{name} is already up to date", "_status": 409}
    wheel = _latest_wheel_url(gh)
    if not wheel:
        return {"error": "latest release has no .whl asset to install", "_status": 502}
    try:
        _spawn_terminal(str(Path.home()), _install_argv(wheel, upgrade=True))
    except Exception as e:
        return {"error": f"could not start upgrade: {e}", "_status": 500}
    _CACHE.pop("products", None)  # status will refresh once pip finishes
    return {"status": "upgrade_started", "product": pid, "wheel": wheel}


def _pip_runnable() -> bool:
    """True if the console's own interpreter can actually run pip. Modern setups
    ship pip-less interpreters (uv-managed pythons, Ubuntu 26.04 system py3.14),
    so `sys.executable -m pip` is no longer a safe assumption."""
    try:
        import importlib.util
        return importlib.util.find_spec("pip") is not None
    except Exception:
        return False


def _install_argv(spec: str, *, upgrade: bool = False, force: bool = False) -> list:
    """Command to install one suite-product wheel, newest mechanism first:
    `uv tool install` > `pipx install` > `python -m pip`. uv/pipx give each tool
    its own isolated environment (which the rail now detects correctly) and run
    on pip-less interpreters; pip stays as the fallback, with `ensurepip` ahead
    of it when the interpreter has no pip. Returns one argv, or a list of argvs
    (ensurepip then pip) that `_spawn_terminal` runs in order."""
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "tool", "install", spec]
        if upgrade or force:
            cmd.append("--force")
        return cmd
    pipx = shutil.which("pipx")
    if pipx:
        return [pipx, "install", "--force", spec] if (upgrade or force) else [pipx, "install", spec]
    if force:
        pip = [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", spec]
    elif upgrade:
        pip = [sys.executable, "-m", "pip", "install", "--upgrade", spec]
    else:
        pip = [sys.executable, "-m", "pip", "install", spec]
    return pip if _pip_runnable() else [[sys.executable, "-m", "ensurepip", "--upgrade"], pip]


def _install_all_argv(specs: list) -> list:
    """Install several product wheels. uv/pipx install one tool per invocation,
    so fan out to one argv each (run in order in one window); pip takes them all
    in a single run. Same uv > pipx > pip ordering as `_install_argv`."""
    uv = shutil.which("uv")
    if uv:
        return [[uv, "tool", "install", s] for s in specs]
    pipx = shutil.which("pipx")
    if pipx:
        return [[pipx, "install", s] for s in specs]
    pip = [sys.executable, "-m", "pip", "install", *specs]
    return pip if _pip_runnable() else [[sys.executable, "-m", "ensurepip", "--upgrade"], pip]


def _uninstall_argv(dist: str) -> list:
    """Remove one suite product, matching the installer that owns it:
    `uv tool uninstall` > `pipx uninstall` > `pip uninstall`. The tool name is
    the distribution name for all three munches."""
    uv = shutil.which("uv")
    if uv:
        return [uv, "tool", "uninstall", dist]
    pipx = shutil.which("pipx")
    if pipx:
        return [pipx, "uninstall", dist]
    return [sys.executable, "-m", "pip", "uninstall", "-y", dist]


def install(product_id: str) -> dict:
    """One-click install of a NOT-yet-installed product from its latest GitHub
    release wheel. This is the new-user front door — the whole point is to spare
    a newcomer the copy-a-command, open-a-terminal, run-pip dance.

    Unlike launch/resume/upgrade this is NOT gated behind ALLOW_LAUNCH: it is
    narrowly bounded (one of three hardcoded product ids, a `.whl` asset from our
    own allowlisted GitHub release, installed into the current interpreter) and
    is the literal purpose of the console for a first-time user. Trust is the
    same a user already extends by running our console at all. Still rides a
    client-side confirm and spawns pip in a visible terminal so nothing happens
    silently. The riskier upgrade path (which can clobber a held .exe) stays
    behind the two-key turn.
    """
    prod = next((p for p in PRODUCTS if p[0] == product_id), None)
    if not prod:
        return {"error": f"unknown product: {product_id!r}", "_status": 400}
    pid, name, binname, _envvar, dist, gh = prod
    if _product_installed(binname, dist):
        return {"error": f"{name} is already installed", "hint": "use update instead", "_status": 409}
    wheel = _latest_wheel_url(gh)
    if not wheel:
        return {"error": "latest release has no .whl asset to install", "_status": 502}
    try:
        _spawn_terminal(str(Path.home()), _install_argv(wheel))
    except Exception as e:
        return {"error": f"could not start install: {e}", "_status": 500}
    _CACHE.pop("products", None)  # status will refresh once pip finishes
    return {"status": "install_started", "product": pid, "wheel": wheel}


def install_all() -> dict:
    """Install every NOT-yet-installed product in a single pip run (one terminal).
    Same ungated, bounded posture as install() — a new user almost always wants
    the whole suite, so one click beats three. No-op if nothing is missing."""
    missing, wheels = [], []
    for pid, _name, binname, _envvar, _dist, gh in PRODUCTS:
        if shutil.which(binname):
            continue
        w = _latest_wheel_url(gh)
        if w:
            missing.append(pid)
            wheels.append(w)
    if not wheels:
        return {"error": "nothing to install — the whole suite is already present", "_status": 409}
    try:
        _spawn_terminal(str(Path.home()), _install_all_argv(wheels))
    except Exception as e:
        return {"error": f"could not start install: {e}", "_status": 500}
    _CACHE.pop("products", None)
    return {"status": "install_started", "products": missing, "count": len(wheels)}


def reinstall(product_id: str) -> dict:
    """Force-reinstall a product's latest release wheel — repairs a broken or
    partial install. Gated behind ALLOW_LAUNCH like upgrade (force-reinstall can
    clobber a held .exe). Refuses not-installed (use install) and editable/dev
    checkouts (those are git working trees, managed with git not pip).

    Uses `--no-deps`: a repair reinstalls the PACKAGE's own files, it must not
    force-reinstall the dependency tree. The suite's shared `mcp` dependency in
    particular is used by every munch (and the running jcm server that serves
    this console), so churning it from a single product's repair would risk the
    whole suite. If deps themselves are broken, install/upgrade is the right tool.
    """
    if not ALLOW_LAUNCH:
        return {"error": "reinstall is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    prod = next((p for p in PRODUCTS if p[0] == product_id), None)
    if not prod:
        return {"error": f"unknown product: {product_id!r}", "_status": 400}
    pid, name, binname, _envvar, dist, gh = prod
    if not _product_installed(binname, dist):
        return {"error": f"{name} is not installed", "hint": "use install instead", "_status": 409}
    if _is_editable(dist.replace("-", "_")):
        return {"error": f"{name} is an editable/dev install", "hint": "manage it with git, not pip", "_status": 409}
    wheel = _latest_wheel_url(gh)
    if not wheel:
        return {"error": "latest release has no .whl asset to install", "_status": 502}
    try:
        _spawn_terminal(str(Path.home()), _install_argv(wheel, force=True))
    except Exception as e:
        return {"error": f"could not start reinstall: {e}", "_status": 500}
    _CACHE.pop("products", None)
    return {"status": "reinstall_started", "product": pid, "wheel": wheel}


def uninstall(product_id: str) -> dict:
    """Uninstall a product via pip. Destructive and can hit a held .exe, so it
    rides the same two-key turn (ALLOW_LAUNCH) as upgrade/reinstall. Refuses
    not-installed and editable/dev checkouts (pip-uninstalling an editable removes
    the link and risks WinError 32 against a running server's .exe)."""
    if not ALLOW_LAUNCH:
        return {"error": "uninstall is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    prod = next((p for p in PRODUCTS if p[0] == product_id), None)
    if not prod:
        return {"error": f"unknown product: {product_id!r}", "_status": 400}
    pid, name, binname, _envvar, dist, gh = prod
    if not _product_installed(binname, dist):
        return {"error": f"{name} is not installed", "_status": 409}
    if _is_editable(dist.replace("-", "_")):
        return {"error": f"{name} is an editable/dev install", "hint": "remove it with git, not pip", "_status": 409}
    try:
        _spawn_terminal(str(Path.home()), _uninstall_argv(dist))
    except Exception as e:
        return {"error": f"could not start uninstall: {e}", "_status": 500}
    _CACHE.pop("products", None)
    return {"status": "uninstall_started", "product": pid, "dist": dist}


def delete_index(repo_id: str) -> dict:
    """Delete a repo's jCodeMunch index via `delete-index <repo> --json`.

    Destructive, so it rides the same ALLOW_LAUNCH two-key turn as the other
    mutating actions. The repo_id is validated against the live index list
    (never trust a raw client id), and the actual deletion runs in jcm through
    the canonical invalidate path (indexes are SQLite-backed — a filesystem
    delete would be unsafe). Requires jcodemunch-mcp >= 1.108.50.
    """
    if not ALLOW_LAUNCH:
        return {"error": "delete is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    repo_id = (repo_id or "").strip()
    if not repo_id:
        return {"error": "no repo_id given", "_status": 400}
    if repo_id not in {r.get("repo_id") for r in repos().get("repos", [])}:
        return {"error": f"repo not indexed: {repo_id!r}", "_status": 404}
    # Run delete-index capturing output even on a non-zero exit (delete-index
    # exits non-zero when there's no matching index), so jcm's own --json error
    # surfaces instead of a blanket "needs jcodemunch-mcp >= 1.108.50" guess that
    # is wrong whenever a recent jcm fails for some other reason.
    if FORCE_FIXTURES:
        return {"error": "delete is unavailable in sample-data (fixtures) mode", "_status": 503}
    exe = shutil.which(MCP_BIN)
    if not exe:
        return {"error": f"{MCP_BIN} not found on PATH", "hint": "is jcodemunch-mcp installed?", "_status": 404}
    try:
        out = subprocess.run(
            [exe, "delete-index", repo_id, "--json"],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"delete failed for {repo_id!r}: {e}", "_status": 500}
    res = None
    if out.stdout.strip():
        try:
            res = json.loads(out.stdout)
        except ValueError:
            res = None
    if res is None:
        # No parseable JSON — surface jcm's stderr / exit code as the real reason.
        detail = (out.stderr or "").strip().splitlines()
        msg = detail[-1] if detail else f"exit code {out.returncode}"
        return {"error": f"delete failed for {repo_id!r}: {msg}", "_status": 500}
    if not res.get("success"):
        return {"error": res.get("error") or f"delete failed for {repo_id!r}", "_status": 500}
    # A deleted repo can flip a starter pack's installed/present state; the pack
    # rail is cached (120s), so drop it now and the next /api/starter-packs recomputes.
    # Drop the repos cache too so the removal reflects without waiting out its TTL.
    _CACHE.pop("starter_packs", None)
    _CACHE.pop("repos", None)
    return {"status": "deleted", "repo_id": repo_id}


def reindex(repo_id: str) -> dict:
    """Re-index a repo in a new terminal (`jcodemunch-mcp index <source_root>`).

    Gated like the other mutating actions. The source path is resolved
    server-side from the live index list (the client only names a repo_id).
    """
    if not ALLOW_LAUNCH:
        return {"error": "reindex is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    repo = next((r for r in repos().get("repos", []) if r.get("repo_id") == repo_id), None)
    if not repo:
        return {"error": f"repo not indexed: {repo_id!r}", "_status": 404}
    cwd = repo.get("source_root", "")
    if not cwd or not Path(cwd).is_dir():
        return {"error": f"repo path unavailable for {repo_id!r}", "_status": 404}
    exe = shutil.which(MCP_BIN)
    if not exe:
        return {"error": f"command not found on PATH: {MCP_BIN!r}", "_status": 404}
    try:
        _spawn_terminal(cwd, [exe, "index", cwd])
    except Exception as e:
        return {"error": f"reindex failed: {e}", "_status": 500}
    return {"status": "reindex_started", "repo_id": repo_id, "cwd": cwd}


_GH_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def index_new(target: str) -> dict:
    """Index a fresh repo from the #index toolbar (`jcodemunch-mcp index <target>`)
    in a new terminal — the 'here' the greyed reindex tooltip refers to.

    Unlike reindex/delete this MUST accept a client-supplied target (there is no
    server-side repo to resolve yet), which breaks the `_spawn_terminal` "never
    raw client input" invariant — so the target is validated tightly before it
    ever reaches the spawned argv: a GitHub `owner/repo` must match a strict slug
    (no separators or shell metacharacters), and a local target must resolve to an
    existing directory whose path carries no cmd/shell metacharacters. Spawns with
    hold_on_error so a bad target leaves its error on screen. ALLOW_LAUNCH-gated;
    jcm's own JCODEMUNCH_TRUSTED_FOLDERS guard still applies at index time.
    """
    if not ALLOW_LAUNCH:
        return {"error": "indexing is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    target = (target or "").strip().strip('"').strip("'").strip()
    if not target:
        return {"error": "no folder path or owner/repo given", "_status": 400}
    exe = shutil.which(MCP_BIN)
    if not exe:
        return {"error": f"command not found on PATH: {MCP_BIN!r}", "_status": 404}
    p = Path(target).expanduser()
    # A relative `owner/repo` slug that isn't an on-disk dir is a GitHub target;
    # anything absolute / with separators / that exists on disk is a local folder.
    looks_github = bool(_GH_REPO_RE.match(target)) and "\\" not in target and not p.is_absolute()
    if looks_github and not p.is_dir():
        cwd, spawn_target, kind = str(Path.home()), target, "github"
    else:
        if not p.is_dir():
            return {"error": f"not a folder or owner/repo: {target!r}",
                    "hint": "enter an existing local directory (absolute path) or a GitHub 'owner/repo'",
                    "_status": 404}
        resolved = str(p.resolve())
        if any(c in resolved for c in '&|<>^"%\n\r'):
            return {"error": "folder path contains unsupported characters", "_status": 400}
        cwd, spawn_target, kind = resolved, resolved, "local"
    try:
        # hold_on_done: indexing is a deliberate one-shot, and it may resolve to an
        # already-indexed repo (refreshing it rather than adding a card). Keep the
        # window open on success too, so its result ("success: true, repo: ...") is
        # readable instead of vanishing — the "closed with no new index" confusion.
        _spawn_terminal(cwd, [exe, "index", spawn_target], hold_on_done=True)
    except Exception as e:
        return {"error": f"index failed to start: {e}", "_status": 500}
    # The new index won't exist until the terminal finishes; drop the pack cache so
    # a pack whose repos just landed recomputes its installed/present state on refresh.
    _CACHE.pop("starter_packs", None)
    return {"status": "index_started", "target": spawn_target, "kind": kind}


def org() -> dict:
    """Org rollup from `org-rollup --json` (team SKU). Unconfigured (no
    JCODEMUNCH_ORG_ID) is a normal state, not an error — the card shows setup
    guidance. Requires jcodemunch-mcp >= 1.108.38."""
    if not os.environ.get("JCODEMUNCH_ORG_ID"):
        return _tag({"configured": False}, "live")
    raw = _run_cli(["org-rollup", "--json"])
    if raw:
        try:
            data = json.loads(raw)
            return _tag({"configured": True, **data}, "live")
        except ValueError:
            pass
    return _tag({"configured": False}, "live")


def _mask_key(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def _load_licenses() -> dict:
    try:
        return json.loads(LICENSES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_licenses(data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    LICENSES_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _validate_license(product_id: str, key: str) -> dict:
    """Validate a key against the shared validate.php backend. Trust the JSON
    body's `valid` over the HTTP status. Returns {valid: bool|None, tier}.
    `valid` is None when the server is unreachable (don't punish offline)."""
    import urllib.parse
    import urllib.request

    if not key:
        return {"valid": False}
    url = VALIDATE_URL + "?" + urllib.parse.urlencode({"product": product_id, "license": key})
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(data, dict) and isinstance(data.get("valid"), bool):
            return {"valid": data["valid"], "tier": data.get("tier")}
    except Exception:
        pass
    return {"valid": None}


def _license_state(product_id: str, key: str) -> dict:
    """Resolve a (product, key) to a UI-ready license status block."""
    key = (key or "").strip()
    if not key:
        return {"license": "none"}
    v = _validate_license(product_id, key)
    state = "valid" if v["valid"] is True else "invalid" if v["valid"] is False else "entered"
    return {"license": state, "tier": v.get("tier"), "key_masked": _mask_key(key)}


def _config_license_key() -> str:
    """jCodeMunch's own `license_key` config value (set via the #config panel /
    `config set license_key`). Empty when unset or on any CLI error. The suite
    reads this (plus JCODEMUNCH_LICENSE_KEY) to gate org-rollup, so the console
    honors it as a license source too — otherwise a key entered in #config never
    lights the sidebar dot."""
    try:
        for k in (config().get("keys") or []):
            if k.get("key") == "license_key":
                v = k.get("value")
                return v.strip() if isinstance(v, str) else ""
    except Exception:
        pass
    return ""


def _effective_license_key(product_id: str) -> tuple:
    """The license key the console treats as active for a product, plus its
    source. Precedence: console store (licenses.json, set via the sidebar dot)
    → the product's env var → jCodeMunch's `license_key` config (jcodemunch
    only). One resolver so the sidebar dot AND the Starter Pack unlock agree
    with what the suite itself sees — a key set in any of the three places
    counts. Returns (key, source) where source ∈ console/env/config/None."""
    store = (_load_licenses().get(product_id) or "").strip()
    if store:
        return store, "console"
    envvar = next((e for pid, _, _, e, _, _ in PRODUCTS if pid == product_id), "")
    env = (os.environ.get(envvar, "") if envvar else "").strip()
    if env:
        return env, "env"
    if product_id == "jcodemunch":
        cfg = _config_license_key()
        if cfg:
            return cfg, "config"
    return "", None


def _any_valid_license() -> bool:
    """True when the console holds any valid suite license — the soft gate for
    license-only panels (e.g. the Savings 'By tool' breakdown). Distinct from
    `_pack_license`, which is strict-valid-only because it authorizes paid pack
    downloads: here we grant offline grace, so a key that's on file but can't be
    reached for validation still unlocks (don't punish a present key when the
    backend is unreachable). Returns False only when every product is genuinely
    keyless or its key validates as explicitly invalid. Cached 60s so the
    Savings panel's polling doesn't re-hit validate.php every tick."""
    def resolve() -> bool:
        any_entered = False
        for pid, *_ in PRODUCTS:
            key, _src = _effective_license_key(pid)
            if not key:
                continue
            v = _validate_license(pid, key).get("valid")
            if v is True:
                return True
            if v is None:  # present but unreachable: offline grace, don't blur
                any_entered = True
        return any_entered
    return _cached("any_valid_license", 60.0, resolve)


_VER_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")


def _cli_version(binname: str) -> str | None:
    """Best-effort version via `<bin> --version`. None when the CLI isn't on
    PATH or doesn't support the flag (jDoc/jData currently don't — only the
    semver is extracted, so unsupported flags that print usage yield None)."""
    if not shutil.which(binname):
        return None
    try:
        out = subprocess.run(
            [binname, "--version"],
            capture_output=True, text=True, timeout=12, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VER_RE.search((out.stdout or "") + " " + (out.stderr or ""))
    return m.group(1) if m else None


def _parse_ver(s: str | None) -> tuple | None:
    """Extract an (major, minor, patch) tuple from a version/tag string."""
    if not s:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", s)
    return tuple(int(x) for x in m.groups()) if m else None


def _venv_python_for(script_path: str) -> str | None:
    """The Python interpreter that owns a console script. uv-tool / pipx / venv
    installs each live in their OWN environment, so the script sits next to its
    interpreter (``<env>/bin`` POSIX, ``<env>/Scripts`` Windows) — or names it on
    the POSIX shebang. Returns None when no sibling interpreter is found."""
    try:
        p = Path(script_path).resolve()
    except Exception:
        return None
    bindir = p.parent
    for cand in ("python", "python3", "python.exe"):
        cp = bindir / cand
        if cp.exists():
            return str(cp)
    try:  # POSIX: the shebang names the exact interpreter
        with open(p, "rb") as fh:
            first = fh.readline(512)
        if first.startswith(b"#!"):
            interp = first[2:].strip().split(b" ", 1)[0].decode("utf-8", "replace")
            if interp and Path(interp).exists():
                return interp
    except Exception:
        pass
    return None


def _isolated_version(binname: str, dist: str) -> str | None:
    """Version via the binary's OWN interpreter. uv-tool / pipx installs can't be
    imported by the console's interpreter (separate environments), so probe the
    environment the launcher actually belongs to. This also doubles as a liveness
    check: a real isolated install answers, an orphaned launcher does not."""
    path = shutil.which(binname)
    if not path:
        return None
    py = _venv_python_for(path)
    if not py:
        return None
    try:
        out = subprocess.run(
            [py, "-c", f"import importlib.metadata as m;print(m.version({dist!r}))"],
            capture_output=True, text=True, timeout=12, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\d+\.\d+\.\d+\S*", out.stdout or "")
    return m.group(0) if m else None


def _installed_version(binname: str, dist: str) -> str | None:
    """Installed version, in order of authority: ``<bin> --version`` (jcm), then
    the binary's own interpreter metadata (uv-tool / pipx isolated installs —
    jDoc/jData don't support --version), then the console-interpreter metadata
    (same-interp / editable installs)."""
    v = _cli_version(binname)
    if v:
        return v
    v = _isolated_version(binname, dist)
    if v:
        return v
    try:
        import importlib.metadata as md
        return md.version(dist)
    except Exception:
        return None


def _latest_release(gh_repo: str) -> str | None:
    """Latest GitHub release tag for a repo (the suite ships via GitHub-release
    wheels, not PyPI). Cached 30 min — respects rate limits and stays
    offline-safe (returns None on any failure, so no false 'update' signal)."""
    def fetch() -> dict:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.github.com/repos/{gh_repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "jmunch-console"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return {"tag": json.loads(r.read().decode("utf-8", "replace")).get("tag_name")}
        except Exception:
            return {"tag": None}

    return _cached(f"release:{gh_repo}", 1800.0, fetch).get("tag")


def _latest_release_prefixed(gh_repo: str, prefix: str) -> str | None:
    """Latest release tag starting with *prefix* — for monorepos whose
    /releases/latest can be a sibling crate's release (tokf's workspace tags
    catalog-types-v*, tokf-server-v*, and tokf-v* into the same stream).
    Same 30-min cache + offline-safe None as _latest_release."""
    def fetch() -> dict:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.github.com/repos/{gh_repo}/releases?per_page=30",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "jmunch-console"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                for rel in json.loads(r.read().decode("utf-8", "replace")):
                    tag = rel.get("tag_name") or ""
                    if tag.startswith(prefix):
                        return {"tag": tag}
        except Exception:
            pass
        return {"tag": None}

    return _cached(f"release:{gh_repo}:{prefix}", 1800.0, fetch).get("tag")


def _latest_wheel_url(gh_repo: str) -> str | None:
    """The `.whl` asset URL on the latest GitHub release (the suite's install
    artifact), or None. Cached 30 min, offline-safe."""
    def fetch() -> dict:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.github.com/repos/{gh_repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "jmunch-console"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            for a in data.get("assets", []):
                if str(a.get("name", "")).endswith(".whl"):
                    return {"url": a.get("browser_download_url")}
        except Exception:
            pass
        return {"url": None}

    return _cached(f"wheel:{gh_repo}", 1800.0, fetch).get("url")


def _is_editable(import_name: str) -> bool:
    """True when the package's code lives OUTSIDE site-packages — i.e. a
    `pip install -e` / dev checkout. Such installs must never be pip-upgraded
    (it would clobber the editable link); `git pull` is the right update path."""
    try:
        import importlib.util
        import sysconfig
        spec = importlib.util.find_spec(import_name)  # resolves path without executing the module
        if not spec or not spec.origin:
            return False
        origin = os.path.realpath(spec.origin)
        for key in ("purelib", "platlib"):
            sp = sysconfig.get_paths().get(key)
            if sp and origin.startswith(os.path.realpath(sp)):
                return False
        return True
    except Exception:
        return False


def _dist_present(dist: str) -> bool:
    """True if the distribution has installed metadata in the console's
    interpreter. Catches packages that are genuinely installed (incl. editable /
    user-site) but whose console-script isn't on the console's PATH."""
    try:
        import importlib.metadata as md
        md.version(dist)
        return True
    except Exception:
        return False


def _product_installed(binname: str, dist: str) -> bool:
    """A product counts as installed if its CLI is on PATH OR its dist has
    metadata. `shutil.which` alone false-negatives user-site / editable installs
    and proxy-launched munches: the `jmunch-mcp` proxy starts a munch by the
    absolute path in its `.toml`, so the bare exe need not be on the console's
    PATH (e.g. jDataMunch here, whose system-site launcher was removed but which
    remains installed editable at user-site and runs fine via the proxy)."""
    return bool(shutil.which(binname)) or _dist_present(dist)


def _editable_root(dist: str) -> Path | None:
    """Source-repo root of an editable install: find_spec on the module gives
    <root>/src/<pkg>/__init__.py; the root is two levels above the package.
    None unless that root is a real git checkout."""
    try:
        import importlib.util
        spec = importlib.util.find_spec(dist.replace("-", "_"))
        origin = Path(spec.origin) if spec and spec.origin else None
        if origin is None:
            return None
        root = origin.parents[2]
        return root if (root / ".git").is_dir() else None
    except Exception:
        return None


def _refresh_editable_metadata(dist: str, root: Path) -> dict:
    """Heal editable-install version drift WITHOUT pip. An editable install
    imports its code live from the source tree, so `git pull` already makes the
    new code current; only the dist-info METADATA version stays frozen at
    install time (that's why a pulled checkout can read 'behind'). We rewrite
    just the `Version:` line of the installed METADATA in place — importlib.metadata
    reports the version from METADATA *content*, not the dir name, so this clears
    the drift. Crucially it never touches the console scripts, so it can't
    collide with a running MCP server that holds `<dist>.exe` open — which is
    exactly the WinError 32 a `pip install -e` refresh hits on Windows."""
    import re
    import importlib.metadata as md
    try:
        pj = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError as e:
        return {"refreshed": False, "reason": f"could not read pyproject.toml: {e}"}
    target = None
    try:
        import tomllib  # py3.11+
        target = (tomllib.loads(pj).get("project") or {}).get("version")
    except ModuleNotFoundError:
        # py3.10 fallback: first version= inside the [project] table.
        sec = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", pj)
        if sec:
            mv = re.search(r"""(?m)^\s*version\s*=\s*["']([^"']+)["']""", sec.group(1))
            target = mv.group(1) if mv else None
    if not target:
        return {"refreshed": False, "reason": "no static [project].version in pyproject.toml"}
    try:
        d = md.distribution(dist)
    except md.PackageNotFoundError:
        return {"refreshed": False, "reason": f"{dist} has no installed metadata"}
    old = d.version
    if old == target:
        return {"refreshed": False, "reason": "already current", "version": target}
    info_dir = getattr(d, "_path", None)
    if not info_dir:
        return {"refreshed": False, "reason": "could not locate dist-info dir"}
    meta_path = Path(info_dir) / "METADATA"
    try:
        txt = meta_path.read_text(encoding="utf-8")
        new_txt, n = re.subn(r"(?m)^Version:.*$", f"Version: {target}", txt, count=1)
        if not n:
            return {"refreshed": False, "reason": "no Version field in METADATA"}
        meta_path.write_text(new_txt, encoding="utf-8")
    except OSError as e:
        return {"refreshed": False, "reason": f"could not rewrite METADATA: {e}"}
    return {"refreshed": True, "old_version": old, "version": target}


def git_update(product_id: str) -> dict:
    """Update an editable/dev checkout: `git pull` its source root, then heal the
    dist-info version in place. Runs in-process — ThreadingHTTPServer gives each
    request its own thread, and both steps are fast + local. Deliberately NOT a
    `pip install -e` refresh: reinstalling rewrites the package's console
    scripts, and a running MCP server holding `<dist>.exe` makes that fail with
    WinError 32 (the held-launcher trap). `git pull` makes the editable code
    live; `_refresh_editable_metadata` clears the version label without touching
    the executable. Gated like upgrade; the repo root is resolved server-side
    from the import system, never client input."""
    if not ALLOW_LAUNCH:
        return {"error": "update is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    prod = next((p for p in PRODUCTS if p[0] == product_id), None)
    if not prod:
        return {"error": f"unknown product: {product_id!r}", "_status": 400}
    _pid, name, binname, _envvar, dist, _gh = prod
    if not _product_installed(binname, dist):
        return {"error": f"{name} is not installed", "_status": 409}
    if not _is_editable(dist.replace("-", "_")):
        return {"error": f"{name} is not a dev checkout", "hint": "use the regular update", "_status": 409}
    root = _editable_root(dist)
    if root is None:
        return {"error": f"could not resolve {name}'s source checkout", "_status": 500}
    # GIT_TERMINAL_PROMPT=0 so a missing credential fails fast instead of hanging
    # the request thread; DEVNULL stdin for the same reason (+ the suite's own
    # git-stdio lesson).
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        pull = subprocess.run(
            ["git", "-C", str(root), "pull"],
            capture_output=True, text=True, timeout=180,
            env=env, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"git pull timed out for {name}", "hint": "check the checkout for conflicts or a credential prompt", "_status": 504}
    except Exception as e:
        return {"error": f"could not run git pull: {e}", "_status": 500}
    if pull.returncode != 0:
        detail = (pull.stderr or pull.stdout or "").strip()
        return {"error": f"git pull failed for {name}", "detail": detail[-500:], "_status": 500}
    git_line = next((ln.strip() for ln in reversed((pull.stdout or "").splitlines()) if ln.strip()), "")
    refresh = _refresh_editable_metadata(dist, root)
    _CACHE.pop("products", None)
    return {"status": "updated", "product": product_id, "git": git_line, "refresh": refresh}


def _products_live() -> dict:
    """Per-product status for the sidebar rail: installed? (+ version, + whether
    a newer GitHub release exists) + license state. Subprocess version probes and
    GitHub release lookups are slow, so fan them out concurrently."""
    with ThreadPoolExecutor(max_workers=2 * len(PRODUCTS)) as ex:
        ver_futs = {pid: ex.submit(_installed_version, binname, dist)
                    for pid, _, binname, _, dist, _ in PRODUCTS}
        rel_futs = {pid: ex.submit(_latest_release, gh)
                    for pid, _, _, _, _, gh in PRODUCTS}
        versions = {pid: f.result() for pid, f in ver_futs.items()}
        latests = {pid: f.result() for pid, f in rel_futs.items()}
    out = []
    for pid, name, binname, envvar, dist, gh in PRODUCTS:
        installed = _product_installed(binname, dist)
        editable = installed and _is_editable(dist.replace("-", "_"))
        ver = versions.get(pid) if installed else None
        # Orphaned launcher: the console-script .exe survives on PATH while the
        # package itself is gone from every interpreter (seen 2026-06-12 — the
        # rail showed a false green while every new spawn died on import). A
        # resolved version (incl. uv-tool / pipx isolated installs, probed via
        # the launcher's own interpreter) proves the package is really there, so
        # only the truly-dead launcher — on PATH, no metadata, no version from
        # any interpreter — counts as broken.
        broken = bool(installed and not _dist_present(dist) and shutil.which(binname) and not ver)
        latest = latests.get(pid)
        pv, lv = _parse_ver(ver), _parse_ver(latest)
        # `behind` = a newer release tag exists than what's installed. When the
        # installed version is current we never surface upgrade info (don't pester).
        behind = bool(installed and pv and lv and pv < lv)
        # For editable/dev checkouts a newer tag is noise (the tree updates via
        # `git pull`, not pip) and the upgrade path refuses them, so don't
        # advertise a clickable pip-upgrade — the rail shows a "dev" state and
        # only mentions `git pull` when actually behind.
        update_available = behind and not editable
        key, key_source = _effective_license_key(pid)
        out.append({
            "id": pid,
            "name": name,
            "installed": installed,
            "editable": editable,
            "broken": broken,
            "version": ver,
            "latest_version": latest,
            "behind": behind,
            "update_available": update_available,
            "key_source": key_source,
            **_license_state(pid, key),
        })
    return _tag({"products": out}, "live")


def products(fresh: bool = False) -> dict:
    """Cached — `which` is instant but validate.php is a network hop; client
    rarely changes install/license state mid-session. `fresh` busts the cache
    so the rail's manual refresh re-probes versions and re-validates keys."""
    if fresh:
        _CACHE.pop("products", None)
    return _cached("products", 60.0, _products_live)


def set_license(product_id: str, key: str) -> dict:
    """Persist a license key into the console store (blank clears it), then
    report the resulting state. The console's first write surface — bounded to
    known product ids and a single JSON file, no RCE."""
    if product_id not in PRODUCT_IDS:
        return {"error": f"unknown product: {product_id}", "_status": 400}
    key = (key or "").strip()
    lics = _load_licenses()
    if key:
        lics[product_id] = key
    else:
        lics.pop(product_id, None)
    try:
        _save_licenses(lics)
    except OSError as e:
        return {"error": f"could not save license: {e}", "_status": 500}
    _CACHE.pop("products", None)  # reflect the change on the next /api/products
    _CACHE.pop("pack_license", None)  # a new key may unlock the pack library
    _CACHE.pop("starter_packs", None)
    _CACHE.pop("any_valid_license", None)  # clear the soft-gate immediately
    return {"status": "saved", "id": product_id, **_license_state(product_id, key)}


# --------------------------------------------------------------------------- #
# Starter Packs — pre-built indexes from the catalog, addable from #index
# --------------------------------------------------------------------------- #
# The catalog is a public JSON endpoint; jcm's `install-pack` CLI downloads and
# extracts a pack into the index store, after which its repos appear in
# list-repos. Free packs need no key; licensed packs unlock once the console
# holds any valid suite license. The key is resolved server-side and handed to
# install-pack — the client never names it.

STARTER_PACK_API = "https://j.gravelle.us/jCodeMunch/starter-packs-system/api/index.php"


def _index_dir() -> Path:
    """The jcm index store packs extract into (mirrors install_pack.py)."""
    return Path(os.environ.get("CODE_INDEX_PATH") or (Path.home() / ".code-index"))


def _pack_license() -> dict:
    """The license that unlocks licensed packs. Any valid suite key unlocks the
    whole library; the jCodeMunch key is preferred because the pack server
    validates downloads against the jcodemunch product. Returns
    {key, product, masked} or {} when no stored key validates. Cached 60s so the
    panel's polling doesn't re-hit validate.php every tick."""
    def resolve() -> dict:
        # Prefer jcodemunch (the pack server's validation product), then any
        # other suite key. Each product's key resolves through the same
        # store -> env -> config precedence the sidebar dot uses.
        order = ["jcodemunch"] + [pid for pid, *_ in PRODUCTS if pid != "jcodemunch"]
        for pid in order:
            key, _src = _effective_license_key(pid)
            if key and _validate_license(pid, key).get("valid") is True:
                return {"key": key, "product": pid, "masked": _mask_key(key)}
        return {}
    return _cached("pack_license", 60.0, resolve)


def _fetch_pack_catalog() -> list[dict]:
    """Fetch the public pack catalog. [] on any failure (offline-safe)."""
    try:
        with urllib.request.urlopen(f"{STARTER_PACK_API}?action=catalog", timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        packs = data.get("packs")
        return packs if isinstance(packs, list) else []
    except Exception:
        return []


def starter_packs(fresh: bool = False) -> dict:
    """Catalog cross-referenced with installed + unlock state, for the #index
    ghost-pack rail. A pack is installed when its marker exists or all its repos
    are already indexed; addable when free or unlocked; locked otherwise."""
    if fresh:
        _CACHE.pop("starter_packs", None)

    def build() -> dict:
        catalog = _fetch_pack_catalog()
        lic = _pack_license()
        unlocked = bool(lic)
        idx = _index_dir()
        installed_ids = {r.get("repo_id") for r in repos().get("repos", [])}
        out = []
        for p in catalog:
            pid = p.get("id", "")
            free = bool(p.get("free"))
            pack_repos = p.get("repos", []) or []
            marker_path = idx / f".pack-{pid}.json"
            marker = marker_path.exists()
            installed = marker or (bool(pack_repos) and all(r in installed_ids for r in pack_repos))
            # The marker is the only safe signal that WE installed this pack —
            # so uninstall/update key on it, never on bare repo overlap (those
            # repos may have been indexed independently, e.g. the observatory).
            installed_version = None
            if marker:
                try:
                    installed_version = json.loads(marker_path.read_text(encoding="utf-8")).get("installed_version")
                except (OSError, ValueError):
                    pass
            catalog_version = p.get("version")
            update_available = bool(
                marker and catalog_version and installed_version and installed_version != catalog_version
            )
            out.append({
                "id": pid,
                "name": p.get("name", pid),
                "description": p.get("description", ""),
                "repos": pack_repos,
                "symbols": p.get("symbols", 0),
                "size": p.get("size", ""),
                "source_size": p.get("source_size", ""),
                "savings_ratio": p.get("savings_ratio"),
                "indexed_date": p.get("indexed_date", ""),
                "free": free,
                "version": catalog_version,
                "installed": installed,
                "pack_installed": marker,       # installed via the console (uninstall/update enabled)
                "installed_version": installed_version,
                "update_available": update_available,
                "addable": (not installed) and (free or unlocked),
                "locked": (not installed) and (not free) and (not unlocked),
                # Re-download (install/update) needs the license again for paid packs.
                "update_addable": marker and (free or unlocked),
            })
        return _tag({
            "packs": out,
            "unlocked": unlocked,
            "key_masked": lic.get("masked"),
            "total": len(out),
        }, "live" if catalog else "offline")

    return _cached("starter_packs", 120.0, build)


def install_pack(pack_id: str, force: bool = False) -> dict:
    """Download + (re)install a starter pack into the index store, in a new
    terminal (`jcodemunch-mcp install-pack <id> [--force] [--license KEY]`).
    Gated like the other index-mutating actions. The pack id is validated against
    the live catalog and the license key is resolved server-side (never
    client-supplied); paid packs refuse unless a valid suite license is on file.

    `force=True` is the Update path: it re-downloads over an existing install, so
    it requires the pack to have been installed via the console (a marker), not
    merely present by repo overlap."""
    verb = "update" if force else "install"
    if not ALLOW_LAUNCH:
        return {"error": f"{verb}-pack is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    pack_id = (pack_id or "").strip()
    pack = next((p for p in starter_packs().get("packs", []) if p.get("id") == pack_id), None)
    if not pack:
        return {"error": f"unknown pack: {pack_id!r}", "_status": 404}
    if force and not pack.get("pack_installed"):
        return {"error": f"pack not installed via the console: {pack_id!r}", "hint": "nothing to update", "_status": 409}
    if not force and pack.get("installed"):
        return {"error": f"pack already installed: {pack_id!r}", "_status": 409}
    lic = _pack_license()
    if not pack.get("free") and not lic:
        return {
            "error": f"this pack needs a jCodeMunch license to {verb}",
            "hint": "enter a valid license key on any suite product to unlock the full pack library",
            "get_license": "https://j.gravelle.us/jCodeMunch/#pricing",
            "_status": 403,
        }
    exe = shutil.which(MCP_BIN)
    if not exe:
        return {"error": f"command not found on PATH: {MCP_BIN!r}", "_status": 404}
    argv = [exe, "install-pack", pack_id]
    if force:
        argv.append("--force")
    if not pack.get("free") and lic.get("key"):
        argv += ["--license", lic["key"]]
    try:
        _spawn_terminal(str(ROOT), argv, hold_on_error=True)
    except Exception as e:
        return {"error": f"{verb}-pack failed: {e}", "_status": 500}
    # Next poll should re-check installed state from a fresh catalog + index list.
    _CACHE.pop("starter_packs", None)
    return {"status": "pack_update_started" if force else "pack_install_started", "pack": pack_id}


def uninstall_pack(pack_id: str) -> dict:
    """Remove a console-installed starter pack: delete each of its repo indexes
    (`delete-index`) and drop the install marker. Local-only (no download), so no
    license check — but gated on the marker so we never delete repos the console
    didn't install (bare repo overlap, e.g. the observatory's own indexes, is
    left alone). Runs inline since delete-index is fast and the caller wants the
    removed list back to refresh the view."""
    if not ALLOW_LAUNCH:
        return {"error": "uninstall-pack is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    pack_id = (pack_id or "").strip()
    pack = next((p for p in starter_packs().get("packs", []) if p.get("id") == pack_id), None)
    if not pack:
        return {"error": f"unknown pack: {pack_id!r}", "_status": 404}
    if not pack.get("pack_installed"):
        return {
            "error": f"pack not installed via the console: {pack_id!r}",
            "hint": "its repos may be indexed independently — remove those from the index grid instead",
            "_status": 409,
        }
    removed, failed = [], []
    for repo_id in pack.get("repos", []):
        raw = _run_cli(["delete-index", repo_id, "--json"])
        # delete-index exits non-zero (→ _run_cli None) when the index is already
        # gone; that's a no-op success for an uninstall, so only flag hard errors.
        if raw is None:
            removed.append(repo_id)  # nothing there to remove; treat as done
            continue
        try:
            ok = json.loads(raw).get("success", True)
        except ValueError:
            ok = True
        (removed if ok else failed).append(repo_id)
    # Drop the install marker so the pack flips back to addable.
    try:
        (_index_dir() / f".pack-{pack_id}.json").unlink(missing_ok=True)
    except OSError as e:
        return {"error": f"removed indexes but could not clear the marker: {e}", "_status": 500}
    _CACHE.pop("starter_packs", None)
    out = {"status": "pack_uninstalled", "pack": pack_id, "removed": removed}
    if failed:
        out["failed"] = failed
    return out


def adopt_pack(pack_id: str) -> dict:
    """Claim an overlap-present pack as console-managed by writing its install
    marker — no download. For packs whose repos are already in the index by
    other means (e.g. the observatory): adoption enables update + uninstall.

    No license check — it only marks data the user already holds, and update
    still re-downloads behind the license gate. Note this is consequential: once
    adopted, Uninstall will delete those repo indexes (the UI confirm says so)."""
    if not ALLOW_LAUNCH:
        return {"error": "adopt-pack is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    pack_id = (pack_id or "").strip()
    pack = next((p for p in starter_packs().get("packs", []) if p.get("id") == pack_id), None)
    if not pack:
        return {"error": f"unknown pack: {pack_id!r}", "_status": 404}
    if pack.get("pack_installed"):
        return {"error": f"pack already managed: {pack_id!r}", "_status": 409}
    if not pack.get("installed"):
        return {"error": f"pack not present to adopt: {pack_id!r}", "hint": "install it instead", "_status": 409}
    marker = _index_dir() / f".pack-{pack_id}.json"
    payload = {
        "pack": pack_id,
        "name": pack.get("name", pack_id),
        "repos": pack.get("repos", []),
        "version": pack.get("version"),
        "installed_version": pack.get("version"),  # treat the adopted index as current
        "adopted": True,
    }
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        return {"error": f"could not write marker: {e}", "_status": 500}
    _CACHE.pop("starter_packs", None)
    return {"status": "pack_adopted", "pack": pack_id}


# --------------------------------------------------------------------------- #
# Other Apps — curated third-party companions (full lifecycle, no licensing)
# --------------------------------------------------------------------------- #
# Each app brings its own install mechanics, captured in the _OTHER_OPS
# registry below: Caveman is a Claude Code skill installed via its upstream
# Node installer (npx); Headroom is a pip package (headroom-ai) whose
# uninstall first unwraps Claude Code. Adding an app = one roster tuple + one
# _OTHER_OPS entry. Apps with no version marker (Caveman) get the release tag
# stamped at install time in data/other_apps.json (Console-owned, gitignored)
# and diffed against the latest GitHub release; apps with dist metadata
# (Headroom) report their real installed version.

OTHER_APPS_FILE = DATA / "other_apps.json"

# (id, display name, GitHub repo)
OTHER_APPS = [
    ("caveman", "Caveman", "JuliusBrussee/caveman"),
    ("headroom", "Headroom", "chopratejas/headroom"),
    ("tokf", "tokf", "mpecan/tokf"),
]
OTHER_APP_IDS = {a[0] for a in OTHER_APPS}

_CAVEMAN_SH = "https://raw.githubusercontent.com/JuliusBrussee/caveman/main/install.sh"
_CAVEMAN_NPX_SPEC = "github:JuliusBrussee/caveman"


def _load_other_stamps() -> dict:
    try:
        return json.loads(OTHER_APPS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_other_stamps(d: dict) -> None:
    try:
        DATA.mkdir(exist_ok=True)
        OTHER_APPS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except OSError:
        pass


def _stamp_other(app_id: str, gh: str) -> None:
    stamps = _load_other_stamps()
    stamps[app_id] = {
        "version": _latest_release(gh),
        "stamped_at": datetime.datetime.now(
            datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "via": "console",
    }
    _save_other_stamps(stamps)


def _claude_config_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")))


def _caveman_installed() -> bool:
    """Best-effort, no subprocess. Modern caveman (v1.9+) installs as a Claude
    Code PLUGIN only — `claude plugin install caveman@caveman`, no standalone
    hook files (upstream #392: the plugin manifest wires hooks itself). The
    faithful signal is the enabledPlugins entry in settings.json: a clean
    `claude plugin uninstall` removes exactly that while leaving the download
    cache and marketplace registration behind. Legacy standalone installs are
    still recognised by their caveman-* hook files."""
    base = _claude_config_dir()
    try:
        if any((base / "hooks").glob("caveman-*")):
            return True
        try:
            settings = json.loads((base / "settings.json").read_text(encoding="utf-8"))
            enabled = settings.get("enabledPlugins") or {}
            if any(k.startswith("caveman@") and v for k, v in enabled.items()):
                return True
        except (OSError, ValueError):
            pass
        plugins = base / "plugins"
        # plugins/cache (download cache) and plugins/marketplaces (marketplace
        # registration) both survive a clean uninstall — `claude plugin
        # uninstall` removes the plugin, not the registration. Neither is an
        # install signal; counting them pinned the dot green forever.
        for pat in ("*caveman*", "*/*caveman*", "*/*/*caveman*"):
            if any(p for p in plugins.glob(pat)
                   if p.relative_to(plugins).parts[0] not in ("cache", "marketplaces")):
                return True
    except OSError:
        pass
    return False


def _other_apps_live() -> dict:
    stamps = _load_other_stamps()
    apps = []
    for aid, name, gh in OTHER_APPS:
        ops = _OTHER_OPS[aid]
        installed = ops["detect"]()
        latest = ops["latest"]() if "latest" in ops else _latest_release(gh)
        # Real version probe first (headroom has dist metadata); fall back to
        # the console's install-time stamp for apps with no version marker.
        version = None
        if installed:
            version = ops["version"]() or (stamps.get(aid) or {}).get("version")
        v_now, v_new = _parse_ver(version), _parse_ver(latest)
        apps.append({
            "id": aid, "name": name,
            "url": f"https://github.com/{gh}",
            "installed": installed,
            "version": version,  # None = installed outside the console (untracked)
            "latest_version": latest,
            "update_available": bool(installed and v_now and v_new and v_now < v_new),
            "description": ops["description"],
            "install_note": ops["install_note"],
            "uninstall_note": ops["uninstall_note"],
        })
    return {"apps": apps}


def other_apps(fresh: bool = False) -> dict:
    if fresh:
        _CACHE.pop("other_apps", None)
    return _tag(_cached("other_apps", 60.0, _other_apps_live), "live")


def _caveman_cmd(*flags: str) -> list[str]:
    """Upstream installer command for the current platform. Windows goes
    STRAIGHT to npx, skipping upstream's install.ps1 shim: under Windows
    PowerShell 5.1 the shim's `param($Args)` (collides with the automatic
    variable) + `@Args` splat into npx.ps1 silently drops EVERY flag — an
    --uninstall ran as a plain install (2026-06-11). cmd resolves npx.cmd,
    which forwards args faithfully, and bin/install.js does its own Node>=18
    check, so the shim adds nothing we need. POSIX keeps the install.sh shim
    (bash forwards args fine). Specs/URLs are hardcoded server-side; nothing
    here is client input."""
    if sys.platform == "win32":
        return ["npx", "-y", _CAVEMAN_NPX_SPEC, *flags]
    return ["bash", "-lc",
            f"f=$(mktemp); curl -fsSL {_CAVEMAN_SH} -o \"$f\" && bash \"$f\" "
            + " ".join(flags)]


def _headroom_installed() -> bool:
    return bool(shutil.which("headroom")) or _dist_present("headroom-ai")


def _node_preflight() -> dict | None:
    if shutil.which("node"):
        return None
    return {"error": "Caveman requires Node.js >= 18 — node was not found on PATH",
            "_status": 409}


def _cargo_preflight() -> dict | None:
    if shutil.which("cargo"):
        return None
    return {"error": "tokf installs via cargo — no Rust toolchain found on PATH",
            "hint": "install Rust from https://rustup.rs (Windows: winget install Rustlang.Rustup), then retry",
            "_status": 409}


# Per-app lifecycle strategy. detect() -> bool; version() -> str|None (None
# falls back to the console's install-time stamp); preflight() -> error dict
# or None; install/upgrade/uninstall() -> list of argv lists, run in order in
# ONE visible terminal (multi-step ops like headroom's unwrap-then-uninstall
# stay on screen together). install_note/uninstall_note feed the client's
# confirm dialogs so the copy stays app-accurate.
_OTHER_OPS = {
    "caveman": {
        "detect": _caveman_installed,
        "version": lambda: None,  # upstream writes no version marker; stamped at install
        "preflight": _node_preflight,
        "install":   lambda: [_caveman_cmd()],
        "upgrade":   lambda: [_caveman_cmd("--force")],
        "uninstall": lambda: [_caveman_cmd("--uninstall")],
        "description": "Caveman-speak compression for Claude Code — hooks + a plugin that strip prompts and responses down to terse, token-cheap shorthand.",
        "install_note": "This runs Caveman's installer from its GitHub repo in a new terminal. Requires Node.js 18+.",
        "uninstall_note": "Runs its installer with --uninstall in a new terminal (removes hooks, plugin, and settings entries).",
    },
    "headroom": {
        "detect": _headroom_installed,
        "version": lambda: _installed_version("headroom", "headroom-ai"),
        "preflight": lambda: None,
        "install":   lambda: [[sys.executable, "-m", "pip", "install", "headroom-ai[all]"]],
        "upgrade":   lambda: [[sys.executable, "-m", "pip", "install", "--upgrade", "headroom-ai[all]"]],
        "uninstall": lambda: [["headroom", "unwrap", "claude"],
                              [sys.executable, "-m", "pip", "uninstall", "-y", "headroom-ai"]],
        "description": "Reversible prompt-stream compression — wraps Claude Code in a local proxy that compresses tool output, logs, and file content before the model sees them; originals stay retrievable.",
        "install_note": "pip-installs headroom-ai (Apache-2.0) in a new terminal. To activate it afterward, run `headroom wrap claude` — that routes Claude Code through Headroom's local compression proxy. Unwrapping fully restores normal routing.",
        "uninstall_note": "Unwraps Claude Code first (restores direct API routing), then pip-uninstalls headroom-ai. Both steps run in one terminal.",
    },
    "tokf": {
        "detect": lambda: bool(shutil.which("tokf")),
        "version": lambda: _cli_version("tokf"),
        "preflight": _cargo_preflight,
        "install":   lambda: [["cargo", "install", "tokf"]],
        "upgrade":   lambda: [["cargo", "install", "tokf", "--force"]],
        "uninstall": lambda: [["cargo", "uninstall", "tokf"]],
        # /releases/latest can be a sibling workspace crate; pin to tokf-v* tags
        "latest": lambda: _latest_release_prefixed("mpecan/tokf", "tokf-v"),
        "description": "Deterministic CLI-output filtering — Rust binary that strips noise from cargo/git/docker/test output via TOML rules before it reaches Claude. No LLM, no latency, reproducible.",
        "install_note": "cargo-installs tokf (MIT) in a new terminal — it builds from source, so allow a few minutes. To activate afterward, run `tokf hook install --global` — that filters Claude Code's Bash output through tokf's deterministic TOML rules.",
        "uninstall_note": "cargo-uninstalls the tokf binary. If you ran `tokf hook install`, remove its PreToolUse entry from .claude/settings.json FIRST — tokf ships no hook uninstaller, and a hook pointing at a deleted binary errors on every Bash call.",
    },
}


def _spawn_other(app_id: str, op: str) -> None:
    _spawn_terminal(str(Path.home()), _OTHER_OPS[app_id][op](), hold_on_error=True)


def other_install(app_id: str) -> dict:
    """Install a curated third-party app. GATED behind ALLOW_LAUNCH as of
    v0.8.1 (2026-06-11 incident): unlike our own release wheels, this executes
    a third party's installer fetched from their repo at run time — that is
    launch-grade trust, so it rides the same two-key turn as launch/upgrade.
    The ungated one-click exception remains products-only."""
    if not ALLOW_LAUNCH:
        return {"error": "third-party install is disabled",
                "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    if app_id not in OTHER_APP_IDS:
        return {"error": f"unknown app: {app_id!r}", "_status": 400}
    _, name, gh = next(a for a in OTHER_APPS if a[0] == app_id)
    ops = _OTHER_OPS[app_id]
    if ops["detect"]():
        return {"error": f"{name} is already installed", "hint": "use update instead", "_status": 409}
    err = ops["preflight"]()
    if err:
        return err
    try:
        _spawn_other(app_id, "install")
    except Exception as e:
        return {"error": f"could not start install: {e}", "_status": 500}
    _stamp_other(app_id, gh)  # harmless if the user aborts: version only surfaces while installed
    _CACHE.pop("other_apps", None)
    return {"status": "install_started", "app": app_id}


def other_upgrade(app_id: str) -> dict:
    """Update = re-run the installer with --force (upstream documents re-runs as
    safe/idempotent). Rides the two-key turn like product upgrades."""
    if not ALLOW_LAUNCH:
        return {"error": "update is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    if app_id not in OTHER_APP_IDS:
        return {"error": f"unknown app: {app_id!r}", "_status": 400}
    _, name, gh = next(a for a in OTHER_APPS if a[0] == app_id)
    if not _OTHER_OPS[app_id]["detect"]():
        return {"error": f"{name} is not installed", "_status": 409}
    try:
        _spawn_other(app_id, "upgrade")
    except Exception as e:
        return {"error": f"could not start update: {e}", "_status": 500}
    _stamp_other(app_id, gh)
    _CACHE.pop("other_apps", None)
    return {"status": "upgrade_started", "app": app_id}


def other_uninstall(app_id: str) -> dict:
    """Uninstall via the upstream installer's --uninstall (it walks an explicit
    file manifest: hooks, plugin, settings.json entries). Two-key turn."""
    if not ALLOW_LAUNCH:
        return {"error": "uninstall is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    if app_id not in OTHER_APP_IDS:
        return {"error": f"unknown app: {app_id!r}", "_status": 400}
    _, name, _gh = next(a for a in OTHER_APPS if a[0] == app_id)
    if not _OTHER_OPS[app_id]["detect"]():
        return {"error": f"{name} is not installed", "_status": 409}
    try:
        _spawn_other(app_id, "uninstall")
    except Exception as e:
        return {"error": f"could not start uninstall: {e}", "_status": 500}
    stamps = _load_other_stamps()
    stamps.pop(app_id, None)
    _save_other_stamps(stamps)
    _CACHE.pop("other_apps", None)
    return {"status": "uninstall_started", "app": app_id}


# --------------------------------------------------------------------------- #
# Claude usage panel — hybrid local + org sources
# --------------------------------------------------------------------------- #
# Two complementary sources, separately cached:
#   * local — per-message `usage` blocks in Claude Code transcripts under
#     ~/.claude/projects. Zero lag, no key needed, this machine only.
#   * org   — the Usage & Cost Admin API (CLAUDE_ADMIN_KEY). Whole org, ~5 min
#     behind, sanctioned polling cadence is once per minute — the 60s cache TTL
#     enforces that server-side no matter how hard the UI polls.
# Dollar figures are computed at read time from the pricing table below; the
# org cost endpoint is daily-only, so it reconciles rather than drives.

_USAGE_WINDOW_H = 24  # local scan lookback

# (model-id substring, input $/MTok, output $/MTok) — first match wins.
# Cache reads bill at 0.1x input; 5-minute cache writes at 1.25x input.
_MODEL_PRICES = [
    ("fable", 10.0, 50.0),
    ("mythos", 10.0, 50.0),
    ("opus", 5.0, 25.0),
    ("sonnet", 3.0, 15.0),
    ("haiku", 1.0, 5.0),
]


def _usage_usd(model: str, t: dict) -> float:
    for frag, in_rate, out_rate in _MODEL_PRICES:
        if frag in model:
            return (t.get("input", 0) * in_rate
                    + t.get("output", 0) * out_rate
                    + t.get("cache_read", 0) * in_rate * 0.10
                    + t.get("cache_creation", 0) * in_rate * 1.25) / 1_000_000
    return 0.0


def _zero_tok() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "messages": 0}


def _tok_add(acc: dict, ev: tuple) -> None:
    acc["input"] += ev[2]
    acc["output"] += ev[3]
    acc["cache_read"] += ev[4]
    acc["cache_creation"] += ev[5]
    acc["messages"] += 1


# Per-file parse cache: transcripts are append-only, so an unchanged
# (size, mtime) means the parsed events are still good and the file is skipped.
_USAGE_FILES: dict[str, tuple[tuple[int, float], list[tuple]]] = {}


def _parse_usage_file(path: Path) -> list[tuple]:
    """(epoch, model, input, output, cache_read, cache_creation) per assistant
    message with a usage block. A multi-block assistant turn is several jsonl
    lines sharing one message id and usage payload — dedup by id, keep the last
    occurrence (its output count is the most complete)."""
    by_id: dict[str, tuple] = {}
    try:
        with path.open(encoding="utf-8") as fh:
            for n, raw in enumerate(fh):
                if '"usage"' not in raw:
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(e, dict) or e.get("type") != "assistant":
                    continue
                m = e.get("message") or {}
                u = m.get("usage") or {}
                ts = e.get("timestamp") or ""
                if not u or not ts:
                    continue
                try:
                    epoch = datetime.datetime.fromisoformat(
                        ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                by_id[m.get("id") or f"line-{n}"] = (
                    epoch,
                    str(m.get("model") or "unknown"),
                    int(u.get("input_tokens") or 0),
                    int(u.get("output_tokens") or 0),
                    int(u.get("cache_read_input_tokens") or 0),
                    int(u.get("cache_creation_input_tokens") or 0),
                )
    except OSError:
        pass
    return list(by_id.values())


def _usage_local_live() -> dict:
    """Aggregate local Claude Code token usage: per-model totals for the last
    hour and last 24h, plus a 60-slot per-minute series for the live chart."""
    root = Path(os.path.expanduser(
        os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")))
    now = time.time()
    cut24, cut60 = now - _USAGE_WINDOW_H * 3600, now - 3600
    events: list[tuple] = []
    seen: set[str] = set()
    try:
        for jf in root.glob("*/*.jsonl"):
            try:
                st = jf.stat()
            except OSError:
                continue
            if st.st_mtime < cut24:
                continue  # append-only: an old mtime can't hold in-window events
            key = str(jf)
            seen.add(key)
            sig = (st.st_size, st.st_mtime)
            ent = _USAGE_FILES.get(key)
            if not ent or ent[0] != sig:
                ent = (sig, _parse_usage_file(jf))
                _USAGE_FILES[key] = ent
            events.extend(ent[1])
    except OSError:
        pass
    for stale in set(_USAGE_FILES) - seen:
        _USAGE_FILES.pop(stale, None)

    models: dict[str, dict] = {}
    hour = _zero_tok()
    day = _zero_tok()
    minutes = [0] * 60  # new (uncached-in + out + cache-write) tokens per minute
    for ev in events:
        if ev[0] < cut24:
            continue
        _tok_add(models.setdefault(ev[1], _zero_tok()), ev)
        _tok_add(day, ev)
        if ev[0] >= cut60:
            _tok_add(hour, ev)
            slot = min(59, int((ev[0] - cut60) // 60))
            minutes[slot] += ev[2] + ev[3] + ev[5]
    model_rows = sorted(
        ({"model": k, **v, "usd": round(_usage_usd(k, v), 4)}
         for k, v in models.items()),
        key=lambda r: r["usd"], reverse=True)
    return {
        "available": bool(events),
        "hour": hour,
        "day": day,
        "day_usd": round(sum(r["usd"] for r in model_rows), 2),
        "minutes": minutes,
        "models": model_rows,
    }


def _admin_get(path_qs: str) -> dict:
    req = urllib.request.Request(
        "https://api.anthropic.com" + path_qs,
        headers={
            "anthropic-version": "2023-06-01",
            "x-api-key": ADMIN_KEY,
            "User-Agent": "jMunchConsole/0.7.0 (https://github.com/jgravelle/jmunch-console)",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _iso_z(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(
        int(epoch), datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _org_bucket_tokens(r: dict) -> dict:
    """Defensive read of one usage-report result row — field names vary a bit
    across report versions, and cache_creation may be a nested object."""
    cc = r.get("cache_creation")
    cc_total = (sum(int(v or 0) for v in cc.values()) if isinstance(cc, dict)
                else int(r.get("cache_creation_input_tokens") or 0))
    return {
        "input": int(r.get("uncached_input_tokens") or r.get("input_tokens") or 0),
        "output": int(r.get("output_tokens") or 0),
        "cache_read": int(r.get("cache_read_input_tokens") or 0),
        "cache_creation": cc_total,
    }


def _usage_org_live() -> dict:
    """Last hour of org-wide usage in 1m buckets, grouped by model. Data lands
    ~5 minutes behind real time; the 60s cache keeps us inside the API's
    sustained 1-request/minute polling guidance."""
    if not ADMIN_KEY:
        return {"available": False,
                "reason": "CLAUDE_ADMIN_KEY not set — add it to the Console's .env "
                          "(requires an organization account; sk-ant-admin... key)"}
    now = time.time()
    try:
        data = _admin_get(
            "/v1/organizations/usage_report/messages"
            f"?starting_at={_iso_z(now - 3600)}&ending_at={_iso_z(now)}"
            "&bucket_width=1m&group_by[]=model&limit=60")
    except (OSError, ValueError) as e:
        return {"available": False, "reason": f"usage API error: {e}"}
    models: dict[str, dict] = {}
    minutes = [0] * 60
    for i, bucket in enumerate(data.get("data") or []):
        for row in bucket.get("results") or []:
            t = _org_bucket_tokens(row)
            model = str(row.get("model") or "unknown")
            acc = models.setdefault(
                model, {**_zero_tok(), "messages": None})
            for k in ("input", "output", "cache_read", "cache_creation"):
                acc[k] += t[k]
            if i < 60:
                minutes[i] += t["input"] + t["output"] + t["cache_creation"]
    model_rows = sorted(
        ({"model": k,
          "input": v["input"], "output": v["output"],
          "cache_read": v["cache_read"], "cache_creation": v["cache_creation"],
          "usd": round(_usage_usd(k, v), 4)}
         for k, v in models.items()),
        key=lambda r: r["usd"], reverse=True)
    return {
        "available": True,
        "minutes": minutes,
        "models": model_rows,
        "hour_usd": round(sum(r["usd"] for r in model_rows), 2),
        "lag_note": "org data lands ~5 min behind real time",
    }


def _cost_org_live() -> dict:
    """Last 7 days of org cost (USD), daily buckets — billing truth for the
    reconcile row. The endpoint is daily-only, hence the long cache."""
    if not ADMIN_KEY:
        return {"available": False}
    now = time.time()
    day = 86400
    start = (int(now) // day - 6) * day  # midnight UTC, 6 days back
    try:
        data = _admin_get(
            f"/v1/organizations/cost_report?starting_at={_iso_z(start)}"
            f"&ending_at={_iso_z(now)}&bucket_width=1d")
    except (OSError, ValueError) as e:
        return {"available": False, "reason": f"cost API error: {e}"}
    days = []
    for bucket in data.get("data") or []:
        cents = 0.0
        for row in bucket.get("results") or []:
            try:
                cents += float(row.get("amount") or 0)
            except (TypeError, ValueError):
                continue
        days.append({"date": str(bucket.get("starting_at") or "")[:10],
                     "usd": round(cents / 100.0, 2)})
    return {"available": True, "days": days,
            "week_usd": round(sum(d["usd"] for d in days), 2)}


def usage_panel() -> dict:
    """Hybrid Claude usage: local transcripts (instant) + org Admin API
    (authoritative, lagged). Each source carries its own cache cadence."""
    if FORCE_FIXTURES:
        return _tag(_fixture("usage"), "fixture")
    local = _cached("usage_local", 10.0, _usage_local_live)
    org = _cached("usage_org", 60.0, _usage_org_live)
    cost = _cached("usage_cost", 3600.0, _cost_org_live)
    if not local.get("available") and not org.get("available"):
        fx = _fixture("usage")
        if fx:
            return _tag(fx, "fixture")
    return _tag({"local": local, "org": org, "cost": cost}, "live")


# --------------------------------------------------------------------------- #
# Alerts: notify-only thresholds over signals the panels already surface.
#
# The catalog is a fixed, server-side allowlist — the browser only ever toggles
# an alert on/off and edits its numeric threshold; it never names a metric or
# supplies code to run. Same posture as config_set's key allowlist. Evaluating
# an alert reads existing data functions and compares a number: it acts on
# nothing in the suite, so (like console_set) it is deliberately NOT
# ALLOW_LAUNCH-gated. Four alerts ship ON with sane defaults so the tab is
# useful with zero configuration; two advanced ones ship OFF behind a drawer.
# --------------------------------------------------------------------------- #

# Each entry: metric (key into _alert_metrics), unit (formats the value + keeps
# token/count thresholds integral), compare ('gt' breaches when value >= thr),
# default threshold, default enabled, tier ('default' shown up front, 'advanced'
# in the collapsed drawer).
_ALERT_CATALOG = [
    {"id": "daily_cost", "label": "Daily spend (est.)", "metric": "day_usd",
     "unit": "usd", "compare": "gt", "default": 25.0, "enabled": True, "tier": "default",
     "desc": "Local Claude Code usage over the last 24h, priced at API list rates — the same "
             "figure as Token Usage's Est. spend. Actual consumption, not a savings estimate; "
             "notional if you're on a subscription."},
    {"id": "daily_tokens", "label": "Daily token burn", "metric": "day_tokens",
     "unit": "tokens", "compare": "gt", "default": 5_000_000, "enabled": True, "tier": "default",
     "desc": "New tokens across local sessions in the last 24 hours (excludes cached reads)."},
    {"id": "hourly_tokens", "label": "Hourly burn spike", "metric": "hour_tokens",
     "unit": "tokens", "compare": "gt", "default": 2_000_000, "enabled": True, "tier": "default",
     "desc": "New tokens in the last 60 minutes (excludes cached reads) — catches a runaway loop early."},
    {"id": "log_errors", "label": "Errors in logs", "metric": "error_lines",
     "unit": "count", "compare": "gt", "default": 5, "enabled": True, "tier": "default",
     "desc": "Error lines in the recent tail of the jCodeMunch logs."},
    {"id": "server_idle", "label": "Server went quiet", "metric": "idle_min",
     "unit": "min", "compare": "gt", "default": 30.0, "enabled": False, "tier": "advanced",
     "desc": "Minutes since jCodeMunch last served a tool call (needs a running server)."},
    {"id": "weekly_cost", "label": "Weekly org cost", "metric": "week_usd",
     "unit": "usd", "compare": "gt", "default": 100.0, "enabled": False, "tier": "advanced",
     "desc": "Org-wide spend over the last 7 days (needs a CLAUDE_ADMIN_KEY)."},
]
_ALERT_BY_ID = {a["id"]: a for a in _ALERT_CATALOG}

# A value within this fraction of (but not past) the threshold reads as "warn"
# (approaching) rather than "ok" — the gentle nudge before the breach.
_ALERT_WARN_BAND = 0.8


def _burn_tok(d) -> int:
    """New tokens in a _zero_tok-shaped dict: input + output + cache_creation.
    Deliberately EXCLUDES cache_read — a prompt cache hit re-counts the whole
    cached context on every turn, so including it inflates a 24h figure into
    something that looks like a lifetime total. This is the same definition the
    Token Usage panel uses for its headline 'new tokens' figure (renderUsage's
    newDay) and its per-minute series, so Alerts and Usage agree."""
    if not isinstance(d, dict):
        return 0
    return int((d.get("input") or 0) + (d.get("output") or 0)
               + (d.get("cache_creation") or 0))


def _alert_metrics() -> dict:
    """Current value for every alert metric, gathered once per poll. A value of
    None means the signal isn't available right now (no transcripts yet, no admin
    key, jcm hasn't served a call) — the alert renders 'no data', never a false
    breach. Reuses the panels' own cached data functions, so this is cheap."""
    m: dict = {}
    try:
        u = usage_panel()
    except Exception:
        u = {}
    local = u.get("local") or {}
    cost = u.get("cost") or {}
    if local.get("available"):
        m["day_usd"] = float(local.get("day_usd") or 0.0)
        m["day_tokens"] = _burn_tok(local.get("day"))
        m["hour_tokens"] = _burn_tok(local.get("hour"))
    else:
        m["day_usd"] = m["day_tokens"] = m["hour_tokens"] = None
    m["week_usd"] = (float(cost["week_usd"]) if cost.get("available")
                     and cost.get("week_usd") is not None else None)
    try:
        diag = diagnostics()
    except Exception:
        diag = {}
    logs = diag.get("logs") or []
    m["error_lines"] = sum(int(f.get("errors") or 0) for f in logs)
    hb = (diag.get("signals") or {}).get("heartbeat")
    m["idle_min"] = (round(hb["age_s"] / 60.0, 1)
                     if isinstance(hb, dict) and hb.get("age_s") is not None else None)
    return m


def _read_alert_overrides() -> dict:
    """Per-alert {enabled, threshold} overrides from console_settings.json. The
    catalog supplies defaults; this only holds what the user has changed."""
    raw = _read_console_settings().get("alerts")
    return raw if isinstance(raw, dict) else {}


def _effective_alert(defn: dict, override) -> dict:
    enabled, threshold = defn["enabled"], defn["default"]
    if isinstance(override, dict):
        if isinstance(override.get("enabled"), bool):
            enabled = override["enabled"]
        t = override.get("threshold")
        if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0:
            threshold = t
    return {"enabled": enabled, "threshold": threshold}


def _alert_state(value, threshold: float, compare: str) -> str:
    """ok | warn | breach | nodata for one evaluated alert."""
    if value is None:
        return "nodata"
    if compare == "gt":
        if value >= threshold:
            return "breach"
        return "warn" if value >= threshold * _ALERT_WARN_BAND else "ok"
    # 'lt': breaches when the value falls to/under the floor (reserved for
    # future health-style alerts; no catalog entry uses it yet).
    if value <= threshold:
        return "breach"
    return "warn" if value <= threshold * (2 - _ALERT_WARN_BAND) else "ok"


def alerts_panel() -> dict:
    """Every catalog alert with its live value and a state. Notify-only: this
    reports, it never acts on the suite. Thresholds + on/off persist in
    console_settings.json under 'alerts'."""
    metrics = _alert_metrics()
    overrides = _read_alert_overrides()
    out: list[dict] = []
    breach = warn = 0
    for defn in _ALERT_CATALOG:
        eff = _effective_alert(defn, overrides.get(defn["id"]))
        value = metrics.get(defn["metric"])
        state = (_alert_state(value, eff["threshold"], defn["compare"])
                 if eff["enabled"] else "off")
        if state == "breach":
            breach += 1
        elif state == "warn":
            warn += 1
        out.append({
            "id": defn["id"], "label": defn["label"], "desc": defn["desc"],
            "unit": defn["unit"], "compare": defn["compare"], "tier": defn["tier"],
            "enabled": eff["enabled"], "threshold": eff["threshold"],
            "value": value, "state": state,
        })
    src = "fixture" if FORCE_FIXTURES else "live"
    return _tag({"alerts": out, "breach_count": breach, "warn_count": warn}, src)


def alert_set(alert_id: str, enabled, threshold) -> dict:
    """Persist one alert's on/off and/or threshold to console_settings.json.
    Console-local, notify-only config: rides token auth + the localhost bind like
    every endpoint, but is deliberately NOT ALLOW_LAUNCH-gated — setting a number
    acts on nothing in the suite. Validated against the catalog allowlist."""
    defn = _ALERT_BY_ID.get(alert_id)
    if defn is None:
        return {"error": f"unknown alert: {alert_id!r}", "_status": 400}
    d = _read_console_settings()
    alerts = d.get("alerts")
    if not isinstance(alerts, dict):
        alerts = {}
    cur = alerts.get(alert_id)
    entry = dict(cur) if isinstance(cur, dict) else {}
    if isinstance(enabled, bool):
        entry["enabled"] = enabled
    if threshold is not None:
        try:
            t = float(threshold)
        except (TypeError, ValueError):
            return {"error": "threshold must be a number", "_status": 400}
        if t <= 0:
            return {"error": "threshold must be greater than 0", "_status": 400}
        # Keep token/count thresholds integral so they round-trip cleanly.
        entry["threshold"] = int(t) if defn["unit"] in ("tokens", "count") else t
    alerts[alert_id] = entry
    d["alerts"] = alerts
    _write_console_settings(d)
    return {"status": "set", "id": alert_id}


def console_set(key: str, value) -> dict:
    """Edit one of the console's own settings from the Config screen. Persists
    to data/console_settings.json (loaded at startup). `actions`, `fixtures`,
    and `org_id` apply live; `port`, `token`, and `mcp_bin` apply on the next
    restart (mcp_bin is captured in tuples at import; a live swap would
    half-apply). Deliberately NOT ALLOW_LAUNCH-gated: the actions switch is the
    gate's own control, and it still rides token auth + the localhost bind. A
    key the environment pinned is refused so an operator's env-var choice can't
    be overridden from the browser."""
    global ALLOW_LAUNCH, FORCE_FIXTURES, CHAT_ENABLED
    if key not in ("port", "token", "mcp_bin", "fixtures", "actions", "org_id", "chat"):
        return {"error": f"unknown setting: {key!r}", "_status": 400}
    stored = "read_only" if key == "actions" else key
    if stored in _ENV_PINNED:
        return {"error": f"{key} is pinned by the {_SETTINGS_ENV[stored]} environment variable",
                "hint": "unset it and restart the console to edit here", "_status": 403}
    d = _read_console_settings()
    note = ""
    if key == "actions":
        ALLOW_LAUNCH = bool(value)
        d["read_only"] = "0" if ALLOW_LAUNCH else "1"
    elif key == "fixtures":
        FORCE_FIXTURES = bool(value)
        d["fixtures"] = "1" if FORCE_FIXTURES else "0"
    elif key == "chat":
        CHAT_ENABLED = bool(value)
        d["chat"] = "1" if CHAT_ENABLED else "0"
        _CACHE.pop("chat_cap", None)  # capability flips immediately, not in 30s
    elif key == "port":
        try:
            port = int(value)
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            return {"error": "port must be a number between 1 and 65535", "_status": 400}
        d["port"] = str(port)
        note = "applies on restart"
    elif key == "mcp_bin":
        binname = str(value or "").strip()
        if not binname:
            return {"error": "the CLI name/path can't be empty", "_status": 400}
        d["mcp_bin"] = binname
        note = "applies on restart"
    elif key == "org_id":
        # The suite's own JCODEMUNCH_ORG_ID. Apply live: org() reads os.environ
        # and the `org-rollup` subprocess inherits it, so no restart needed.
        org_id = str(value or "").strip()
        if org_id:
            d["org_id"] = org_id
            os.environ["JCODEMUNCH_ORG_ID"] = org_id
        else:
            d.pop("org_id", None)
            os.environ.pop("JCODEMUNCH_ORG_ID", None)
    else:  # token
        token = str(value or "").strip()
        if token:
            d["token"] = token
        else:
            d.pop("token", None)
        note = "applies on restart"
    _write_console_settings(d)
    out = {"status": "set", "key": key}
    if note:
        out["note"] = note
    return out


# The live server, captured in main() so console_stop can shut it down without a
# module-global import cycle. None until main() binds.
_HTTPD: ThreadingHTTPServer | None = None


def _defer(fn, delay: float = 0.35) -> None:
    """Run `fn` on a daemon thread after a short delay, so the HTTP response that
    triggered it has time to flush before the process restarts or exits."""
    def run() -> None:
        time.sleep(delay)
        fn()
    threading.Thread(target=run, daemon=True).start()


def console_restart() -> dict:
    """Re-exec the console itself — same mechanism as the dev auto-reload, but on
    demand from the Config screen. `os.execv` swaps in a fresh interpreter on the
    same argv and port, so settings captured at import (port, token, mcp_bin) take
    effect and the browser reconnects on its next poll. Deferred so this response
    flushes first; `_serve`'s bind-retry covers the momentary port overlap. Gated
    behind ALLOW_LAUNCH like the other process-touching actions."""
    if not ALLOW_LAUNCH:
        return {"error": "restart is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    print("jMunch Console  ->  restart requested from Config", flush=True)
    _defer(_console_reexec)
    return {"status": "restarting", "port": int(os.environ.get("JMUNCH_CONSOLE_PORT", "8765")),
            "hint": "the console is re-launching on the same port — this page reconnects in a moment"}


def console_stop() -> dict:
    """Shut the console down. Unblocks serve_forever via httpd.shutdown() so main()
    returns and the process exits cleanly; a hard os._exit fallback fires if the
    graceful path wedges. There's no UI to start it again afterward (the process is
    gone) — relaunch from your terminal or launcher. Gated behind ALLOW_LAUNCH."""
    if not ALLOW_LAUNCH:
        return {"error": "stop is disabled", "hint": "read-only mode is on; unset JMUNCH_CONSOLE_READ_ONLY to enable", "_status": 403}
    print("jMunch Console  ->  stop requested from Config", flush=True)

    def _shutdown() -> None:
        try:
            if _HTTPD is not None:
                _HTTPD.shutdown()
        except Exception:
            pass
        time.sleep(1.0)  # main() should have returned by now; force it if not
        os._exit(0)

    _defer(_shutdown)
    return {"status": "stopping",
            "hint": "the console is shutting down — relaunch it from your terminal or launcher to return"}


# --------------------------------------------------------------------------- #
# Productivity / cost-per-outcome (durable-change delivery)                    #
# --------------------------------------------------------------------------- #
# The token meters show INPUT (used / saved). This panel shows OUTPUT per cost:
# jcm's `delivery` CLI gives durable-change counts over a window (the numerator),
# and the local transcripts give the AI spend attributable to that repo over the
# same window (the denominator). Joined here = cost-per-durable-change.
# Attribution is approximate by design (spend made while the session cwd was in
# the repo); durability is trailing (commits_provisional). Diagnostic trend, not
# a leaderboard.

_DELIVERY_WIN_CHOICES = (14, 30, 90)


def _clamp_window(raw, default: int = 30) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(365, n))


def _repo_roots() -> tuple:
    """(roots, id_to_root) for indexed repos with a local path. `roots` is a
    list of (normcased_abspath_source_root, repo_id) sorted longest-first so a
    nested repo wins the prefix match over its parent; `id_to_root` maps a repo
    id straight to its root (for `repo=` tool args)."""
    roots, id_to_root = [], {}
    for r in (repos().get("repos") or []):
        sr, rid = r.get("source_root"), r.get("repo_id")
        if sr and r.get("has_source"):
            n = os.path.normcase(os.path.abspath(sr))
            roots.append((n, rid))
            if rid:
                id_to_root[rid] = n
    roots.sort(key=lambda t: len(t[0]), reverse=True)
    return roots, id_to_root


def _resolve_root(sig: str, roots: list, id_to_root: dict):
    """Map one touch signal — a `repo=` id or an absolute file path — to a repo
    root. Relative paths are ignored (they'd resolve against the wrong cwd)."""
    if sig in id_to_root:
        return id_to_root[sig]
    if os.path.isabs(sig):
        n = os.path.normcase(os.path.abspath(sig))
        for root, _rid in roots:
            if n == root or n.startswith(root + os.sep):
                return root
    return None


def _dominant_root(touched: list, roots: list, id_to_root: dict):
    """The repo a single message worked in = the most-referenced root among its
    tool touches (repo args + absolute paths); None if it touched no repo."""
    counts: dict = {}
    for sig in touched:
        r = _resolve_root(sig, roots, id_to_root)
        if r:
            counts[r] = counts.get(r, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


_TOUCH_KEYS = ("repo", "file_path", "path", "source_root", "folder", "cwd", "notebook_path")
_TOUCH_LIST_KEYS = ("file_paths", "paths")

# Per-file attribution-parse cache (append-only transcripts → (size,mtime) safe).
_ATTR_FILES: dict = {}


def _parse_attribution_file(path: Path) -> tuple:
    """Per assistant message: (epoch, model, input, output, cache_read,
    cache_creation, [touch signals]). Touch signals are `repo=` ids and the
    file/path args from every tool_use block in the message — the durable
    evidence of which repo the spend was for, independent of the session cwd.
    Returns (records sorted by epoch, session_cwd)."""
    by_id: dict = {}
    cwd = ""
    try:
        with path.open(encoding="utf-8") as fh:
            for n, raw in enumerate(fh):
                if '"usage"' not in raw:
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(e, dict) or e.get("type") != "assistant":
                    continue
                if not cwd and isinstance(e.get("cwd"), str):
                    cwd = e["cwd"]
                m = e.get("message") or {}
                u = m.get("usage") or {}
                ts = e.get("timestamp") or ""
                if not u or not ts:
                    continue
                try:
                    epoch = datetime.datetime.fromisoformat(
                        ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                touched: list = []
                for block in (m.get("content") or []):
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    inp = block.get("input") or {}
                    if not isinstance(inp, dict):
                        continue
                    for k in _TOUCH_KEYS:
                        v = inp.get(k)
                        if isinstance(v, str) and v:
                            touched.append(v)
                    for k in _TOUCH_LIST_KEYS:
                        v = inp.get(k)
                        if isinstance(v, list):
                            touched.extend(x for x in v if isinstance(x, str) and x)
                by_id[m.get("id") or f"line-{n}"] = (
                    epoch,
                    str(m.get("model") or "unknown"),
                    int(u.get("input_tokens") or 0),
                    int(u.get("output_tokens") or 0),
                    int(u.get("cache_read_input_tokens") or 0),
                    int(u.get("cache_creation_input_tokens") or 0),
                    touched,
                )
    except OSError:
        pass
    return sorted(by_id.values(), key=lambda r: r[0]), cwd


def _attr_parse_cached(path: Path) -> tuple:
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return [], ""
    sig = (st.st_size, st.st_mtime)
    ent = _ATTR_FILES.get(key)
    if not ent or ent[0] != sig:
        ent = (sig, _parse_attribution_file(path))
        _ATTR_FILES[key] = ent
    return ent[1]


def _delivery_cost(source_root: str, window_days: int) -> dict:
    """AI spend attributable to a repo over a window, by what each session
    TOUCHED — not its cwd. Within each transcript, each message is attributed to
    the repo it referenced (repo arg / absolute path); messages with no touch
    inherit the session's current repo (carry-forward), seeded from the cwd when
    that cwd is itself inside a repo. Spend lands on a repo only when the session
    actually worked there, so an umbrella-folder cwd no longer blanks attribution."""
    roots, id_to_root = _repo_roots()
    target = os.path.normcase(os.path.abspath(source_root))
    proot = Path(os.path.expanduser(
        os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")))
    cut = time.time() - window_days * 86400
    usd = 0.0
    events = 0
    sessions = 0
    seen: set = set()
    try:
        for jf in proot.glob("*/*.jsonl"):
            try:
                if jf.stat().st_mtime < cut:
                    continue
            except OSError:
                continue
            recs, cwd = _attr_parse_cached(jf)
            current = _resolve_root(cwd, roots, id_to_root) if cwd else None
            hit = False
            for ev in recs:
                if ev[0] < cut:
                    continue
                mr = _dominant_root(ev[6], roots, id_to_root)
                if mr:
                    current = mr
                if current == target:
                    usd += _usage_usd(ev[1], {
                        "input": ev[2], "output": ev[3],
                        "cache_read": ev[4], "cache_creation": ev[5],
                    })
                    events += 1
                    hit = True
            if hit:
                sessions += 1
    except OSError:
        pass
    return {"cost_usd": round(usd, 4), "message_events": events, "matched_sessions": sessions}


def _record_delivery_history(repo_id: str, snap: dict) -> list:
    """Append today's per-repo cost-per-durable snapshot to a Console-owned
    history file and return this repo's series (one point/day, today updated in
    place, capped 90 days). Best-effort; never raises."""
    today = datetime.date.today().isoformat()
    book: dict = {}
    try:
        if DELIVERY_HISTORY.exists():
            book = json.loads(DELIVERY_HISTORY.read_text(encoding="utf-8"))
            if not isinstance(book, dict):
                book = {}
    except (OSError, ValueError):
        book = {}
    series = book.get(repo_id) or []
    point = {"date": today, **snap}
    if series and series[-1].get("date") == today:
        series[-1] = point
    else:
        series.append(point)
    series = series[-90:]
    book[repo_id] = series
    try:
        DATA.mkdir(exist_ok=True)
        DELIVERY_HISTORY.write_text(json.dumps(book), encoding="utf-8")
    except OSError:
        pass
    return series


def _repo_picklist(rows: list) -> list:
    return [{"repo_id": r.get("repo_id"), "display_name": r.get("display_name"),
             "has_source": r.get("has_source")} for r in rows]


def _run_exec(argv: list, timeout: int = 25) -> str | None:
    """Run an arbitrary local executable (git / gh) and return UTF-8-decoded
    stdout, or None on any failure. NOT MCP_BIN-prefixed — this is the console's
    own git/gh probe path, kept separate from the jcm CLI. Decoded as explicit
    UTF-8 (not text=True) so Windows cp1252 can't mangle unicode in output."""
    if FORCE_FIXTURES:
        return None
    try:
        out = subprocess.run(argv, capture_output=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
        if out.returncode != 0:
            return None
        return out.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None


def _repo_github_slug(source_root: str):
    """`owner/repo` from the repo's `origin` remote, or None (no remote, not a
    GitHub URL, or git absent). A local git call — no network, honors the same
    'the console shells CLIs, jcm stays offline' split as the rest of ROI."""
    if not source_root:
        return None
    url = _run_exec(["git", "-C", source_root, "remote", "get-url", "origin"], timeout=10)
    if not url or "github.com" not in url:
        return None
    tail = re.split(r"github\.com[:/]", url.strip(), maxsplit=1)
    if len(tail) < 2:
        return None
    parts = tail[1].strip().removesuffix(".git").strip("/").split("/")
    return "%s/%s" % (parts[0], parts[1]) if len(parts) >= 2 and parts[0] and parts[1] else None


def _gh_count(slug: str, kind: str, window_days: int):
    """Count merged PRs (`kind='pr'`) or closed issues (`kind='issue'`) on a
    GitHub repo within the window, via `gh`. int, or None if gh is missing / not
    authenticated / the API errored. Capped at 1000 (the `--limit`)."""
    since = (datetime.date.today() - datetime.timedelta(days=window_days)).isoformat()
    state = "merged" if kind == "pr" else "closed"
    search = ("merged:>=%s" if kind == "pr" else "closed:>=%s") % since
    out = _run_exec(["gh", kind, "list", "-R", slug, "--state", state,
                     "--search", search, "--limit", "1000", "--json", "number"], timeout=25)
    if out is None:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return len(data) if isinstance(data, list) else None


def _gh_unit_counts(slug: str, window_days: int) -> dict:
    """Merged-PR + closed-issue counts for a GitHub repo/window, cached 10 min
    (they're network calls). Degrades honestly when gh is absent/unauthed."""
    if not shutil.which("gh"):
        return {"gh_available": False, "reason": "GitHub CLI (gh) not installed"}

    def compute():
        prs = _gh_count(slug, "pr", window_days)
        issues = _gh_count(slug, "issue", window_days)
        if prs is None and issues is None:
            return {"gh_available": True,
                    "reason": "gh returned no data (run `gh auth login`, or the repo isn't on GitHub)"}
        return {"gh_available": True, "merged_prs": prs, "closed_issues": issues, "reason": ""}

    return _cached("ghunits_%s_%d" % (slug, window_days), 600.0, compute)


def delivery_panel(repo_id: str, window_raw) -> dict:
    """Cost-per-outcome panel: join jcm `delivery` durable-change counts with the
    AI spend attributable to the repo over the same window. Also reports ROI by
    other units of value (merged PR, closed issue) via `gh` when the repo is on
    GitHub — the SAME attributable spend over a different denominator. Read-only.
    Requires jcodemunch-mcp >= 1.108.69 (the `delivery` subcommand); the PR/issue
    units additionally need `gh` installed + authenticated."""
    window_days = _clamp_window(window_raw)
    rd = repos()
    rows = rd.get("repos") or []
    repo = next((r for r in rows if r.get("repo_id") == repo_id and r.get("has_source")), None)
    if repo is None:
        repo = next((r for r in rows if r.get("has_source")), None)
    if repo is None:
        return _tag({"available": False,
                     "reason": "no indexed repo with a local source path to measure",
                     "window_days": window_days, "window_choices": list(_DELIVERY_WIN_CHOICES),
                     "repos": _repo_picklist(rows)}, rd.get("_source", "live"))
    source_root = repo.get("source_root") or ""
    metrics = _run_cli_json(["delivery", source_root, "--window-days", str(window_days), "--json"])
    base = {"repo_id": repo.get("repo_id"), "display_name": repo.get("display_name"),
            "window_days": window_days, "window_choices": list(_DELIVERY_WIN_CHOICES),
            "repos": _repo_picklist(rows)}
    if metrics.get("error"):
        return _tag({**base, "available": False, "reason": metrics["error"]}, "live")
    cost = _delivery_cost(source_root, window_days)
    durable = int(metrics.get("commits_durable") or 0)
    # Only claim a cost-per-outcome when spend is confidently this repo's: at
    # least one session whose cwd was at/under the repo root. Umbrella-dir
    # sessions (cwd a parent of many repos) can't be split per-repo, so we show
    # "not attributable" rather than a misleading $0/change.
    attributable = cost["matched_sessions"] > 0
    cpd = round(cost["cost_usd"] / durable, 4) if (attributable and durable > 0) else None
    cost_hint = "" if attributable else (
        "No AI spend is attributable to this repo in the window. Cost counts the "
        "Claude Code sessions that actually worked here — edited the repo's files "
        "or called jCodeMunch tools against it. Either none ran in this window, or "
        "the repo wasn't indexed when they did."
    )
    series = _record_delivery_history(repo.get("repo_id"), {
        "durable": durable,
        "total": int(metrics.get("commits_total") or 0),
        "rework_rate": metrics.get("rework_rate") or 0,
        "cost_usd": cost["cost_usd"] if attributable else None,
        "cost_per_durable": cpd,
    })
    # ROI by unit (Guercio, "whatever unit matters"): the SAME attributable repo
    # spend measured against different units of value. Durable commits come from
    # jcm (local git); merged PRs + closed issues come from `gh` (GitHub) — jcm is
    # never asked for them, keeping its offline/local charter intact.
    def _cost_per(count):
        return round(cost["cost_usd"] / count, 4) if (attributable and count and count > 0) else None
    units = [{"key": "durable", "label": "durable change", "count": durable,
              "cost_per": cpd, "source": "git (jcm delivery)"}]
    slug = _repo_github_slug(source_root)
    gh = _gh_unit_counts(slug, window_days) if slug else {
        "gh_available": bool(shutil.which("gh")),
        "reason": "this repo has no GitHub `origin` remote"}
    if slug and gh.get("gh_available") and not gh.get("reason"):
        if gh.get("merged_prs") is not None:
            units.append({"key": "pr", "label": "merged PR", "count": gh["merged_prs"],
                          "cost_per": _cost_per(gh["merged_prs"]), "source": "GitHub (%s)" % slug})
        if gh.get("closed_issues") is not None:
            units.append({"key": "issue", "label": "closed issue", "count": gh["closed_issues"],
                          "cost_per": _cost_per(gh["closed_issues"]), "source": "GitHub (%s)" % slug})
    return _tag({**base, "available": True, "metrics": metrics,
                 "cost_usd": cost["cost_usd"], "cost_attribution": cost,
                 "cost_attributable": attributable, "cost_hint": cost_hint,
                 "cost_per_durable": cpd, "series": series,
                 "units": units,
                 "units_meta": {"github_slug": slug,
                                "gh_available": gh.get("gh_available", False),
                                "reason": gh.get("reason", "")},
                 # Soft gate: the UI frosts every numeric value (labels stay) and
                 # swaps the chart for a CTA when no valid suite license is on file.
                 "licensed": _any_valid_license()}, "live")


def _spend_by_repo(window_days: int) -> dict:
    """One pass over all transcripts, attributing each assistant message's spend
    to the repo it TOUCHED (carry-forward within a session, cwd-seeded) and
    bucketing by repo_id. Returns {repo_id: {cost_usd, message_events,
    matched_sessions}}. The single-sweep sibling of `_delivery_cost` so the
    suite-wide ROI roll-up doesn't re-scan the transcripts once per repo."""
    roots, id_to_root = _repo_roots()
    root_to_id = {root: rid for root, rid in roots}
    proot = Path(os.path.expanduser(
        os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")))
    cut = time.time() - window_days * 86400
    by_id: dict = {}
    try:
        for jf in proot.glob("*/*.jsonl"):
            try:
                if jf.stat().st_mtime < cut:
                    continue
            except OSError:
                continue
            recs, cwd = _attr_parse_cached(jf)
            current = _resolve_root(cwd, roots, id_to_root) if cwd else None
            session_roots: set = set()
            for ev in recs:
                if ev[0] < cut:
                    continue
                mr = _dominant_root(ev[6], roots, id_to_root)
                if mr:
                    current = mr
                rid = root_to_id.get(current) if current else None
                if not rid:
                    continue
                d = by_id.setdefault(
                    rid, {"cost_usd": 0.0, "message_events": 0, "matched_sessions": 0})
                d["cost_usd"] += _usage_usd(ev[1], {
                    "input": ev[2], "output": ev[3],
                    "cache_read": ev[4], "cache_creation": ev[5],
                })
                d["message_events"] += 1
                session_roots.add(rid)
            for rid in session_roots:
                by_id[rid]["matched_sessions"] += 1
    except OSError:
        pass
    for d in by_id.values():
        d["cost_usd"] = round(d["cost_usd"], 4)
    return by_id


def _roi_summary(window_days: int) -> dict:
    """Suite-wide ROI: aggregate cost-per-durable-change across every indexed repo
    that had attributable AI spend in the window. The console's headline answer to
    'ROI-maxing, not token-maxing' — dollars spent per change that landed and
    stuck, not tokens burned. Read-only; the durable counts need jcm >= 1.108.69
    (the `delivery` subcommand), and we only shell it for repos that actually saw
    spend, so the cost is bounded by how many repos were worked in, not the whole
    index."""
    rd = repos()
    rows = [r for r in (rd.get("repos") or []) if r.get("has_source")]
    id_row = {r.get("repo_id"): r for r in rows}
    spend = _spend_by_repo(window_days)
    total_cost = 0.0
    total_durable = 0
    contributors = []
    for rid, sp in spend.items():
        row = id_row.get(rid)
        if not row or sp.get("matched_sessions", 0) <= 0:
            continue
        m = _run_cli_json(["delivery", row.get("source_root") or "",
                           "--window-days", str(window_days), "--json"])
        if m.get("error"):
            continue
        durable = int(m.get("commits_durable") or 0)
        if durable <= 0:
            continue
        total_cost += sp["cost_usd"]
        total_durable += durable
        contributors.append({
            "repo_id": rid, "display_name": row.get("display_name"),
            "cost_usd": round(sp["cost_usd"], 4), "durable": durable,
            "cost_per_durable": round(sp["cost_usd"] / durable, 4),
        })
    contributors.sort(key=lambda c: c["cost_usd"], reverse=True)
    cpd = round(total_cost / total_durable, 4) if total_durable > 0 else None
    return _tag({
        "available": True,
        "window_days": window_days,
        "window_choices": list(_DELIVERY_WIN_CHOICES),
        "cost_per_durable": cpd,
        "total_cost_usd": round(total_cost, 4),
        "total_durable": total_durable,
        "contributor_count": len(contributors),
        "contributors": contributors[:5],
        "attributable": cpd is not None,
        "hint": "" if cpd is not None else (
            "No durable changes with attributable AI spend in this window yet. "
            "ROI lights up once Claude Code sessions work in an indexed repo and "
            "changes land (see the Productivity screen for the per-repo view)."
        ),
    }, rd.get("_source", "live"))


def roi_panel(window_raw) -> dict:
    """Cached suite-wide ROI roll-up. The delivery CLI walks git per active repo,
    so the whole summary is cached 5 min per window; the sidebar rail and Savings
    header both read it without re-paying the walk on every poll."""
    if FORCE_FIXTURES:
        return _tag(_fixture("roi"), "fixture")
    window_days = _clamp_window(window_raw)
    return _cached("roi_%d" % window_days, 300.0, lambda: _roi_summary(window_days))


# --------------------------------------------------------------------------- #
# Help chat: a read-only "Ask" bot grounded in the user's own installed source.
# It shells out to their `claude` CLI in headless print mode, so the console
# manages no API keys and the cost lands on the user's own Claude quota. The bot
# gets the built-in Read/Glob/Grep tools scoped to the console repo (cwd) and
# nothing else - it reads the real code on the machine but never edits or runs.
# The user's own MCP servers are suppressed (empty --mcp-config + strict) to
# keep each turn cheap and deterministic. We deliberately do NOT load
# jCodeMunch-MCP live: a single MCP server injects its whole ~90-tool surface
# into context every turn, which cost MORE than reading files natively and
# confused tool selection. The Build mode that implements features and opens
# PRs is a deliberate future phase.
# --------------------------------------------------------------------------- #

# Kept small on purpose: a big always-on system prompt is paid every turn.
CHAT_PERSONA = (
    "You are the jMunch Console help assistant, embedded in the running console "
    f"v{VERSION}. Answer questions about installing, configuring, and using the "
    "console and the jMunch suite (jCodeMunch / jDocMunch / jDataMunch MCPs). "
    "The console's full source is in the current working directory - read the "
    "real files with Read/Glob/Grep to ground your answer and cite file:line; "
    "do not guess. Answer directly: do not narrate your steps or say you have "
    "gathered what you need - just give the answer. Be concise and concrete. "
    "You are STRICTLY READ-ONLY: never "
    "edit files, never run shell commands, never propose a command for the user "
    "to paste as 'the answer'. If the user asks for a feature the console does "
    "not have, say so plainly, then mention that a future Build mode will be "
    "able to implement it into their install and share it back as a pull "
    "request. Screens available: Index & Watcher, Savings, Token Usage, "
    "Productivity, Sessions, Launch, Processes, Logging, Alerts, Config, Help."
)


# Auth sources that take precedence over the claude.ai subscription login. We
# drop these from the chat subprocess (unless CHAT_USE_API) so the bot bills the
# user's plan rather than their API account.
_CHAT_API_AUTH_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
)


def _chat_env() -> dict:
    """Subprocess env for the help bot. By default, strip API-key/cloud auth so
    `claude` uses the subscription OAuth login (no metered API charge)."""
    if CHAT_USE_API:
        return dict(os.environ)
    return {k: v for k, v in os.environ.items() if k not in _CHAT_API_AUTH_VARS}


def _chat_auth_mode() -> str:
    """'subscription' unless an API key is present and we're set to honor it."""
    return "api" if (CHAT_USE_API and os.environ.get("ANTHROPIC_API_KEY")) else "subscription"


def _chat_capability() -> dict:
    """What the Help chat can do right now. 'off' when claude is missing or the
    feature is disabled, else 'live'. `indexed` reports whether jCodeMunch has
    this console indexed - informational only (Phase 1 reads files natively;
    live MCP retrieval is deferred because it bloats per-turn cost)."""
    if not CHAT_ENABLED:
        return _tag({"available": False, "mode": "off",
                     "hint": "Help chat is turned off (Config -> help chat)."}, "live")
    if not shutil.which("claude"):
        return _tag({"available": False, "mode": "off",
                     "hint": "Install Claude Code (the `claude` CLI) to enable in-console help."}, "live")
    try:
        indexed = any(
            r.get("source_root") and Path(r["source_root"]).resolve() == ROOT
            for r in repos().get("repos", [])
        )
    except Exception:
        indexed = False
    return _tag({"available": True, "mode": "live", "model": CHAT_MODEL,
                 "indexed": indexed, "auth": _chat_auth_mode()}, "live")


def chat(message: str, session_id) -> dict:
    """One read-only Help turn. Spawns `claude -p` with Read/Glob/Grep scoped to
    the console repo, grounded in the on-disk source. Returns the reply plus a
    session_id the client echoes back to continue the conversation."""
    if FORCE_FIXTURES:
        return _tag(_fixture("chat"), "fixture")
    cap = _cached("chat_cap", 30.0, _chat_capability)
    if not cap.get("available"):
        return {"error": cap.get("hint", "Help chat is unavailable."), "_status": 404}
    msg = (message or "").strip()
    if not msg:
        return {"error": "empty message", "_status": 400}
    exe = shutil.which("claude")
    if not exe:
        return {"error": "the `claude` CLI is no longer on PATH", "_status": 404}

    # Read-only file tools only; suppress the user's own MCP servers so each turn
    # stays small and predictable. cwd=ROOT scopes relative reads to the console.
    args = [exe, "-p", "--output-format", "json", "--model", CHAT_MODEL,
            "--append-system-prompt", CHAT_PERSONA,
            "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
            "--tools", "Read,Glob,Grep"]
    if session_id:
        args += ["--resume", str(session_id)]

    try:
        out = subprocess.run(args, input=msg, capture_output=True, text=True,
                             encoding="utf-8", cwd=str(ROOT), timeout=180,
                             env=_chat_env())
    except subprocess.TimeoutExpired:
        return {"error": "the assistant took too long to respond - try a shorter question",
                "_status": 504}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"chat invocation failed: {e}", "_status": 500}
    raw = (out.stdout or "").strip()
    try:
        j = json.loads(raw)
    except ValueError:
        return {"error": (out.stderr or raw or "no output from claude").strip()[:500],
                "_status": 502}
    if j.get("is_error"):
        return {"error": (j.get("result") or "the assistant returned an error").strip()[:500],
                "hint": "run `claude` once in a terminal to finish logging in",
                "_status": 502}
    return _tag({"reply": j.get("result", ""), "session_id": j.get("session_id"),
                 "cost_usd": j.get("total_cost_usd"), "mode": cap.get("mode")}, "live")


_API = {
    "/api/agents": lambda q: agents(),
    "/api/chat-capability": lambda q: _cached("chat_cap", 30.0, _chat_capability),
    "/api/products": lambda q: products(fresh=bool((q.get("fresh") or [""])[0])),
    "/api/org": lambda q: org(),
    "/api/repos": lambda q: repos(fresh=(q.get("fresh") or [""])[0] in ("1", "true", "yes")),
    "/api/config": lambda q: config(),
    "/api/sibling-config": lambda q: sibling_config(),
    "/api/savings": lambda q: savings(),
    "/api/savings/live": lambda q: savings_live(),
    "/api/usage": lambda q: usage_panel(),
    "/api/delivery": lambda q: delivery_panel((q.get("repo") or [""])[0], (q.get("window") or ["30"])[0]),
    "/api/roi": lambda q: roi_panel((q.get("window") or ["30"])[0]),
    "/api/other-apps": lambda q: other_apps(fresh=bool((q.get("fresh") or [""])[0])),
    "/api/starter-packs": lambda q: starter_packs(fresh=bool((q.get("fresh") or [""])[0])),
    "/api/sessions": lambda q: sessions(),
    "/api/processes": lambda q: processes(),
    "/api/diagnostics": lambda q: diagnostics(),
    "/api/alerts": lambda q: alerts_panel(),
    "/api/health": lambda q: health((q.get("repo") or [""])[0]),
    "/api/meta": lambda q: {
        "name": "jMunch Console",
        "version": VERSION,
        "phase": 3,
        "fixtures_forced": FORCE_FIXTURES,
        "launch_enabled": ALLOW_LAUNCH,
        "port": int(os.environ.get("JMUNCH_CONSOLE_PORT", "8765")),
        "bind": "127.0.0.1 (localhost only)",
        "token_set": bool(TOKEN),
        "mcp_bin": MCP_BIN,
        "org_id": os.environ.get("JCODEMUNCH_ORG_ID", ""),
        "chat_enabled": CHAT_ENABLED,
        "pinned": sorted(_ENV_PINNED),
        "_source": "live",
    },
}

# POST endpoints. Each handler takes the parsed JSON body and returns a dict;
# a `_status` key (popped before sending) overrides the 200 default. Auth +
# body-parse are handled once in do_POST.
_POST_API = {
    "/api/launch":      lambda b: launch(str(b.get("agent", "")), str(b.get("repo_id", ""))),
    "/api/resume":      lambda b: resume(str(b.get("session_id", ""))),
    "/api/chat":        lambda b: chat(str(b.get("message", "")), b.get("session_id")),
    "/api/license":     lambda b: set_license(str(b.get("product", "")), str(b.get("key", ""))),
    "/api/upgrade":     lambda b: upgrade(str(b.get("product", ""))),
    "/api/git-update":  lambda b: git_update(str(b.get("product", ""))),
    "/api/install":     lambda b: install(str(b.get("product", ""))),
    "/api/install-all": lambda b: install_all(),
    "/api/reinstall":   lambda b: reinstall(str(b.get("product", ""))),
    "/api/uninstall":   lambda b: uninstall(str(b.get("product", ""))),
    "/api/restart":     lambda b: restart_server(str(b.get("product", ""))),
    "/api/start-watcher": lambda b: start_watcher(),
    "/api/stop-watcher": lambda b: stop_watcher(),
    "/api/enable-server-logging": lambda b: enable_server_logging(),
    "/api/disable-server-logging": lambda b: disable_server_logging(),
    "/api/clear-logs": lambda b: clear_logs(),
    "/api/kill-process": lambda b: kill_process(b.get("pid", 0)),
    "/api/delete-index": lambda b: delete_index(str(b.get("repo_id", ""))),
    "/api/reindex":      lambda b: reindex(str(b.get("repo_id", ""))),
    "/api/index-repo":   lambda b: index_new(str(b.get("target", ""))),
    "/api/install-pack": lambda b: install_pack(str(b.get("pack", ""))),
    "/api/update-pack": lambda b: install_pack(str(b.get("pack", "")), force=True),
    "/api/uninstall-pack": lambda b: uninstall_pack(str(b.get("pack", ""))),
    "/api/adopt-pack": lambda b: adopt_pack(str(b.get("pack", ""))),
    "/api/config-set":   lambda b: config_set(str(b.get("key", "")), b.get("value")),
    "/api/config-unset": lambda b: config_unset(str(b.get("key", ""))),
    "/api/console-set":  lambda b: console_set(str(b.get("key", "")), b.get("value")),
    "/api/alert-set":    lambda b: alert_set(str(b.get("id", "")), b.get("enabled"), b.get("threshold")),
    "/api/console-restart": lambda b: console_restart(),
    "/api/console-stop":    lambda b: console_stop(),
    "/api/other-install":   lambda b: other_install(str(b.get("app", ""))),
    "/api/other-upgrade":   lambda b: other_upgrade(str(b.get("app", ""))),
    "/api/other-uninstall": lambda b: other_uninstall(str(b.get("app", ""))),
}


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = f"jMunchConsole/{VERSION}"  # derived from web/index.html brand

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), _CONTENT_TYPES[".json"])

    def _authorized(self, query: dict) -> bool:
        if not TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        if header == f"Bearer {TOKEN}":
            return True
        return (query.get("token") or [""])[0] == TOKEN

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (WEB / rel).resolve()
        # Path-traversal guard: must stay under WEB.
        if WEB not in target.parents and target != WEB:
            self._send(403, b"forbidden", "text/plain")
            return
        if not target.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def do_GET(self) -> None:  # noqa: N802
        # A browser abandoning a request mid-response (tab closed, poll
        # canceled) raises ConnectionError from the socket write; there is
        # nobody left to answer and nothing wrong server-side — swallow it
        # instead of stack-tracing the console.
        try:
            self._do_get()
        except ConnectionError:
            pass

    def _do_get(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path.startswith("/api/"):
            if not self._authorized(query):
                self._json({"error": "unauthorized"}, 401)
                return
            handler = _API.get(parsed.path)
            if handler is None:
                self._json({"error": f"no such endpoint: {parsed.path}"}, 404)
                return
            try:
                self._json(handler(query))
            except ConnectionError:
                raise  # client gone — let do_GET's swallow handle it
            except Exception as exc:  # never 500 the console
                self._json({"error": str(exc), "_source": "error"}, 200)
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._do_post()
        except ConnectionError:
            pass

    def _do_post(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        handler = _POST_API.get(parsed.path)
        if handler is None:
            self._json({"error": f"no such endpoint: {parsed.path}"}, 404)
            return
        if not self._authorized(query):
            self._json({"error": "unauthorized"}, 401)
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = json.loads(self.rfile.read(length) or "{}")
        except (ValueError, TypeError):
            self._json({"error": "invalid request body"}, 400)
            return
        result = handler(body)
        code = result.pop("_status", 200)
        self._json(result, code)

    def log_message(self, *args) -> None:  # quiet
        pass


# --------------------------------------------------------------------------- #
# Dev-mode auto-reload (opt-in, JMUNCH_CONSOLE_RELOAD=1)
# --------------------------------------------------------------------------- #

def _watched_py_files() -> list[Path]:
    """Top-level .py sources to watch. The console is a single server.py today;
    the glob keeps any future split modules covered. web/ is deliberately not
    watched — those files are already hot (served fresh from disk each request)."""
    return sorted(ROOT.glob("*.py"))


def _console_reexec() -> None:
    """Relaunch the console on the same argv and cwd. Shared by the dev auto-reload
    watcher and the on-demand restart from the Config screen.

    POSIX `os.execv` replaces the image in place, passing argv as an array — a
    space in the interpreter path is harmless. Windows has no native execv: the
    CRT emulation rebuilds the command line by space-joining argv *without*
    quoting, so an interpreter path with a space (`C:\\Program Files\\Python310\\
    python.exe`) gets re-split and the relaunch dies with "can't open file". So on
    Windows spawn a fresh child (subprocess quotes via list2cmdline) and exit
    instead; `_serve`'s bind-retry covers the momentary port overlap."""
    args = [sys.executable, *sys.argv]
    if os.name == "nt":
        subprocess.Popen(args)
        os._exit(0)
    os.execv(sys.executable, args)


def _reload_signature() -> dict[str, float]:
    sig: dict[str, float] = {}
    for f in _watched_py_files():
        try:
            sig[str(f)] = f.stat().st_mtime
        except OSError:
            pass
    return sig


def _reload_watcher() -> None:
    """Re-exec the process when a watched .py file changes. Single-process
    self-exec, like werkzeug's reloader: `os.execv` swaps in a fresh interpreter
    on the same argv (and cwd), so `server.py` edits take effect without a manual
    restart. A short settle wait avoids re-execing mid-save; HTTPServer's
    allow_reuse_address + the bind retry in `_serve` cover the momentary port
    overlap as the old image steps aside. Daemon thread — never blocks shutdown."""
    baseline = _reload_signature()
    while True:
        time.sleep(1.0)
        current = _reload_signature()
        if current == baseline:
            continue
        time.sleep(0.4)  # let an in-progress save settle before acting
        confirmed = _reload_signature()
        if confirmed != current:
            baseline = confirmed  # still being written; pick it up next tick
            continue
        changed = sorted(
            os.path.basename(k) for k in (set(confirmed) | set(baseline))
            if confirmed.get(k) != baseline.get(k)
        )
        print(f"jMunch Console  ->  reloading ({', '.join(changed) or 'source'} changed)", flush=True)
        try:
            _console_reexec()
        except OSError as e:  # a failed reload must not take the server down for good
            print(f"jMunch Console  ->  reload failed, staying up: {e}", flush=True)
            baseline = confirmed


def _serve(port: int) -> ThreadingHTTPServer:
    """Bind the server, retrying briefly so a dev-reload re-exec can reclaim the
    port while the prior process image is still stepping aside."""
    last: OSError | None = None
    for _ in range(20):  # ~4s at 0.2s spacing
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            last = e
            time.sleep(0.2)
    raise last  # type: ignore[misc]


def main() -> None:
    global _HTTPD
    port = int(os.environ.get("JMUNCH_CONSOLE_PORT", "8765"))
    httpd = _serve(port)
    _HTTPD = httpd
    mode = "fixtures forced" if FORCE_FIXTURES else f"live via {MCP_BIN!r} where available"
    auth = "token required" if TOKEN else "no token (localhost only)"
    reload_note = "  [dev auto-reload ON]" if RELOAD else ""
    print(f"jMunch Console  ->  http://127.0.0.1:{port}   [{mode}; {auth}]{reload_note}")
    # Warm the slow adapters in the background so the first page load doesn't
    # wait on `claude mcp list` (agents) or the per-product `--version` probes
    # (products rail). Daemon threads: never block startup or shutdown.
    if not FORCE_FIXTURES:
        threading.Thread(target=agents, daemon=True).start()
        threading.Thread(target=products, daemon=True).start()
    if RELOAD:
        threading.Thread(target=_reload_watcher, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
