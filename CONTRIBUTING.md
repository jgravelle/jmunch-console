# Contributing to jMunch Console

Maintainer and contributor notes: architecture, conventions, how to extend each
part, and the deeper implementation detail behind the trickier features. For how
to *use* the console, see the [User Guide](USER_GUIDE.md).

- [Philosophy](#philosophy)
- [Project layout](#project-layout)
- [Running for development](#running-for-development)
- [Architecture](#architecture)
- [Conventions](#conventions)
- [Extending the console](#extending-the-console)
- [Implementation deep-dives](#implementation-deep-dives)
- [Testing and verification](#testing-and-verification)
- [Submitting changes](#submitting-changes)

---

## Philosophy

Three principles shape what belongs here:

- **Front-end-first.** Every feature traces to something the suite already
  exposes (config schema, `list_repos`, watch status, savings receipts, Claude
  Code transcripts, install status, process locks). New backend capability lands
  in the **suite**, not the console; the console shells the CLI and renders. If a
  feature needs new data the suite doesn't emit, the suite ships it first.
- **Identity guard.** This is a control plane, not an editor and not a chat/agent
  shell. The browser is the cross-platform layer (no Electron). The only
  OS-specific code is the terminal-spawn adapter and the process
  enumerate/kill helpers.
- **Safety is structural, not a prompt.** The console performs system-changing
  actions, but every one is allowlisted server-side, paths are resolved on the
  server (never taken from the client), the server binds to `127.0.0.1`, and
  `JMUNCH_CONSOLE_READ_ONLY=1` disables all of it.

## Project layout

```
server.py        stdlib HTTP server + data adapters (live CLI -> fixture fallback)
web/
  index.html     shell: brand, nav mount, view mount, asset links
  app.js         all screens, routing, polling, actions (vanilla JS, no build)
  tokens.css     design tokens (dark default + light, accent palettes)
  components.css base component styles
  polish.css     additive visual polish layer (loaded after components.css)
fixtures/        sample payloads per endpoint (repos, config, savings, usage, ...)
data/            runtime state, gitignored (settings, licenses, history)
.github/workflows/release.yml   auto-tag + release on every version bump to main
```

`PRD.md` is an internal design doc kept local and gitignored; it is not part of
the public repository.

## Running for development

```bash
python server.py                      # http://127.0.0.1:8765
JMUNCH_CONSOLE_RELOAD=1 python server.py   # auto-restart on server.py changes
JMUNCH_CONSOLE_FIXTURES=1 python server.py # force sample data (no suite needed)
```

No dependencies beyond the Python standard library (3.10+). `web/` files are
served fresh from disk on every request, so a frontend edit only needs a browser
refresh; `server.py` edits need a restart (or the reload flag above).

## Architecture

### Server (`server.py`)

- **Dispatch.** Two route tables: `_API` (GET) maps `/api/<name>` to a handler
  taking the parsed query, and `_POST_API` maps POST paths to a handler taking the
  JSON body. A handler returns a dict; a `_status` key sets the HTTP code (popped
  before serialization). GET endpoints under `/api/` enforce the optional token;
  exceptions are caught and returned as `{"error": ...}` so the console never
  500s. A client disconnecting mid-response is swallowed (`do_GET`/`do_POST` wrap
  `_do_get`/`_do_post` and ignore `ConnectionError`).
- **Data adapter.** Each panel's function prefers live data by shelling the
  jMunch CLI (`_run_cli` / `_run_cli_json`) and normalizing its JSON; on any
  failure it falls back to `_fixture(name)`. Every response is wrapped by
  `_tag(payload, "live"|"fixture")` so the UI can badge its source.
- **Caching.** `_cached(key, ttl, fn)` memoizes expensive probes (CLI shell-outs,
  process scans) under a per-key lock with single-flight, so concurrent polls
  don't stampede.
- **Settings.** Console-local settings live in `data/console_settings.json`
  (`_read_console_settings` / `_write_console_settings`), loaded at startup with
  environment-variable-wins precedence. The version is read from
  `web/index.html`'s `<small>vX.Y.Z</small>` brand by `_read_version()` (one
  source of truth, see [Conventions](#conventions)).

### Frontend (`web/app.js`)

- **Screens.** The `SCREENS` array drives the nav and routing; each entry is
  `{ id, label, render }`. `buildNav()` renders the menu, `go(id)` switches
  screens (sets the hash, stops other polls, calls the render function).
- **Fetching.** `api(path)` is the GET helper (returns parsed JSON, or an error
  object); POST actions `fetch` directly with a JSON body. `toast(msg, kind)`
  surfaces results.
- **Polling.** Live screens register a `setInterval` that stops when you navigate
  away and repaints only when a signature of the data actually changes (e.g.
  `diagSig`, `procSig`), so a steady tail never yanks scroll or selection.

## Conventions

- **Versioning (single source of truth).** The version is the
  `<small>vX.Y.Z</small>` brand in `web/index.html`. `_read_version()` reads it
  for the HTTP `Server` header and `/api/meta`. **Bump it on every release.**
- **Releases are automatic.** `.github/workflows/release.yml` runs on every push
  to `main`: it reads that version and, if no release exists for the tag, creates
  the tag + GitHub release. A non-bump push (docs, infra) is a no-op. So releases
  can never lag the code; just bump the brand.
- **Cache-bust web assets.** Browsers serve `app.js` / `polish.css` from the
  same-session memory cache even with `Cache-Control: no-store`. On every web
  edit, bump the `?v=N` query on the relevant `<link>` / `<script>` in
  `index.html`, or your change won't reach an open tab.
- **No new dependencies.** Server stays standard-library-only; frontend stays
  vanilla JS with no build step. Offline-safe (system fonts, no CDNs).
- **No em-dashes** in shipped copy or docs.

## Extending the console

### Add a screen

1. Add `{ id, label, render: renderX }` to `SCREENS` in `app.js`.
2. Write `renderX()` (fetch via `api("/api/x")`, build `view.innerHTML`, wire
   handlers). Register a poll only if it shows live-changing data.
3. Add the `GET /api/x` handler to `_API` in `server.py`, returning
   `_tag({...}, "live")` with a `_fixture("x")` fallback. Add `fixtures/x.json`.

### Add a Compatible App (third-party tool)

Add one entry to the `_OTHER_OPS` registry in `server.py` with its detect /
version / preflight / install / upgrade / uninstall / confirm-copy. Install is
ungated (confirm + visible terminal); upgrade and uninstall ride `ALLOW_LAUNCH`.
The client renders it from `/api/other-apps`; the status dot is the action
control (no separate buttons). Never drive a third-party installer silently.

### Expose a console or suite setting

- A console-only knob: add it to `console_set` (write to
  `data/console_settings.json`) and surface it in the Config screen.
- A *suite* environment variable: add it to `_SETTINGS_ENV` mapped to the suite's
  env var plus a `console_set` branch that writes `os.environ` live. The startup
  injector and the env-pin lock logic then work for free.

### Add an alert

Add an entry to `_ALERT_CATALOG` (`id`, `label`, `metric`, `unit`, `compare`,
`default`, `enabled`, `tier`, `desc`) and compute its value in `_alert_metrics`.
The catalog is a server-side allowlist: the browser only toggles a card and edits
its number, never names a metric. Keep alerts notify-only (no actions), so
`/api/alert-set` stays un-gated like `console_set`.

### Add a license soft-gate to a panel

Add `"licensed": _any_valid_license()` to the endpoint payload, read it
client-side as `d.licensed !== false`, and use the CSS helpers below. Wrap any
disabled, tooltipped control in `.lic-lock` so the prompt works cross-browser.
See [the soft-gate deep-dive](#license-soft-gate).

## Implementation deep-dives

The features below carry enough subtlety to be worth documenting in full.

### Restarting MCP servers

The product menu's **Restart MCP server** action (`POST /api/restart`) and the
**Processes** screen are two views of the same machinery. The console never owned
these processes: the MCP **client** (Claude Code/Desktop/etc.) spawns each server
as a stdio subprocess, so "restart" means *terminate, and let the client respawn*.

- **Matching.** `_running_server_pids(binname)` matches the product name anywhere
  in a process command line, however the server was launched: the product binary
  directly (`jcodemunch-mcp[.exe]`, no subcommand) **or** the `jmunch-mcp` proxy
  whose `--config` names the product (`...\configs\jcodemunch-mcp.toml`), the
  shape the suite actually runs in. The discriminator (`_is_server_cmd`) is
  shape-based and inverse: a *server* line is just the binary (optionally under
  `python`, or the proxy's `--config <toml>`), so any **bareword** token that
  isn't a flag, a path/file, a number, the binary itself, or a known
  program/`serve` token is a subcommand (`index-file` / `list-repos` / `config` /
  ...) and marks a transient call, not a server. Enumerating subcommands instead
  missed `index-file` and surfaced a live indexing job as a fake server.
- **OS surface.** Process enumeration and kill live in one helper pair
  (`_enumerate_processes` / `_kill_pids`: CIM on Windows, `ps` + `SIGTERM` on
  POSIX), the only platform-specific code besides the terminal-spawn adapter.
- **Gating.** Rides the same `ALLOW_LAUNCH` gate as the other process-touching
  actions; disabled with a reason under read-only mode.
- **Scope.** A product's whole server transport: the direct binary and the
  `jmunch-mcp` proxy instance that fronts it (its config is per-product, so
  killing it only restarts that one). Other products' proxies don't match.
- **Per-client hint.** On success the toast is tailored by `_respawn_hint()` to
  the wired clients `agents()` detects (Claude Code's `/mcp` reconnect, a GUI app
  restart, etc.); see `_RESTART_HINTS`. Add a client there to extend it.

The **Processes** screen (`GET /api/processes`) enumerates the table once
(capturing `ParentProcessId`) and groups matched servers by product, ordered as a
**parent-before-child tree**: `_order_process_tree` nests each spawned process
under the proxy/launcher that owns it and stamps `depth` + `descendants`. The UI
indents by depth with a connector and an "encloses N" badge, and
hovering/focusing a `stop` button highlights the row plus its subtree, so it's
obvious that stopping a parent takes everything beneath it. It auto-refreshes
every 6s, repainting only when the pids-by-product set changes.
`POST /api/kill-process` stops one by pid, gated behind `ALLOW_LAUNCH` and
**validated against the live recognized-server set**, so the client names a pid
but the console only kills it when it currently matches a known server (never an
arbitrary process).

### Stopping and restarting the console itself

The **Config** screen's **Console process** group has two gated buttons:

- **Restart** (`POST /api/console-restart`) relaunches via `_console_reexec` (the
  same path the dev auto-reload uses). It rebinds the **same port**, so
  restart-only settings (port, token, CLI path) take effect and the open page
  reconnects on its own: `waitForConsole()` polls `/api/meta` until the fresh
  image answers, then reloads. The relaunch is deferred ~0.35s on a daemon thread
  so the response flushes first, and `_serve`'s bind-retry covers the momentary
  port overlap. On POSIX `os.execv` replaces the image in place. On Windows there
  is no native execv, and the CRT emulation rebuilds the command line by
  space-joining argv *without* quoting, so an interpreter path with a space
  (`C:\Program Files\Python310\python.exe`) gets re-split and the relaunch dies
  with "can't open file". So on Windows `_console_reexec` spawns a fresh child via
  `subprocess` (which quotes via `list2cmdline`) and `os._exit(0)`s the old one.
- **Stop** (`POST /api/console-stop`) calls `_HTTPD.shutdown()` on a deferred
  thread so `serve_forever` unblocks and `main()` returns cleanly, with an
  `os._exit(0)` fallback if the graceful path wedges. There is no UI to start it
  again, so the page swaps to a "Console stopped" notice.

### Logging / Diagnostics internals

The **Logging** screen (`GET /api/diagnostics`, route id `logging`, with a
back-compat alias from the old `#diagnostics` hash) is a read-only view over the
crash/perf evidence the suite leaves on disk. Everything is a cheap
filesystem/process probe (no CLI shell-out), so it polls every few seconds.

- **Logs.** Discovers and tails the watcher's per-PID logs
  (`<temp>/jcw_<pid>.log`) plus an explicit `JCODEMUNCH_LOG_FILE`. Each card shows
  size, age, error/warn counts, and the last ~200 lines colour-coded by level
  (only the trailing 256 KB of a large log is read).
- **Signals.** Badges for file logging, watcher-log count, **perf telemetry**,
  and jcm's last-activity age (from the `_session_live.json` heartbeat mtime:
  `active` < 30s else `idle`).
- **Perf telemetry is tri-state.** The badge reads `telemetry.db` presence
  **and** the effective `perf_telemetry_enabled` config flag (`_perf_telemetry_cfg`,
  read from `config --json`, cached): **on** (db present), **pending restart**
  (flag set but the running server loaded config at startup and hasn't written the
  db yet), or **off**. `perf_telemetry_enabled` controls only durable persistence;
  the db is created on the first tool call after a server restart. `diagSig`
  includes both perf fields so the badge repaints when they change.
- **Capture actions** (`ALLOW_LAUNCH`-gated). "Start watcher + logging"
  (`POST /api/start-watcher`) spawns `watch-all` with file logging; "Enable server
  logging" (`POST /api/enable-server-logging`) writes the `log_file` config and
  restarts the jcm server (needs `jcodemunch-mcp` 1.108.64+).

### License soft-gate

Some panels nudge unlicensed seats toward entering a key by greying out or
blurring secondary actions. It is a **soft gate**, a UX nudge, not a security
boundary (the data is still in the DOM; a determined user can bypass the CSS). It
exists to surface the value of a license, not to enforce one.

**Server.** `_any_valid_license()` returns whether the console holds any valid
suite license. It reuses the same store -> env -> config key resolution as the
sidebar dot, validates against the licensing backend, and grants **offline grace**
(a key on file that can't be reached still unlocks). Result cached 60s. Endpoints
that drive a gated panel add the flag to their payload:

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
(`savings` / `index` / `sessions`). The same path re-applies the gate when the
last valid key is cleared. Forgetting the server-side pop is the classic
regression: the gate then "sticks" for up to the 60s TTL.

**CSS helpers** (`components.css`, "license soft-gate" block):

| Class | Effect |
|-------|--------|
| `.lic-cta-block` | Dashed placeholder that displaces a chart with an "enter a license" prompt (Savings 30-day) |
| `.lic-blur` | Blurs a table row's cells and blocks selection (Savings *By tool*, rows past the first) |
| `.lic-blur-btn` | Blurs the button inside a `.lic-lock` (Sessions *resume*, items past the first three) |
| `.lic-lock` | Wrapper span that carries the license `title` tooltip; the disabled button inside is `pointer-events:none` so the span owns the hover, required for the tooltip to show in Firefox (a disabled `<button>` suppresses its own title) |

## Testing and verification

Before pushing:

- `python -m py_compile server.py` (syntax) and `node --check web/app.js`.
- For a behavior change, run it: `JMUNCH_CONSOLE_FIXTURES=1 python server.py` on a
  throwaway port and exercise the endpoint, or drive the UI in a browser and
  confirm zero console errors. Fixtures mode lets you verify without the suite.
- Server-side actions should be checked for their gated (403 under read-only) and
  validation (400/404) paths, not only the happy path.

## Submitting changes

- Keep diffs focused; match the surrounding style (comment density, naming).
- Bump the `<small>vX.Y.Z</small>` version in `index.html` for a user-facing
  change, and the `?v=N` cache-bust for any web-asset edit.
- Use a clear commit subject; the release workflow turns the version bump into a
  GitHub release automatically.
- Open a PR against `main`.
