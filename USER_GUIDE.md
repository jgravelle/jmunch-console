# jMunch Console User Guide

This guide walks through every screen and feature of the Console, plus the
operational topics (environment, safety, restarting servers, capturing logs,
licensing). For a quick overview and install, see the [README](README.md).

- [Getting started](#getting-started)
- [The interface](#the-interface)
- [Screens](#screens)
  - [Index & Watcher](#index--watcher)
  - [Savings](#savings)
  - [Token Usage](#token-usage)
  - [Productivity](#productivity)
  - [Sessions](#sessions)
  - [Launch](#launch)
  - [Processes](#processes)
  - [Logging](#logging)
  - [Alerts](#alerts)
  - [Help](#help)
  - [Config](#config)
- [Operations](#operations)
  - [Environment variables](#environment-variables)
  - [Actions and safety](#actions-and-safety)
  - [Restarting MCP servers](#restarting-mcp-servers)
  - [Stopping and restarting the console](#stopping-and-restarting-the-console)
  - [Capturing logs and perf telemetry](#capturing-logs-and-perf-telemetry)
  - [Licensing and the soft-gate](#licensing-and-the-soft-gate)
  - [Sample-data (fixtures) mode](#sample-data-fixtures-mode)
- [Where your data lives](#where-your-data-lives)

---

## Getting started

```bash
python server.py
# -> http://127.0.0.1:8765
```

Open the URL in any browser. The console binds to `127.0.0.1` only, needs no
dependencies beyond the Python standard library, and works offline.

- **No suite installed?** Every panel renders on built-in sample data so you can
  tour the whole thing. Panels showing sample data are tagged `sample data`.
- **Suite installed?** The console shells the `jcodemunch-mcp` CLI for live data
  and panels tag themselves `live`. If a panel can't reach the CLI, it quietly
  falls back to sample data rather than erroring.

Set the theme (dark/light) and accent palette (aurora/ember/spectrum) from the
top-right; both persist in your browser.

## The interface

- **Left rail, top:** the navigation menu (the screens below).
- **Left rail, "jMunch, LLC Apps":** per-product status for `jcodemunch-mcp`,
  `jdocmunch-mcp`, and `jdatamunch-mcp`. Each row has two dots: an **install** dot
  (installed / update available / broken / not installed) and a **license** dot
  (valid / invalid / entered-unverified / none). Click a dot or row to install,
  update, restart, or enter a license key.
- **Left rail, "Compatible Apps":** a curated set of complementary third-party
  tools you can install from here.
- **Top bar:** a one-word mode indicator (`live`, `read-only`, or `fixtures
  mode`), the **gear** (Config), and the theme/accent controls.
- **Brand dot (top-left):** click it to stop or restart the console itself.

Each data panel carries a `live` / `sample data` badge so you always know whether
you're looking at your real environment.

---

## Screens

### Index & Watcher

Your indexed repositories at a glance: name, languages, symbol and file counts,
index freshness, and watcher state.

**How to:**
- **Find a repo:** type in the search box; filter by freshness (fresh / stale) or
  watcher state (watching / idle); sort by name, symbols, files, or recency.
- **Reindex or delete an index:** use the per-repo controls (these change state,
  so they're disabled in read-only mode).
- **Install a Starter Pack:** scroll to the Starter Packs section beneath the
  grid. Free packs install with no key; the paid library unlocks with any valid
  suite license. You can also **adopt** a pack to claim repos you already indexed
  independently.
---

### Savings

What the suite has saved you by returning compact, ranked results instead of raw
file bytes.

**How to read it:**
- **Headline tiles:** tokens and dollars saved over the last 30 days, plus
  all-time totals (the all-time tiles tick up live as the suite serves calls).
- **By tool:** a per-tool breakdown of where the savings came from.
- **Trend:** a rolling 30-day chart the console accumulates day by day.

The per-tool table past the first row is part of the [license soft-gate](#licensing-and-the-soft-gate);
enter a valid key to reveal it in full.

---

### Token Usage

Your live Claude token consumption, priced at API list rates. Two complementary
sources:

- **Local (always available):** parsed from your Claude Code transcripts under
  `~/.claude/projects`. Last-hour and last-24h tiles, a per-minute chart, and a
  per-model table. "New tokens" means input + output + cache writes; cache
  **reads** are shown separately, since a cache hit re-counts the whole cached
  prompt every turn.
- **Org-wide (optional):** the Anthropic Usage and Cost Admin API, which needs a
  `CLAUDE_ADMIN_KEY` (organization accounts only). Without the key, the local
  half still works in full.

> Note on cost: the dollar figures are an **estimate at API list prices**. If
> you're on a subscription, the number is notional, not out-of-pocket.
---

### Productivity

Cost per *durable* change: the AI spend attributable to a repo divided by the
work that actually stuck (durable vs reverted/reworked commits), so volume-gaming
is self-defeating rather than rewarded.

**How to:**
- Pick a **repo** and a **window** (14 / 30 / 90 days).
- The headline tile is cost-per-durable-change when spend is attributable, else
  the durable-change count, with rework-rate and a by-kind breakdown.

> Attribution honesty: if you run sessions from a parent ("umbrella") folder that
> contains many repos, per-repo cost can't always be split and the tile says
> **"not attributable"** rather than a misleading `$0`. Launching a session at
> the repo root (the Launch screen does this) makes the cost overlay light up.
---

### Sessions

Your past Claude Code sessions under `~/.claude/projects`, newest first, with the
project, branch, summary, and message count.

**How to:**
- **Resume** a session to relaunch `claude --resume` in that project's directory
  (a system-changing action, so it's gated; see [Actions and safety](#actions-and-safety)).

Resuming sessions past the first three is part of the [license soft-gate](#licensing-and-the-soft-gate).

---

### Launch

Open a detected agent (e.g. Claude Code) in one of your indexed repos, in a fresh
terminal at the repo root.

**How to:**
- Pick the agent and the repo, then launch. The repo path is resolved
  server-side from the index, never taken from the browser, and the command comes
  from a fixed map. Gated like other actions.
---

### Processes

The live, per-process view of the MCP servers the console can see, grouped by
product and laid out as a **parent-before-child tree** (the `jmunch-mcp` proxy and
the server it fronts). Auto-refreshes every few seconds.

**How to:**
- **Stop one process:** click its `stop` button. Hovering the button highlights
  the whole subtree it will take down, so it's clear that stopping a parent stops
  everything beneath it.
- The console never owned these processes (your MCP client spawns them), so a
  "stop" is really *terminate, and let the client respawn on its next reconnect or
  tool call*. Gated.
---

### Logging

A read-only window onto the crash and indexing evidence the suite leaves on disk,
so a hang or crash is visible at a glance instead of buried in a temp folder.
Everything is a cheap local probe; nothing leaves your machine. (This screen was
previously called "Diagnostics"; the old `#diagnostics` link still works.)

**What you see:**
- **Status badges:** file logging on/off, the watcher and its log count, **perf
  telemetry** state, and jcm's last-activity age.
- **Logs:** the watcher's per-PID logs (`jcw_<pid>.log`) and any explicit
  `JCODEMUNCH_LOG_FILE`, each card showing size, age, error/warn counts, and the
  last ~200 lines colour-coded by level.

**How to capture a wedge or crash** (gated actions):
- **Start watcher + logging** spawns the background reindexer with file logging on,
  so its activity lands in a log this screen tails.
- **Enable server logging** sets the jcm `log_file` config and restarts the jcm
  server so it logs to a file (needs `jcodemunch-mcp` 1.108.64+).

**Perf telemetry** records per-tool latency to `telemetry.db`. The badge has three
states:
- **on**: `telemetry.db` exists and is being written.
- **pending restart**: the `perf_telemetry_enabled` flag is set in config, but the
  running jcm server loaded its config at startup and hasn't acted on it yet.
  **Restart the jcm server** (Processes tab, or reconnect your client); the db is
  created on its first tool call after that.
- **off**: turn on `perf_telemetry_enabled` in Config (or set
  `JCODEMUNCH_PERF_TELEMETRY=1`), then restart the jcm server.
---

### Alerts

Notify-only thresholds over the signals the other panels already track. Nothing
here acts on your servers; alerts only tell you when a line is crossed. A breach
shows a count badge on the Alerts tab and a banner that's visible from any screen.

**Defaults (on out of the box):**
- **Daily spend (est.)**: local Claude Code usage over 24h, at list prices.
- **Daily token burn**: new tokens over 24h (excludes cached reads).
- **Hourly burn spike**: new tokens in the last 60 minutes (catches a runaway
  loop early).
- **Errors in logs**: error lines in the recent jCodeMunch log tail.

**Advanced (off by default, under the "Advanced" drawer):**
- **Server went quiet**: minutes since jcm last served a tool call.
- **Weekly org cost**: org-wide spend over 7 days (needs a `CLAUDE_ADMIN_KEY`).

**How to:**
- Each card shows the **live value** next to an editable **threshold**, a toggle,
  and a status (OK / Approaching / Over threshold / No data). A value within 80%
  of the threshold reads as "Approaching" before it breaches.
- Edit a threshold and press Enter or **save**; toggle a card on/off with its
  switch. Your choices persist in `data/console_settings.json` and survive
  restarts.
---

### Help

An in-console assistant that answers questions about installing, configuring, and
using the console and the jMunch suite. It is **read-only**: it can read the real
source on your machine to give specific, cited answers, but it never edits files,
runs commands, or changes your setup.

**How it works:**
- It runs through your own local **Claude Code** (`claude`) CLI, so the console
  manages no API keys. By default it uses your **Claude subscription**, not
  metered API billing, even if an `ANTHROPIC_API_KEY` is present in your
  environment (the console strips it for this call). Set
  `JMUNCH_CONSOLE_CHAT_USE_API=1` if you have no subscription and prefer to pay
  per token.
- It reads the installed console source with read-only file tools to ground its
  answers and cite `file:line`, rather than guessing.
- Conversations are multi-turn within a visit; ask follow-ups and it keeps
  context.

**How to:**
- Open the **Help** tab and type a question (Enter sends; Shift+Enter for a new
  line). Try "How do I turn on the file watcher?" or "What does fixtures mode do?"
- Don't see the tab? It needs the `claude` CLI installed and the feature enabled
  (it's on by default). Turn it off any time with the **help chat** switch in
  Config, or `JMUNCH_CONSOLE_CHAT=0` for offline/air-gapped deployments.

> If you ask for something the console can't do yet, it will say so plainly. A
> future Build mode will be able to implement a requested feature into your own
> installation and help you share it back as a pull request.

**Note on scope:** the console is a control plane for installing, configuring, and
watching the suite. jCodeMunch-MCP itself keeps gaining read-only analysis tools
that live in your coding agent, not on a console screen; recent additions include
migration parity mapping (`get_parity_map`), a decorator/annotation census
(`get_decorator_census`), and structural architecture metrics
(`get_architecture_metrics`). You call those from your IDE or agent against an
indexed repo; the console doesn't drive them, and you don't need it to.

---

### Config

Reached via the **gear** in the top bar. Three groups:

1. **jMunch Console settings**: port, auth token, the jCodeMunch CLI path,
   fixtures mode, the help chat switch, the actions on/off switch, and your team
   org id. Most apply live; port / token / CLI path apply on the next console
   restart. A setting pinned by an environment variable renders locked with the
   reason.
2. **jCodeMunch-MCP settings**: the suite's own config keys, grouped by section,
   each tagged with its effective source (default / global / project) so override
   confusion is obvious. Editable when actions are enabled; writes go to the
   global `config.jsonc` (a project's `.jcodemunch.jsonc` can still override).
3. **Sibling MCPs**: jDoc and jData's environment-driven settings, shown
   read-only for reference. On `jdocmunch-mcp` 1.94.0+ this includes the
   auto-reindex hook throttle knobs (`JDOCMUNCH_HOOK_DEBOUNCE_SECONDS`,
   `JDOCMUNCH_HOOK_MAX_REINDEX`, `JDOCMUNCH_HOOK_LOG`), added after a
   large-corpus crash report to bound the reindex fan-out on bursty edits. Both
   siblings also carry `readOnlyHint` annotations now (jDoc 1.93.0+, jData
   1.17.0+), so Claude Code plan mode runs their read tools without prompting.

The Config screen also holds the **Console process** controls (restart / stop);
see [Stopping and restarting the console](#stopping-and-restarting-the-console).

---

## Operations

### Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `JMUNCH_CONSOLE_PORT` | `8765` | Listen port |
| `JMUNCH_CONSOLE_TOKEN` | (none) | If set, `/api/*` requires `?token=` or `Authorization: Bearer` |
| `JMUNCH_MCP_BIN` | `jcodemunch-mcp` | jMunch CLI name/path the data adapter shells |
| `CLAUDE_ADMIN_KEY` | (none) | Anthropic Admin API key (`sk-ant-admin...`) for the org Usage/Cost panel. Also read from a gitignored `.env` at the repo root. Organization accounts only; the local half of Token Usage works without it. |
| `JMUNCH_CONSOLE_FIXTURES` | (off) | Set `1` to force sample data for every endpoint |
| `JMUNCH_CONSOLE_READ_ONLY` | (off) | Set `1` to disable every system-changing action. Actions are **on by default**. |
| `JMUNCH_CONSOLE_RELOAD` | (off) | Dev convenience: set `1` to auto-restart the console when `server.py` changes |
| `JMUNCH_CONSOLE_CHAT` | (on) | Set `0` to disable the Help chat assistant (hides the tab and the endpoint) |
| `JMUNCH_CONSOLE_CHAT_MODEL` | `sonnet` | Model the Help chat uses (e.g. `opus` for harder questions) |
| `JMUNCH_CONSOLE_CHAT_USE_API` | (off) | Set `1` to let the Help chat use `ANTHROPIC_API_KEY` (metered) instead of your Claude subscription |

Port, auth token, CLI path, fixtures mode, the actions switch, and org id are also
editable in the Config screen; those edits persist to a gitignored
`data/console_settings.json` read at startup. An explicitly-set environment
variable wins over the file and locks its control in the UI.

---

### Actions and safety

The console performs system-changing actions out of the box: editing config,
launching and resuming agents, installing/updating/uninstalling products and
curated third-party apps, reindexing and deleting indexes, restarting MCP server
processes, and stopping or restarting itself.

The safety model is not a global on/off prompt; it's structural:
- **Server-side allowlisting.** Every action is validated server-side: an agent
  must be a live detected client, a repo a live indexed repo, a command from a
  fixed map. Filesystem paths are resolved on the server, never taken from the
  browser.
- **Localhost only.** The server binds to `127.0.0.1`; set
  `JMUNCH_CONSOLE_TOKEN` to require a token on top of that.
- **Read-only mode.** Set `JMUNCH_CONSOLE_READ_ONLY=1` for a look-don't-touch
  deployment; every action control disables with a reason.
---

### Restarting MCP servers

The product menu in the **jMunch, LLC Apps** rail has a **Restart MCP server**
action, and the **Processes** screen is the live view of the same machinery.

Your MCP client (Claude Code/Desktop/etc.) owns these processes, spawning each
server as a stdio subprocess. So "restart" means *terminate, and let the client
respawn it*. A stopped server stays down until the host acts; some clients only
respawn on their next reconnect or tool call. Restarting one product's transport
(its direct binary plus the `jmunch-mcp` proxy instance that fronts it) leaves the
others alone. The success toast is tailored to the clients the console detects
(for example, Claude Code's `/mcp` reconnect).

---

### Stopping and restarting the console

The **Config** screen's **Console process** group has two buttons (both gated):

- **Restart** relaunches the console on the **same port**, so settings that only
  apply on restart (port, auth token, CLI path) take effect; the open page
  reconnects on its own once the fresh process answers.
- **Stop** shuts the console down completely. There is no in-page way to start it
  again afterward, so the page swaps to a "Console stopped" notice; relaunch from
  your terminal or launcher to return.

You can also click the **brand dot** (top-left) for the same stop/restart menu.

---

### Capturing logs and perf telemetry

The [Logging](#logging) screen turns the suite's log/telemetry recipe into
buttons:

- **File logging** captures crashes and indexing wedges. Turn it on from Logging
  ("Start watcher + logging" or "Enable server logging"), or set
  `JCODEMUNCH_LOG_FILE` in your MCP config.
- **Perf telemetry** persists per-tool latency to `~/.code-index/telemetry.db`.
  Enable `perf_telemetry_enabled` in Config (or `JCODEMUNCH_PERF_TELEMETRY=1`),
  then **restart the jcm server** so it loads the setting; the db appears on the
  first tool call after the restart. The Logging badge shows on / pending restart
  / off so you can see exactly which step is outstanding.
---

### Licensing and the soft-gate

Some panels nudge unlicensed seats toward entering a key by greying out or
blurring secondary actions. This is a **soft gate**: a UX nudge, not a security
boundary. It exists to surface the value of a license, not to enforce one.

- **Where it shows:** the Savings *By tool* table (rows past the first), the
  Savings 30-day chart, and Sessions *resume* (past the first three).
- **Entering a key:** click a product's license dot in the rail and paste the key.
  The console validates it against the licensing backend and reflects the result
  immediately (the gate clears without a restart).
- **Offline grace:** a key on file that can't be reached for validation still
  unlocks, so a licensed-but-offline seat is never punished.
- **Key sources:** the console resolves a key in priority order from its own store
  (the dot prompt), then an environment variable, then the suite's `license_key`
  config, so a key set in any of those places counts and agrees with what the
  suite sees.
---

### Sample-data (fixtures) mode

Set `JMUNCH_CONSOLE_FIXTURES=1` (or flip **fixtures mode** in Config) to force
every panel onto built-in sample payloads, handy for demos and screenshots. The
top bar shows `fixtures mode` and panels tag themselves `sample data`.

---

## Where your data lives

The console keeps its own state in a gitignored `data/` directory (never
committed):

| File | Holds |
|------|-------|
| `data/console_settings.json` | Your Config-screen settings and Alert thresholds |
| `data/licenses.json` | Per-product license keys you entered |
| `data/savings_history.json` | The rolling 30-day savings series the console accumulates |
| `data/delivery_history.json` | Per-repo Productivity history |
| `.env` (repo root) | Optional `CLAUDE_ADMIN_KEY` for the org Usage/Cost panel |

Everything the console reads about your repos, sessions, and usage comes from what
the suite and Claude Code already write to disk; the console adds no new tracking.
