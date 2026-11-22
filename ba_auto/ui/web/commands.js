"use strict";
/* The Run page: every task with real controls, the client's controls, and read-only views.
   The server's schema decides what exists, and it re-checks everything sent here.
   Uses S, api, post, toast, render, closeModal, esc, ic, known and UNREAD from app.js. */
const Commands = (() => {
  const P = { schema: null, client: null, sweep: null, sweepText: "", tab: "tasks", group: "All", q: "", values: {},
    pending: null, inspect: null, inspecting: null, halt: null };
  const TABS = [["tasks", "Tasks", "play"], ["client", "Client", "power"], ["inspect", "Inspect", "eye"]];
  const el = (id) => document.getElementById(id);
  const find = (name) => P.schema && P.schema.tasks.find((t) => t.name === name);
  const blocked = () => {
    const st = S.state || {};
    return !st.connected ? "The client isn't running" : st.sample ? "This is a preview" : st.halt ? "Input is halted" : st.busy ? "A run is active" : "";
  };

  /* ---------- values ---------- */
  function vals(t) {
    if (!P.values[t.name]) {
      const v = {};
      t.fields.forEach((f) => { if (f.default !== null && f.default !== undefined) v[f.id] = f.default; });
      P.values[t.name] = v;
    }
    return P.values[t.name];
  }
  const active = (f, v) => (!f.when || f.when.includes(v.mode)) && Object.entries(f.depends || {}).every(([k, list]) => list.includes(String(v[k])));
  function payload(t) {
    const v = vals(t), out = {};
    t.fields.forEach((f) => {
      if (!active(f, v) || v[f.id] === undefined || v[f.id] === null) return;
      if (f.kind === "sweep" && !Object.keys(v[f.id]).length) return;
      out[f.id] = v[f.id];
    });
    return out;
  }

  /* ---------- fields ---------- */
  function pointHtml(t, f, v) {
    const [x1, y1, x2, y2] = f.box, value = v[f.id];
    const [px, py] = value ? String(value).split(",").map(Number) : [];
    const box = `left:${(x1 / 19.2).toFixed(2)}%;top:${(y1 / 10.8).toFixed(2)}%;width:${((x2 - x1) / 19.2).toFixed(2)}%;height:${((y2 - y1) / 10.8).toFixed(2)}%`;
    const dot = value ? `<i style="left:${(px / 19.2).toFixed(2)}%;top:${(py / 10.8).toFixed(2)}%"></i>` : "";
    return `<div class="cf wide"><span>${esc(f.label)}</span><button type="button" class="cf-point" data-cpoint="${esc(t.name)}|${esc(f.id)}" aria-label="${esc(f.label)}. Click the area to choose."><em style="${box}"></em>${dot}</button><small>${value ? `Game position ${esc(value)}` : "Click inside the blue area"}</small></div>`;
  }
  function sweepHtml(t, f, v) {
    const data = P.sweep;
    if (!data) return `<p class="cmd-note">Loading the last scan…</p>`;
    if (data.error) return `<p class="cmd-note warn-t">${esc(data.error)}</p>`;
    if (!data.rows.length) return `<p class="cmd-note">No mission scan yet. Run Scan missions first.</p>`;
    const picked = v.stages || {}, byKey = new Map(data.rows.map((r) => [r.key, r]));
    const ready = data.rows.filter((r) => r.status === "ready" && !(r.key in picked));
    const options = ready.map((r) => `<option value="${esc(r.key)}">${esc(r.key)} · ${r.cost ?? "?"} AP${r.remaining !== null && r.remaining !== undefined ? ` · ${esc(r.remaining)} left` : ""}</option>`).join("");
    const rows = Object.entries(picked).map(([key, count]) => {
      const r = byKey.get(key) || {};
      return `<div class="sw-row"><b>${esc(key)}</b><small>${r.cost ?? "?"} AP each</small><input type="number" min="1" max="${r.max || 99}" value="${esc(count)}" data-sweep-count="${esc(key)}" aria-label="Times to sweep ${esc(key)}"><button type="button" class="iconbtn" data-sweep-remove="${esc(key)}" aria-label="Remove ${esc(key)}">${ic("close")}</button></div>`;
    }).join("");
    const total = Object.entries(picked).reduce((sum, [key, count]) => sum + (byKey.get(key)?.cost || 0) * (Number(count) || 0), 0);
    const over = known(data.ap) && total > data.ap;
    return `<div class="cf wide"><span>Add a stage from the last scan</span><select data-sweep-add aria-label="Add a stage"><option value="">Choose a stage…</option>${options}</select></div>${rows}
      ${rows ? `<p class="cmd-note ${over ? "warn-t" : ""}">Total ${total} AP${known(data.ap) ? ` of ${esc(data.ap)} at the last scan` : ""}</p>` : ""}`;
  }
  function fieldHtml(t, f, v) {
    const key = `${esc(t.name)}|${esc(f.id)}`;
    if (f.kind === "number") {
      const hint = f.blank ? `placeholder="${esc(f.blank)}"` : "";
      return `<label class="cf"><span>${esc(f.label)}${f.unit ? ` (${esc(f.unit)})` : ""}</span><input type="number" min="${f.min}" max="${f.max}" ${hint} value="${esc(v[f.id] ?? "")}" data-cf="${key}"></label>`;
    }
    if (f.kind === "choice") return `<label class="cf"><span>${esc(f.label)}</span><select data-cf="${key}">${f.options.map((o) => `<option value="${esc(o)}" ${String(o) === String(v[f.id]) ? "selected" : ""}>${esc(String(o)[0].toUpperCase() + String(o).slice(1))}</option>`).join("")}</select></label>`;
    if (f.kind === "text") return `<label class="cf"><span>${esc(f.label)}</span><input type="text" maxlength="40" placeholder="${esc(f.hint || "")}" value="${esc(v[f.id] ?? "")}" data-cf="${key}"></label>`;
    if (f.kind === "point") return pointHtml(t, f, v);
    if (f.kind === "sweep") return sweepHtml(t, f, v);
    return "";
  }
  function cardHtml(t) {
    if (t.missing) return `<article class="cmd"><header><span class="tile-ic">${ic("alert")}</span><div><b>${esc(t.label)}</b><p>This task has no controls yet.</p></div></header></article>`;
    const v = vals(t), modeField = t.fields.find((f) => f.kind === "mode");
    const option = modeField && modeField.options.find((o) => o.value === v.mode);
    const spends = t.confirm || (option && option.confirm);
    const fields = t.fields.filter((f) => f.kind !== "mode" && active(f, v)).map((f) => fieldHtml(t, f, v)).join("");
    const mode = modeField ? `<div class="seg cmd-mode" role="group" aria-label="Mode">${modeField.options.map((o) => `<button type="button" class="${o.value === v.mode ? "on" : ""} ${o.confirm ? "real" : ""}" data-cmode="${esc(t.name)}|${esc(o.value)}" aria-pressed="${o.value === v.mode}"><span>${esc(o.label)}</span></button>`).join("")}</div>` : "";
    const action = t.via ? `<button class="btn small primary" data-nav="${esc(t.via)}">${ic("map")}Open ${esc(t.via[0].toUpperCase() + t.via.slice(1))}</button>`
      : `<button class="btn small ${spends ? "gold" : "primary"}" data-cmd-run="${esc(t.name)}">${ic("play", "solid")}${spends ? "Review" : "Start"}</button>`;
    return `<article class="cmd ${spends ? "spends" : ""}" data-cmd="${esc(t.name)}"><header><span class="tile-ic">${ic(t.icon)}</span><div><b>${esc(t.label)}</b><p>${esc(t.desc)}</p></div></header>
      ${mode}${option && option.note ? `<p class="cmd-note">${esc(option.note)}</p>` : ""}${fields ? `<div class="cmd-fields">${fields}</div>` : ""}
      <footer>${spends ? `<span class="spend-tag">${ic("alert")}Asks before it acts</span>` : ""}${action}</footer></article>`;
  }
  function redrawCard(name) {
    const node = document.querySelector(`[data-cmd="${CSS.escape(name)}"]`), t = find(name);
    if (node && t) { node.outerHTML = cardHtml(t); syncButtons(); }
  }

  /* ---------- tabs ---------- */
  function tasksHtml() {
    const groups = ["All", ...P.schema.groups];
    return `<div class="card cmd-bar"><label class="search">${ic("search")}<input id="cmd-q" placeholder="Find a task" value="${esc(P.q)}" aria-label="Find a task"></label>
      <div class="seg" role="group" aria-label="Task group">${groups.map((g) => `<button type="button" class="${g === P.group ? "on" : ""}" data-cmd-group="${esc(g)}"><span>${esc(g)}</span></button>`).join("")}</div></div>
      <div class="cmd-grid" id="cmd-grid"></div>`;
  }
  function drawGrid() {
    const grid = el("cmd-grid");
    if (!grid) return;
    const q = P.q.trim().toLowerCase();
    const rows = P.schema.tasks.filter((t) => (P.group === "All" || t.group === P.group) && (!q || `${t.label} ${t.desc} ${t.name}`.toLowerCase().includes(q)));
    grid.innerHTML = rows.map(cardHtml).join("") || `<div class="mini-empty">No tasks match.</div>`;
    syncButtons();
  }
  function syncButtons() {
    const why = blocked();
    document.querySelectorAll("[data-cmd-run]").forEach((b) => { b.disabled = Boolean(why); b.title = why; });
    document.querySelectorAll("[data-inspect]").forEach((b) => {
      const off = why && why !== "Input is halted" ? why : P.inspecting ? "Reading the game" : "";
      b.disabled = Boolean(off); b.title = off;
    });
  }
  function row(icon, title, text, control, tone = "") {
    return `<div class="tile ${tone}"><div class="tile-ic">${ic(icon)}</div><div style="flex:1;min-width:0"><b>${title}</b><p>${text}</p></div>${control}</div>`;
  }
  function clientHtml() {
    const st = S.state || {}, L = st.launcher || {}, guard = (P.client && P.client.guard) || {}, ui = P.client && P.client.ui;
    const live = st.connected && !st.sample;
    const run = st.busy && st.run
      ? row("play", esc(st.run.label), `Running for ${esc(st.run.seconds)} s, started ${esc(st.run.started)} by ${esc(st.run.actor)}${Object.keys(st.run.stats || {}).length ? ` · ${Object.entries(st.run.stats).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(", ")}` : ""}`, `<button class="btn small" data-stop>${ic("stop", "solid")}Stop</button>`)
      : row("clock", "No run active", st.last ? `Last: ${esc(st.last.label)}, ${esc(st.last.outcome)} at ${esc(st.last.started)}${st.last.error ? ` · ${esc(st.last.error)}` : ""}` : "Nothing has run in this client yet.", "");
    const halt = st.halt
      ? row("alert", "Input is halted", esc(st.halt.reason), `<button class="btn small" data-halt-review>Review</button>`, "bad")
      : row("check", "No halt", "Game input is allowed.", "");
    const hold = L.login_hold
      ? row("key", "Login hold is on", L.duplicate ? "Someone else may be on this account." : esc(L.reason || "The game ended the session."), `<button class="btn small" data-hold-review>Review</button>`, "bad")
      : row("check", "No login hold", "Auto login may run when it's on.", "");
    const autoOn = L.auto_login, autoKnown = autoOn === true || autoOn === false;
    const auto = row("pointer", "Auto login", autoKnown ? (autoOn ? (L.login_hold ? "On, but on hold" : "On. Presses Click to Start once you're idle.") : "Off.") : "Not available.",
      `<button class="switch ${autoOn ? "on" : ""}" role="switch" aria-checked="${Boolean(autoOn)}" aria-label="Auto login" data-autologin="${autoOn ? 0 : 1}" ${autoKnown && live ? "" : "disabled"}><span></span></button>`);
    const guardText = guard.problem ? `<span class="warn-t">${esc(guard.problem)}</span>`
      : guard.active ? "Runs for every input sent to the game. It can't be turned off." : "Status unavailable.";
    const guardWhy = guard.last_block ? `<br><span class="warn-t">Last: ${esc(guard.last_block)}</span>` : "";
    const guardRow = row("shield", "Input guard", guardText + guardWhy, `<span class="chip guard-state ${guard.active ? "ok" : "bad"}">${guard.active ? "Active" : "Check"}</span>`);
    const overlay = row("eye", "Game overlay", "Small status card. Clicks pass through it.",
      `<button class="switch ${st.overlay ? "on" : ""}" role="switch" aria-checked="${Boolean(st.overlay)}" aria-label="Game overlay" data-overlay="${st.overlay ? 0 : 1}" ${st.overlay === null || st.overlay === undefined || !live ? "disabled" : ""}><span></span></button>`);
    const view = row("window", "Client window at next start", "Applies the next time the client starts.",
      `<select data-ui-choice aria-label="Client window at next start" ${live ? "" : "disabled"}><option value="new" ${ui === "new" ? "selected" : ""}>New panel</option><option value="classic" ${ui === "classic" ? "selected" : ""}>Classic HUD</option></select>`);
    const off = !live || st.busy;
    const life = row("power", "Restart client", "Closes and reopens the client. F8 is off for a few seconds.", `<button class="btn small" data-restart ${off ? "disabled" : ""}>Restart</button>`)
      + row("close", "Quit client", "Closing this window leaves the client running. Quit ends it.", `<button class="btn small" data-quit ${off ? "disabled" : ""}>Quit</button>`);
    const tools = row("radar", "Mission sweeper window", "The original sweeper with its AP guards.", `<button class="btn small" data-open="sweeper" ${live ? "" : "disabled"}>${ic("window")}Open</button>`);
    return `<section class="card"><h2 class="h">${ic("play")}Current run</h2>${run}</section>
      <section class="card"><h2 class="h">${ic("alert")}Safety state</h2>${halt}${hold}</section>
      <section class="card"><h2 class="h">${ic("key")}Settings</h2>${auto}${guardRow}${overlay}${view}</section>
      <section class="card"><h2 class="h">${ic("power")}Client</h2>${life}${tools}</section>`;
  }
  const VIEWS = [["look", "eye", "Look", "Screen, popup, resources and known safe buttons"], ["detect", "search", "Detect", "Which screen matched, with the best template scores"],
    ["who", "students", "Who", "Students on the open list, rooms or battle result"], ["boss", "target", "Read boss", "Enemy types from an open Boss Info popup, with team advice"]];
  const LINKS = [["home", "list", "Today", "To-dos"], ["students", "students", "Students", "Roster"], ["team", "team", "Team", "Advisor"], ["battles", "swords", "Battles", "History"], ["journal", "clock", "Journal", "Every action"], ["lessons", "book", "Lessons", "Atlas"]];
  function resultHtml() {
    if (P.inspecting) return `<div class="mini-empty">Reading the game…</div>`;
    const r = P.inspect;
    if (!r) return `<div class="mini-empty">Choose a view. These only read the screen and never click.</div>`;
    if (!r.ok) return `<p class="warn-t">${esc(r.error)}</p>`;
    const d = r.result || {};
    const kv = (k, v) => `<div><small>${esc(k)}</small><b>${known(v) && v !== "" ? esc(typeof v === "number" ? v.toLocaleString("en-US") : v) : UNREAD}</b></div>`;
    if (d.error) return `<p class="warn-t">${esc(d.error)}</p>`;
    if (r.view === "look") {
      const res = d.resources || {};
      return `<div class="kv">${kv("Screen", d.screen)}${kv("Popup", d.popup)}${kv("AP", res.ap)}${kv("Credits", res.credits)}${kv("Pyroxene", res.pyroxene)}</div>
        ${d.warning ? `<p class="warn-t" style="margin-top:8px">${esc(d.warning)}</p>` : ""}${d.popup_text ? `<p class="tiny muted" style="margin-top:8px">${esc(d.popup_text)}</p>` : ""}
        ${(d.buttons || []).length ? `<p class="tiny muted" style="margin:9px 0 4px">Known buttons here</p>${d.buttons.map((b) => `<span class="chip" style="margin:0 4px 4px 0">${esc(b.name)}</span>`).join("")}` : ""}`;
    }
    if (r.view === "detect") return `<div class="kv">${kv("Screen", d.screen)}</div><div class="scores">${(d.scores || []).map(([n, v]) => `<div><span>${esc(n)}</span><i style="width:${Math.max(0, v) * 100}%"></i><b>${esc(v)}</b></div>`).join("")}</div>`;
    if (r.view === "who") return `<div class="kv">${kv("Layout", d.layout)}</div><div style="margin-top:8px">${(d.students || []).map((s) => `<span class="chip" style="margin:0 4px 4px 0">${esc(typeof s === "string" ? s : s.name || s.student || s.dev || UNREAD)}</span>`).join("") || `<span class="muted">Nobody identified.</span>`}</div>`;
    if (r.view === "boss") {
      const e = d.enemy || {}, team = (d.advice && d.advice.team) || [];
      return `<div class="kv">${kv("Boss", e.boss)}${kv("Attack", e.attack)}${kv("Armor", e.armor)}</div>${team.length ? `<p class="tiny muted" style="margin:9px 0 4px">Suggested (the advisor never changes a team)</p>${team.map((m) => `<span class="chip" style="margin:0 4px 4px 0">${esc(m.name)}</span>`).join("")}` : ""}`;
    }
    return "";
  }
  function inspectHtml() {
    const log = (S.state && S.state.log) || [];
    return `<section class="card"><h2 class="h">${ic("eye")}Read the game<span class="right">Read only</span></h2>
        <div class="view-btns">${VIEWS.map(([id, icon, name, text]) => `<button class="vbtn ${P.inspect && P.inspect.view === id ? "on" : ""}" data-inspect="${id}">${ic(icon)}<b>${name}</b><small>${text}</small></button>`).join("")}</div><div id="cmd-result" class="cmd-result">${resultHtml()}</div></section>
      <section class="card"><h2 class="h">${ic("list")}Client log<span class="right">Latest</span></h2><div class="logbox" id="cmd-log">${log.map((l) => `<div>${esc(l)}</div>`).join("") || `<span class="muted">Nothing yet.</span>`}</div></section>
      <section class="card cmd-links"><h2 class="h">${ic("window")}Other views</h2><div>${LINKS.map(([id, icon, name, text]) => `<button class="vbtn" data-nav="${id}">${ic(icon)}<b>${name}</b><small>${text}</small></button>`).join("")}</div></section>`;
  }
  function drawBody() {
    const body = el("cmd-body");
    if (!body) return;
    if (P.tab === "tasks") { body.className = "cmd-tasks"; body.innerHTML = tasksHtml(); drawGrid(); }
    else if (P.tab === "client") { body.className = "cmd-client"; body.innerHTML = `<div id="cmd-client" class="cmd-columns">${clientHtml()}</div>`; }
    else { body.className = "cmd-inspect"; body.innerHTML = `<div class="cmd-columns" id="cmd-inspect">${inspectHtml()}</div>`; syncButtons(); }
    document.querySelectorAll("[data-cmd-tab]").forEach((b) => { const on = b.dataset.cmdTab === P.tab; b.classList.toggle("on", on); b.setAttribute("aria-selected", String(on)); });
  }
  let lastClient = "";
  function renderPage(v) {
    if (!P.schema) { if (!v.querySelector(".mini-empty")) v.innerHTML = `<div class="card mini-empty">Loading…</div>`; return; }
    if (!v.querySelector("#cmd")) {
      v.innerHTML = `<div id="cmd"><div class="cmd-tabs" role="tablist">${TABS.map(([id, label, icon]) => `<button type="button" role="tab" data-cmd-tab="${id}">${ic(icon)}${label}</button>`).join("")}</div><div id="cmd-body"></div></div>`;
      lastClient = "";
      drawBody();
    }
    if (P.tab === "tasks") syncButtons();
    if (P.tab === "client") {
      const html = clientHtml();
      if (html !== lastClient && !(document.activeElement && document.activeElement.matches("[data-ui-choice]"))) { el("cmd-client").innerHTML = html; lastClient = html; }
    }
    if (P.tab === "inspect") {
      const log = el("cmd-log"), lines = ((S.state && S.state.log) || []).map((l) => `<div>${esc(l)}</div>`).join("");
      if (log && log.dataset.h !== lines) { log.innerHTML = lines || `<span class="muted">Nothing yet.</span>`; log.dataset.h = lines; log.scrollTop = log.scrollHeight; }
      syncButtons();
    }
  }
  async function load() {
    if (!P.schema) P.schema = await api("/api/commands");
    P.client = await api("/api/client");
    const sweep = await api("/api/sweep"), text = JSON.stringify(sweep);
    if (text !== P.sweepText) {
      P.sweep = sweep; P.sweepText = text;
      if (P.tab === "tasks" && document.querySelector('[data-cmd="sweep"]') && !document.activeElement?.matches("[data-sweep-count]")) redrawCard("sweep");
    }
  }

  /* ---------- starting a task ---------- */
  async function start(name, key) {
    const t = find(name);
    if (!t) return;
    const r = await post("/api/command", { task: name, values: payload(t), confirm: key || null });
    if (r.confirm && r.key) { openConfirm(t, r); return; }
    closeModal();
    toast(r.ok ? `Started: ${t.label}` : r.error || "Couldn't start that");
    S.state = await api("/api/state").catch(() => S.state);
    render();
  }
  function openConfirm(t, r) {
    P.pending = { name: t.name, key: r.key };
    const rows = (r.summary || []).map(([k, v]) => `<div class="mrow"><div class="grow"><small>${esc(k)}</small></div><b>${esc(v)}</b></div>`).join("");
    document.getElementById("modal").innerHTML = `<div class="scrim" data-close><div class="sheet confirm" role="dialog" aria-modal="true" aria-label="${esc(r.label)}"><div class="sheet-head"><h2>${esc(r.label)}</h2><button class="btn small" data-close aria-label="Close">${ic("close")}</button></div>
      <div class="sheet-body"><p class="spend-text">${ic("alert")}<span>${esc(r.confirm)}</span></p>${rows ? `<div class="mlist">${rows}</div>` : ""}<p class="tiny muted" style="margin-top:8px">Stop and F8 work throughout.</p>
      <div class="confirm-row"><button class="btn" data-close>Cancel</button><button class="btn gold" data-cmd-yes>${ic("play", "solid")}Yes, start</button></div></div></div></div>`;
    document.querySelector(".confirm-row [data-close]").focus();    // a repeated Enter must never confirm a spend
  }

  /* ---------- halt and login hold ---------- */
  const money = (v) => (known(v) ? esc(typeof v === "number" ? v.toLocaleString("en-US") : v) : UNREAD);
  function valueTable(saw, now) {
    const rows = [["AP", "ap"], ["Credits", "credits"], ["Pyroxene", "pyroxene"]].map(([label, key]) => {
      const a = (saw || {})[key], b = (now || {})[key], changed = known(a) && known(b) && a !== b;
      return `<tr class="${changed ? "changed" : ""}"><th>${label}</th><td>${money(a)}</td><td>${money(b)}</td></tr>`;
    }).join("");
    return `<table class="halt-values"><thead><tr><th></th><th>When it stopped</th><th>Now</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  async function openHalt() {
    let d;
    try { d = await api("/api/halt"); } catch (e) { toast("Couldn't read the halt"); return; }
    P.halt = d;
    if (!d.halt) { if (d.hold) drawHold(d); else { closeModal(); toast("Nothing is halted"); } return; }
    const h = d.halt;
    const danger = h.game_says ? `<p class="spend-text bad">${ic("alert")}<span>${h.duplicate ? "<b>Duplicate login.</b> Someone else may be on this account. Check that first." : "The game ended the session."} For two minutes after clearing, the client may answer this popup.</span></p>` : "";
    document.getElementById("modal").innerHTML = `<div class="scrim" data-close><div class="sheet halt-sheet" role="dialog" aria-modal="true" aria-label="Input is halted"><div class="sheet-head"><h2>Input is halted</h2><button class="btn small" data-close aria-label="Close">${ic("close")}</button></div>
      <div class="sheet-body halt-grid"><div>${h.has_frame ? `<img class="frame" src="/api/halt/frame?k=${esc(h.key)}" alt="The game when input was halted">` : `<div class="mini-empty">No frame was saved for this halt.</div>`}</div>
      <div><p class="halt-reason">${esc(h.reason)}</p><p class="tiny muted">${esc(h.time || "Time unknown")}</p>${danger}${valueTable(h.values, d.now)}
      <label class="cf wide"><span>What did you check?</span><textarea id="halt-note" maxlength="200" rows="2" placeholder="Saved to the journal"></textarea></label>
      <label class="check-line"><input type="checkbox" id="halt-ok"><span>I've looked at the game and it's safe to allow input again.</span></label>
      <div class="confirm-row"><button class="btn" data-close>Cancel</button><button class="btn primary" data-halt-clear disabled>Clear halt</button></div>
      ${d.hold ? `<p class="tiny muted" style="margin-top:8px">A login hold is also set, and it stays on until you clear it separately.</p>` : ""}</div></div></div></div>`;
  }
  function openHold() {
    api("/api/halt").then((d) => { P.halt = d; if (d.hold) drawHold(d); else { closeModal(); toast("There's no login hold"); } }).catch(() => toast("Couldn't read the login hold"));
  }
  function drawHold(d) {
    const h = d.hold;
    document.getElementById("modal").innerHTML = `<div class="scrim" data-close><div class="sheet confirm hold-sheet" role="dialog" aria-modal="true" aria-label="Login hold"><div class="sheet-head"><h2>Login hold</h2><button class="btn small" data-close aria-label="Close">${ic("close")}</button></div>
      <div class="sheet-body"><p class="spend-text bad">${ic("alert")}<span>${h.duplicate ? "<b>Someone else may be signed in to this account.</b> Logging in now would kick them off." : "<b>The game ended the session.</b> Check why before logging in again."} After clearing, Auto login can press Click to Start by itself.</span></p>
      <p class="halt-reason">${esc(h.reason || "No reason was saved")}</p><p class="tiny muted">${esc(h.time || "Time unknown")}${h.title_seen ? " · The title screen has been seen since" : ""}</p>
      <label class="cf wide"><span>What did you check?</span><textarea id="hold-note" maxlength="200" rows="2" placeholder="Saved to the journal"></textarea></label>
      <label class="cf wide"><span>Type ${esc(h.phrase)} to confirm</span><input id="hold-phrase" autocomplete="off" spellcheck="false"></label>
      <div class="confirm-row"><button class="btn" data-close>Cancel</button><button class="btn stoplike" data-hold-clear disabled>Clear login hold</button></div></div></div></div>`;
  }
  function syncClearButtons() {
    const haltBtn = document.querySelector("[data-halt-clear]");
    if (haltBtn) haltBtn.disabled = !(el("halt-ok").checked && el("halt-note").value.trim().length >= 3);
    const holdBtn = document.querySelector("[data-hold-clear]");
    if (holdBtn) holdBtn.disabled = !(P.halt && P.halt.hold && el("hold-note").value.trim().length >= 3 && el("hold-phrase").value === P.halt.hold.phrase);
  }
  async function afterClear(r, done) {
    if (r.changed) { toast(r.error); await openHalt(); return; }
    if (!r.ok) { toast(r.error || "Couldn't clear that"); return; }
    closeModal();
    toast(done);
    S.state = await api("/api/state").catch(() => S.state);
    render();
    const next = await api("/api/halt").catch(() => null);
    if (next && next.hold && !next.halt && done.startsWith("Halt")) { P.halt = next; drawHold(next); }
  }

  /* ---------- client actions ---------- */
  function restartSheet() {
    document.getElementById("modal").innerHTML = `<div class="scrim" data-close><div class="sheet confirm" role="dialog" aria-modal="true" aria-label="Restart the client"><div class="sheet-head"><h2>Restart the client?</h2><button class="btn small" data-close aria-label="Close">${ic("close")}</button></div>
      <div class="sheet-body"><p>The client and this window close, then a new one opens. F8 is off for those few seconds. The game keeps running, and a halt or login hold stays in place.</p>
      <div class="confirm-row"><button class="btn" data-close>Cancel</button><button class="btn primary" data-restart-yes>Restart</button></div></div></div></div>`;
  }
  async function inspect(view) {
    P.inspecting = view; P.inspect = { ok: true, view };
    const box = el("cmd-result");
    if (box) box.innerHTML = resultHtml();
    syncButtons();
    P.inspect = await post("/api/inspect", { view });
    P.inspect.view = view;
    P.inspecting = null;
    if (P.tab === "inspect") { const b = el("cmd-inspect"); if (b) b.innerHTML = inspectHtml(); syncButtons(); }
  }

  /* ---------- events ---------- */
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-cmd-tab],[data-cmd-group],[data-cmode],[data-cpoint],[data-cmd-run],[data-cmd-yes],[data-sweep-remove],[data-inspect],[data-restart],[data-restart-yes],[data-halt-review],[data-hold-review],[data-halt-clear],[data-hold-clear]");
    if (!b || b.disabled) return;
    const d = b.dataset;
    if (d.cmdTab) { P.tab = d.cmdTab; lastClient = ""; drawBody(); render(); }
    else if (d.cmdGroup) { P.group = d.cmdGroup; document.querySelectorAll("[data-cmd-group]").forEach((x) => x.classList.toggle("on", x.dataset.cmdGroup === P.group)); drawGrid(); }
    else if (d.cmode) { const [name, value] = d.cmode.split("|"); vals(find(name)).mode = value; redrawCard(name); }
    else if (d.cpoint) {
      const [name, id] = d.cpoint.split("|"), t = find(name), f = t.fields.find((x) => x.id === id), r = b.getBoundingClientRect();
      const x = Math.round(Math.min(f.box[2], Math.max(f.box[0], ((e.clientX - r.left) / r.width) * 1920)));
      const y = Math.round(Math.min(f.box[3], Math.max(f.box[1], ((e.clientY - r.top) / r.height) * 1080)));
      vals(t)[id] = `${x},${y}`;
      redrawCard(name);
    } else if (d.cmdRun) await start(d.cmdRun);
    else if ("cmdYes" in d) { const p = P.pending; P.pending = null; if (p) await start(p.name, p.key); }
    else if (d.sweepRemove) { delete vals(find("sweep")).stages[d.sweepRemove]; redrawCard("sweep"); }
    else if (d.inspect) await inspect(d.inspect);
    else if ("restart" in d) restartSheet();
    else if ("restartYes" in d) { const r = await post("/api/restart", { confirmed: true }); closeModal(); toast(r.ok ? "Restarting" : r.error || "Couldn't restart"); }
    else if ("haltReview" in d) await openHalt();
    else if ("holdReview" in d) openHold();
    else if ("haltClear" in d) afterClear(await post("/api/halt/clear", { note: el("halt-note").value, expect: P.halt.halt.key }), "Halt cleared");
    else if ("holdClear" in d) afterClear(await post("/api/hold/clear", { note: el("hold-note").value, expect: P.halt.hold.key, phrase: el("hold-phrase").value }), "Login hold cleared");
  });
  document.addEventListener("input", (e) => {
    const t = e.target;
    if (t.id === "cmd-q") { P.q = t.value; drawGrid(); }
    else if (t.dataset.cf) { const [name, id] = t.dataset.cf.split("|"), task = find(name); vals(task)[id] = t.value; if (task.fields.some((f) => f.depends && id in f.depends)) redrawCard(name); }
    else if (t.dataset.sweepCount) vals(find("sweep")).stages[t.dataset.sweepCount] = t.value;
    else if (["halt-note", "halt-ok", "hold-note", "hold-phrase"].includes(t.id)) syncClearButtons();
  });
  document.addEventListener("change", async (e) => {
    const t = e.target;
    if (t.matches("[data-sweep-add]") && t.value) { const v = vals(find("sweep")); v.stages = { ...(v.stages || {}), [t.value]: 1 }; redrawCard("sweep"); }
    else if (t.dataset.sweepCount) redrawCard("sweep");
    else if (t.matches("[data-ui-choice]")) {
      const r = await post("/api/ui-choice", { choice: t.value });
      toast(r.ok ? "Applies at the next client start" : r.error || "Couldn't save that");
      P.client = await api("/api/client").catch(() => P.client);
      t.blur();
      render();
    }
  });
  return { load, render: renderPage, openHalt, openHold };
})();
