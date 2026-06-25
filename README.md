# jMunch Console

[![Release](https://github.com/jgravelle/jmunch-console/actions/workflows/release.yml/badge.svg)](https://github.com/jgravelle/jmunch-console/actions/workflows/release.yml)
[![Version](https://img.shields.io/github/v/release/jgravelle/jmunch-console?label=version&color=blue)](https://github.com/jgravelle/jmunch-console/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A local, opt-in **control plane** for the jMunch suite — config, index/watcher
health, savings/ROI, session recall, and agent launching, in the browser.

> Standalone and opt-in. It is **not** bundled into or required by
> `jcodemunch-mcp` / `jdocmunch-mcp` / `jdatamunch-mcp`. Existing CLI/MCP users
> are unaffected. See [`PRD.md`](PRD.md) for scope, phasing, and the design spec.

## Run

```bash
python server.py
# -> http://127.0.0.1:8765
```

Zero dependencies (Python stdlib only). Offline-safe. Binds to `127.0.0.1`.

### Environment

| Var | Default | Purpose |
|-----|---------|---------|
| `JMUNCH_CONSOLE_PORT` | `8765` | Listen port |
| `JMUNCH_CONSOLE_TOKEN` | — | If set, `/api/*` requires `?token=` or `Authorization: Bearer` |
| `JMUNCH_MCP_BIN` | `jcodemunch-mcp` | jMunch CLI name/path the adapter shells |
| `CLAUDE_ADMIN_KEY` | — | Anthropic Admin API key (`sk-ant-admin...`) for the org Usage/Cost panel. Also read from a gitignored `.env` at the repo root. Org accounts only; the local half of the Claude Usage panel works without it. |
| `JMUNCH_CONSOLE_FIXTURES` | — | Set `1` to force fixtures for every endpoint |
| `JMUNCH_CONSOLE_READ_ONLY` | — | Set `1` to disable every system-changing action (config edits, launch/resume, install/update/uninstall, reindex/delete). Actions are **on by default**. |

Port, auth token, CLI path, fixtures mode, and the actions switch are also
editable in the Config screen; those edits persist to a gitignored
`data/console_settings.json` read at startup. An explicitly-set environment
variable wins over the file and locks its control in the UI.

## Actions and safety

The console performs system-changing actions out of the box: editing config,
launching/resuming agents, installing, updating, and uninstalling products and
curated third-party apps, reindexing and deleting indexes, restarting MCP
server processes, and stopping or restarting the console itself. Every action
request is allowlisted server-side (agent must be
a live detected client, repo a live indexed repo, command from a fixed map;
filesystem paths are resolved server-side, never taken from the client), and the
server binds to `127.0.0.1` only. Set `JMUNCH_CONSOLE_READ_ONLY=1` for a
look-don't-touch deployment.

## Restarting MCP servers

The product menu in the **jMunch, LLC Apps** rail has a **Restart MCP server**
action (`POST /api/restart`). Important: the console never owned these
processes — the MCP **client** (Claude Code/Desktop/…) spawns each server as a
stdio subprocess — so "restart" is really *terminate, and let the client respawn
it*. A killed server stays down until the host acts; some clients only respawn on
their next reconnect or tool call.

- **Matching.** `_running_server_pids(binname)` matches the product name
  anywhere in a process command line, however the server was launched: the
  product binary directly (`jcodemunch-mcp[.exe]`, no subcommand) **or** the
  `jmunch-mcp` proxy whose `--config` names the product
  (`…\configs\jcodemunch-mcp.toml`) — the shape the suite actually runs in here.
  An earlier `serve`-subcommand match found nothing because neither shape carries
  `serve`. The discriminator (`_is_server_cmd`) is the inverse and shape-based: a
  *server* line is just the binary (optionally under `python`, or the proxy's
  `--config <toml>`), so any **bareword** token that isn't a flag (`-…`), a
  path/file (`/ \ .`), a number, the binary itself, or a known program/`serve`
  token is a subcommand — `index-file` / `list-repos` / `config` / … — and marks
  a transient call, not a server. (Enumerating the subcommands instead missed
  `index-file` and surfaced a live indexing job as a fake server.)
- **OS surface.** Process enumeration + kill lives in one helper pair
  (`_enumerate_processes` / `_kill_pids`: CIM on Windows, `ps` + `SIGTERM` on
  POSIX) — the only platform-specific code besides the terminal-spawn adapter.
- **Gating.** Rides the same `ALLOW_LAUNCH` two-key turn as the other
  process-touching actions; disabled with a reason under read-only mode.
- **Scope.** A product's whole server transport: the direct binary and the
  `jmunch-mcp` proxy instance that fronts it (its config is per-product, so
  killing it only restarts that one). Other products' proxies don't match.
- **Per-client hint.** On success the toast is tailored by `_respawn_hint()` to
  the wired clients `agents()` detects (Claude Code's `/mcp` reconnect, a GUI
  app restart, etc.); see `_RESTART_HINTS`. Add a client there to extend it.

The **Processes** screen is the live, per-process view of the same machinery.
`GET /api/processes` enumerates the table once (capturing `ParentProcessId`) and
groups the matched server processes by product, ordered as a **parent-before-
child tree**: `_order_process_tree` nests each spawned process under the
proxy/launcher that owns it and stamps `depth` + `descendants`. The UI indents by
depth with a `↳` connector and an "encloses N" badge, and hovering/focusing a
`stop` button highlights the row plus its whole subtree — so it's obvious that
stopping a parent (the proxy → `python` proxy → server → `python` server chain)
takes everything beneath it. It auto-refreshes every 6s, repainting only when the
pids-by-product set changes. `POST /api/kill-process` stops one by pid — gated
behind `ALLOW_LAUNCH` and **validated against the live recognized-server set**,
so the client names a pid but the console only kills it when it currently matches
a known server (never an arbitrary process). `taskkill /T` (or the process group)
takes the subtree with it; the client respawns the server on reconnect.

## Stopping and restarting the console itself

The **Config** screen has a **Console process** group with two buttons (both
gated behind `ALLOW_LAUNCH`, disabled with a reason under read-only mode):

- **Restart** (`POST /api/console-restart`) relaunches the console — the same
  `_console_reexec` the dev auto-reload uses. It rebinds the **same port**, so
  settings that only apply on restart (port, auth token, jCodeMunch CLI) take
  effect and the open page reconnects on its own: `waitForConsole()` polls
  `/api/meta` until the fresh image answers, then reloads. The relaunch is
  deferred ~0.35s on a daemon thread so this response flushes first, and
  `_serve`'s bind-retry covers the momentary port overlap as the old image steps
  aside. On POSIX `os.execv` replaces the image in place. On Windows there is no
  native execv — the CRT emulation rebuilds the command line by space-joining
  argv *without* quoting, so an interpreter path with a space (`C:\Program
  Files\Python310\python.exe`) gets re-split and the relaunch dies with "can't
  open file" — so `_console_reexec` spawns a fresh child via `subprocess` (which
  quotes via `list2cmdline`) and `os._exit(0)`s the old one (new pid).
- **Stop** (`POST /api/console-stop`) shuts the console down: a deferred thread
  calls `_HTTPD.shutdown()` so `serve_forever` unblocks and `main()` returns
  cleanly, with an `os._exit(0)` fallback if the graceful path wedges. There is no
  UI to start it again afterward — the process is gone — so the page swaps to a
  "Console stopped" notice; relaunch from your terminal or launcher to return.

## Diagnostics

The **Diagnostics** screen (`GET /api/diagnostics`) is a read-only view over the
crash/perf evidence the suite already leaves on disk, so a hang or a crash is
visible at a glance instead of buried in a temp folder. Everything is a cheap
filesystem/process probe (no CLI shell-out), so it polls every few seconds and a
log stays live. All reads are local to the machine; nothing leaves it.

- **Logs.** Discovers and tails the watcher's auto per-PID logs
  (`<temp>/jcw_<pid>.log`, written by `watch` / `watch-all`) plus an explicit
  `JCODEMUNCH_LOG_FILE` if set. Each card shows size, age, error/warn counts over
  the tail, and the last ~200 lines colour-coded by level (only the trailing
  256 KB of a large log is read).
- **Servers + liveness.** Reuses the process enumeration from the Processes panel
  to list running munch servers, and derives jcm's state from its
  `_session_live.json` heartbeat mtime (`active` < 30 s, else `idle`); the
  siblings report `running` since they have no equivalent signal yet.
- **Signals + hints.** Badges for file logging on/off, watcher-log count, perf
  telemetry on/off (`telemetry.db` presence), and jcm's last-activity age. Hints
  point at the next step (turn on `JCODEMUNCH_LOG_FILE`, enable
  `JCODEMUNCH_PERF_TELEMETRY=1`, or where to look when a server is alive but has
  gone quiet). This is the same recipe a user follows to capture a wedge, surfaced
  live.
- **Capture actions** (`ALLOW_LAUNCH`-gated). Two buttons do the recipe from the
  panel instead of by hand:
  - **Start watcher + logging** (`POST /api/start-watcher`) spawns
    `jcodemunch-mcp watch-all --log-file <temp>/jmunch-console-watch.log
    --log-level INFO` in a terminal (the reindexer that wedges on a huge tree),
    so its activity lands in a log the panel tails. Refuses to double-spawn when a
    watcher is already running.
  - **Enable server logging** (`POST /api/enable-server-logging`) writes the
    `log_file` + `log_level` config keys and restarts the jcm server so the client
    respawns it logging to a file. The server is client-launched, so the console
    can't inject an env var into it — but **jcodemunch-mcp 1.108.64+** honors the
    `log_file` config key, which the console can set. Older servers ignore it (set
    `JCODEMUNCH_LOG_FILE` in the MCP config instead).

  Perf-detail (`analyze_perf`) and per-repo reindex timing are the next increments.

## License soft-gate

Some panels nudge unlicensed seats toward entering a key by greying out or
blurring secondary actions. This is a **soft gate** — a UX nudge, not a security
boundary (the data is still in the DOM; a determined user can bypass the CSS).
It exists to surface the value of a license, not to enforce one.

**Server.** `_any_valid_license()` (server.py) returns whether the console holds
any valid suite license. It reuses the same `store → env → config` key
resolution as the sidebar dot, validates against `validate.php`, and grants
**offline grace** (a key on file that can't be reached for validation still
unlocks, so a licensed-but-offline seat is never punished). Result is cached 60s.
Endpoints that drive a gated panel add the flag to their payload:

```python
return _tag({"...": ..., "licensed": _any_valid_license()}, "live")
```

Currently on `/api/savings`, `/api/repos`, and `/api/sessions`.

**Client.** The panel reads `d.licensed` and gates the UI. Treat a **missing**
flag as licensed (`d.licensed !== false`) so an older server never wrongly locks.
`renderIndex` / `renderSessions` also publish it to the module-global `LICENSED`.

**Live clear (no restart).** A license change must flip the gate immediately, so
both sides invalidate their cache on save: `set_license` pops `any_valid_license`
from the server cache (alongside `products` / `pack_license` / `starter_packs`),
and `enterLicense` re-renders the current screen when it's a gated one
(`savings` / `index` / `sessions`). The same path also *re-applies* the gate when
the last valid key is cleared. Forgetting the server-side pop is the classic
regression — the gate then "sticks" for up to the 60s TTL (or until restart).

**CSS helpers** (components.css, "license soft-gate" block):

| Class | Effect |
|-------|--------|
| `.lic-cta-block` | Dashed placeholder that displaces a chart with an "enter a license" prompt (Savings 30-day) |
| `.lic-blur` | Blurs a table row's cells + blocks selection (Savings *By tool*, rows past the first) |
| `.lic-blur-btn` | Blurs the button inside a `.lic-lock` (Sessions *resume*, items past the first three) |
| `.lic-lock` | Wrapper span that carries the license `title` tooltip; the disabled button inside is `pointer-events:none` so the span owns the hover — **required for the tooltip to show in Firefox** (a disabled `<button>` suppresses its own title) |

**Adding a gate to a new panel:** add `"licensed": _any_valid_license()` to the
endpoint payload, read it client-side as `d.licensed !== false`, and reach for
the classes above — wrap any disabled, tooltipped control in `.lic-lock` so the
prompt works cross-browser.

## Layout

```
server.py        stdlib HTTP server + data adapters (live CLI -> fixture fallback)
web/             tokens.css · components.css · index.html · app.js
fixtures/        DATA MAP sample payloads (repos, config, savings, sessions, agents, health)
PRD.md           product requirements + design spec (§12)
```

## License

The Console's code is **MIT-licensed** (see [LICENSE](LICENSE)): use, fork, modify,
and ship it freely, just keep the copyright notice. This covers the Console source in
this repository only. The jCodeMunch suite the Console controls is licensed separately,
and the "License soft-gate" section above refers to suite *license keys*, not this
repository's code license.
