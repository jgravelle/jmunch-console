"use strict";

// jMunch Console — Phase 1 UI. Vanilla JS, no framework, no build.

const SCREENS = [
  { id: "index", label: "Index & Watcher", render: renderIndex },
  { id: "savings", label: "Savings", render: renderSavings },
  { id: "usage", label: "Token Usage", render: renderUsage },
  { id: "delivery", label: "Productivity", render: renderDelivery },
  { id: "sessions", label: "Sessions", render: renderSessions },
  { id: "launch", label: "Launch", render: renderLaunch },
  { id: "processes", label: "Processes", render: renderProcesses },
  { id: "logging", label: "Logging", render: renderDiagnostics },
  { id: "alerts", label: "Alerts", render: renderAlerts },
  { id: "help", label: "Help", render: renderHelp },
  { id: "config", label: "Config", render: renderConfig, gear: true }, // reached via the topbar gear, not the menu
];

const view = document.getElementById("view");
const nav = document.getElementById("nav");
let LAUNCH_ENABLED = false;
let PRODUCTS = []; // last /api/products payload, for the per-product action menu

// "Compatible Apps" rail group: selectively curated third-party packages (sourced
// from the versus.php picks that complement the suite). The roster lives
// server-side (OTHER_APPS in server.py); /api/other-apps adds install/version
// state. This holds the last payload for the per-app action menu.
let OTHER_APPS = [];

// ---- helpers ----
const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path) {
  try {
    const r = await fetch(path);
    return await r.json();
  } catch (e) {
    return { error: String(e), _source: "error" };
  }
}

// Busy state: views are backed by multi-second CLI calls, so a navigation gets
// an immediate spinner in the pane plus a top progress bar, cleared when the
// render resolves. Driven only by go() (navigation), not background polls.
function setBusy(on) {
  document.body.classList.toggle("busy", on);
}

function loadingView(label) {
  return `<div class="loading"><span class="spinner" role="status" aria-label="Loading"></span>
      <span>Loading ${esc(label)}…</span></div>`;
}

function sourceBadge(src) {
  if (src === "live") return `<span class="live-badge">live</span>`;
  if (src === "fixture") return `<span class="fixture-badge">sample data</span>`;
  return `<span class="fixture-badge">unavailable</span>`;
}

function head(title, sub, src) {
  return `<div class="row" style="justify-content:space-between">
      <div><h1>${esc(title)}</h1></div>${src ? sourceBadge(src) : ""}
    </div><p class="sub">${esc(sub)}</p>`;
}

function fmt(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

// Full, comma-grouped number (e.g. 58,332,197). Used for the All-Time tiles,
// which tick in real time — the unabridged figure is the more impressive one.
function fullNum(n) {
  return (Number(n) || 0).toLocaleString("en-US");
}

// Comma-grouped dollars with cents (e.g. $871.63 / $1,234.56).
function money(n) {
  return "$" + (Number(n) || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ---- screens ----
// The control widget for one jcm config key. Editable when actions are enabled
// (ALLOW_LAUNCH) — writing config.jsonc is a system change, same gate as
// launch/resume. list/dict edit as JSON so null vs [] vs {} stays unambiguous.
function cfgControl(k) {
  const dis = LAUNCH_ENABLED ? "" : " disabled";
  const dk = `data-cfgkey="${esc(k.key)}" data-cfgtype="${esc(k.type)}"`;
  if (k.type === "bool") {
    return `<button class="switch" role="switch" aria-checked="${k.value === true}" aria-label="${esc(k.key)}" ${dk}${dis}></button>`;
  }
  if (k.type === "enum") {
    const opts = (k.enum_choices || [k.value])
      .map((c) => `<option ${c === k.value ? "selected" : ""}>${esc(c)}</option>`)
      .join("");
    return `<select class="enum cfg-edit" ${dk}${dis}>${opts}</select>`;
  }
  if (k.type === "list" || k.type === "dict") {
    const text = k.value == null ? "null" : JSON.stringify(k.value, null, 2);
    const rows = Math.min(Math.max(text.split("\n").length, 2), 16);
    return `<textarea class="field field--multi cfg-edit" rows="${rows}" ${dk}${dis} spellcheck="false">${esc(text)}</textarea>`;
  }
  return `<input class="field cfg-edit" value="${esc(k.value)}" ${dk}${dis} />`;
}

function configRow(k) {
  const badge = k.source && k.source !== "default" ? `<span class="source-badge ${esc(k.source)}">${esc(k.source)}</span>` : "";
  const desc = k.description ? `<div class="desc">${esc(k.description)}</div>` : "";
  const typeTag = `<span class="cfg-type">${esc(k.type)}</span>`;
  // Save button only for typed inputs (bool/enum auto-save on change). Reset
  // appears when the key is actually overridden (source != default).
  const saveBtn = LAUNCH_ENABLED && k.type !== "bool" && k.type !== "enum"
    ? `<button class="cfg-btn cfg-save" data-cfgsave="${esc(k.key)}" disabled>save</button>` : "";
  const resetBtn = LAUNCH_ENABLED && k.source && k.source === "global"
    ? `<button class="cfg-btn cfg-reset" data-cfgreset="${esc(k.key)}" title="clear from config.jsonc; built-in default applies">reset</button>` : "";

  if (k.type === "list" || k.type === "dict") {
    return `<div class="config-row config-row--multi" data-cfgrow="${esc(k.key)}">
        <div class="cfg-rowhead"><span class="label">${esc(k.key)} ${typeTag}</span><span class="control">${badge}${saveBtn}${resetBtn}</span></div>
        ${desc}
        ${cfgControl(k)}
      </div>`;
  }
  return `<div class="config-row" data-cfgrow="${esc(k.key)}">
      <div><div class="label">${esc(k.key)} ${typeTag}</div>${desc}</div>
      <div class="control">${badge}${cfgControl(k)}${saveBtn}${resetBtn}</div>
    </div>`;
}

async function saveConfig(key, value) {
  let res;
  try {
    res = await (await fetch("/api/config-set", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }),
    })).json();
  } catch (e) { res = { error: String(e) }; }
  if (res.status === "set") { toast(`saved ${key}`, "ok"); renderConfig(); }
  else { toast((res.error || "save failed") + (res.hint ? ` — ${res.hint}` : ""), "err"); }
}

async function unsetConfig(key) {
  if (!confirm(`Reset ${key} to its built-in default?\n\nThis removes it from config.jsonc.`)) return;
  let res;
  try {
    res = await (await fetch("/api/config-unset", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    })).json();
  } catch (e) { res = { error: String(e) }; }
  if (res.status === "unset") { toast(res.changed ? `reset ${key}` : `${key} was already default`, "ok"); renderConfig(); }
  else { toast((res.error || "reset failed") + (res.hint ? ` — ${res.hint}` : ""), "err"); }
}

// Wire the editable config controls after a render (no-op when read-only).
function wireConfigEditors() {
  if (!LAUNCH_ENABLED) return;
  view.querySelectorAll(".switch[data-cfgkey]").forEach((btn) => {
    btn.onclick = () => saveConfig(btn.dataset.cfgkey, btn.getAttribute("aria-checked") !== "true");
  });
  view.querySelectorAll("select.enum[data-cfgkey]").forEach((sel) => {
    sel.onchange = () => saveConfig(sel.dataset.cfgkey, sel.value);
  });
  view.querySelectorAll("input.cfg-edit, textarea.cfg-edit").forEach((inp) => {
    const row = inp.closest("[data-cfgrow]");
    const save = row && row.querySelector(".cfg-save");
    if (save) inp.oninput = () => { save.disabled = false; };
  });
  view.querySelectorAll(".cfg-save").forEach((btn) => {
    btn.onclick = () => {
      const row = btn.closest("[data-cfgrow]");
      const ctrl = row.querySelector(".cfg-edit");
      const type = ctrl.dataset.cfgtype;
      let value;
      if (type === "list" || type === "dict") {
        const t = ctrl.value.trim();
        try { value = t === "" ? null : JSON.parse(t); }
        catch (e) { toast(`invalid JSON: ${e.message}`, "err"); return; }
      } else if (type === "int" || type === "float") {
        const n = Number(ctrl.value);
        if (ctrl.value.trim() === "" || Number.isNaN(n)) { toast("enter a number", "err"); return; }
        value = type === "int" ? Math.trunc(n) : n;
      } else {
        value = ctrl.value;
      }
      saveConfig(btn.dataset.cfgsave, value);
    };
  });
  view.querySelectorAll(".cfg-reset").forEach((btn) => {
    btn.onclick = () => unsetConfig(btn.dataset.cfgreset);
  });
}

function cfgGroup(title, count, rowsHtml, opts = {}) {
  return `<details class="cfg-group"${opts.open ? " open" : ""}>
      <summary>${esc(title)}<span class="cfg-count">${esc(String(count))}</span></summary>
      <div class="cfg-body">${opts.note ? `<div class="cfg-note">${esc(opts.note)}</div>` : ""}${rowsHtml}</div>
    </details>`;
}

function consoleSettingsGroup(meta) {
  // Explanatory text is written for all knowledge levels — don't assume the
  // reader knows what a port or a bind address is. Everything here is editable
  // except bind (a localhost-only design decision, not a setting); a key the
  // server reports as env-pinned renders locked with the reason.
  const pinned = meta.pinned || [];
  const items = [
    {
      key: "port", pin: "port",
      label: "port",
      value: String(meta.port),
      desc: `The network port the console listens on — open it in a browser at http://127.0.0.1:${meta.port}. Change it if another program already uses this one. Applies the next time the console starts.`,
    },
    {
      label: "bind",
      value: meta.bind,
      desc: `Which addresses the console accepts connections from. "127.0.0.1 (localhost only)" means only this computer can reach it — it is not exposed to your network or the internet. Not configurable, by design.`,
    },
    {
      key: "token", pin: "token",
      label: "auth token",
      value: "",
      placeholder: meta.token_set ? "set — type a new one, or save empty to remove" : "none — type one to require it",
      desc: `An optional access token that callers must supply to use the console. None is required while it stays localhost-only. Applies the next time the console starts.`,
    },
    {
      key: "fixtures", pin: "fixtures",
      label: "fixtures mode",
      bool: !!meta.fixtures_forced,
      desc: `When on, the console shows built-in sample data instead of your real servers — handy for demos or screenshots. Off means every panel is live. Takes effect immediately.`,
    },
    {
      key: "actions", pin: "read_only",
      label: "actions (config / launch / installs)",
      bool: !!meta.launch_enabled,
      desc: `Whether the console may perform system-changing actions — editing config, launching an agent, resuming a session, installing or upgrading a product. Off is a look-don't-touch mode. Takes effect immediately.`,
    },
    {
      key: "chat", pin: "chat",
      label: "help chat",
      bool: !!meta.chat_enabled,
      desc: `The in-console Help assistant. When on, the Help tab answers questions about the console using your local Claude Code (on your Claude subscription by default, not metered API). It's read-only — it never changes your setup. Off hides the tab and disables the endpoint. Takes effect immediately.`,
    },
    {
      key: "mcp_bin", pin: "mcp_bin",
      label: "jcodemunch CLI",
      value: meta.mcp_bin,
      desc: `The jCodeMunch-MCP command the console runs to fetch live data (indexed repos, config, savings). Use its full path if it isn't found on your system PATH. Applies the next time the console starts.`,
    },
    {
      key: "org_id", pin: "org_id",
      label: "team org id",
      value: meta.org_id || "",
      placeholder: "e.g. acme — leave empty to disable the rollup",
      desc: `Sets JCODEMUNCH_ORG_ID for the team savings rollup (Savings → Aggregate across seats). Pick any stable name shared across your seats, then run "jcodemunch-mcp org-report" on each one to feed it. Takes effect immediately. The rollup itself needs a Studio or Platform license — a Builder key records seats but can't aggregate them.`,
    },
  ];
  const rows = items
    .map((it) => {
      const isPinned = it.pin && pinned.includes(it.pin);
      const lock = isPinned ? ` disabled title="pinned by an environment variable — unset it to edit here"` : "";
      let ctrl;
      if ("bool" in it) {
        ctrl = `<button class="switch" role="switch" aria-checked="${it.bool}" aria-label="${esc(it.label)}"${it.key ? ` data-conskey="${it.key}"` : ""}${lock}></button>`;
      } else if (it.key) {
        ctrl = `<input class="field cons-edit" value="${esc(it.value)}"${it.placeholder ? ` placeholder="${esc(it.placeholder)}"` : ""} data-conskey="${it.key}"${lock} />` +
          (isPinned ? "" : `<button class="cfg-btn cons-save" data-conssave="${it.key}" disabled>save</button>`);
      } else {
        ctrl = `<input class="field" value="${esc(it.value)}" disabled />`;
      }
      return `<div class="config-row">
          <div><div class="label">${esc(it.label)}</div><div class="desc">${esc(it.desc)}</div></div>
          <div class="control">${ctrl}</div>
        </div>`;
    })
    .join("");
  return cfgGroup("jMunch Console", "console", rows, {
    open: true,
    note: "The console's own settings. Changes persist to data/console_settings.json; an explicitly-set JMUNCH_CONSOLE_* environment variable wins and locks its control. License keys are managed in the Products rail.",
  });
}

// Stop/restart the console itself, reached by clicking the brand dot (top-left).
// Restart/stop ride the two-key turn (ALLOW_LAUNCH), shown disabled with the
// reason when off — same shape as the product/other-app menus.
function consoleMenu() {
  const gateReason = "read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable";
  const mi = (action, label, desc, extra) =>
    `<button class="btn menu-item${extra ? " " + extra : ""}" data-action="${action}"${
      LAUNCH_ENABLED ? "" : ` disabled title="${esc(gateReason)}"`
    }><span>${esc(label)}</span><small class="menu-desc">${esc(desc)}</small></button>`;
  const items = [
    mi("restart", "Restart console",
       "Re-launch on the same port so settings that apply on restart take effect; this page reconnects on its own."),
    mi("stop", "Stop console",
       "Shut down completely — relaunch from your terminal or launcher to return.", "menu-item--danger"),
  ].join("");
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="modal"><h3>jMunch Console</h3>
      <div class="muted">${LAUNCH_ENABLED ? "Stop or restart the console process." : esc(gateReason)}</div>
      <div class="menu-list">${items}</div>
      <div class="actions"><button class="btn btn--ghost" id="menu-cancel">close</button></div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (e) => { if (e.target === ov) close(); };
  ov.querySelector("#menu-cancel").onclick = close;
  ov.querySelectorAll(".menu-item:not([disabled])").forEach(
    (b) => (b.onclick = () => { close(); ({ restart: restartConsole, stop: stopConsole }[b.dataset.action]?.()); })
  );
}

// Poll /api/meta until the re-exec'd server answers again, then reload the page.
function waitForConsole() {
  let tries = 0;
  const tick = async () => {
    tries++;
    try {
      const r = await fetch("/api/meta", { cache: "no-store" });
      if (r.ok) { location.reload(); return; }
    } catch (e) { /* still stepping aside */ }
    if (tries < 40) setTimeout(tick, 500);
    else toast("console didn't come back — reload the page manually", "err");
  };
  setTimeout(tick, 1200); // give the old image time to release the port
}

async function restartConsole() {
  if (!confirm("Restart the console?\n\nIt re-launches on the same port and this page reconnects automatically.")) return;
  let res;
  try {
    res = await (await fetch("/api/console-restart", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    })).json();
  } catch (e) { res = { error: String(e) }; }
  if (res.status !== "restarting") {
    toast((res.error || "restart failed") + (res.hint ? ` — ${res.hint}` : ""), "err");
    return;
  }
  toast("restarting — reconnecting…", "ok");
  waitForConsole();
}

async function stopConsole() {
  if (!confirm("Stop the console?\n\nIt shuts down completely. You'll need to relaunch it from your terminal or launcher to return.")) return;
  let res;
  try {
    res = await (await fetch("/api/console-stop", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    })).json();
  } catch (e) { res = { error: String(e) }; }
  if (res.status !== "stopping") {
    toast((res.error || "stop failed") + (res.hint ? ` — ${res.hint}` : ""), "err");
    return;
  }
  document.body.innerHTML =
    `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;text-align:center;gap:.5rem;font-family:inherit;">
       <h1 style="margin:0;">jMunch Console stopped</h1>
       <p style="opacity:.7;">Relaunch it from your terminal or launcher, then reopen this page.</p>
     </div>`;
}

async function saveConsole(key, value) {
  let res;
  try {
    res = await (await fetch("/api/console-set", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }),
    })).json();
  } catch (e) { res = { error: String(e) }; }
  if (res.status === "set") {
    toast(`saved ${key}${res.note ? ` — ${res.note}` : ""}`, "ok");
    // actions/fixtures change what the whole app may do — refresh the global
    // state and re-render so every control reflects it.
    const meta = await api("/api/meta");
    LAUNCH_ENABLED = !!meta.launch_enabled;
    document.getElementById("meta").textContent = meta.fixtures_forced
      ? "fixtures mode"
      : LAUNCH_ENABLED ? "launch enabled" : "read-only";
    renderConfig();
  } else {
    toast((res.error || "save failed") + (res.hint ? ` — ${res.hint}` : ""), "err");
  }
}

// Wire the console-settings controls. Unlike wireConfigEditors this runs even
// in read-only mode — the actions switch is how you turn read-only back off.
function wireConsoleEditors() {
  view.querySelectorAll(".switch[data-conskey]").forEach((btn) => {
    btn.onclick = () => saveConsole(btn.dataset.conskey, btn.getAttribute("aria-checked") !== "true");
  });
  view.querySelectorAll("input.cons-edit").forEach((inp) => {
    const save = view.querySelector(`.cons-save[data-conssave="${inp.dataset.conskey}"]`);
    if (save) {
      inp.oninput = () => { save.disabled = false; };
      save.onclick = () => saveConsole(inp.dataset.conskey, inp.value);
    }
  });
}

function siblingGroup(sib) {
  const rows = (sib.settings || [])
    .map((s) => {
      const badge = s.source === "env" ? `<span class="source-badge global">env</span>` : "";
      return `<div class="config-row">
          <div><div class="label">${esc(s.key)}</div><div class="desc"><span class="mono">${esc(s.env)}</span> · ${esc(s.description)}</div></div>
          <div class="control">${badge}<input class="field" value="${esc(s.value)}" disabled title="default: ${esc(s.default)}" /></div>
        </div>`;
    })
    .join("");
  return cfgGroup(sib.name, sib.settings.length, rows, { note: sib.note });
}

async function renderConfig() {
  const [meta, d, sib] = await Promise.all([api("/api/meta"), api("/api/config"), api("/api/sibling-config")]);
  const keys = d.keys || [];

  // group jcm keys by their config-section, preserving first-appearance order, "Other" last
  const byGroup = {};
  const order = [];
  for (const k of keys) {
    const g = k.group || "Other";
    if (!byGroup[g]) {
      byGroup[g] = [];
      order.push(g);
    }
    byGroup[g].push(k);
  }
  order.sort((a, b) => (a === "Other" ? 1 : 0) - (b === "Other" ? 1 : 0));

  const groupsHtml = order
    .map((g, i) => cfgGroup(g, byGroup[g].length, byGroup[g].map(configRow).join(""), { open: i === 0 }))
    .join("");

  const sub = LAUNCH_ENABLED
    ? "Editable — changes write to the global config.jsonc (a project's .jcodemunch.jsonc still overrides; see the source tag)."
    : "Read-only mode is on (JMUNCH_CONSOLE_READ_ONLY=1) — unset it to edit settings here; a project's .jcodemunch.jsonc can also override (see the source tag).";

  view.innerHTML =
    head("jCodeMunch-MCP Configuration", sub, d._source) +
    consoleSettingsGroup(meta) +
    `<div class="section-title">jCodeMunch-MCP settings · ${keys.length} keys</div>` +
    (groupsHtml || `<div class="empty">No config keys.</div>`) +
    `<div class="section-title">Sibling MCPs · env-configured</div>` +
    (sib.siblings || []).map(siblingGroup).join("");

  wireConfigEditors();
  wireConsoleEditors();
}

// ---- index management (client-side search / sort / filter over /api/repos) ----
let INDEX_REPOS = [];
// Whether the console holds a valid suite license. Set from /api/repos; a
// missing flag (older server) is treated as licensed so we never wrongly gate.
let LICENSED = true;
let INDEX_SEARCH = "";
let INDEX_SORT = "name";
let INDEX_FILTER = "all";

function indexMatches(r) {
  const q = INDEX_SEARCH.trim().toLowerCase();
  if (q) {
    const hay = `${r.repo_id || ""} ${r.display_name || ""} ${Object.keys(r.languages || {}).join(" ")}`.toLowerCase();
    if (!hay.includes(q)) return false;
  }
  const fr = r.freshness || "";
  const w = r.watcher_state || "";
  switch (INDEX_FILTER) {
    case "stale": return fr.includes("stale");
    case "fresh": return fr === "fresh" || fr === "up_to_date";
    case "watching": return w === "watching" || w === "reindexing";
    case "idle": return !w || w === "idle";
    default: return true;
  }
}

function indexSorted(list) {
  const name = (r) => (r.display_name || r.repo_id || "").toLowerCase();
  return [...list].sort((a, b) => {
    switch (INDEX_SORT) {
      case "symbols": return (b.symbol_count || 0) - (a.symbol_count || 0);
      case "files": return (b.file_count || 0) - (a.file_count || 0);
      case "indexed": return String(b.indexed_at || "").localeCompare(String(a.indexed_at || ""));
      case "freshness": return String(a.freshness || "").localeCompare(String(b.freshness || ""));
      default: return name(a).localeCompare(name(b));
    }
  });
}

function indexCard(r) {
  const langs = Object.entries(r.languages || {})
    .map(([l, c]) => `<span class="chip">${esc(l)} ${c}</span>`)
    .join("");
  // Unlicensed: reindex / copy id / delete are all greyed out, each prompting
  // for a valid license on hover. This gate sits above read-only and has_source.
  let actions;
  if (!LICENSED) {
    const tip = "Enter a valid license # in jMunch, LLC Apps to enable this.";
    // Firefox suppresses the title tooltip on a disabled button, so carry it on
    // a wrapper span (the button has pointer-events:none, so the span gets the
    // hover). lock(label, extra) builds one greyed, tooltipped action.
    const lock = (label, extra = "") =>
      `<span class="lic-lock" title="${tip}"><button class="btn${extra}" disabled>${label}</button></span>`;
    actions = lock("reindex") + lock("copy id") + lock("delete", " btn--danger");
  } else {
    // Reindex needs a resolvable on-disk path; remote/URL-indexed repos carry an
    // empty source_root and can't be re-indexed from here (delete/copy still work,
    // they only need the repo_id).
    const reindexBtn = r.has_source
      ? `<button class="btn" data-reindex="${esc(r.repo_id)}">reindex</button>`
      : `<button class="btn" disabled title="No local source path on record (indexed from GitHub or an older format). Use the 'index a repo' button above with its GitHub owner/repo, or a local path, to refresh it.">reindex</button>`;
    actions = LAUNCH_ENABLED
      ? `${reindexBtn}
         <button class="btn" data-copy="${esc(r.repo_id)}">copy id</button>
         <button class="btn btn--danger" data-del="${esc(r.repo_id)}" data-name="${esc(r.display_name || r.repo_id)}">delete</button>`
      : `<button class="btn" data-copy="${esc(r.repo_id)}">copy id</button>
         <button class="btn" disabled title="Read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable reindex/delete">delete</button>`;
  }
  return `<div class="card">
      <div class="card-head"><span class="name">${esc(r.display_name || r.repo_id)}</span>
        <span class="pill pill--${esc(r.freshness || "neutral")}">${esc((r.freshness || "unknown").replace(/_/g, " "))}</span></div>
      <div class="kv"><span>symbols</span><b>${fmt(r.symbol_count)}</b></div>
      <div class="kv"><span>files</span><b>${fmt(r.file_count)}</b></div>
      <div class="kv"><span>watcher</span><b>${esc(r.watcher_state || "—")}</b></div>
      <div class="kv"><span>indexed</span><b>${esc((r.indexed_at || "").slice(0, 10) || "—")}</b></div>
      <div class="chips">${langs}</div>
      <div class="idx-actions">${actions}</div>
    </div>`;
}

function renderIndexList() {
  const shown = indexSorted(INDEX_REPOS.filter(indexMatches));
  const grid = document.getElementById("index-grid");
  if (!grid) return;
  const count = document.getElementById("index-count");
  if (count) count.textContent = `${shown.length} of ${INDEX_REPOS.length}`;
  grid.innerHTML = shown.length
    ? shown.map(indexCard).join("")
    : `<div class="empty">${INDEX_REPOS.length ? "No repos match." : "No indexed repos."}</div>`;
  grid.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = () => {
    navigator.clipboard?.writeText(b.dataset.copy);
    toast(`copied ${b.dataset.copy}`, "ok");
  }));
  grid.querySelectorAll("[data-reindex]").forEach((b) => (b.onclick = () =>
    postIndexAction("/api/reindex", { repo_id: b.dataset.reindex }, "reindex_started", `reindexing ${b.dataset.reindex}`)));
  grid.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => deleteIndex(b.dataset.del, b.dataset.name, b)));
}

async function postIndexAction(path, body, okStatus, okMsg) {
  let res;
  try {
    res = await (await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })).json();
  } catch (e) {
    res = { error: String(e) };
  }
  if (res.status === okStatus) {
    toast(okMsg + (okStatus.endsWith("_started") ? " — watch the new terminal" : ""), "ok");
  } else {
    toast((res.error || "action failed") + (res.hint ? ` — ${res.hint}` : ""), "err");
  }
  return res;
}

async function indexNew() {
  // The "here" the greyed-reindex tooltip refers to: index a fresh local folder
  // or a GitHub owner/repo. Validation + local-vs-GitHub routing happen server-side
  // (the target is client input, so it never reaches a spawned argv unvalidated).
  const target = prompt(
    "Index a repo here.\n\n" +
    "Enter a local folder path (e.g. C:\\code\\my-repo)\n" +
    "or a GitHub owner/repo (e.g. octocat/Hello-World):"
  );
  if (target === null) return; // cancelled
  const t = target.trim();
  if (!t) return;
  if (res.status !== "index_started") return;
  // Indexing runs in its own terminal (kept open with its result). The repos list
  // is cached (30s) AND list-repos is slow, so a plain refresh would serve stale
  // data — the just-indexed repo wouldn't show until the cache expired or a manual
  // reload (the reported symptom). Poll a couple of FRESH reads (?fresh=1 bypasses
  // the cache) sequentially; the index usually finishes before the first returns.
  // Guarded on the hash so we don't yank the user back if they've navigated away.
  // (An already-indexed path refreshes its existing card rather than adding one.)
  for (let i = 0; i < 2 && location.hash.replace("#", "") === "index"; i++) {
    await new Promise((r) => setTimeout(r, 4000));
    if (location.hash.replace("#", "") !== "index") break;
    await renderIndex(true);
  }
}

async function deleteIndex(repoId, name, btn) {
  if (!confirm(`Delete the index for ${name}?\n\nThis removes ${repoId} from jCodeMunch. Your source files are untouched — you can re-index any time.`)) return;
  // Delete is synchronous on the server and can take a few seconds: show the
  // working indicator (top progress bar) and put the clicked button into a busy
  // spinner state so it's clear the action is in flight.
  setBusy(true);
  let prevHtml;
  if (btn) {
    prevHtml = btn.innerHTML;
    btn.disabled = true;
    btn.classList.add("btn--busy");
    btn.innerHTML = `<span class="spinner spinner--sm" role="status" aria-label="Deleting"></span>deleting…`;
  }
  try {
    const res = await postIndexAction("/api/delete-index", { repo_id: repoId }, "deleted", `deleted ${name}`);
    if (res.status === "deleted") {
      // Drop the card immediately, and refresh the starter-pack rail (whose
      // installed/present state can flip when a repo is deleted). We deliberately
      // do NOT block on a full /api/repos re-fetch here: list-repos can take 60s+
      // on a machine with many repos, so an authoritative re-pull would freeze the
      // delete behind a long spinner. The server invalidates its repos cache on
      // delete, so the next #index visit is authoritative without the wait.
      INDEX_REPOS = INDEX_REPOS.filter((r) => r.repo_id !== repoId);
      renderIndexList();   // instant: rebuilds the grid, replacing the busy button
      renderStarterPacks(); // refresh the pack rail (fire-and-forget)
      return;
    }
    // Failed: restore the button so the user can retry.
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("btn--busy");
      btn.innerHTML = prevHtml;
    }
  } finally {
    setBusy(false);
  }
}

async function renderIndex(fresh = false) {
  const d = await api("/api/repos" + (fresh ? "?fresh=1" : ""));
  INDEX_REPOS = d.repos || [];
  LICENSED = d.licensed !== false;
  const opt = (v, sel) => `<option value="${v}"${sel === v ? " selected" : ""}>${v}</option>`;
  const sortOpts = ["name", "symbols", "files", "indexed", "freshness"].map((v) => opt(v, INDEX_SORT)).join("");
  const filterOpts = ["all", "stale", "fresh", "watching", "idle"].map((v) => opt(v, INDEX_FILTER)).join("");
  // "Index a repo" is the missing entry point the greyed-reindex tooltip points at:
  // it indexes a fresh local folder or GitHub owner/repo (jcm's `index` verb does both).
  // Gated like the per-card actions — read-only mode and unlicensed both lock it.
  const addBtn = !LAUNCH_ENABLED
    ? `<button class="btn idx-add" disabled title="Read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable indexing">+ index a repo</button>`
    : !LICENSED
      ? `<button class="btn idx-add" disabled title="Enter a valid license # in jMunch, LLC Apps to enable this.">+ index a repo</button>`
      : `<button id="index-add" class="btn btn--accent idx-add">+ index a repo</button>`;
  // Honest degradation: the server tags _degraded when a LIVE list-repos was
  // attempted but failed/timed out (vs a deliberate fixtures pin). Without this
  // banner the 3 sample repos look like a real-but-incomplete index — the "it
  // isn't showing everything indexed" confusion.
  const degraded = d._degraded
    ? `<div class="diag-hint">Live index data didn't load — ${esc(d._degraded_reason || "showing sample data")} The repos below are <b>sample data</b>, not your real index.</div>`
    : "";
  view.innerHTML =
    head("Index & Watcher", "Indexed repositories, freshness, and watcher state.", d._source) +
    degraded +
    `<div class="idx-toolbar">
       <input id="index-search" class="idx-search" type="search" placeholder="search name / id / language" value="${esc(INDEX_SEARCH)}">
       <label class="idx-ctl">sort <select id="index-sort" class="idx-select">${sortOpts}</select></label>
       <label class="idx-ctl">filter <select id="index-filter" class="idx-select">${filterOpts}</select></label>
       <span id="index-count" class="idx-count"></span>
       ${addBtn}
     </div>
     <div id="index-grid" class="grid"></div>
     <div id="packs-section" class="packs-section"></div>`;
  const s = document.getElementById("index-search");
  s.oninput = () => { INDEX_SEARCH = s.value; renderIndexList(); };
  document.getElementById("index-sort").onchange = (e) => { INDEX_SORT = e.target.value; renderIndexList(); };
  document.getElementById("index-filter").onchange = (e) => { INDEX_FILTER = e.target.value; renderIndexList(); };
  document.getElementById("index-add")?.addEventListener("click", indexNew);
  renderIndexList();
  renderStarterPacks();
}

// ---- starter packs (ghost rows on #index; add = download a pre-built index) ----
async function renderStarterPacks() {
  const sec = document.getElementById("packs-section");
  if (!sec) return;
  let d;
  try { d = await api("/api/starter-packs"); } catch (e) { sec.innerHTML = ""; return; }
  const all = d.packs || [];
  if (!all.length) { sec.innerHTML = ""; return; }
  // Show the whole catalog. Actionable packs (add / locked) sort first;
  // already-installed ones last so the rail leads with what you can act on.
  const packs = all.slice().sort((a, b) => (a.installed === b.installed ? 0 : a.installed ? 1 : -1));
  const sub = d.unlocked
    ? `License on file (${esc(d.key_masked || "valid")}) — the full pack library is unlocked.`
    : `Free packs are ready to add. Enter a valid license on any suite product to unlock the rest.`;
  sec.innerHTML =
    `<div class="packs-head">
       <h3>Starter Packs</h3>
       <span class="packs-tag">Framework context without the framework</span>
     </div>
     <div class="packs-pitch">
       <p class="packs-pitch-lead">Pre-built symbol indexes of popular codebases. Search them the moment they land — skip the clone, the index build, and the API key.</p>
       <div class="packs-benefits">
         <span class="benefit"><b>~97%</b><span>fewer tokens</span><small>~490 vs ~21,500 reading source</small></span>
         <span class="benefit"><b>seconds</b><span>to first answer</span><small>no clone, no index build</small></span>
         <span class="benefit"><b>no key</b><span>to get started</span><small>free pack needs no license</small></span>
       </div>
     </div>
     <p class="packs-sub">${esc(sub)}</p>
     <div class="grid">${packs.map(packCard).join("")}</div>`;
  sec.querySelectorAll("[data-addpack]").forEach((b) =>
    (b.onclick = () => addPack(b.dataset.addpack, b.dataset.name)));
  sec.querySelectorAll("[data-updatepack]").forEach((b) =>
    (b.onclick = () => updatePack(b.dataset.updatepack, b.dataset.name)));
  sec.querySelectorAll("[data-uninstallpack]").forEach((b) =>
    (b.onclick = () => uninstallPack(b.dataset.uninstallpack, b.dataset.name)));
  sec.querySelectorAll("[data-adoptpack]").forEach((b) =>
    (b.onclick = () => adoptPack(b.dataset.adoptpack, b.dataset.name)));
}

function packCard(p) {
  const stat = (val, label) => `<span class="pack-stat"><b>${val}</b><small>${label}</small></span>`;
  const stats = [];
  if (p.symbols) stats.push(stat(fmt(p.symbols), "symbols"));
  if (p.size) stats.push(stat(esc(p.size), "pack size"));
  if (p.source_size) stats.push(stat(esc(p.source_size), "source repos"));
  if (p.repos && p.repos.length) stats.push(stat(p.repos.length, `repo${p.repos.length > 1 ? "s" : ""}`));
  const nrepos = (p.repos && p.repos.length) || 0;
  const benefit = `Search ${nrepos > 1 ? "these codebases" : "this codebase"} at the symbol level — no clone, no index build, no key.`;
  const built = p.indexed_date ? `Pre-built ${esc(p.indexed_date)}` : "";
  const tag = p.free
    ? `<span class="pill pill--fresh">free</span>`
    : `<span class="pill pill--neutral">license</span>`;
  const ro = `title="Read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable"`;
  let action, state;
  if (p.pack_installed) {
    // Installed via the console → full lifecycle (update + uninstall).
    state = "installed";
    let updateBtn;
    if (!LAUNCH_ENABLED) {
      updateBtn = `<button class="btn" disabled ${ro}>update</button>`;
    } else if (p.update_addable) {
      const label = p.update_available ? "update available" : "update";
      const cls = p.update_available ? "btn btn--accent" : "btn";
      updateBtn = `<button class="${cls}" data-updatepack="${esc(p.id)}" data-name="${esc(p.name)}">${label}</button>`;
    } else {
      updateBtn = `<button class="btn" disabled title="Enter a valid jCodeMunch license to re-download this pack.">update</button>`;
    }
    const uninstallBtn = LAUNCH_ENABLED
      ? `<button class="btn btn--danger" data-uninstallpack="${esc(p.id)}" data-name="${esc(p.name)}">uninstall</button>`
      : `<button class="btn" disabled ${ro}>uninstall</button>`;
    action = updateBtn + uninstallBtn;
  } else if (p.installed) {
    // Present only by repo overlap (indexed independently, no pack marker).
    // Offer to adopt it — claim those repos as this pack so it gains the
    // update/uninstall lifecycle. No download.
    state = "present";
    const adoptBtn = LAUNCH_ENABLED
      ? `<button class="btn" data-adoptpack="${esc(p.id)}" data-name="${esc(p.name)}" title="These repos are in your index but weren't installed as a pack. Adopt to manage (update / uninstall) them here. No download.">adopt</button>`
      : `<button class="btn" disabled ${ro}>adopt</button>`;
    action = `<span class="pill pill--fresh">installed</span>${adoptBtn}`;
  } else if (p.addable) {
    state = "addable";
    action = LAUNCH_ENABLED
      ? `<button class="btn btn--accent" data-addpack="${esc(p.id)}" data-name="${esc(p.name)}">install</button>`
      : `<button class="btn" disabled ${ro}>install</button>`;
  } else {
    // Locked: no valid license on file. Disabled install + a path to get one.
    state = "locked";
    action = `<button class="btn" disabled title="Enter a valid jCodeMunch license to unlock this pack.">install</button>
              <a class="btn btn--ghost" href="https://j.gravelle.us/jCodeMunch/#pricing" target="_blank" rel="noopener">get license</a>`;
  }
  return `<div class="card pack-card pack-card--${state}">
      <div class="card-head"><span class="name">${esc(p.name)}</span>${tag}</div>
      ${p.savings_ratio ? `<div class="pack-savings" title="The pre-built index is ${esc(String(p.savings_ratio))}x smaller than cloning ${esc(p.source_size || "the source")}.">${esc(String(p.savings_ratio))}&times; smaller</div>` : ""}
      <div class="pack-stats">${stats.join("")}</div>
      <div class="pack-desc">${esc(p.description || "")}</div>
      <div class="pack-benefit">${esc(benefit)}</div>
      ${built ? `<div class="pack-built">${built}</div>` : ""}
      <div class="idx-actions">${action}</div>
    </div>`;
}

async function addPack(packId, name) {
  const res = await postIndexAction("/api/install-pack", { pack: packId }, "pack_install_started", `installing ${name}`);
  if (res.status === "pack_install_started") watchPack(packId, name);
}

async function updatePack(packId, name) {
  if (!confirm(`Update ${name} to the latest pack version?\n\nRe-downloads and re-extracts its pre-built index in a new terminal.`)) return;
  const res = await postIndexAction("/api/update-pack", { pack: packId }, "pack_update_started", `updating ${name}`);
  // Re-download runs in the terminal; installed stays true throughout, so just
  // refresh the rail shortly rather than polling an install→installed flip.
  if (res.status === "pack_update_started") setTimeout(() => renderStarterPacks(), 8000);
}

async function uninstallPack(packId, name) {
  if (!confirm(`Uninstall ${name}?\n\nDeletes this pack's pre-built indexes from jCodeMunch. Your source files are untouched — you can re-add it any time.`)) return;
  const res = await postIndexAction("/api/uninstall-pack", { pack: packId }, "pack_uninstalled", `uninstalled ${name}`);
  if (res.status === "pack_uninstalled") renderIndex();
}

async function adoptPack(packId, name) {
  if (!confirm(`Adopt ${name} as a managed pack?\n\nMarks the repos already in your index as this pack so you can update or uninstall it from here. No download now.\n\nNote: a later Uninstall will then delete those repo indexes.`)) return;
  const res = await postIndexAction("/api/adopt-pack", { pack: packId }, "pack_adopted", `adopted ${name}`);
  if (res.status === "pack_adopted") renderStarterPacks();
}

async function watchPack(packId, name) {
  // The download + extract runs in a terminal. Poll the catalog until the pack
  // flips to installed, then refresh the whole index view so its repos appear.
  for (let i = 0; i < 90; i++) {
    await new Promise((r) => setTimeout(r, 4000));
    let d;
    try { d = await api("/api/starter-packs?fresh=1"); } catch (e) { continue; }
    const p = (d.packs || []).find((x) => x.id === packId);
    if (p && p.installed) {
      toast(`${name} added`, "ok");
      renderIndex();
      return;
    }
  }
  toast("still downloading — the index list catches up once the terminal finishes", "");
}

// Selectable savings windows. Keys match the server's SAVINGS_RANGES; the
// server resolves each to receipt --since/--until against the local calendar.
const SAVINGS_RANGES = [
  ["today", "Today"],
  ["yesterday", "Yesterday"],
  ["week", "This Week"],
  ["month", "This Month"],
  ["year", "This Year"],
  ["all", "All Time"],
];
const SAVINGS_RANGE_LABELS = Object.fromEntries(SAVINGS_RANGES);
let SAVINGS_RANGE = "month";

// The tokens saved are a measurement; the dollars are that measurement valued at
// a price, so the price is the user's to pick. Persisted because it's a
// preference (which model your work actually runs on), not a transient view.
let SAVINGS_MODEL = localStorage.getItem("jmunch.savings.model") || "opus";

function rangeChips(active) {
  return `<div class="row range-chips" role="group" aria-label="savings window">` +
    SAVINGS_RANGES.map(([k, label]) =>
      `<button class="chip${k === active ? " chip-on" : ""}" data-range="${k}"${k === active ? ' aria-current="true"' : ""}>${esc(label)}</button>`
    ).join("") +
    `</div>`;
}

// Options come from the rates jcm published, never a list hardcoded here — that
// way a repriced or newly-added model shows up without a console change.
function modelPicker(models, active) {
  const names = Object.keys(models || {});
  if (!names.length) return "";
  const opts = names
    .map((m) => `<option value="${esc(m)}"${m === active ? " selected" : ""}>${esc(m[0].toUpperCase() + m.slice(1))} · $${esc(String(models[m]))}/MTok</option>`)
    .join("");
  return `<label class="row model-picker" title="Rate used to value the tokens saved. The token counts don't change — only what they're worth.">
      <span class="muted">Priced at</span>
      <select id="savings-model" aria-label="pricing model">${opts}</select>
    </label>`;
}

function sparkbars(series) {
  if (!series || !series.length)
    return `<div class="empty">no jCodeMunch tool calls in this window</div>`;
  const w = 720, h = 150, pad = 10;
  const max = Math.max(...series.map((p) => Number(p.tokens_saved) || 0), 1);
  const bw = (w - pad * 2) / series.length;
  const bars = series
    .map((p, i) => {
      const v = Number(p.tokens_saved) || 0;
      const bh = (v / max) * (h - pad * 2);
      const x = pad + i * bw;
      const y = h - pad - bh;
      return `<rect x="${(x + 1).toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="var(--chart-1)"><title>${esc(p.date)}: ${fmt(v)} tokens · $${(Number(p.usd) || 0).toFixed(2)}</title></rect>`;
    })
    .join("");
  const first = series[0].date, last = series[series.length - 1].date;
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="tokens saved over time">${bars}</svg>
    <div class="row" style="justify-content:space-between"><span class="muted mono" style="font-size:var(--fs-xs)">${esc(first)}</span><span class="muted mono" style="font-size:var(--fs-xs)">${esc(last)}</span></div>`;
}

// ROI hero: the outcome the token meters below are inputs to. Cost per durable
// change = dollars spent ÷ changes that landed and stuck. Guercio's "ROI-maxing
// not token-maxing" made concrete — the number a finance team actually wants.
function roiHero(roi, s) {
  const r = roi || {};
  const cpd = r.cost_per_durable == null ? "n/a" : money(r.cost_per_durable);
  const savedTok = s.tokens_saved_window == null ? "n/a" : fullNum(s.tokens_saved_window);
  const spend = r.total_cost_usd == null ? "n/a" : money(r.total_cost_usd);
  const note = r.attributable
    ? `${fmt(r.total_durable)} durable change${r.total_durable === 1 ? "" : "s"} across ${fmt(r.contributor_count)} repo${r.contributor_count === 1 ? "" : "s"} · last ${fmt(r.window_days)}d`
    : (r.hint || "not yet attributable — work in an indexed repo and let changes land");
  return `<div class="roi-hero" title="Cost per durable change: dollars spent per change that landed and stuck. ROI, not tokens.">
      <div class="roi-hero-main">
        <div class="roi-label">Cost per durable change <span class="roi-tag">ROI</span></div>
        <div class="roi-value">${esc(cpd)}</div>
        <div class="roi-inputs">&larr; ${esc(savedTok)} tokens saved · ${esc(spend)} spend <span class="roi-muted">(inputs)</span></div>
      </div>
      <div class="roi-note">${esc(note)}</div>
    </div>`;
}

async function renderSavings() {
  const rng = SAVINGS_RANGE;
  const label = SAVINGS_RANGE_LABELS[rng] || rng;
  const [d, roi] = await Promise.all([
    api(`/api/savings?range=${encodeURIComponent(rng)}&model=${encodeURIComponent(SAVINGS_MODEL)}`),
    api("/api/roi"),
  ]);
  const s = d.savings || {};
  // The server has the last word on the model: a stored preference naming a model
  // jcm no longer prices falls back to jcm's default, and the UI must follow that
  // rather than keep showing a selection the numbers aren't actually priced at.
  if (s.model && s.model !== SAVINGS_MODEL) {
    SAVINGS_MODEL = s.model;
    localStorage.setItem("jmunch.savings.model", SAVINGS_MODEL);
  }
  const total = s.tokens_saved_total == null ? "n/a" : fullNum(s.tokens_saved_total);
  const usdTotal = s.usd_saved_total == null ? "n/a" : money(s.usd_saved_total);
  // Window tiles first, then the two lifetime tiles. The lifetime pair reads the
  // suite's own counter rather than transcripts, so it legitimately exceeds even
  // the All Time window (transcripts get cleared on reinstall, the counter
  // doesn't) — hence the explicit "all-time" labels rather than leaving a reader
  // to assume every tile moves with the chips. Both carry ids so the live poll
  // can update them in place.
  const modelName = s.model ? s.model[0].toUpperCase() + s.model.slice(1) : "";
  const rate = s.models && s.model ? s.models[s.model] : null;
  const atRate = rate == null ? `at ${modelName} rates` : `at ${modelName} rates ($${rate}/MTok)`;
  // Window figures come from the suite's own per-call meter when available
  // (window_source "meter"); "transcripts" is the older, much smaller
  // transcript-scan fallback and the sub-label says which one you're seeing.
  const winSub = s.window_source === "meter" ? "suite meter, all sessions" : "modeled from transcripts";
  const tiles = [
    [`Tokens saved (${label})`, fullNum(s.tokens_saved_window), winSub],
    [`$ saved (${label})`, s.usd_saved_window == null ? "n/a" : money(s.usd_saved_window), atRate],
    ["Tokens saved (all-time)", total, "lifetime counter, all sessions", "kpi-alltime"],
    ["$ saved (all-time)", usdTotal, `lifetime · ${atRate}`, "kpi-usd-alltime"],
    [`jCode tool calls (${label})`, fmt(s.calls), "transcript-scanned jCode calls only"],
  ]
    .map((t) => `<div class="kpi"><div class="k-label">${esc(t[0])}</div><div class="k-value"${t[3] ? ` id="${t[3]}"` : ""}>${esc(t[1])}</div><div class="k-sub">${esc(t[2])}</div></div>`)
    .join("");
  // Soft license gate: when no valid suite license is on file the server sends
  // licensed:false, and we blur every per-tool row past the first as an upsell
  // nudge. Treat a missing flag (older server) as licensed — never blur then.
  const locked = s.licensed === false;
  const tools = (s.tool_breakdown || [])
    .map((t, i) => `<tr${locked && i > 0 ? ' class="lic-blur"' : ""}><td class="mono">${esc(t.tool)}</td><td class="num">${fmt(t.tokens)}</td><td class="num">${fmt(t.calls)}</td></tr>`)
    .join("");
  const o = await api("/api/org");
  let orgHtml;
  if (!o.configured) {
    orgHtml = `<div class="card"><div class="card-head"><span class="name">Aggregate savings across seats</span><span class="gated">team SKU</span></div>
       <div class="muted">Set <span class="mono">JCODEMUNCH_ORG_ID</span> and run <span class="mono">jcodemunch-mcp org-report</span> on each seat. Seats report into the org host; this rolls them up.</div></div>`;
  } else {
    const t = o.totals || {};
    const seatRows = (o.seats || [])
      .map((x) => `<tr><td class="mono">${esc(x.seat_id)}</td><td class="num">${fmt(x.tokens_saved)}</td><td class="num">$${(Number(x.usd) || 0).toFixed(2)}</td><td class="num">${fmt(x.calls)}</td></tr>`)
      .join("");
    orgHtml = `<div class="kpis">
        <div class="kpi"><div class="k-label">Org</div><div class="k-value mono" style="font-size:var(--fs-lg)">${esc(o.org_id)}</div></div>
        <div class="kpi"><div class="k-label">Seats</div><div class="k-value">${fmt(t.seat_count)}</div></div>
        <div class="kpi"><div class="k-label">Tokens saved</div><div class="k-value">${fmt(t.tokens_saved)}</div></div>
        <div class="kpi"><div class="k-label">$ avoided</div><div class="k-value">$${(Number(t.usd) || 0).toFixed(2)}</div></div>
      </div>` +
      (seatRows
        ? `<table class="grid-tbl" style="margin-top:var(--sp-3)"><thead><tr><th>seat</th><th class="num">tokens saved</th><th class="num">$ avoided</th><th class="num">calls</th></tr></thead><tbody>${seatRows}</tbody></table>`
        : `<div class="empty">No seats have reported yet.</div>`);
  }
  view.innerHTML =
    head("Savings", "Tokens and dollars saved by jCodeMunch MCP tool calls, modeled from your Claude transcripts. Plain edits, shell, and other tools don't register here.", d._source) +
    `<div class="row savings-controls">${rangeChips(rng)}${modelPicker(s.models, s.model)}</div>` +
    roiHero(roi, s) +
    `<div class="section-title">Inputs — tokens &amp; spend</div>` +
    `<div class="kpis">${tiles}</div>` +
    (s.meter_note ? `<div class="muted" style="margin-top:var(--sp-2)">${esc(s.meter_note)}</div>` : "") +
    `<div class="section-title">Savings per day — ${esc(label)}</div>` +
    // Unlicensed: the CTA displaces the bar graph; the per-tool table below
    // stays but with all rows past the first blurred.
    (locked
      ? `<div class="lic-cta-block">(enter a valid license # in jMunch, LLC Apps to view)</div>`
      : sparkbars(s.series)) +
    `<div class="section-title">By tool — ${esc(label)}</div>` +
    (tools
      ? `<table class="grid-tbl"><thead><tr><th>tool</th><th class="num">tokens saved</th><th class="num">calls</th></tr></thead><tbody>${tools}</tbody></table>`
      : `<div class="empty">No telemetry yet.</div>`) +
    `<div class="section-title">Org rollup${o.configured ? "" : ' <span class="gated">team SKU</span>'}</div>` +
    orgHtml;

  view.querySelectorAll(".range-chips .chip").forEach((b) =>
    b.addEventListener("click", () => {
      if (b.dataset.range === SAVINGS_RANGE) return;
      SAVINGS_RANGE = b.dataset.range;
      renderSavings(); // re-renders (and restarts the live poll) for the new window
    })
  );
  const modelSel = view.querySelector("#savings-model");
  if (modelSel)
    modelSel.addEventListener("change", () => {
      SAVINGS_MODEL = modelSel.value;
      localStorage.setItem("jmunch.savings.model", SAVINGS_MODEL);
      renderSavings();
    });

  // Live updates: poll the cheap lifetime counter so the All-Time tile climbs in
  // near-real-time as jcm runs, without re-paying the receipt transcript scan.
  SAVINGS_LAST_TOTAL = s.tokens_saved_total ?? null;
  SAVINGS_RENDERED_AT = Date.now();
  startSavingsPoll();
}

// --- Live savings polling -------------------------------------------------
// The All-Time tile is jcm's lifetime counter (a tiny `_savings.json` read);
// the windowed tiles/chart come from the expensive `receipt` transcript scan. So we
// poll only the cheap signal on a fast cadence and update the headline in place,
// and refresh the full (receipt-backed) panel only when the counter has actually
// moved AND it's been a while — bounding the heavy scan during active work.
let SAVINGS_POLL = null;
let SAVINGS_LAST_TOTAL = null;
let SAVINGS_RENDERED_AT = 0;
const SAVINGS_POLL_MS = 4000;       // cheap lifetime read cadence
const SAVINGS_RECEIPT_MIN_MS = 30000; // min gap between expensive full refreshes

function stopSavingsPoll() {
  if (SAVINGS_POLL) {
    clearInterval(SAVINGS_POLL);
    SAVINGS_POLL = null;
  }
}

// Set a KPI value element's text in place, with a brief brand-teal flash when it
// actually changes so the live tick is perceptible. No-op if the tile is absent.
function flashSet(id, next) {
  const el = document.getElementById(id);
  if (!el || el.textContent === next) return;
  el.textContent = next;
  el.style.color = "#4cc2a8";
  el.style.transition = "none";
  requestAnimationFrame(() => {
    el.style.transition = "color 0.9s ease-out";
    el.style.color = "";
  });
}

function startSavingsPoll() {
  stopSavingsPoll();
  SAVINGS_POLL = setInterval(async () => {
    // Only poll while the Savings screen is actually showing.
    if ((location.hash || "").slice(1) !== "savings") {
      stopSavingsPoll();
      return;
    }
    const d = await api(`/api/savings/live?model=${encodeURIComponent(SAVINGS_MODEL)}`);
    const total = d && d.tokens_saved_total;
    if (total == null) return;

    // Update the All-Time tokens + dollars tiles in place; both track the
    // lifetime counter, so both tick together with a brief brand-teal flash.
    flashSet("kpi-alltime", fullNum(total));
    if (d.usd_saved_total != null) flashSet("kpi-usd-alltime", money(d.usd_saved_total));

    // Counter moved → the windowed view is stale too, but the receipt scan is heavy,
    // so refresh it at most once per SAVINGS_RECEIPT_MIN_MS. Idle = never fires.
    if (SAVINGS_LAST_TOTAL != null && total !== SAVINGS_LAST_TOTAL &&
        Date.now() - SAVINGS_RENDERED_AT >= SAVINGS_RECEIPT_MIN_MS) {
      renderSavings(); // full refresh (restarts this poll cleanly)
    } else {
      SAVINGS_LAST_TOTAL = total;
    }
  }, SAVINGS_POLL_MS);
}

// --- Claude Usage panel -----------------------------------------------------
// Hybrid: local Claude Code transcripts (instant, this machine) + the org
// Usage/Cost Admin API (authoritative, ~5 min behind). The server caches each
// source on its own cadence (10s local / 60s org / 1h cost), so the UI can
// poll freely without breaching the Admin API's 1-request/minute guidance.
let USAGE_POLL = null;
const USAGE_POLL_MS = 10000;

function stopUsagePoll() {
  if (USAGE_POLL) {
    clearInterval(USAGE_POLL);
    USAGE_POLL = null;
  }
}

// Minute-bucket bar chart (last 60 minutes, oldest on the left).
function minuteBars(minutes, label) {
  const series = minutes || [];
  if (!series.some((v) => Number(v) > 0))
    return `<div class="empty">no ${esc(label)} activity in the last hour</div>`;
  const w = 720, h = 110, pad = 8;
  const max = Math.max(...series.map((v) => Number(v) || 0), 1);
  const bw = (w - pad * 2) / series.length;
  const bars = series
    .map((v, i) => {
      v = Number(v) || 0;
      const bh = (v / max) * (h - pad * 2);
      const x = pad + i * bw;
      const y = h - pad - bh;
      const ago = series.length - 1 - i;
      return `<rect x="${(x + 0.5).toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(1, bw - 1).toFixed(1)}" height="${Math.max(bh, v > 0 ? 2 : 0).toFixed(1)}" rx="1" fill="var(--chart-1)"><title>${ago ? ago + " min ago" : "now"}: ${fmt(v)} tokens</title></rect>`;
    })
    .join("");
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="${esc(label)} tokens per minute">${bars}</svg>
    <div class="row" style="justify-content:space-between"><span class="muted mono" style="font-size:var(--fs-xs)">60 min ago</span><span class="muted mono" style="font-size:var(--fs-xs)">now</span></div>`;
}

function usageModelTable(rows) {
  const tr = (rows || [])
    .map((m) => `<tr><td class="mono">${esc(m.model)}</td><td class="num">${fmt(m.input)}</td><td class="num">${fmt(m.output)}</td><td class="num">${fmt(m.cache_read)}</td><td class="num">${fmt(m.cache_creation)}</td><td class="num">${money(m.usd)}</td></tr>`)
    .join("");
  if (!tr) return `<div class="empty">No usage recorded yet.</div>`;
  return `<table class="grid-tbl"><thead><tr><th>model</th><th class="num">input</th><th class="num">output</th><th class="num">cache read</th><th class="num">cache write</th><th class="num">est. $</th></tr></thead><tbody>${tr}</tbody></table>`;
}

// ---- productivity / cost-per-outcome (durable-change delivery) ----
// The token meters show input (used / saved); this shows output per cost.
// Repo + window selection persist across re-renders.
let DELIVERY_REPO = null;
let DELIVERY_WINDOW = 30;
let DELIVERY_UNIT = "durable"; // ROI unit: durable change | merged PR | closed issue

async function renderDelivery() {
  const repoData = await api("/api/repos");
  const repos = (repoData.repos || []).filter((r) => r.has_source);
  let html = head(
    "Productivity",
    "Cost per outcome: AI spend over a window ÷ the unit of value it bought. Pick the unit — durable change (git), merged PR or closed issue (GitHub). Output for outlay, not raw activity.",
    repoData._source
  );
  if (!repos.length) {
    view.innerHTML = html + `<div class="empty">No indexed repo has a local source path to measure. Index a local folder first.</div>`;
    return;
  }
  if (!DELIVERY_REPO || !repos.some((r) => r.repo_id === DELIVERY_REPO)) DELIVERY_REPO = repos[0].repo_id;
  const repoOpts = repos.map((r) => `<option value="${esc(r.repo_id)}"${r.repo_id === DELIVERY_REPO ? " selected" : ""}>${esc(r.display_name)}</option>`).join("");
  const winOpts = [14, 30, 90].map((w) => `<option value="${w}"${w === DELIVERY_WINDOW ? " selected" : ""}>${w} days</option>`).join("");
  view.innerHTML = html +
    `<div class="row" style="gap:var(--sp-3);margin-bottom:var(--sp-3)">
       <label class="idx-ctl">repo <select id="dlv-repo" class="idx-select">${repoOpts}</select></label>
       <label class="idx-ctl">window <select id="dlv-window" class="idx-select">${winOpts}</select></label></div>
     <div id="dlv-body">${loadingView("productivity")}</div>`;
  document.getElementById("dlv-repo").onchange = (e) => { DELIVERY_REPO = e.target.value; loadDelivery(); };
  document.getElementById("dlv-window").onchange = (e) => { DELIVERY_WINDOW = Number(e.target.value); loadDelivery(); };
  loadDelivery();
}

async function loadDelivery() {
  const body = document.getElementById("dlv-body");
  if (!body) return;
  body.innerHTML = loadingView("productivity");
  const d = await api(`/api/delivery?repo=${encodeURIComponent(DELIVERY_REPO)}&window=${DELIVERY_WINDOW}`);
  if (!d || !d.available) {
    body.innerHTML = `<div class="empty">${esc((d && d.reason) || "Delivery metrics unavailable. Requires jcodemunch-mcp ≥ 1.108.69.")}</div>`;
    return;
  }
  const m = d.metrics || {};
  const meta = m._meta || {};
  const durable = m.commits_durable || 0;
  const attributable = d.cost_attributable;
  // Soft license gate: with no valid suite license on file the server sends
  // licensed:false, and we frost every figure (tile values + subs, table
  // numbers) and swap the chart for a CTA — labels stay crisp so an unlicensed
  // viewer sees what the panel measures, just not the numbers. Missing flag
  // (older server) is treated as licensed — never frost then.
  const locked = d.licensed === false;
  const bl = locked ? " lic-blur-val" : "";
  // ROI by unit: the same attributable spend over the chosen denominator.
  const units = d.units && d.units.length ? d.units : [{ key: "durable", label: "durable change", count: durable, cost_per: d.cost_per_durable }];
  const unit = units.find((u) => u.key === DELIVERY_UNIT) || units[0];
  DELIVERY_UNIT = unit.key;
  const um = d.units_meta || {};
  // Unit selector (only when there's more than one to pick from).
  const unitSel = units.length > 1
    ? `<label class="idx-ctl">cost per <select id="dlv-unit" class="idx-select">${
        units.map((u) => `<option value="${esc(u.key)}"${u.key === DELIVERY_UNIT ? " selected" : ""}>${esc(u.label)}</option>`).join("")
      }</select></label>`
    : "";
  const headlineTile = attributable
    ? [`Cost per ${unit.label}`, unit.cost_per == null ? "n/a" : money(unit.cost_per), `${money(d.cost_usd)} spend / ${fmt(unit.count)} ${esc(unit.label)}${unit.count === 1 ? "" : "s"}`]
    : [`${unit.label.charAt(0).toUpperCase()}${unit.label.slice(1)}s`, fmt(unit.count), `landed this window · last ${m.window_days}d`];
  const tiles = [
    headlineTile,
    ["Durable rate", Math.round((m.durable_rate || 0) * 100) + "%", `${fmt(durable)} of ${fmt(m.commits_total)} commits`],
    ["Rework rate", Math.round((m.rework_rate || 0) * 100) + "%", "reverted or re-touched (churn-back)"],
    attributable
      ? ["AI spend (window)", money(d.cost_usd), `${fmt((d.cost_attribution || {}).matched_sessions)} session(s) · ${fmt((d.cost_attribution || {}).message_events)} msgs`]
      : ["AI spend (window)", "not attributable", "no sessions worked in this repo"],
  ].map((t) => `<div class="kpi"><div class="k-label">${esc(t[0])}</div><div class="k-value${bl}">${esc(t[1])}</div><div class="k-sub${bl}">${esc(t[2])}</div></div>`).join("");

  const cats = Object.entries(m.by_category || {});
  const catRows = cats.length
    ? `<table class="grid-tbl"><thead><tr><th>kind</th><th class="num">durable commits</th></tr></thead><tbody>${
        cats.map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td class="num${bl}">${fmt(v)}</td></tr>`).join("")
      }</tbody></table>`
    : `<div class="empty">No durable commits in this window.</div>`;

  const prov = m.commits_provisional || 0;
  const provNote = prov ? ` <span class="muted">${fmt(prov)} too recent to be final (under the ${m.rework_horizon_days}d horizon).</span>` : "";
  const costNote = attributable ? "" : `<div class="muted" style="margin-top:var(--sp-2)">${esc(d.cost_hint || "")}</div>`;

  // By-unit comparison: same spend, three denominators (Guercio's dashboard).
  const unitRows = units.map((u) =>
    `<tr${u.key === DELIVERY_UNIT ? ' class="unit-active"' : ""}><td>${esc(u.label)}</td><td class="num${bl}">${fmt(u.count)}</td><td class="num${bl}">${attributable && u.cost_per != null ? money(u.cost_per) : "—"}</td><td class="muted">${esc(u.source || "")}</td></tr>`
  ).join("");
  const unitsTable = `<table class="grid-tbl"><thead><tr><th>unit</th><th class="num">count</th><th class="num">cost / unit</th><th>source</th></tr></thead><tbody>${unitRows}</tbody></table>`;
  // Degrade note when the GitHub units are unavailable (no gh / not authed / no remote).
  const ghHint = !um.gh_available
    ? "Install the GitHub CLI (gh) and run gh auth login to add cost per merged PR and closed issue."
    : (um.reason || "");
  const ghNote = (units.length <= 1 && ghHint)
    ? `<div class="muted" style="margin-top:var(--sp-2);font-size:var(--fs-xs)">${esc(ghHint)}</div>` : "";

  body.innerHTML =
    (unitSel ? `<div class="row" style="margin-bottom:var(--sp-3)">${unitSel}</div>` : "") +
    `<div class="kpis">${tiles}</div>` +
    `<div class="section-title">ROI by unit</div>` + unitsTable + ghNote +
    `<div class="section-title">${attributable ? "Cost per durable change over time" : "Durable changes over time"}</div>` +
    // Unlicensed: the CTA displaces the trend chart, mirroring the Savings panel.
    (locked
      ? `<div class="lic-cta-block">(enter a valid license # in jMunch, LLC Apps to view)</div>`
      : deliverySpark(d.series || [], attributable)) +
    `<div class="muted" style="margin-top:2px;font-size:var(--fs-xs)">Trend tracks durable changes only (the daily series the console records); the PR/issue units are point-in-time.</div>` +
    `<div class="section-title">Durable work by kind</div>` + catRows +
    `<div class="muted${bl}" style="margin-top:var(--sp-3)">${esc(m.assessment || "")}${provNote}</div>` +
    costNote +
    `<div class="muted" style="margin-top:var(--sp-2);font-size:var(--fs-xs)">Diagnostic trend, not a score to chase. Rework excludes <span class="${locked ? "lic-blur-val" : ""}">${fmt(meta.hub_files_excluded)}</span> churn-hub file(s); durability is trailing; cost attribution is approximate. PR/issue counts are per-window GitHub totals ÷ the same repo spend — an average, not per-unit attribution.</div>`;

  const unitPick = document.getElementById("dlv-unit");
  if (unitPick) unitPick.onchange = (e) => { DELIVERY_UNIT = e.target.value; loadDelivery(); };
}

// History bars over this repo's daily series. The keyed metric is cost-per-durable
// when spend is attributable, else the durable count.
function deliverySpark(series, attributable) {
  const key = attributable ? "cost_per_durable" : "durable";
  const pts = (series || []).filter((p) => p[key] != null);
  if (!pts.length) return `<div class="empty">a daily snapshot accumulates here each time you open this panel</div>`;
  const w = 720, h = 150, pad = 10;
  const max = Math.max(...pts.map((p) => Number(p[key]) || 0), attributable ? 0.01 : 1);
  const bw = (w - pad * 2) / pts.length;
  const bars = pts.map((p, i) => {
    const v = Number(p[key]) || 0;
    const bh = (v / max) * (h - pad * 2);
    const x = pad + i * bw, y = h - pad - bh;
    const label = attributable ? `${esc(p.date)}: ${money(v)}/durable` : `${esc(p.date)}: ${fmt(v)} durable`;
    return `<rect x="${(x + 1).toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="var(--chart-1)"><title>${label}</title></rect>`;
  }).join("");
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="delivery over time">${bars}</svg>
    <div class="row" style="justify-content:space-between"><span class="muted mono" style="font-size:var(--fs-xs)">${esc(pts[0].date)}</span><span class="muted mono" style="font-size:var(--fs-xs)">${esc(pts[pts.length - 1].date)}</span></div>`;
}

async function renderUsage() {
  const d = await api("/api/usage");
  const local = d.local || {};
  const org = d.org || {};
  const cost = d.cost || {};
  const hour = local.hour || {};
  const day = local.day || {};
  const newHour = (hour.input || 0) + (hour.output || 0) + (hour.cache_creation || 0);
  const newDay = (day.input || 0) + (day.output || 0) + (day.cache_creation || 0);
  const tiles = [
    ["Tokens (last hour)", fullNum(newHour), "this machine · excl. cache reads"],
    ["Tokens (24h)", fullNum(newDay), "this machine · excl. cache reads"],
    ["Cache reads (24h)", fullNum(day.cache_read), "served from prompt cache at 0.1×"],
    ["Est. spend (24h)", money(local.day_usd), "local · at list prices"],
  ]
    .map((t) => `<div class="kpi"><div class="k-label">${esc(t[0])}</div><div class="k-value">${esc(t[1])}</div><div class="k-sub">${esc(t[2])}</div></div>`)
    .join("");

  let orgHtml;
  if (!org.available) {
    orgHtml = `<div class="card"><div class="card-head"><span class="name">Org-wide usage</span><span class="gated">admin key</span></div>
      <div class="muted">${esc(org.reason || "Unavailable.")} The org view reads the Anthropic Usage &amp; Cost Admin API and covers every seat, not just this machine.</div></div>`;
  } else {
    const orgTiles = [
      ["Org est. spend (last hour)", money(org.hour_usd), org.lag_note || ""],
    ]
      .map((t) => `<div class="kpi"><div class="k-label">${esc(t[0])}</div><div class="k-value">${esc(t[1])}</div><div class="k-sub">${esc(t[2])}</div></div>`)
      .join("");
    orgHtml = `<div class="kpis">${orgTiles}</div>` +
      minuteBars(org.minutes, "org") +
      `<div class="section-title">Org by model (last hour)</div>` +
      usageModelTable(org.models);
  }

  let costHtml = "";
  if (cost.available && (cost.days || []).length) {
    const rows = cost.days
      .map((c) => `<tr><td class="mono">${esc(c.date)}</td><td class="num">${money(c.usd)}</td></tr>`)
      .join("");
    costHtml = `<div class="section-title">Org cost — last 7 days (billing truth, daily)</div>
      <table class="grid-tbl"><thead><tr><th>day (UTC)</th><th class="num">cost</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="sub">Week total: ${money(cost.week_usd)}. Local figures above are list-price estimates; this table is what Anthropic actually bills.</p>`;
  }

  // Provider-scoped layout: everything below the "Claude" subheader is
  // Anthropic-specific; future providers (OpenAI, etc.) append their own
  // provider section rather than reworking the panel.
  view.innerHTML =
    head("Token Usage", "What your models are burning right now. Local tiles read this machine's agent transcripts live; org sections read each provider's usage API.", d._source) +
    `<div class="section-title">Claude</div>` +
    `<div class="kpis">${tiles}</div>` +
    `<div class="section-title">This machine — tokens per minute (last hour)</div>` +
    minuteBars(local.minutes, "local") +
    `<div class="section-title">This machine — by model (24h)</div>` +
    usageModelTable(local.models) +
    `<div class="section-title">Org rollup${org.available ? "" : ' <span class="gated">admin key</span>'}</div>` +
    orgHtml +
    costHtml;

  startUsagePoll();
}

function startUsagePoll() {
  stopUsagePoll();
  USAGE_POLL = setInterval(() => {
    if ((location.hash || "").slice(1) !== "usage") {
      stopUsagePoll();
      return;
    }
    renderUsage(); // server-side caches make this cheap; org stays on its 60s cadence
  }, USAGE_POLL_MS);
}

async function resumeSession(session_id, summary) {
  if (!confirm(`Resume session?\n\n${summary || session_id}`)) return;
  let res;
  try {
    res = await (await fetch("/api/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id }),
    })).json();
  } catch (e) {
    res = { error: String(e) };
  }
  if (res.status === "resumed") toast(`resumed in ${res.cwd}`, "ok");
  else toast(res.error || "resume failed", "err");
}

async function renderSessions() {
  const d = await api("/api/sessions");
  LICENSED = d.licensed !== false;
  const rows = (d.sessions || [])
    .map((s, i) => {
      // Unlicensed: only the first three sessions are resumable; the rest get a
      // blurred, disabled resume that prompts for a license on hover (wrapper
      // span so the tooltip shows in Firefox too).
      const gated = !LICENSED && i >= 3;
      let action;
      if (gated) {
        const tip = "Enter a valid license # in jMunch, LLC Apps to enable this.";
        action = `<span class="lic-lock lic-blur-btn" title="${tip}"><button class="btn btn--accent" disabled>resume</button></span>`;
      } else {
        action = LAUNCH_ENABLED
          ? `<button class="btn btn--accent" data-resume="${esc(s.session_id)}" data-summary="${esc(s.summary || "")}">resume</button>`
          : `<span class="gated">resume · phase 3</span><button class="btn" disabled title="Read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable">resume</button>`;
      }
      // message_count is enrichment-only from sessions-index.json and is often
      // absent (the transcript parser deliberately doesn't count, to avoid
      // walking large jsonl files). Show the "N msgs" segment only when we have
      // a real count — never a misleading "0 msgs".
      const msgs = s.message_count ? ` · ${fmt(s.message_count)} msgs` : "";
      return `<div class="session">
        <div class="s-main"><div class="s-title">${esc(s.summary || s.session_id)}</div>
          <div class="s-meta mono">${esc(s.repo_id)} · ${esc((s.started_at || "").slice(0, 16))}${msgs}</div></div>
        ${action}
      </div>`;
    })
    .join("");
  const sub = LAUNCH_ENABLED
    ? "Past agent sessions. Resume opens the session in a new terminal (claude --resume)."
    : "Past agent sessions per repo. Resume is gated (read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable).";
  view.innerHTML = head("Sessions", sub, d._source) + (rows || `<div class="empty">No sessions recorded.</div>`);
  view.querySelectorAll("[data-resume]").forEach((b) => (b.onclick = () => resumeSession(b.dataset.resume, b.dataset.summary)));
}

function toast(msg, kind) {
  const t = document.createElement("div");
  t.className = "toast " + (kind || "");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 5000);
}

async function launchFlow(agent) {
  const d = await api("/api/repos");
  // Only repos with a resolvable on-disk path can be launched into. Remote- or
  // URL-indexed repos (and older indexes that never stored a path) carry an
  // empty source_root and would 404 with 'repo path unavailable'.
  const repos = (d.repos || []).filter((r) => r.has_source);
  if (!repos.length) {
    return toast("no launchable repos — indexed repos have no local source path", "err");
  }
  const opts = repos.map((r) => `<option value="${esc(r.repo_id)}">${esc(r.display_name)}</option>`).join("");
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="modal"><h3>Launch ${esc(agent)}</h3>
      <div class="muted">Opens ${esc(agent)} in a new terminal at the selected repo.</div>
      <select class="enum" id="launch-repo">${opts}</select>
      <div class="actions"><button class="btn btn--ghost" id="launch-cancel">cancel</button>
        <button class="btn btn--accent" id="launch-go">launch</button></div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (e) => { if (e.target === ov) close(); };
  ov.querySelector("#launch-cancel").onclick = close;
  ov.querySelector("#launch-go").onclick = async () => {
    const repo_id = ov.querySelector("#launch-repo").value;
    close();
    let res;
    try {
      res = await (await fetch("/api/launch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent, repo_id }),
      })).json();
    } catch (e) {
      res = { error: String(e) };
    }
    if (res.status === "launched") toast(`launched ${agent} in ${res.cwd}`, "ok");
    else toast(res.error || "launch failed", "err");
  };
}

async function renderLaunch() {
  const d = await api("/api/agents");
  const rows = (d.agents || [])
    .map((a) => {
      let action;
      if (a.launchable === false) {
        // GUI-only client (e.g. Claude Desktop) — no CLI launch path, so don't
        // offer a button that 400s. Show why instead.
        action = `<span class="gated" title="This is a GUI app with no CLI launch command — open it yourself. The console can launch CLI agents like Claude Code.">no CLI launch</span>`;
      } else if (LAUNCH_ENABLED) {
        action = `<button class="btn btn--accent" data-launch="${esc(a.agent)}">launch</button>`;
      } else {
        action = `<span class="gated">launch · phase 3</span><button class="btn" disabled title="Read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable">launch</button>`;
      }
      return `<div class="session">
        <div class="s-main"><div class="s-title">${esc(a.agent)}</div>
          <div class="s-meta mono">${a.wired ? "jMunch wired" : "not wired"}${a.config_path ? " · " + esc(a.config_path) : ""}</div></div>
        <span class="pill pill--${a.wired ? "fresh" : "neutral"}">${a.wired ? "ready" : "not wired"}</span>
        ${action}
      </div>`;
    })
    .join("");
  const sub = LAUNCH_ENABLED
    ? "Detected agent clients. Launch is enabled — opens the agent in a new terminal at a chosen repo."
    : "Detected agent clients. Launch is gated (read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable).";
  view.innerHTML = head("Launch", sub, d._source) + (rows || `<div class="empty">No agent clients detected.</div>`);
  view.querySelectorAll("[data-launch]").forEach((b) => (b.onclick = () => launchFlow(b.dataset.launch)));
}

// ---- live process panel ----
// The MCP servers are spawned by the client, not the console, so this is a
// read-mostly window: see what's running per product (direct binary + jmunch-mcp
// proxy) and stop one. Stopping it lets the client respawn a fresh server.
async function renderProcesses() {
  drawProcesses(await api("/api/processes"));
  startProcessesPoll();
}

// Render-from-data, split out so the poll can redraw without re-deriving the
// fetch path. Stores a signature of the rendered set so the poll only repaints
// when processes actually appear/disappear (no flicker while the list is steady).
function drawProcesses(d) {
  const groups = d.groups || [];
  PROCESSES_SIG = procSig(groups);
  const total = groups.reduce((n, g) => n + (g.count || 0), 0);
  const body = groups
    .map((g) => {
      const rows = (g.procs || [])
        .map((p) => {
          const depth = p.depth || 0;
          const desc = p.descendants || 0;
          // A child is nested under the process that spawned it; the connector +
          // indent + "encloses N" badge make the containment legible at a glance.
          const branch = depth ? `<span class="proc-branch" aria-hidden="true"></span>` : "";
          const encloses = desc
            ? ` <span class="proc-encloses" title="stopping this also stops these">encloses ${desc}</span>`
            : "";
          const stopTitle = LAUNCH_ENABLED
            ? desc
              ? `Stops pid ${p.pid} and the ${desc} process${desc === 1 ? "" : "es"} nested under it`
              : `Stop pid ${p.pid}`
            : "Read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable";
          const stop = LAUNCH_ENABLED
            ? `<button class="btn btn--danger" data-kill="${p.pid}" data-name="${esc(g.name)}" data-desc="${desc}" title="${esc(stopTitle)}">stop</button>`
            : `<button class="btn" disabled title="${esc(stopTitle)}">stop</button>`;
          return `<div class="session proc-row" data-pid="${p.pid}" data-depth="${depth}" style="--depth:${depth}">
            <div class="s-main">
              <div class="s-title">${branch}pid ${p.pid} <span class="pill pill--neutral">${esc(p.kind)}</span>${encloses}</div>
              <div class="s-meta mono">${esc(p.cmd || "")}</div></div>
            ${stop}
          </div>`;
        })
        .join("");
      return (
        `<div class="section-title">${esc(g.name)} · ${g.count} ${g.count === 1 ? "process" : "processes"}</div>` +
        (rows || `<div class="empty">No running ${esc(g.name)} server — start it from your MCP client.</div>`)
      );
    })
    .join("");
  view.innerHTML =
    head(
      "Processes",
      `Live MCP server processes the console can see, grouped by product (${total} running). Each row is nested under the process that spawned it (proxy → server); stopping a parent stops everything beneath it, and your MCP client respawns it on reconnect.`,
      d._source,
    ) + body;
  view.querySelectorAll(".proc-row").forEach((row) => {
    const btn = row.querySelector("[data-kill]");
    if (!btn) return;
    btn.onclick = () =>
      killProcess(parseInt(btn.dataset.kill, 10), btn.dataset.name, btn, parseInt(btn.dataset.desc || "0", 10));
    // Hovering/focusing a stop button previews exactly what it takes down: the
    // row itself plus every process nested under it. Makes the parent→child
    // impact instinctive before the click.
    const kin = procDescendantRows(row);
    const hot = () => {
      row.classList.add("proc-hot-root");
      kin.forEach((r) => r.classList.add("proc-hot"));
    };
    const cool = () => {
      row.classList.remove("proc-hot-root");
      kin.forEach((r) => r.classList.remove("proc-hot"));
    };
    btn.addEventListener("mouseenter", hot);
    btn.addEventListener("mouseleave", cool);
    btn.addEventListener("focus", hot);
    btn.addEventListener("blur", cool);
  });
}

// Rows nested under a given row: the contiguous following .proc-row siblings
// with a deeper depth (the process tree is laid out pre-order, so a parent's
// descendants follow it until depth returns to its level or the group ends).
function procDescendantRows(row) {
  const depth = +row.dataset.depth;
  const out = [];
  let el = row.nextElementSibling;
  while (el && el.classList.contains("proc-row") && +el.dataset.depth > depth) {
    out.push(el);
    el = el.nextElementSibling;
  }
  return out;
}

// --- Live process polling -----------------------------------------------------
// Auto-refresh the panel so a server that's stopped/respawned (by us or the
// client) shows up without a manual reload. The enumeration is a CIM/ps shell-out
// (~1-2s), so the cadence is gentle and it only repaints on an actual change.
let PROCESSES_POLL = null;
let PROCESSES_SIG = "";     // pids-by-product signature of the last paint
let PROCESSES_BUSY = false; // true while a stop is in flight — don't repaint under it
const PROCESSES_POLL_MS = 6000;

function procSig(groups) {
  return (groups || [])
    .map((g) => g.product + ":" + (g.procs || []).map((p) => p.pid).join(","))
    .join("|");
}

function stopProcessesPoll() {
  if (PROCESSES_POLL) {
    clearInterval(PROCESSES_POLL);
    PROCESSES_POLL = null;
  }
}

function startProcessesPoll() {
  stopProcessesPoll();
  PROCESSES_POLL = setInterval(async () => {
    if ((location.hash || "").slice(1) !== "processes") {
      stopProcessesPoll(); // left the screen
      return;
    }
    if (PROCESSES_BUSY) return; // a stop is mid-flight; let it finish + redraw
    let d;
    try {
      d = await api("/api/processes");
    } catch (e) {
      return; // transient; try again next tick
    }
    if (procSig(d.groups || []) !== PROCESSES_SIG) drawProcesses(d);
  }, PROCESSES_POLL_MS);
}

// --- Logging: crash/perf log tail + capture controls --------------------------
// Read-only view over the same artifacts the customer log-recipe collects:
// watcher logs (jcw_<pid>.log), an explicit JCODEMUNCH_LOG_FILE, and jcm's
// heartbeat badge. Polls a few seconds so a tail stays live.
async function renderDiagnostics() {
  drawDiagnostics(await api("/api/diagnostics"));
  startDiagnosticsPoll();
}

function diagBadge(label, value, tone) {
  return `<span class="diag-badge diag-badge--${tone}"><span class="diag-badge-k">${esc(label)}</span><span class="diag-badge-v">${esc(value)}</span></span>`;
}

function drawDiagnostics(d) {
  if (d && d.error) {
    view.innerHTML = head("Logging", "Couldn't load logging.", "error") +
      `<div class="empty">${esc(d.error)}</div>`;
    DIAG_SIG = "err";
    return;
  }
  const sig = d.signals || {};
  const logs = d.logs || [];
  const hints = d.hints || [];
  DIAG_SIG = diagSig(d);

  const hb = sig.heartbeat ? `${Math.round(sig.heartbeat.age_s)}s ago` : "none yet";
  const fileLogOn = !!(sig.log_file_env || sig.server_logging);
  // Perf telemetry has three states: persisting (db on disk), enabled-in-config
  // but the server hasn't restarted to write it yet (pending), or off.
  const perfOn = !!sig.perf_telemetry_db;
  const perfPending = !perfOn && !!sig.perf_telemetry_enabled;
  const badges = [
    diagBadge("file logging", fileLogOn ? "on" : "off", fileLogOn ? "ok" : "off"),
    diagBadge("watcher", sig.watcher_running ? "running" : "off", sig.watcher_running ? "ok" : "off"),
    diagBadge("watcher logs", String(sig.watcher_logs || 0), sig.watcher_logs ? "ok" : "off"),
    diagBadge("perf telemetry", perfOn ? "on" : perfPending ? "pending restart" : "off",
      perfOn ? "ok" : perfPending ? "warn" : "off"),
    diagBadge("jcm last activity", hb, sig.heartbeat ? "ok" : "off"),
  ].join("");

  // Watcher + server-logging buttons toggle to their stop/disable counterpart
  // when the service is active (state from sig.watcher_running / sig.server_logging).
  const watcherBtn = sig.watcher_running
    ? `<button class="btn btn--danger" id="diag-watcher-stop" title="Terminate the running watch-all process(es)">Stop watcher</button>`
    : `<button class="btn" id="diag-watcher" title="Open a terminal watching every indexed repo, logging to a file below">Start watcher + logging</button>`;
  const srvlogBtn = sig.server_logging
    ? `<button class="btn btn--danger" id="diag-srvlog-off" title="Unset log_file config + restart the jCodeMunch server so it stops file logging">Disable server logging</button>`
    : `<button class="btn" id="diag-srvlog" title="Set log_file config + restart the jCodeMunch server (needs jcodemunch-mcp 1.108.64+)">Enable server logging</button>`;
  const clearBtn = `<button class="btn" id="diag-clearlogs" title="Delete the console-managed capture logs + per-PID watcher logs (jcw_*.log) in your temp folder. Logs in use are left in place; index/telemetry/savings data untouched.">Clear logs</button>`;
  const actionsHtml = LAUNCH_ENABLED
    ? `<div class="diag-actions">${watcherBtn}${srvlogBtn}${clearBtn}</div>`
    : `<div class="diag-actions"><span class="muted">Read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to capture logs from here.</span></div>`;

  const hintHtml = hints.length
    ? `<div class="diag-hints">${hints.map((h) => `<div class="diag-hint">${esc(h)}</div>`).join("")}</div>`
    : "";

  const logHtml = logs.length
    ? logs.map((f) => {
        const counts = [
          f.errors ? `<span class="logcount logcount--error">${f.errors} err</span>` : "",
          f.warnings ? `<span class="logcount logcount--warn">${f.warnings} warn</span>` : "",
        ].join("");
        const lines = (f.lines || [])
          .map((ln) => `<div class="logline logline--${esc(ln.level)}">${esc(ln.text)}</div>`)
          .join("") || `<div class="logline logline--info">(empty)</div>`;
        return `<div class="logcard">
            <div class="logcard-head">
              <span class="logcard-name mono">${esc(f.name)}</span>
              <span class="pill pill--neutral">${esc(f.kind)}</span>
              ${counts}
              <span class="logcard-meta">${fmt(f.size)}B · ${esc(diagAge(f.age_s))}</span>
            </div>
            <pre class="logtail" tabindex="0">${lines}</pre>
            <div class="logcard-path mono">${esc(f.path)}</div>
          </div>`;
      }).join("")
    : `<div class="empty">No jCodeMunch logs found. Set JCODEMUNCH_LOG_FILE, or run watch / watch-all so the watcher writes jcw_&lt;pid&gt;.log to your temp folder.</div>`;

  view.innerHTML =
    head("Logging",
      `Crash and indexing logs, read from this machine only. Nothing leaves your computer. Index store: ${esc(sig.index_dir || "")}`,
      d._source) +
    `<div class="diag-signals">${badges}</div>` +
    actionsHtml +
    hintHtml +
    `<div class="section-title">Logs · ${logs.length}</div>` + logHtml;

  // Tail newest-at-bottom: keep each log scrolled to the latest line.
  view.querySelectorAll(".logtail").forEach((pre) => { pre.scrollTop = pre.scrollHeight; });

  view.querySelector("#diag-watcher")?.addEventListener("click", startWatcher);
  view.querySelector("#diag-watcher-stop")?.addEventListener("click", stopWatcher);
  view.querySelector("#diag-srvlog")?.addEventListener("click", enableServerLogging);
  view.querySelector("#diag-srvlog-off")?.addEventListener("click", disableServerLogging);
  view.querySelector("#diag-clearlogs")?.addEventListener("click", clearLogs);
}

// Re-render the panel shortly after an action so its new state (watcher up, a
// fresh log file) shows without waiting for the next poll tick.
function refreshDiagnosticsSoon(ms) {
  setTimeout(() => { if ((location.hash || "").slice(1) === "logging") renderDiagnostics(); }, ms);
}

async function startWatcher() {
  if (!confirm("Start the jCodeMunch watcher?\n\nIt opens a terminal that watches every indexed repo and logs indexing activity to a file shown below. Close that terminal to stop it.")) return;
  let res;
  try {
    res = await (await fetch("/api/start-watcher", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).json();
  } catch (e) { res = { error: String(e) }; }
  if (res.status === "watcher_started") toast("watcher started — reindex activity now logs below", "ok");
  else if (res.status === "already_running") toast("a watcher is already running", "ok");
  else toast((res.error || "couldn't start the watcher") + (res.hint ? ` — ${res.hint}` : ""), "err");
  refreshDiagnosticsSoon(1500);
}

async function enableServerLogging() {
  if (!confirm("Enable jCodeMunch server logging?\n\nThis sets the log_file config and restarts the jCodeMunch server so it logs to a file. Needs jcodemunch-mcp 1.108.64+; older servers ignore it (set JCODEMUNCH_LOG_FILE in your MCP config instead).")) return;
  let res;
  try {
    res = await (await fetch("/api/enable-server-logging", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).json();
  } catch (e) { res = { error: String(e) }; }
  if (res.status === "server_logging_enabled") toast(`server logging on — ${res.note || ""}`, "ok");
  else toast((res.error || "couldn't enable server logging") + (res.hint ? ` — ${res.hint}` : ""), "err");
  refreshDiagnosticsSoon(2500);
}

async function stopWatcher() {
  if (!confirm("Stop the jCodeMunch watcher?\n\nThis terminates the running watch-all process(es). Indexed repos stay indexed — you can start the watcher again any time.")) return;
  let res;
  try {
    res = await (await fetch("/api/stop-watcher", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).json();
  } catch (e) { res = { error: String(e) }; }
  if (res.status === "watcher_stopped") toast(`watcher stopped (${res.stopped} process${res.stopped === 1 ? "" : "es"})`, "ok");
  else if (res.status === "not_running") toast("no watcher was running", "ok");
  else toast((res.error || "couldn't stop the watcher") + (res.hint ? ` — ${res.hint}` : ""), "err");
  refreshDiagnosticsSoon(1200);
}

async function disableServerLogging() {
  if (!confirm("Disable jCodeMunch server logging?\n\nThis unsets the log_file config and restarts the jCodeMunch server so it stops writing the log file.")) return;
  let res;
  try {
    res = await (await fetch("/api/disable-server-logging", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).json();
  } catch (e) { res = { error: String(e) }; }
  if (res.status === "server_logging_disabled") toast(`server logging off — ${res.note || ""}`, "ok");
  else toast((res.error || "couldn't disable server logging") + (res.hint ? ` — ${res.hint}` : ""), "err");
  refreshDiagnosticsSoon(2500);
}

async function clearLogs() {
  if (!confirm("Clear jCodeMunch logs?\n\nDeletes the console-managed capture logs and per-PID watcher logs (jcw_*.log) from your temp folder. Logs in use by a running watcher/server are left in place. Your index, telemetry, and savings data are untouched.")) return;
  let res;
  try {
    res = await (await fetch("/api/clear-logs", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).json();
  } catch (e) { res = { error: String(e) }; }
  if (res.status === "logs_cleared") {
    const n = (res.removed || []).length;
    const sk = (res.skipped || []).length;
    toast(`cleared ${n} log file${n === 1 ? "" : "s"}${sk ? ` (${sk} in use, left)` : ""}`, "ok");
  } else toast((res.error || "couldn't clear logs") + (res.hint ? ` — ${res.hint}` : ""), "err");
  refreshDiagnosticsSoon(800);
}

function diagAge(s) {
  s = Math.round(Number(s) || 0);
  if (s < 90) return s + "s ago";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
}

// Repaint only when something actually moved: any log's size/error/warn counts
// or a signal toggle. Avoids yanking scroll/selection on a steady tail.
function diagSig(d) {
  const sg = d.signals || {};
  const l = (d.logs || []).map((f) => `${f.name}:${f.size}:${f.errors}:${f.warnings}`).join(",");
  const h = (d.hints || []).length;
  return `${l}|${h}|${sg.watcher_running ? 1 : 0}|${sg.server_logging ? 1 : 0}|${sg.log_file_env || ""}` +
    `|${sg.perf_telemetry_db ? 1 : 0}|${sg.perf_telemetry_enabled ? 1 : 0}`;
}

let DIAG_POLL = null;
let DIAG_SIG = "";
const DIAG_POLL_MS = 5000;

function stopDiagnosticsPoll() {
  if (DIAG_POLL) {
    clearInterval(DIAG_POLL);
    DIAG_POLL = null;
  }
}

function startDiagnosticsPoll() {
  stopDiagnosticsPoll();
  DIAG_POLL = setInterval(async () => {
    if ((location.hash || "").slice(1) !== "logging") {
      stopDiagnosticsPoll(); // left the screen
      return;
    }
    let d;
    try {
      d = await api("/api/diagnostics");
    } catch (e) {
      return; // transient; try again next tick
    }
    if (diagSig(d) !== DIAG_SIG) drawDiagnostics(d);
  }, DIAG_POLL_MS);
}

async function killProcess(pid, name, btn, desc = 0) {
  const nested = desc
    ? `\n\nThis also stops the ${desc} process${desc === 1 ? "" : "es"} nested under it.`
    : "";
  if (!confirm(`Stop ${name} process ${pid}?${nested}\n\nYour MCP client respawns it on its next reconnect or tool call.`)) return;
  setBusy(true);
  PROCESSES_BUSY = true; // pause the poll so it can't wipe the busy button mid-stop
  let prevHtml;
  if (btn) {
    prevHtml = btn.innerHTML;
    btn.disabled = true;
    btn.classList.add("btn--busy");
    btn.innerHTML = `<span class="spinner spinner--sm" role="status" aria-label="Stopping"></span>stopping…`;
  }
  try {
    let res;
    try {
      res = await (
        await fetch("/api/kill-process", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pid }),
        })
      ).json();
    } catch (e) {
      res = { error: String(e) };
    }
    if (res.status === "killed") {
      toast(`${name} process ${pid} stopped — ${res.hint || "your MCP client respawns it on reconnect"}`, "ok");
      renderProcesses(); // rebuild the list (drops the stopped row, replaces the busy button)
      return;
    }
    toast((res.error || "stop failed") + (res.hint ? ` — ${res.hint}` : ""), "err");
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("btn--busy");
      btn.innerHTML = prevHtml;
    }
  } finally {
    setBusy(false);
    PROCESSES_BUSY = false;
  }
}

// ---- alerts (notify-only thresholds over signals the panels already track) ----
// State word + status-dot class for one evaluated alert. The dot reuses the
// shared .sdot palette so Alerts reads the same as every other health signal.
const ALERT_STATE = {
  ok:     { dot: "ok",   word: "OK" },
  warn:   { dot: "warn", word: "Approaching" },
  breach: { dot: "bad",  word: "Over threshold" },
  nodata: { dot: "off",  word: "No data yet" },
  off:    { dot: "off",  word: "Off" },
};
const ALERT_UNIT = { usd: "USD", tokens: "tokens", min: "minutes", count: "errors", x: "× normal" };

function alertValue(unit, v) {
  if (v == null) return "—";
  if (unit === "usd") return money(v);
  if (unit === "tokens") return fmt(v);
  if (unit === "min") return v < 60 ? `${Math.round(v)}m` : `${(v / 60).toFixed(1)}h`;
  if (unit === "x") return `${Number(v).toFixed(1)}×`;
  return fullNum(v);
}

function alertCard(a) {
  const st = ALERT_STATE[a.state] || ALERT_STATE.off;
  const cmp = a.compare === "gt" ? "over" : "under";
  const now = a.enabled ? alertValue(a.unit, a.value) : "—";
  return `<div class="alert-card alert--${esc(a.state)}">
      <div class="alert-head">
        <span class="sdot ${st.dot}" title="${esc(st.word)}"></span>
        <div class="alert-text">
          <div class="alert-label">${esc(a.label)} <span class="alert-state">${esc(st.word)}</span></div>
          <div class="desc">${esc(a.desc)}</div>
        </div>
        <button class="switch" role="switch" aria-checked="${a.enabled}" aria-label="${esc(a.label)}" data-alertkey="${esc(a.id)}"></button>
      </div>
      <div class="alert-foot">
        <span class="alert-now">now <strong>${esc(now)}</strong></span>
        <span class="alert-thr">warn ${cmp}
          <input class="field alert-edit" type="number" min="0" step="any" value="${esc(a.threshold)}" data-alertthr="${esc(a.id)}" aria-label="${esc(a.label)} threshold" />
          <span class="alert-unit">${esc(ALERT_UNIT[a.unit] || a.unit)}</span>
          <button class="cfg-btn alert-save" data-alertsave="${esc(a.id)}" disabled>save</button>
        </span>
      </div>
    </div>`;
}

function drawAlerts(d) {
  if (d.error) {
    view.innerHTML = head("Alerts", "", "error") +
      `<div class="empty">Couldn't load alerts. ${esc(d.error)}</div>`;
    return;
  }
  const alerts = d.alerts || [];
  const defaults = alerts.filter((a) => a.tier !== "advanced");
  const advanced = alerts.filter((a) => a.tier === "advanced");
  const sub = "Notify-only thresholds over what the panels already track. Nothing here touches your servers — alerts only tell you when a line is crossed.";
  const breach = Number(d.breach_count) || 0;
  const warn = Number(d.warn_count) || 0;
  const summary = breach
    ? `<div class="alert-summary breach">${breach} alert${breach > 1 ? "s" : ""} over threshold</div>`
    : warn
    ? `<div class="alert-summary warn">${warn} approaching its threshold</div>`
    : `<div class="alert-summary ok">All clear</div>`;
  view.innerHTML =
    head("Alerts", sub, d._source) +
    summary +
    `<div class="alert-grid">${defaults.map(alertCard).join("")}</div>` +
    (advanced.length
      ? `<details class="cfg-group alert-advanced"><summary>Advanced<span class="cfg-count">${advanced.length}</span></summary>
           <div class="cfg-body"><div class="alert-grid">${advanced.map(alertCard).join("")}</div></div></details>`
      : "");
  wireAlerts();
  applyAlertSignals(d); // keep the nav badge + banner in step with what's drawn
}

function wireAlerts() {
  view.querySelectorAll(".switch[data-alertkey]").forEach((btn) => {
    btn.onclick = () => saveAlert(btn.dataset.alertkey, { enabled: btn.getAttribute("aria-checked") !== "true" });
  });
  view.querySelectorAll("input[data-alertthr]").forEach((inp) => {
    const save = view.querySelector(`.alert-save[data-alertsave="${inp.dataset.alertthr}"]`);
    if (!save) return;
    inp.oninput = () => { save.disabled = false; };
    const commit = () => saveAlert(inp.dataset.alertthr, { threshold: Number(inp.value) });
    save.onclick = commit;
    inp.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } };
  });
}

async function saveAlert(id, body) {
  let res;
  try {
    res = await (await fetch("/api/alert-set", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, ...body }),
    })).json();
  } catch (e) { res = { error: String(e) }; }
  if (res.status === "set") {
    toast(`saved ${id.replace(/_/g, " ")}`, "ok");
    const d = await api("/api/alerts"); // re-evaluate so the value/state reflect the new line
    if (!d.error) drawAlerts(d);
  } else {
    toast(res.error || "save failed", "err");
  }
}

let ALERTS_POLL = null;
const ALERTS_POLL_MS = 20000;
function stopAlertsPoll() {
  if (ALERTS_POLL) { clearInterval(ALERTS_POLL); ALERTS_POLL = null; }
}
function startAlertsPoll() {
  stopAlertsPoll();
  ALERTS_POLL = setInterval(async () => {
    if ((location.hash || "").slice(1) !== "alerts") { stopAlertsPoll(); return; }
    // Don't redraw out from under an in-progress threshold edit.
    const f = document.activeElement;
    if (f && f.classList && f.classList.contains("alert-edit")) return;
    let d;
    try { d = await api("/api/alerts"); } catch (e) { return; }
    if (!d.error) drawAlerts(d);
  }, ALERTS_POLL_MS);
}

async function renderAlerts() {
  const d = await api("/api/alerts");
  drawAlerts(d);
  startAlertsPoll();
}

// Always-on watch (independent of the current screen) so the nav badge and the
// in-Console banner surface a breach from anywhere, not only on the Alerts tab.
let ALERTS_WATCH = null;
const ALERTS_WATCH_MS = 30000;
function startAlertWatch() {
  if (ALERTS_WATCH) return;
  const tick = async () => {
    const d = await api("/api/alerts");
    if (!d.error) applyAlertSignals(d);
  };
  tick();
  ALERTS_WATCH = setInterval(tick, ALERTS_WATCH_MS);
}

function applyAlertSignals(d) {
  updateAlertBadge(d);
  updateAlertBanner(d);
}

function updateAlertBadge(d) {
  const btn = nav.querySelector('.nav-item[data-id="alerts"]');
  if (!btn) return;
  const n = Number(d.breach_count) || 0;
  let badge = btn.querySelector(".nav-badge");
  if (n > 0) {
    if (!badge) { badge = document.createElement("span"); badge.className = "nav-badge"; btn.appendChild(badge); }
    badge.textContent = String(n);
    btn.classList.add("has-alert");
  } else {
    if (badge) badge.remove();
    btn.classList.remove("has-alert");
  }
}

function updateAlertBanner(d) {
  const n = Number(d.breach_count) || 0;
  let b = document.getElementById("alert-banner");
  if (n > 0) {
    const labels = (d.alerts || []).filter((a) => a.state === "breach").map((a) => a.label);
    const txt = `${n} alert${n > 1 ? "s" : ""} over threshold: ${labels.join(", ")}`;
    if (!b) {
      b = document.createElement("div");
      b.id = "alert-banner";
      b.className = "alert-banner";
      b.onclick = () => go("alerts");
      document.body.appendChild(b);
    }
    b.innerHTML = `<span class="alert-banner-dot"></span><span class="alert-banner-msg">${esc(txt)}</span><span class="alert-banner-go">view →</span>`;
  } else if (b) {
    b.remove();
  }
}

// ---- Help chat (read-only "Ask" bot, backed by the user's local Claude) ----
let CHAT_SESSION = null; // session id from the last reply; echoed back to continue

// Minimal, XSS-safe markdown: escape first, then format on the escaped text.
function mdLite(s) {
  let t = esc(String(s ?? ""));
  t = t.replace(/```(\w+)?\n?([\s\S]*?)```/g, (m, _lang, code) => `<pre class="chat-code">${code.replace(/\n$/, "")}</pre>`);
  t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return t.replace(/\n/g, "<br>");
}

function chatAppend(who, text, pending) {
  const log = document.getElementById("chat-log");
  const wrap = document.createElement("div");
  wrap.className = "chat-msg " + who;
  const body = pending
    ? `<span class="chat-typing"><i></i><i></i><i></i></span>`
    : who === "user" ? esc(text) : mdLite(text);
  wrap.innerHTML = `<div class="chat-bubble">${body}</div>`;
  log.appendChild(wrap);
  log.scrollTop = 1e9;
  return wrap;
}

async function renderHelp() {
  const cap = await api("/api/chat-capability");
  if (!cap || !cap.available) {
    view.innerHTML = head("Help", "In-console assistant", "live") +
      `<div class="empty">${esc((cap && cap.hint) || "Help chat is unavailable.")}</div>`;
    return;
  }
  CHAT_SESSION = null;
  const authNote = cap.auth === "subscription"
    ? "on your Claude subscription" : "on your Anthropic API key (billed per token)";
  view.innerHTML = head("Help",
    "Ask how to install, configure, or use the console and the jMunch suite. Answers read the real source on this machine. Read-only — it never changes your setup.", "live") +
    `<div class="chat">
       <div id="chat-log" class="chat-log">
         <div class="chat-msg bot"><div class="chat-bubble">Hi! I'm the jMunch Console assistant. Ask me anything about installing, configuring, or using the console and the suite. I read the real code on your machine, so I can be specific — and I'm read-only, so I won't touch your setup.</div></div>
       </div>
       <form id="chat-form" class="chat-composer">
         <textarea id="chat-input" rows="2" placeholder="e.g. How do I turn on the file watcher? What does fixtures mode do?" autocomplete="off"></textarea>
         <button class="btn btn--accent" type="submit" id="chat-send">Send</button>
       </form>
       <div class="chat-foot muted">${esc(cap.model || "claude")} · ${esc(authNote)} · read-only</div>
     </div>`;
  wireChat();
}

function wireChat() {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const send = document.getElementById("chat-send");
  if (!form) return;
  const submit = async () => {
    const msg = input.value.trim();
    if (!msg) return;
    input.value = "";
    chatAppend("user", msg);
    const pend = chatAppend("bot", "", true);
    send.disabled = input.disabled = true;
    let res;
    try {
      res = await (await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg, session_id: CHAT_SESSION }),
      })).json();
    } catch (e) { res = { error: String(e) }; }
    send.disabled = input.disabled = false;
    input.focus();
    if (res && !res.error && res.reply !== undefined) {
      CHAT_SESSION = res.session_id || CHAT_SESSION;
      pend.querySelector(".chat-bubble").innerHTML = mdLite(res.reply || "(no answer)");
    } else {
      pend.classList.add("err");
      const m = (res && (res.error || res.hint)) || "chat failed";
      pend.querySelector(".chat-bubble").innerHTML = esc(m);
    }
    document.getElementById("chat-log").scrollTop = 1e9;
  };
  form.onsubmit = (e) => { e.preventDefault(); submit(); };
  // Enter sends; Shift+Enter inserts a newline.
  input.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } };
  input.focus();
}

// ---- nav + routing ----
async function go(id) {
  if (id === "diagnostics") id = "logging"; // back-compat: the screen was renamed from #diagnostics
  stopSavingsPoll(); // leaving any screen halts the savings live-poll; renderSavings restarts it
  stopAlertsPoll(); // and the Alerts on-screen poll; renderAlerts restarts it (the global watch keeps running)
  stopUsagePoll();   // same for the Claude Usage poll; renderUsage restarts it
  stopProcessesPoll(); // and the Processes auto-refresh; renderProcesses restarts it
  stopDiagnosticsPoll(); // and the Diagnostics log/liveness poll; renderDiagnostics restarts it
  const screen = SCREENS.find((s) => s.id === id) || SCREENS[0];
  nav.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.id === screen.id));
  const gear = document.getElementById("config-gear");
  if (gear) gear.classList.toggle("active", screen.id === "config");
  location.hash = screen.id;
  view.innerHTML = loadingView(screen.label); // immediate feedback before the slow fetch
  setBusy(true);
  try {
    await screen.render();
  } catch (e) {
    view.innerHTML = `<div class="empty">Couldn't load ${esc(screen.label)}. ${esc(String(e))}</div>`;
  } finally {
    setBusy(false);
  }
}

function buildNav() {
  nav.innerHTML =
    SCREENS.filter((s) => !s.gear).map((s) => `<button class="nav-item" data-id="${s.id}">${esc(s.label)}</button>`).join("") +
    `<div class="rail" id="prodrail"></div>`;
  nav.querySelectorAll(".nav-item").forEach((b) => (b.onclick = () => go(b.dataset.id)));
}

// Single-pane suite health roll-up at the top of the rail: one glance tells a
// power user (demographic #2) whether anything needs attention without scanning
// every row. Pure client-side aggregate of the products payload.
function railSummary(products) {
  const total = products.length;
  const installed = products.filter((p) => p.installed);
  const notInstalled = total - installed.length;
  const updates = products.filter((p) => p.update_available).length;
  // "unlicensed" = installed but no valid key (none/invalid). An unverified
  // "entered" key (server unreachable) is ambiguous, so it isn't flagged here.
  const unlicensed = installed.filter((p) => p.license === "none" || p.license === "invalid").length;
  // Updates and license gaps drive the headline dot; not-installed is neutral.
  const dot = !installed.length ? "off" : updates ? "update" : unlicensed ? "warn" : "ok";
  let text;
  if (!installed.length) {
    text = "Nothing installed yet";
  } else if (!updates && !unlicensed) {
    text = `All set — ${installed.length} up to date & licensed`;
  } else {
    const parts = [];
    if (updates) parts.push(`${updates} update${updates > 1 ? "s" : ""} available`);
    if (unlicensed) parts.push(`${unlicensed} unlicensed`);
    text = parts.join(" · ");
  }
  const sub = notInstalled ? ` <span class="rail-summary-sub">· ${notInstalled} not installed</span>` : "";
  return `<div class="rail-summary" title="Suite health at a glance">
      <span class="sdot ${dot}"></span><span class="rail-summary-text">${esc(text)}${sub}</span></div>`;
}

// Persistent per-product status in the sidebar: installed? + license entered?
// `fresh` busts the server's 60s cache so the dots re-probe on manual refresh.
async function renderProducts(fresh = false) {
  const rail = document.getElementById("prodrail");
  if (!rail) return;
  const [d, oa] = await Promise.all([
    api("/api/products" + (fresh ? "?fresh=1" : "")),
    api("/api/other-apps" + (fresh ? "?fresh=1" : "")),
  ]);
  PRODUCTS = d.products || [];
  OTHER_APPS = oa.apps || [];
  const dotClass = { valid: "ok", invalid: "bad", entered: "warn", none: "off" };
  const licText = { valid: "licensed", invalid: "invalid key", entered: "key entered (unverified)", none: "no license key" };
  rail.innerHTML =
    `<div class="rail-title"><span><span style="text-transform:none">j</span>Munch, LLC Apps</span>` +
    `<button class="rail-refresh" id="prod-refresh" title="Re-check installs and licenses" aria-label="Refresh product status">↻</button></div>` +
    railSummary(d.products || []) +
    // ROI at a glance, filled in async by loadRailRoi() so the (heavier)
    // suite-wide delivery walk never blocks the rail render.
    `<a class="rail-roi" id="rail-roi" href="#savings" title="Cost per durable change across the suite — ROI, not tokens saved">` +
    `<span class="rail-roi-label">ROI</span><span class="rail-roi-val" id="rail-roi-val">…</span></a>` +
    (d.products || [])
      .map((p) => {
        const lic = dotClass[p.license] || "off";
        const licLabel = licText[p.license] || p.license;
        const licTip = licLabel + (p.tier ? ` · ${p.tier}` : "") + (p.key_masked ? ` · ${p.key_masked}` : "");
        // Install dot: off → broken → dev (editable) → update (newer release) → ok.
        const instClass = !p.installed ? "off" : p.broken ? "bad" : p.editable ? "dev" : p.update_available ? "update" : "ok";
        const instTip = !p.installed
          ? "not installed — click to install"
          : p.broken
          ? "broken install — launcher present but package missing; open menu to reinstall"
          : p.editable
          ? `dev checkout${p.version ? ` · v${p.version}` : ""}` +
            (p.behind ? ` → v${p.latest_version} · git pull to update` : " (latest)")
          : p.update_available
          ? `update available · v${p.version || "?"} → v${p.latest_version}`
          : p.version
          ? `installed · v${p.version} (latest)`
          : "installed";
        const upd = p.installed && p.update_available; // editable already excluded server-side
        // The install dot is clickable both to install (not yet installed) and
        // to upgrade (installed + update available). The row's own handler routes
        // the not-installed case, so only the upgrade dot needs its own handler.
        const instClickable = upd || !p.installed;
        // Tier badge inline next to the name, but only once a key actually validates.
        const tierTag = p.license === "valid" && p.tier ? ` <span class="tier">${esc(p.tier)}</span>` : "";
        const rowTitle = p.installed ? `Click for ${p.name} options` : `Click to install ${p.name}`;
        return `<div class="prod" data-prod="${esc(p.id)}" data-name="${esc(p.name)}" data-installed="${p.installed ? 1 : 0}" title="${esc(rowTitle)}">
            <span class="prod-name">${esc(p.name)}${tierTag}</span>
            <span class="dots">
              <span class="sdot ${instClass}${instClickable ? " clickable" : ""}"${upd ? ` data-upgrade="${esc(p.id)}"` : ""} role="img" aria-label="install: ${esc(instTip)}" title="${esc(instTip)}${upd ? " — click to update" : ""}"></span>
              <span class="sdot ${lic}" role="img" aria-label="license: ${esc(licTip)}" title="${esc(licTip)}"></span>
            </span></div>`;
      })
      .join("") +
    // New-user front door: one click to install whatever's still missing.
    (((d.products || []).filter((p) => !p.installed).length) >= 2
      ? `<button class="btn rail-install-all" id="install-all">Install all (${(d.products || []).filter((p) => !p.installed).length})</button>`
      : "") +
    // Curated third-party companions (hand-picked from the versus.php
    // comparisons that complement the suite rather than compete with it).
    // The app NAME describes itself on hover and opens the repo on click;
    // installing happens ONLY via the explicit button — never a row click.
    `<div class="rail-divider"></div>` +
    `<div class="rail-title">Compatible Apps</div>` +
    OTHER_APPS.map((a) => {
      const instClass = !a.installed ? "off" : a.update_available ? "update" : "ok";
      const instTip = !a.installed
        ? `not installed — click to install ${a.name}`
        : a.update_available
        ? `update available · ${a.version} → ${a.latest_version} — click for options`
        : (a.version
        ? `installed · ${a.version} (latest)`
        : "installed · version untracked (installed outside the console)") + " — click for options";
      // The status dot IS the action button (same as the products rail):
      // grey = click to install; green/orange = click for update/uninstall.
      const act = a.installed
        ? `data-othermenu="${esc(a.id)}"`
        : `data-otherinstall="${esc(a.id)}"`;
      return `<div class="prod prod--static" data-other="${esc(a.id)}">
          <span class="prod-name app-link" data-otherrepo="${esc(a.url)}" title="${esc(a.description || "")}\n\nClick to open ${esc(a.url)}">${esc(a.name)}</span>
          <span class="dots"><span class="sdot ${instClass} clickable" ${act} role="img" aria-label="install: ${esc(instTip)}" title="${esc(instTip)}"></span></span></div>`;
    }).join("");
  rail.querySelectorAll("div.prod[data-prod]").forEach(
    (el) =>
      (el.onclick = () =>
        el.dataset.installed === "0"
          ? installFlow(el.dataset.prod, el.dataset.name)
          : productMenu(el.dataset.prod))
  );
  const installAll = document.getElementById("install-all");
  if (installAll) installAll.onclick = installAllFlow;
  rail.querySelectorAll("[data-upgrade]").forEach(
    (el) =>
      (el.onclick = (e) => {
        e.stopPropagation(); // don't also open the license prompt
        upgradeFlow(el.dataset.upgrade, el.closest(".prod").dataset.name);
      })
  );
  rail.querySelectorAll("[data-otherrepo]").forEach((el) => {
    el.onclick = (e) => { e.stopPropagation(); window.open(el.dataset.otherrepo, "_blank", "noopener"); };
  });
  rail.querySelectorAll("[data-otherinstall]").forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      const a = OTHER_APPS.find((x) => x.id === el.dataset.otherinstall);
      if (a) otherInstallFlow(a);
    };
  });
  rail.querySelectorAll("[data-othermenu]").forEach((el) => {
    el.onclick = (e) => { e.stopPropagation(); otherAppMenu(el.dataset.othermenu); };
  });
  const refresh = document.getElementById("prod-refresh");
  if (refresh)
    refresh.onclick = (e) => {
      e.stopPropagation();
      refresh.classList.add("spin");
      renderProducts(true);
    };
  loadRailRoi();
}

// Fill the rail's ROI line after the rail has already rendered — the suite-wide
// cost-per-durable roll-up shells the delivery walk per active repo, so it's
// slower than the products probe and must never gate the rail. Cached 5 min
// server-side, so this is cheap on every poll after the first.
async function loadRailRoi() {
  const el = document.getElementById("rail-roi-val");
  if (!el) return;
  try {
    const r = await api("/api/roi");
    if (r && r.attributable && r.cost_per_durable != null) {
      el.textContent = `${money(r.cost_per_durable)}/change`;
      el.title = `${money(r.total_cost_usd)} spend ÷ ${fmt(r.total_durable)} durable changes · ${fmt(r.window_days)}d`;
    } else {
      el.textContent = "not yet attributable";
      el.classList.add("rail-roi-muted");
    }
  } catch (_e) {
    el.textContent = "—";
  }
}

// ---- Compatible Apps lifecycle (third-party; everything products get, minus licensing)

function otherInstallFlow(a) {
  // Third-party installers are launch-grade trust (their code, fetched at run
  // time) — gated since v0.8.1, unlike our own products' ungated install.
  if (!LAUNCH_ENABLED) {
    toast("read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable installs", "err");
    return;
  }
  if (!confirm(`Install ${a.name}?\n\n${a.install_note || `This runs ${a.name}'s own installer in a new terminal.`}`)) return;
  postAction("/api/other-install", { app: a.id }, "install_started", `installing ${a.name}`, 0)
    .then((r) => r.status === "install_started" && watchOtherApp(a.id, true, `${a.name} installed`));
}

// Mirrors productMenu, minus the license item. Update/uninstall ride the
// two-key turn (ALLOW_LAUNCH), shown disabled with the reason when off.
function otherAppMenu(id) {
  const a = OTHER_APPS.find((x) => x.id === id);
  if (!a) return;
  const gateReason = "read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable";
  const mi = (action, label, enabled, reason, extra) =>
    `<button class="btn menu-item${extra ? " " + extra : ""}" data-action="${action}"${
      enabled ? "" : ` disabled title="${esc(reason)}"`
    }>${esc(label)}</button>`;
  const items = [
    mi("github", "Open on GitHub ↗", true, ""),
    a.update_available
      ? mi("update", `Update to ${a.latest_version}`, LAUNCH_ENABLED, gateReason, "menu-item--accent")
      : mi("update", "Reinstall / repair", LAUNCH_ENABLED, gateReason),
    mi("uninstall", "Uninstall", LAUNCH_ENABLED, gateReason, "menu-item--danger"),
  ].join("");
  const sub = "installed" +
    (a.version ? ` · ${a.version}` : " · version untracked") +
    (a.latest_version ? ` · latest ${a.latest_version}` : "");
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="modal"><h3>${esc(a.name)}</h3>
      <div class="muted">${esc(sub)}</div>
      <div class="menu-list">${items}</div>
      <div class="actions"><button class="btn btn--ghost" id="menu-cancel">close</button></div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (e) => { if (e.target === ov) close(); };
  ov.querySelector("#menu-cancel").onclick = close;
  ov.querySelectorAll(".menu-item:not([disabled])").forEach(
    (b) =>
      (b.onclick = () => {
        close();
        ({
          github: () => window.open(a.url, "_blank", "noopener"),
          update: () =>
            confirm(`Update ${a.name}?\n\nRuns its updater in a new terminal.`) &&
            postAction("/api/other-upgrade", { app: a.id }, "upgrade_started", `updating ${a.name}`, 8000),
          uninstall: () =>
            confirm(`Uninstall ${a.name}?\n\n${a.uninstall_note || "Runs its uninstaller in a new terminal."}`) &&
            postAction("/api/other-uninstall", { app: a.id }, "uninstall_started", `uninstalling ${a.name}`, 0)
              .then((r) => r.status === "uninstall_started" && watchOtherApp(a.id, false, `${a.name} uninstalled`)),
        }[b.dataset.action]?.());
      })
  );
}

// Every pip-spawning action shares the same shape: POST, expect an
// "<x>_started" status, toast + schedule a fresh rail re-check, surface errors.
async function postAction(path, body, okStatus, okMsg, refreshDelay = 5000) {
  let res;
  try {
    res = await (
      await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
    ).json();
  } catch (e) {
    res = { error: String(e) };
  }
  if (res.status === okStatus) {
    toast(`${okMsg} — watch the new terminal`, "ok");
    if (refreshDelay) setTimeout(() => renderProducts(true), refreshDelay);
  } else {
    toast((res.error || "action failed") + (res.hint ? ` — ${res.hint}` : ""), "err");
  }
  return res;
}

// pip runs in a terminal we can't await, so poll the rail until the just-installed
// product(s) actually appear, then offer to enter a license key for any that
// still have none (keys live console-side, so a fresh install starts unlicensed).
// Chains install -> license — the new-user hand-holding demographic #1 wants.
async function nudgeLicenseAfterInstall(ids) {
  const pending = new Set(ids);
  const landed = [];
  for (let i = 0; i < 12 && pending.size; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    await renderProducts(true); // refresh the rail + repopulate PRODUCTS as pip lands
    for (const p of PRODUCTS) {
      if (pending.has(p.id) && p.installed) {
        pending.delete(p.id);
        if (p.license === "none") landed.push(p);
      }
    }
  }
  for (const p of landed) {
    if (confirm(`${p.name} is installed. Enter its license key now?\n\n(You can always add it later from the product menu.)`)) {
      await enterLicense(p.id, p.name);
    }
  }
}

// The installer runs in a terminal we can't await, so poll until the app's
// installed state flips to what the action promised, then refresh the rail
// and say so. The loop polls ONLY the cheap other-apps probe (fresh — it's
// server-cached 60s); the full rail re-render happens once, on success.
// 6 minutes covers a heavy pip install (headroom-ai[all] compiles wheels);
// on timeout, say the rail will catch up rather than going silent.
async function watchOtherApp(appId, wantInstalled, doneMsg) {
  for (let i = 0; i < 90; i++) {
    await new Promise((r) => setTimeout(r, 4000));
    let a;
    try {
      const oa = await api("/api/other-apps?fresh=1");
      a = (oa.apps || []).find((x) => x.id === appId);
    } catch (e) { continue; }
    if (a && a.installed === wantInstalled) {
      toast(doneMsg, "ok");
      renderProducts(true);
      return;
    }
  }
  toast("still waiting on the terminal — the rail catches up once it finishes", "");
}

async function installFlow(productId, name) {
  if (!confirm(`Install ${name} from its latest release?\n\nThis runs pip in a new terminal — watch it there.`)) return;
  const res = await postAction("/api/install", { product: productId }, "install_started", `installing ${name}`, 0);
  if (res.status === "install_started") nudgeLicenseAfterInstall([productId]);
}

async function installAllFlow() {
  const missing = PRODUCTS.filter((p) => !p.installed).map((p) => p.name).join(", ");
  if (!confirm(`Install the rest of the suite?\n\n${missing}\n\nThis runs pip in a new terminal.`)) return;
  const res = await postAction("/api/install-all", {}, "install_started", "installing the suite", 0);
  if (res.status === "install_started") nudgeLicenseAfterInstall(res.products || []);
}

async function upgradeFlow(productId, name) {
  if (!confirm(`Update ${name} to the latest release?\n\nThis runs pip in a new terminal.`)) return;
  postAction("/api/upgrade", { product: productId }, "upgrade_started", `updating ${name}`, 4000);
}

async function gitUpdateFlow(productId, name) {
  if (!confirm(`Update ${name} (dev checkout)?\n\nRuns git pull in its source repo and refreshes the install metadata in place. Safe while ${name} is in use — it never touches the running executable. Restart the MCP server to load the pulled code.`)) return;
  let res;
  try {
    res = await (await fetch("/api/git-update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product: productId }),
    })).json();
  } catch (e) {
    res = { error: String(e) };
  }
  if (res.status === "updated") {
    const r = res.refresh || {};
    const msg = r.refreshed
      ? `${name} updated → ${r.version} (restart its MCP server to load it)`
      : `${name}: ${res.git || "up to date"}${r.version ? ` (v${r.version})` : ""}`;
    toast(msg, "ok");
    renderProducts(true);
  } else {
    toast((res.error || "update failed") + (res.hint ? ` — ${res.hint}` : ""), "err");
  }
}

async function reinstallFlow(productId, name) {
  if (!confirm(`Reinstall ${name} (force-reinstall the latest release)?\n\nRepairs a broken install. Runs pip in a new terminal.`)) return;
  postAction("/api/reinstall", { product: productId }, "reinstall_started", `reinstalling ${name}`, 6000);
}

async function uninstallFlow(productId, name) {
  if (!confirm(`Uninstall ${name}?\n\nThis removes the package via pip (in a new terminal). You can reinstall it later.`)) return;
  postAction("/api/uninstall", { product: productId }, "uninstall_started", `uninstalling ${name}`, 5000);
}

async function restartFlow(productId, name) {
  // The console never owned these processes (the MCP client spawns them as stdio
  // subprocesses), so a "restart" is really: stop the running server and let the
  // client respawn it. Custom toasts because it spawns no terminal and has a
  // friendly not-running case, so postAction's phrasing doesn't fit.
  if (!confirm(`Restart the ${name} MCP server?\n\nStops the running server process so your MCP client starts a fresh one. Some clients only respawn on their next reconnect or tool call.`)) return;
  let res;
  try {
    res = await (
      await fetch("/api/restart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product: productId }),
      })
    ).json();
  } catch (e) {
    res = { error: String(e) };
  }
  if (res.status === "restarted") {
    // hint is tailored server-side to the detected client(s) (e.g. Claude Code's
    // /mcp reconnect vs. a GUI app restart).
    toast(`${name} server stopped — ${res.hint || "your MCP client respawns it on reconnect"}`, "ok");
  } else if (res.status === "not_running") {
    toast(res.hint || `no running ${name} server found`, "");
  } else {
    toast((res.error || "restart failed") + (res.hint ? ` — ${res.hint}` : ""), "err");
  }
}

// Per-product action menu (installed products). Reinstall/uninstall ride the
// two-key turn (ALLOW_LAUNCH) and refuse dev checkouts, so they're shown
// disabled with the reason when unavailable rather than hidden.
function productMenu(id) {
  const p = PRODUCTS.find((x) => x.id === id);
  if (!p) return;
  const pipReason = !LAUNCH_ENABLED
    ? "read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable"
    : p.editable
    ? "dev checkout — manage with git, not pip"
    : "";
  const canPip = !LAUNCH_ENABLED ? false : !p.editable;
  const mi = (action, label, enabled, reason, extra) =>
    `<button class="btn menu-item${extra ? " " + extra : ""}" data-action="${action}"${
      enabled ? "" : ` disabled title="${esc(reason)}"`
    }>${esc(label)}</button>`;
  const items = [
    mi("license", p.license === "none" ? "Enter license key…" : "Change license key…", true, ""),
    p.update_available
      ? mi("update", `Update to v${p.latest_version}`, LAUNCH_ENABLED, "read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable", "menu-item--accent")
      : "",
    // Dev checkouts update via git, not pip — offer it when a newer release exists.
    p.editable && p.behind
      ? mi("gitpull", `Update (git pull) → v${p.latest_version}`, LAUNCH_ENABLED, "read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable", "menu-item--accent")
      : "",
    mi("restart", "Restart MCP server", LAUNCH_ENABLED, "read-only mode is on — unset JMUNCH_CONSOLE_READ_ONLY to enable"),
    mi("reinstall", "Reinstall", canPip, pipReason),
    mi("uninstall", "Uninstall", canPip, pipReason, "menu-item--danger"),
  ].join("");
  const sub = !p.installed
    ? "not installed"
    : p.broken
    ? "broken install — launcher present but package missing; reinstall"
    : (p.editable ? "dev checkout" : "installed") + (p.version ? ` · v${p.version}` : "");
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="modal"><h3>${esc(p.name)}</h3>
      <div class="muted">${esc(sub)}</div>
      <div class="menu-list">${items}</div>
      <div class="actions"><button class="btn btn--ghost" id="menu-cancel">close</button></div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.onclick = (e) => { if (e.target === ov) close(); };
  ov.querySelector("#menu-cancel").onclick = close;
  ov.querySelectorAll(".menu-item:not([disabled])").forEach(
    (b) =>
      (b.onclick = () => {
        close();
        ({
          license: () => enterLicense(p.id, p.name),
          update: () => upgradeFlow(p.id, p.name),
          gitpull: () => gitUpdateFlow(p.id, p.name),
          restart: () => restartFlow(p.id, p.name),
          reinstall: () => reinstallFlow(p.id, p.name),
          uninstall: () => uninstallFlow(p.id, p.name),
        }[b.dataset.action]?.());
      })
  );
}

async function enterLicense(productId, name) {
  const key = prompt(`Enter the ${name || productId} license key:\n(leave blank to clear)`);
  if (key === null) return;
  let res;
  try {
    res = await (
      await fetch("/api/license", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ product: productId, key }),
      })
    ).json();
  } catch (e) {
    res = { error: String(e) };
  }
  if (res.status === "saved") {
    const label = { valid: "licensed", invalid: "invalid key", entered: "saved (unverified)", none: "cleared" }[res.license] || res.license;
    toast(`${name || productId}: ${label}${res.tier ? " (" + res.tier + ")" : ""}`, res.license === "invalid" ? "err" : "ok");
  } else {
    toast(res.error || "save failed", "err");
  }
  renderProducts();
  // A license change flips the soft-gates; re-render the current screen if it's
  // a gated one so the gate clears (or re-applies) immediately, no nav or
  // server restart needed. The server also busts its license cache on save.
  if (res.status === "saved") {
    const cur = (location.hash || "").slice(1);
    if (cur === "savings" || cur === "index" || cur === "sessions") go(cur);
  }
}

// ---- theme ----
const themeBtn = document.getElementById("theme-toggle");
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("jmc-theme", t);
}
themeBtn.onclick = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

// ---- accent palette (aurora / ember / spectrum) ----
const ACCENTS = ["aurora", "ember", "spectrum"];
const accentSel = document.getElementById("accent-select");
function applyAccent(a) {
  if (!ACCENTS.includes(a)) a = "aurora";
  document.documentElement.dataset.accent = a;
  localStorage.setItem("jmc-accent", a);
  if (accentSel) accentSel.value = a;
}
if (accentSel) accentSel.onchange = () => applyAccent(accentSel.value);

document.getElementById("config-gear").onclick = () => go("config");
const consoleDot = document.getElementById("console-dot");
consoleDot.onclick = consoleMenu;
consoleDot.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); consoleMenu(); } };

async function init() {
  applyTheme(localStorage.getItem("jmc-theme") || "dark");
  applyAccent(localStorage.getItem("jmc-accent") || "aurora");
  buildNav();
  renderProducts();
  const meta = await api("/api/meta");
  LAUNCH_ENABLED = !!meta.launch_enabled;
  document.getElementById("meta").textContent = meta.fixtures_forced
    ? "fixtures mode"
    : LAUNCH_ENABLED ? "launch enabled" : "read-only";
  go((location.hash || "#index").slice(1));
  startAlertWatch(); // nav badge + banner surface a breach from any screen
}

init();
