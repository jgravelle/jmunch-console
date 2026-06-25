# jMunch Console

[![Release](https://github.com/jgravelle/jmunch-console/actions/workflows/release.yml/badge.svg)](https://github.com/jgravelle/jmunch-console/actions/workflows/release.yml)
[![Version](https://img.shields.io/github/v/release/jgravelle/jmunch-console?label=version&color=blue)](https://github.com/jgravelle/jmunch-console/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A local, opt-in **control plane** for the jMunch suite, served in your browser.
One pane of glass for config, index and watcher health, token savings and spend,
session recall, agent launching, process control, and alerts.

> Standalone and opt-in. It is **not** bundled into or required by
> `jcodemunch-mcp` / `jdocmunch-mcp` / `jdatamunch-mcp`, and existing CLI/MCP
> users are unaffected. It only ever reads what the suite already exposes; new
> backend capability lands in the suite, not here.

## Quick start

```bash
python server.py
# -> http://127.0.0.1:8765
```

Open the URL in a browser. With no jMunch suite installed you still get a full
tour on built-in sample data; with the suite present, every panel goes live.

**Requirements:** Python 3.10+ (standard library only, no `pip install`). The
`jcodemunch-mcp` CLI on your PATH is what feeds the live panels; without it the
console falls back to sample data. Offline-safe, binds to `127.0.0.1` only.

## What's inside

| Screen | What it does |
|--------|--------------|
| **Index & Watcher** | Your indexed repos: freshness, symbols, reindex/delete, and one-click Starter Packs |
| **Savings** | Tokens and dollars saved by the suite: headline tiles, per-tool table, 30-day trend |
| **Token Usage** | Live Claude token spend from your transcripts (and org-wide, with an admin key) |
| **Productivity** | Cost per durable change: AI spend joined to git outcomes |
| **Sessions** | Browse and resume past Claude Code sessions |
| **Launch** | Open a detected agent in an indexed repo |
| **Processes** | Live MCP server process tree, with stop/restart |
| **Logging** | Crash and index log tail, plus one-click capture and perf telemetry |
| **Alerts** | Notify-only thresholds on daily/hourly cost, token burn, and log errors |
| **Config** (gear) | Edit suite and console settings; restart or stop the console |

The left rail also shows per-product install and license status, and a curated
**Compatible Apps** group.

## Configuration essentials

| Var | Default | Purpose |
|-----|---------|---------|
| `JMUNCH_CONSOLE_PORT` | `8765` | Listen port |
| `JMUNCH_CONSOLE_TOKEN` | (none) | If set, `/api/*` requires `?token=` or `Authorization: Bearer` |
| `JMUNCH_CONSOLE_READ_ONLY` | (off) | Set `1` for a look-don't-touch deployment (disables every system-changing action) |

Actions are **on by default**: the console exists to do things, and every action
is allowlisted server-side and bound to localhost. The full environment table,
safety model, and a how-to for every screen live in the **[User Guide](USER_GUIDE.md)**.

## License

The Console's code is **MIT-licensed** (see [LICENSE](LICENSE)): use, fork, modify,
and ship it freely, just keep the copyright notice. This covers the Console source
in this repository only; the jCodeMunch suite the Console controls is licensed
separately, and the in-app "license soft-gate" refers to suite *license keys*, not
this repository's code license.
