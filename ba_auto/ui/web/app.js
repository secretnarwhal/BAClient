"use strict";
/* BAClient panel. Everything shown comes from the client's API; an unread value shows as "—". */

const SECTIONS = [
  ["home", "Home", "home"], ["students", "Students", "students"], ["team", "Team", "team"],
  ["run", "Run", "play"], ["journal", "Journal", "list"], ["battles", "Battles", "swords"],
  ["lessons", "Lessons", "book"],
];
const TITLES = { home: "Dashboard", students: "Students", team: "Active team", run: "Run controls",
  journal: "Journal", battles: "Battle history", lessons: "Lessons" };
const ATTACKS = ["Explosive", "Piercing", "Mystic", "Sonic"];
const ARMORS = ["Light", "Heavy", "Special", "Elastic"];
const ROLES = ["Tank", "Dealer", "Healer", "Support", "T.S."];
const UNREAD = "—";

const S = { state: null, roster: null, battles: null, journal: null, lessons: null, tasks: null, art: null,
  students: null, advice: null, armor: "", attack: "", day: null, frame: null };
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ic = (name, cls = "") => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const route = () => { const r = location.hash.slice(1); return SECTIONS.some((s) => s[0] === r) ? r : "home"; };
const initials = (n) => esc(String(n || "?").replace(/\(.*\)/, "").trim().slice(0, 2));
const fmt = (v) => (typeof v === "number" ? v.toLocaleString("en-US") : v ?? UNREAD);
const typeClass = (v) => (v ? `t-${String(v).toLowerCase()}` : "");
const known = (v) => v !== null && v !== undefined && !(typeof v === "string" && v.startsWith("?"));

const _last = new Map();
function put(el, html) {           // only touch the DOM when something changed, so art never flickers
  if (!el || _last.get(el) === html) return;
  const top = el.scrollTop;
  el.innerHTML = html;
  el.scrollTop = top;
  _last.set(el, html);
}

async function api(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
  return r.json();
}
async function post(path, body = {}) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-BAClient": "1" }, body: JSON.stringify(body) });
    return await r.json();
  } catch (e) {
    return { ok: false, error: "the client isn't answering" };
  }
}
let toastTimer;
function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("on"), 3200);
}

/* ---------- small pieces ---------- */
function stars(n) {
  if (!known(n)) return `<span class="tiny muted">${UNREAD}</span>`;
  let out = "";
  for (let i = 0; i < 5; i++) out += ic("star", i < n ? "" : "off");
  return `<span class="stars" title="${n} stars">${out}</span>`;
}
const bond = (b) => `<span class="bond" title="Bond level">${ic("heart")}${known(b) ? esc(b) : UNREAD}</span>`;
const tico = (kind, v) => (known(v) ? `<span class="tico ${typeClass(v)}" title="${esc(v)} ${kind}">${ic(kind === "attack" ? "target" : "shield")}</span>` : "");
const typeChip = (kind, v) => (known(v) ? `<span class="chip type ${typeClass(v)}">${ic(kind === "attack" ? "target" : "shield")}${esc(v)}</span>` : "");
function avatar(p, extra = "") {
  const src = p.art && (p.art.small || p.art.icon || p.art.portrait);
  const inner = src ? `<img loading="lazy" src="${esc(src)}" alt="">` : `<span class="ph">${initials(p.name)}</span>`;
  return `<span class="avatar ${p.owned ? "owned" : ""}" title="${esc(p.name || "Unknown")}">${inner}${extra}</span>`;
}
function emptyState(icon, title, text, action = "") {
  return `<div class="fill"><div class="empty-state"><div class="empty-ic">${ic(icon)}</div><h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div></div>`;
}
function runBtn(task, label, cls = "primary") {
  const st = S.state || {};
  const off = !st.connected || st.busy || st.halt;
  const why = !st.connected ? "The client isn't running" : st.halt ? "Input is halted" : st.busy ? "A run is active" : "";
  return `<button class="btn ${cls}" data-run="${esc(task)}" ${off ? "disabled" : ""} title="${esc(why)}">${ic("play", "solid")}${esc(label)}</button>`;
}
const rosterEntry = (dev, name) => ((S.roster && S.roster.students) || []).find((s) => (dev && s.dev === dev) || (name && s.name === name));

/* ---------- frame ---------- */
function renderRail() {
  const r = route();
  put($("rail"), `<div class="brand" title="BAClient">${ic("halo")}</div>` +
    SECTIONS.map(([id, label, icon]) => `<button class="nav ${id === r ? "on" : ""}" data-nav="${id}" ${id === r ? 'aria-current="page"' : ""}>${ic(icon)}${label}</button>`).join("") +
    `<div class="rail-gap"></div>`);
}
function resPill(cls, icon, label, value) {
  const unk = !known(value);
  return `<div class="res ${cls}" title="${unk ? `${label}: not read yet` : esc(label)}">${ic(icon, "solid")}<div><small>${label}</small><b class="${unk ? "unk" : ""}">${unk ? UNREAD : esc(fmt(value))}</b></div></div>`;
}
function renderTop() {
  const st = S.state;
  if (!st) return;
  const r = st.resources || {};
  const dot = st.halt ? "bad" : st.busy ? "run" : st.connected ? "ok" : "";
  const line = !st.connected ? "Client not running" : st.busy ? `${st.run ? st.run.label : "Running"} · ${st.screen || ""}` : `Idle · ${st.screen || "-"}`;
  put($("top"), `<div class="where"><h1>${TITLES[route()]}</h1><p><span class="pulse ${dot}"></span>${esc(String(line).replaceAll("_", " "))}</p></div>` +
    resPill("ap", "bolt", "AP", r.ap) + resPill("cr", "coin", "Credits", r.credits) + resPill("py", "gem", "Pyroxene", r.pyroxene) +
    `<button class="btn stop" data-stop ${st.busy ? "" : "disabled"} title="Stop the current run"><span style="display:inline-flex;align-items:center;gap:7px">${ic("stop", "solid")}Stop <kbd>F8</kbd></span></button>`);
  let b = "";
  if (st.sample) b += `<div class="banner sample">${ic("alert")}Sample data for this preview. These students, numbers and fights are not your account.</div>`;
  const L = st.launcher || {};
  const clearHalt = `<code>python -m ba_auto ctl clear-halt --note "why"</code>`;
  const clearHold = `<code>python -m ba_auto ctl clear-login-hold --note "why"</code>`;
  const dupHalt = L.duplicate && st.halt;
  const panelFix = st.api >= 2 && !st.sample;            // this server can clear them from the page
  const fix = (attr) => (st.api >= 2 ? `<button class="btn small bfix" ${attr}>Review and clear</button>` : "");
  const haltStep = panelFix ? "Review it here when you've checked" : `After checking, run ${clearHalt}`;
  const holdStep = panelFix ? "review it here" : `run ${clearHold}`;
  if (dupHalt) b += `<div class="banner bad big">${ic("alert")}<span><b>Duplicate login: this needs you.</b> Someone else may be on this account, so all game input and auto login have stopped. Check the account first. ${haltStep}. Auto login stays off until you also clear the login hold or log in by hand.</span>${fix("data-halt-review")}</div>`;
  else if (st.halt) b += `<div class="banner bad">${ic("alert")}<span>Halted: ${esc(st.halt.reason)}. All game input is refused. ${haltStep}.</span>${fix("data-halt-review")}</div>`;
  if (L.login_hold && !dupHalt) b += `<div class="banner bad">${ic("alert")}<span><b>${L.duplicate ? "Duplicate login: auto login is on hold." : "Auto login is on hold."}</b> ${L.duplicate ? "Someone else may be on this account." : `The game said: ${esc(gameSaid(L.reason) || "the session ended")}.`} Nothing logs in by itself until you ${holdStep} or log in by hand.</span>${fix("data-hold-review")}</div>`;
  else if (L.needs_user && !L.duplicate && !st.halt) b += `<div class="banner bad">${ic("alert")}<span><b>The launcher needs you.</b> ${esc(L.reason || "It stopped without a reason.")}</span></div>`;
  if (!st.connected && !st.sample) b += `<div class="banner warn">${ic("alert")}The client isn't running, so live status and run controls are off. Saved data is still shown.</div>`;
  put($("banner"), b);
}

/* ---------- dashboard ---------- */
function heroHtml(st) {
  const f = st.featured || {};
  const src = f.art && f.art.portrait;
  if (!src) {
    const tiles = ((S.art && S.art.students) || []).filter((s) => !s.name.startsWith("CH")).slice(0, 16)
      .map((s) => `<img src="${esc(s.art.portrait)}" alt="" loading="lazy">`).join("");
    return `<section class="hero none"><div class="hero-empty">${tiles}</div>
      <div class="hero-tag"><span>${ic("sparkle")}</span><span>Featured student</span></div>
      <div class="hero-body"><div class="hero-name"><h2>Choose your student</h2>
      <div class="hero-meta"><span class="muted">Pick anyone from your art pack. Or follow your team leader once a team has been read from Formation.</span></div></div>
      <button class="btn primary" data-pick>${ic("students")}Choose</button></div></section>`;
  }
  const s = rosterEntry(f.dev, f.name);
  const meta = s ? `<span class="chip">Lv. ${known(s.level) ? esc(s.level) : UNREAD}</span>${stars(s.stars)}${bond(s.bond)}${typeChip("attack", s.attack)}${typeChip("armor", s.armor)}`
    : `<span class="muted tiny">Not in the last roster scan</span>`;
  const tag = f.mode === "leader" ? `${ic("crown")}</span><span>Team leader` : `${ic("sparkle")}</span><span>Featured`;
  return `<section class="hero"><img class="blur" src="${esc(src)}" alt=""><img class="art" src="${esc(src)}" alt="${esc(f.name)}">
    <div class="hero-tag"><span>${tag}</span></div>
    <div class="hero-body"><div class="hero-name"><h2>${esc(f.name)}</h2><div class="hero-meta">${meta}</div></div>
    ${f.mode === "chosen" ? `<button class="btn small" data-follow title="Show the team leader instead">${ic("crown")}Follow leader</button>` : ""}
    <button class="btn small" data-pick>${ic("students")}Change</button></div></section>`;
}
const LAUNCH_STEPS = [["launching", "Launch"], ["loading", "Load"], ["title", "Title"], ["waiting_idle", "Wait"], ["logging_in", "Log in"], ["lobby", "Lobby"]];
function launchCopy(L, st) {
  const screen = String(st.screen || "").replaceAll("_", " ");
  if (!st.connected) return ["idle", "power", "Launcher status unknown", "Open BAClient to see the game's state."];
  switch (L.stage) {
    case "not_running": return ["idle", "power", "Blue Archive isn't running", L.can_launch ? "Ready to launch through Steam." : launchBlock(L, st)];
    case "launching": return ["work", "play", "Launching", "Asking Steam to start the game."];
    case "loading": return ["work", "clock", "Loading", "Waiting for the title screen."];
    case "title": return L.auto_login === false ? ["ok", "halo", "At the title screen", "Auto login is off, so Click to Start is yours."]
      : L.auto_login ? ["work", "halo", "At the title screen", "Auto login is on and waits until you're idle."] : ["ok", "halo", "At the title screen", "The game is open."];
    case "waiting_idle": {
      const timed = L.idle_seconds !== null && L.idle_needed !== null;
      if (timed && L.idle_seconds >= L.idle_needed) return ["work", "clock", "Waiting for the game window", "You're idle. Login continues when Blue Archive is the front window."];
      return ["work", "clock", "Waiting for you to stop using the PC", `Login won't move your cursor while you're active.${timed ? ` Idle ${L.idle_seconds}s of ${L.idle_needed}s.` : ""}`];
    }
    case "logging_in": return ["work", "pointer", "Logging in", "Pressing Click to Start once."];
    case "lobby": return ["ok", "check", "Blue Archive is running", "In the lobby."];
    case "running": return ["ok", "check", "Blue Archive is running", screen ? `On ${screen}.` : "The game is open."];
    case "stopped": return [L.needs_user ? "bad" : "warn", "alert", L.duplicate ? "Duplicate login" : L.login_hold ? "Login on hold" : L.needs_user ? "Stopped: needs you" : "Launch stopped", L.duplicate ? "Someone else may be on this account. Nothing will be pressed." : L.login_hold ? `The game said: ${gameSaid(L.reason) || "the session ended"}.` : L.reason || "No reason was reported."];
    default: return ["idle", "clock", "Checking the game", "Waiting for the client's first read."];
  }
}
const gameSaid = (text) => String(text || "").replace(/^game says:\s*/i, "").replace(/[.\s]+$/, "");
function launchBlock(L, st) {
  if (!L.available || !L.reported) return "The launcher isn't available in this client yet.";
  if (st.halt) return "Input is halted.";
  if (st.busy) return "Waiting for the current run to finish.";
  return "Launch isn't available right now.";
}
function launchHtml(st) {
  const L = st.launcher || {};
  const [tone, icon, title, sub] = launchCopy(L, st);
  const at = LAUNCH_STEPS.findIndex(([k]) => k === L.stage);
  const steps = at >= 0 && L.stage !== "lobby" ? `<ol class="steps" aria-label="Launch progress">${LAUNCH_STEPS.map(([k, name], i) => `<li class="${i < at ? "done" : i === at ? "now" : ""}" ${i === at ? 'aria-current="step"' : ""}><span>${i < at ? ic("check") : i + 1}</span>${name}</li>`).join("")}</ol>` : "";
  const showButton = !L.game_running && !["launching", "loading", "waiting_idle", "logging_in"].includes(L.stage) && !L.needs_user;
  const button = showButton ? `<button class="btn launch" data-launch ${L.can_launch ? "" : "disabled"} title="${esc(L.can_launch ? "Start Blue Archive through Steam" : launchBlock(L, st))}">${ic("play", "solid")}Launch Blue Archive</button>` : "";
  const on = L.auto_login;
  const known2 = on === true || on === false;
  const toggle = `<div class="auto ${known2 ? "" : "off"}"><button class="switch ${on ? "on" : ""}" role="switch" aria-checked="${on ? "true" : "false"}" aria-label="Auto login" data-autologin="${on ? 0 : 1}" ${known2 && st.connected ? "" : "disabled"} title="${known2 ? "Press Click to Start automatically when you're idle" : "Not available in this client yet"}"><span></span></button><div><b>Auto login</b><small>${known2 ? (on ? (L.login_hold ? "On, but on hold" : "On") : "Off") : "Not available yet"}</small></div></div>`;
  return `<section class="card launcher ${tone}"><div class="launch-top"><div class="now-ic l-${tone}">${ic(icon, icon === "play" ? "solid" : "")}</div><div class="launch-text"><div class="tiny muted">Blue Archive</div><h3>${esc(title)}</h3><div class="tiny l-sub">${esc(sub)}</div></div>${button}${toggle}</div>${steps}</section>`;
}
function nowHtml(st) {
  let cls = "", icon = "clock", title = "Idle", sub = "Nothing is running.";
  if (st.halt) { cls = "bad"; icon = "alert"; title = "Halted"; sub = st.halt.reason; }
  else if (st.busy && st.run) { cls = "run"; icon = "play"; title = st.run.label; sub = `Started ${st.run.started} by ${st.run.actor}`; }
  else if (!st.connected) { icon = "power"; title = "Client not running"; sub = "Open BAClient to start runs."; }
  else if (st.last) { sub = `Last: ${st.last.label}, ${st.last.outcome} at ${st.last.started}`; }
  const stats = st.busy && st.run ? Object.entries(st.run.stats || {}) : [];
  return `<section class="card now"><div class="now-ic ${cls}">${ic(icon, icon === "play" ? "solid" : "")}</div><div style="min-width:0;flex:1"><div class="tiny muted">Current task</div><h3>${esc(title)}</h3><div class="tiny muted">${esc(sub)}</div>
    ${stats.length ? `<div class="statline">${stats.map(([k, v]) => `<span class="chip">${esc(k)} ${esc(v)}</span>`).join("")}</div>` : ""}</div>
    <button class="btn small" data-nav="run">${ic("play")}Runs</button></section>`;
}
function todoHtml(st) {
  const t = st.todo || { todo: [], unknown: [] };
  const names = { tasks: "Tasks", mail: "Mail", lesson: "Lessons", cafe: "Cafe", bounty: "Bounty", scrimmage: "Scrimmage", tactical_challenge: "Tactical" };
  const list = t.todo.map((row) => `<li><span class="tick">${ic("check")}</span><div><b>${esc(row.do)}</b></div></li>`).join("");
  let body = list ? `<ul class="todo-list">${list}</ul>` : "";
  if (!list) body = `<div class="mini-empty grow">${t.unknown.length ? "Nothing has been read since the last daily reset." : "All clear for today."}</div>`;
  const unk = t.unknown.length ? `<div class="unk-row"><span class="tiny muted">Not read yet:</span>${t.unknown.map((k) => `<span class="chip">${esc(names[k] || k)}</span>`).join("")}<span style="margin-left:auto">${runBtn("daily-scan", "Read now", "small")}</span></div>` : "";
  return `<section class="card todo"><h2>${ic("list")}Today</h2>${body}${unk}</section>`;
}
function teamStrip(st) {
  const m = st.team.members;
  const src = st.team.source === "formation" ? "from Formation" : st.team.source === "battle" ? "from your last fight" : "";
  let faces = "";
  for (let i = 0; i < 6; i++) {
    const p = m[i];
    if (!p) { faces += `<div class="face empty">${ic("students")}</div>`; continue; }
    const img = p.art.portrait || p.art.icon;
    faces += `<div class="face" title="${esc(p.name)}">${img ? `<img src="${esc(img)}" alt="">` : `<span class="ph">${initials(p.name)}</span>`}${i === 0 ? `<span class="lead">${ic("crown", "solid")}</span>` : ""}</div>`;
  }
  return `<section class="card"><h2 class="h">${ic("team")}Active team<span class="right">${m.length ? esc(src) : "Open Formation in game to read it"}</span></h2><div class="faces">${faces}</div></section>`;
}
function renderHome(v) {
  const st = S.state;
  if (!v.querySelector(".dash")) { _last.delete(v); v.innerHTML = `<div class="dash"><div id="d-hero" style="display:grid"></div><div class="dash-side"><div id="d-launch"></div><div id="d-now"></div><div id="d-todo" class="todo" style="display:flex"></div><div id="d-team"></div></div></div>`; }
  put($("d-hero"), heroHtml(st));
  put($("d-launch"), launchHtml(st));
  put($("d-now"), nowHtml(st));
  put($("d-todo"), todoHtml(st));
  put($("d-team"), teamStrip(st));
}

/* ---------- students (see students.js) ---------- */
function renderStudents(v) {
  if (!v.querySelector(".sb")) { _last.delete(v); Students.mount(v, { runBtn }); }
  Students.update(S.students, S.state);
}

/* ---------- team ---------- */
function renderTeam(v) {
  const t = S.state.team;
  const src = t.source === "formation" ? "Read from the Formation screen" : t.source === "battle" ? `From your last recorded fight, ${esc(t.when)}` : "";
  let squad = "";
  for (let i = 0; i < 6; i++) {
    const p = t.members[i];
    const role = i < 4 ? `<span class="role">Striker</span>` : `<span class="role sp">Special</span>`;
    if (!p) { squad += `<div class="slot none">${ic("students")}</div>`; continue; }
    const img = p.art.portrait || p.art.icon;
    const s = rosterEntry(p.dev, p.name);
    squad += `<div class="slot">${img ? `<img src="${esc(img)}" alt="">` : `<span class="ph">${initials(p.name)}</span>`}${role}<div class="cap">${i === 0 ? ic("crown") : ""} ${esc(p.name)}${s && known(s.level) ? `<div class="tiny muted">Lv.${esc(s.level)}</div>` : ""}</div></div>`;
  }
  const left = t.members.length ? `<section class="card"><h2 class="h">${ic("team")}Squad<span class="right">${src}</span></h2><div class="squad">${squad}</div></section>`
    : `<section class="card">${emptyState("team", "No team read yet", "Open the Formation screen in the game, or finish a fight, and your team appears here.")}</section>`;
  const a = S.advice;
  const pick = (id, label, list, cur) => `<select id="${id}" aria-label="${label}"><option value="">${label}</option>${list.map((o) => `<option ${o === cur ? "selected" : ""}>${o}</option>`).join("")}</select>`;
  let advice = `<div class="mini-empty">Pick the enemy's armor and attack.</div>`;
  if (a && a.empty) advice = `<div class="mini-empty">The advisor needs a roster scan first.</div>`;
  else if (a) advice = `<div class="adv-list">` + a.team.map((r) => `<div class="adv-row">${avatar(r)}<div style="flex:1;min-width:0"><b style="color:var(--navy)">${esc(r.name)}</b><div class="tiny muted">${esc(r.role || "")}${r.why && r.why.length ? " · " + esc(r.why[0]) : ""}</div></div>${tico("attack", r.attack)}${tico("armor", r.armor)}</div>`).join("") + `</div>` + a.notes.map((n) => `<div class="tiny muted" style="margin-top:6px">${esc(n)}</div>`).join("");
  put(v, `<div class="team-wrap">${left}<section class="card"><h2 class="h">${ic("sparkle")}Team advisor<span class="right">Suggests only</span></h2>
    <div style="display:flex;gap:8px;margin:10px 0 4px">${pick("armor", "Enemy armor", ARMORS, S.armor)}${pick("attack", "Enemy attack", ATTACKS, S.attack)}</div>${advice}</section></div>`);
}

/* ---------- run ---------- */
function plainDesc(text) {          // task descriptions are written for `ctl`, so drop its option hints
  const s = String(text || "").replace(/[;:]?\s*--\w.*$/, "").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}
function renderRun(v) {
  if (S.state.api >= 2) { Commands.render(v); return; }
  const data = S.tasks;
  if (!data) { put(v, `<div class="card mini-empty">Loading…</div>`); return; }
  const st = S.state;
  const groups = {};
  data.tasks.forEach((t) => (groups[t.group] = groups[t.group] || []).push(t));
  const order = ["Navigate", "Story", "Battle", "Plans", "Read only"];
  const groupIcon = { Navigate: "home", Story: "book", Battle: "swords", Plans: "list", "Read only": "eye" };
  const cards = order.filter((g) => groups[g]).map((g) => `<section class="card"><h2 class="h">${ic(groupIcon[g])}${g}</h2>` + groups[g].map((t) => `<div class="tile"><div class="tile-ic">${ic(t.icon)}</div><div style="flex:1;min-width:0"><b>${esc(t.label)}</b><p>${esc(plainDesc(t.desc))}</p>${t.spends ? `<p class="spend">${esc(t.spends)}</p>` : ""}</div>${runBtn(t.name, "Start", t.spends ? "gold small" : "primary small")}</div>`).join("") + `</section>`).join("");
  const tool = (what, icon, title, text) => `<div class="tile"><div class="tile-ic">${ic(icon)}</div><div style="flex:1"><b>${title}</b><p>${text}</p></div><button class="btn small" data-open="${what}" ${st.connected && !st.sample ? "" : "disabled"}>${ic("window")}Open</button></div>`;
  const overlayOn = st.overlay;
  const windows = `<section class="card"><h2 class="h">${ic("window")}Tools</h2>
    ${tool("sweeper", "radar", "Mission sweeper", "Plan sweeps with AP guards. Spends AP only when you confirm there.")}
    <div class="tile"><div class="tile-ic">${ic("map")}</div><div style="flex:1"><b>Lesson Atlas</b><p>Every location on one map. Mark rooms and teach them.</p></div><button class="btn small" data-nav="lessons">${ic("book")}Open</button></div>
    <div class="tile"><div class="tile-ic">${ic("eye")}</div><div style="flex:1"><b>Game overlay</b><p>Small status card. Clicks pass through it.</p></div><button class="btn small" data-overlay="${overlayOn ? 0 : 1}" ${overlayOn === null || overlayOn === undefined ? "disabled" : ""}>${overlayOn ? "Turn off" : "Turn on"}</button></div>
    <div class="tile"><div class="tile-ic">${ic("power")}</div><div style="flex:1"><b>Quit client</b><p>Closing this window leaves the client and F8 running.</p></div><button class="btn small" data-quit ${st.connected && !st.busy && !st.sample ? "" : "disabled"}>Quit</button></div></section>`;
  const ctl = data.ctl_only.length ? `<section class="card"><h2 class="h">${ic("key")}Command line only</h2><p class="tiny muted" style="margin:6px 0">These need options, spend something or can't be undone, so they stay in <code>ctl run</code>.</p>${data.ctl_only.map((t) => `<span class="chip" style="margin:0 4px 4px 0" title="${esc(t.desc)}">${esc(t.label)}</span>`).join("")}</section>` : "";
  put(v, `<div class="run-groups">${cards}${windows}${ctl}</div>`);
}

/* ---------- journal ---------- */
function renderJournal(v) {
  const j = S.journal;
  if (!j) { put(v, `<div class="card mini-empty">Loading…</div>`); return; }
  if (!j.rows.length) { put(v, emptyState("list", "Nothing in the journal yet", "Every click and run is recorded here once the client plays.")); return; }
  const rows = j.rows.map((r, i) => `<button class="jrow ${S.frame === i ? "on" : ""}" data-frame="${i}" ${r.frame ? "" : "disabled"}><span class="jic ${r.icon}">${ic(r.icon, r.icon === "play" || r.icon === "stop" ? "solid" : "")}</span><span class="jtime">${esc(r.time)}</span><span class="jtext"><b>${esc(r.text)}</b><span>${esc(r.actor)}${r.path ? " · " + esc(r.path) : ""}</span></span>${r.frame ? ic("eye", "muted") : ""}</button>`).join("");
  const sel = S.frame !== null && j.rows[S.frame];
  const days = j.days.map((d) => `<option ${d === j.day ? "selected" : ""}>${d}</option>`).join("");
  const side = sel && sel.frame ? `<h2 class="h">${ic("eye")}What the client saw<span class="right">${esc(sel.time)}</span></h2><div class="tiny muted" style="margin-top:3px">${esc(sel.text)}</div><img class="frame" src="${esc(sel.frame)}" alt="Game frame for this event">`
    : `<div class="fill"><div class="empty-state"><div class="empty-ic">${ic("eye")}</div><h3>Pick an event</h3><p>Events with an eye icon show the frame the client saw when it acted.</p></div></div>`;
  put(v, `<div class="split"><section class="card col"><h2 class="h">${ic("list")}Timeline<span class="right"><select id="day" aria-label="Day">${days}</select></span></h2><div class="scroll grow" style="margin-top:8px">${rows}</div></section><section class="card col scroll">${side}</section></div>`);
}

/* ---------- battles ---------- */
function renderBattles(v) {
  const b = S.battles;
  if (!b) { put(v, `<div class="card mini-empty">Loading…</div>`); return; }
  if (!b.total) { put(v, emptyState("swords", "No fights recorded yet", "Fights appear here as soon as the client sees one finish, including ones you play by hand.")); return; }
  const bosses = [...new Set(b.rows.map((r) => r.boss).filter(Boolean))].sort();
  const shown = S.boss ? b.rows.filter((r) => r.boss === S.boss) : b.rows;
  const rate = Math.round((b.won / b.total) * 100);
  const stat = (icon, val, label) => `<section class="card stat"><div class="tile-ic">${ic(icon)}</div><div><b>${val}</b><span class="tiny muted">${label}</span></div></section>`;
  const rows = shown.map((r) => `<div class="fight"><span class="badge ${r.outcome === "won" ? "won" : "defeat"}"><span>${r.outcome === "won" ? "WON" : "DEFEAT"}</span></span><div style="min-width:0"><b style="color:var(--navy)">${esc(r.boss || "Unknown enemy")}</b><div class="tiny muted">${esc(r.when)}</div></div><span class="num">${esc(r.time || UNREAD)}</span><span class="num">${typeof r.damage === "number" ? r.damage.toLocaleString("en-US") : UNREAD}</span><span class="mini-faces">${(r.team || []).map((p) => avatar(p)).join("")}</span></div>`).join("");
  put(v, `<div class="stats3">${stat("swords", b.total, "Fights")}${stat("check", b.won, "Won")}${stat("crown", rate + "%", "Win rate")}</div><section class="card"><h2 class="h">${ic("clock")}Recent fights<span class="right"><select id="boss" aria-label="Filter by enemy"><option value="">All enemies</option>${bosses.map((n) => `<option ${n === S.boss ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></span></h2><div style="margin-top:6px">${rows}</div></section>`);
}

/* ---------- lessons: the Atlas ---------- */
const ROOM_STATE = { queued: ["Queued", "clock"], opening: ["Opening", "clock"], teaching: ["Teaching", "play"], done: ["Done", "check"], failed: ["Failed", "alert"], skipped: ["Skipped", "dot"] };
const allRooms = (l) => [...l.locations, ...l.off_map].flatMap((loc) => loc.rooms.map((r) => ({ ...r, location: loc.title })));
const roomChip = (state) => { const [name, icon] = ROOM_STATE[state]; return `<span class="rstate ${state}">${ic(icon)}${name}</span>`; };
function ticketPips(l) {
  const { owned, max } = l.tickets, n = l.marks.length;
  if (owned === null || owned === undefined) return `<div class="tick-row"><span class="tiny muted">Today's tickets haven't been read.</span>${runBtn("daily-scan", "Read tickets", "small")}</div>`;
  const total = Math.max(max || 0, owned, n);
  let pips = "";
  for (let i = 0; i < total; i++) pips += `<span class="pip ${i < n ? "marked" : i < owned ? "have" : ""}"></span>`;
  return `<div class="tick-row"><b class="num">${n} / ${owned}</b><span class="tiny muted">tickets</span><span class="pips" aria-hidden="true">${pips}</span></div>`;
}
function scanLine(l) {
  if (l.scanning) return `<span class="pulse run"></span>Reading the game: ${l.read} location${l.read === 1 ? "" : "s"} so far`;
  if (!l.has_scan) return "No scan yet. Scanning spends nothing.";
  if (l.stale) return `<span class="warn-t">From before today's reset. Scan again.</span>`;
  if (!l.complete) return `<span class="warn-t">Partial scan: ${l.read} location${l.read === 1 ? "" : "s"}.</span>`;
  return `Scanned ${esc(String(l.scanned_at || "").slice(11, 16))} today`;
}
function teachBlock(l) {
  const t = l.teach;
  if (!t) return "";
  const names = { running: "Teaching", done: "Finished", stopped: "Stopped", failed: "Stopped on a failure", unreadable: "Status unreadable" };
  const byId = new Map(allRooms(l).map((r) => [r.id, r]));
  const rows = (t.rooms || []).map((r) => { const info = byId.get(r.id); const [loc, room] = String(r.id).split("|"); return `<div class="mrow"><div class="grow"><b>${esc(info ? info.name : room)}</b><small>${esc(loc)}${r.note ? ` · ${esc(r.note)}` : ""}</small></div>${ROOM_STATE[r.state] ? roomChip(r.state) : ""}</div>`; }).join("");
  const spent = known(t.tickets_start) && known(t.tickets_now) ? `<span class="right">Tickets ${esc(t.tickets_start)} → ${esc(t.tickets_now)}</span>` : "";
  const title = t.rehearse ? `Rehearsal: ${names[t.state] || t.state}` : names[t.state] || t.state;
  const lost = l.teach_interrupted ? `<p class="warn-t tiny" style="margin-top:6px">The run ended without finishing its status. Check the game before teaching again.</p>` : "";
  const failure = t.error && t.state !== "running" ? `<p class="warn-t tiny" style="margin-top:6px">${esc(t.error)}</p>` : "";
  const again = !l.teach_active && !t.rehearse ? allRooms(l).filter((r) => r.state === "skipped" && r.markable && !r.marked).length : 0;
  const remark = again ? `<button class="linkbtn" data-remark>Mark ${again} skipped room${again === 1 ? "" : "s"} again</button>` : "";
  return `<section class="card side-card"><h2 class="h">${ic(t.state === "running" ? "play" : t.state === "done" ? "check" : "alert")}${esc(title)}${spent}</h2>${failure}<div class="mlist">${rows}</div>${lost}${remark}</section>`;
}
function roomPicker(l) {
  const list = [...l.locations.filter((loc) => loc.scanned && loc.rooms.length), ...l.off_map];
  if (!list.length) return "";
  const chip = (r) => `<button class="rchip ${r.marked ? "on" : ""} ${r.state || ""}" data-room="${esc(r.id)}" aria-pressed="${r.marked}" ${r.marked || r.markable ? "" : `disabled title="${esc(r.why || "Can't be marked right now")}"`}>${r.locked ? ic("key") : ""}${esc(r.name)}${r.students.length ? ` <small>${r.students.filter((s) => s.owned).length}/${r.students.length}</small>` : ""}</button>`;
  return `<section class="card side-card"><h2 class="h">${ic("pin")}All rooms<span class="right">Yours / students</span></h2>${list.map((loc) => `<div class="rgroup"><b>${esc(loc.title)}</b><div>${loc.rooms.map(chip).join("")}</div></div>`).join("")}</section>`;
}
function atlasSide(l) {
  if (!l) return `<section class="card mini-empty">Loading…</section>`;
  const n = l.marks.length, byId = new Map(allRooms(l).map((r) => [r.id, r]));
  const marked = l.marks.map((id) => byId.get(id)).filter(Boolean);
  const groups = new Map();
  marked.forEach((r) => groups.set(r.location, [...(groups.get(r.location) || []), r]));
  const rows = [...groups].map(([loc, rooms]) => `<div class="mloc">${esc(loc)}</div>` + rooms.map((r) => `<div class="mrow"><div class="grow"><b>${esc(r.name)}</b><div class="mfaces">${r.students.filter((s) => s.owned).map((s) => avatar({ name: s.name, owned: true, art: { icon: s.icon } })).join("") || `<small>None of yours</small>`}</div></div><button class="iconbtn" data-room="${esc(r.id)}" aria-pressed="true" aria-label="Unmark ${esc(r.name)}" ${l.teach_active ? "disabled" : ""}>${ic("close")}</button></div>`).join("")).join("");
  const why = n ? l.blockers[0] : "";
  const label = `Teach ${n || ""} lesson${n === 1 ? "" : "s"}`.replace("  ", " ");
  const scanBtn = runBtn("lesson-scan", l.has_scan ? "Scan again" : "Scan lessons", l.has_scan && !l.stale ? "small" : "primary small");
  return `<section class="card side-card"><div class="side-head"><div><h2>Lesson Atlas</h2><p class="tiny muted">${scanLine(l)}</p></div>${scanBtn}</div>${ticketPips(l)}</section>
    ${teachBlock(l)}
    <section class="card side-card"><h2 class="h">${ic("check")}Marked rooms${n ? `<button class="linkbtn right" data-clear-marks ${l.teach_active ? "disabled" : ""}>Clear</button>` : ""}</h2>
      ${n ? `<div class="mlist">${rows}</div>` : `<p class="tiny muted" style="margin-top:6px">${l.has_scan ? "Click a student on the map to mark their room. One lesson teaches everyone in it." : "Scan first, then click students on the map to choose rooms."}</p>`}
      <button class="btn teach" data-teach ${l.can_teach ? "" : "disabled"}>${ic("book")}${esc(label)}</button>
      ${why ? `<p class="tiny warn-t" style="margin-top:6px">${esc(why)}</p>` : n ? `<p class="tiny muted" style="margin-top:6px">Marking spends nothing. Tickets are used only after you press Teach.</p>` : ""}
      ${n ? `<button class="linkbtn" data-rehearse ${l.can_teach ? "" : "disabled"} title="Opens and checks the first room, then closes it. Spends nothing.">Rehearse first room (spends nothing)</button>` : ""}
    </section>${roomPicker(l)}`;
}
function renderLessons(v) {
  if (!v.querySelector(".atlas-wrap")) {
    _last.delete(v);
    v.innerHTML = `<div class="atlas-wrap"><div id="atlas-stage"></div><aside id="atlas-side" aria-label="Lesson selection"></aside></div>`;
    LessonAtlas.mount($("atlas-stage"), { onRoom: clickRoom, searchIcon: ic("search") });
  }
  if (S.atlasMaps) LessonAtlas.setMaps(S.atlasMaps.locations);
  if (S.lessons) LessonAtlas.setLive(S.lessons);
  put($("atlas-side"), atlasSide(S.lessons));
}
let lessonEpoch = 0;
async function setRoom(id, on) {
  lessonEpoch++;
  const r = await post("/api/lessons/mark", { room: id, on });
  lessonEpoch++;
  if (r.lessons) S.lessons = r.lessons;
  if (!r.ok) toast(r.error || "Couldn't change that room");
  render();
}
function clickRoom(room) {
  if (room.marked) { setRoom(room.id, false); return; }
  if (!room.markable) { toast(room.why || "That room can't be marked right now"); return; }
  setRoom(room.id, true);
}
function openTeachConfirm() {
  const l = S.lessons;
  if (!l || !l.can_teach) return;
  const n = l.marks.length, byId = new Map(allRooms(l).map((r) => [r.id, r]));
  const rows = l.marks.map((id) => { const r = byId.get(id); return `<div class="mrow"><div class="grow"><b>${esc(r.name)}</b><small>${esc(r.location)}</small></div><div class="mfaces">${r.students.map((s) => avatar({ name: s.name, owned: s.owned, art: { icon: s.icon } })).join("")}</div></div>`; }).join("");
  $("modal").innerHTML = `<div class="scrim" data-close><div class="sheet confirm" role="dialog" aria-label="Teach ${n} lessons"><div class="sheet-head"><h2>Teach ${n} lesson${n === 1 ? "" : "s"}?</h2><button class="btn small" data-close aria-label="Close">${ic("close")}</button></div>
    <div class="sheet-body"><p style="margin-bottom:10px">This spends <b>${n} of your ${esc(l.tickets.owned)} tickets</b>, one per room. It can't be undone. Stop and F8 work throughout.</p><div class="mlist">${rows}</div>
    <div class="confirm-row"><button class="btn" data-close>Cancel</button><button class="btn primary" data-teach-go>${ic("book")}Teach ${n} lesson${n === 1 ? "" : "s"}</button></div></div></div></div>`;
}
async function startTeach(rehearse) {
  const l = S.lessons;
  if (!l) return;
  const r = await post("/api/lessons/teach", { rooms: l.marks, scan_ts: l.ts, rehearse, confirmed: !rehearse });
  closeModal();
  toast(r.ok ? (rehearse ? "Rehearsing the first room" : "Teaching started") : r.error || "Couldn't start that");
  await loadSection();
}

/* ---------- modals ---------- */
function openPicker() {
  const list = (S.art && S.art.students) || [];
  $("modal").innerHTML = `<div class="scrim" data-close><div class="sheet" role="dialog" aria-label="Choose a featured student"><div class="sheet-head"><h2>Choose your featured student</h2>
    <label class="search" style="flex:0 1 240px">${ic("search")}<input id="pickq" placeholder="Search" aria-label="Search students"></label>
    <button class="btn small" data-follow>${ic("crown")}Follow team leader</button><button class="btn small" data-close aria-label="Close">${ic("close")}</button></div>
    <div class="sheet-body"><div class="pick" id="picks">${pickTiles(list)}</div></div></div></div>`;
  $("pickq").focus();
}
const pickTiles = (list) => list.map((s) => `<button data-feature="${esc(s.dev)}" title="${esc(s.name)}"><img loading="lazy" src="${esc(s.art.portrait)}" alt=""><span>${esc(s.name)}</span></button>`).join("");
const closeModal = () => { $("modal").innerHTML = ""; };

/* ---------- render + data ---------- */
function render() {
  renderRail();
  renderTop();
  if (!S.state) return;
  const v = $("view");
  const r = route();
  if (v.dataset.r !== r) { if (v.dataset.r === "lessons") LessonAtlas.unmount(); if (v.dataset.r === "students") Students.unmount(); v.dataset.r = r; v.innerHTML = ""; _last.delete(v); v.scrollTop = 0; }
  ({ home: renderHome, students: renderStudents, team: renderTeam, run: renderRun, journal: renderJournal, battles: renderBattles, lessons: renderLessons }[r])(v);
}
const loadRoster = () => api("/api/roster").then((d) => (S.roster = d));
const LOADERS = { students: async () => {
    const d = await api("/api/students" + (S.students ? `?rev=${S.students.rev}` : ""));
    if (!d.same) S.students = d;
    return !d.same;
  }, journal: () => api("/api/journal" + (S.day ? `?day=${S.day}` : "")).then((d) => (S.journal = d)),
  battles: () => api("/api/battles").then((d) => (S.battles = d)), lessons: async () => {
    if (!S.atlasMaps) S.atlasMaps = await api("/api/atlas/maps");
    const asked = lessonEpoch;
    const d = await api("/api/lessons" + (S.lessons ? `?rev=${S.lessons.rev}` : ""));
    if (!d.same && asked === lessonEpoch) S.lessons = d;   // a reply from before the latest click is out of date
  }, run: async () => {
    if (S.state && S.state.api >= 2) await Commands.load();
    else S.tasks = await api("/api/tasks");
  } };
async function loadSection() {
  const r = route();
  try {
    if (LOADERS[r]) await LOADERS[r]();
    if ((r === "home" || r === "team") && ((await LOADERS.students()) || !S.roster)) await loadRoster();
    if (r === "home" && !S.art) S.art = await api("/api/art");
  } catch (e) { /* the next poll tries again */ }
  render();
}
let ticks = 0, stateFails = 0;
async function poll() {
  try {
    S.state = await api("/api/state");
    stateFails = 0;
  } catch (e) {
    if (++stateFails >= 3 && S.state) S.state = { ...S.state, connected: false, busy: false };
  }
  if (document.visibilityState === "visible") {
    const live = route() === "lessons" && S.state && S.state.busy;   // zones and room status fill in as they change
    if (++ticks % 5 === 0 || live) await loadSection(); else render();
  }
  setTimeout(poll, 1000);
}
async function advise() {
  if (!S.armor && !S.attack) { S.advice = null; render(); return; }
  const p = new URLSearchParams({ armor: S.armor, attack: S.attack });
  S.advice = await api("/api/advise?" + p).catch(() => null);
  render();
}
async function feature(value) {
  const r = await post("/api/prefs", { featured: value });
  if (!r.ok) { toast(r.error || "Couldn't save that"); return; }
  closeModal();
  S.state = await api("/api/state").catch(() => S.state);
  render();
}
async function startRun(task, confirmed = false) {
  const r = await post("/api/run", { task, confirmed });
  if (r.confirm && !confirmed) {
    if (window.confirm(`${r.confirm}\n\nStart it?`)) await startRun(task, true);
    return;
  }
  toast(r.ok ? "Started" : r.error || "Couldn't start that");
  S.state = await api("/api/state").catch(() => S.state);
  render();
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-nav],[data-room],[data-teach],[data-teach-go],[data-remark],[data-rehearse],[data-clear-marks],[data-stop],[data-launch],[data-autologin],[data-run],[data-open],[data-overlay],[data-quit],[data-pick],[data-follow],[data-feature],[data-frame],[data-close]");
  if (!t) return;
  const d = t.dataset;
  if ("close" in d) { if (e.target === t || t.tagName === "BUTTON") closeModal(); return; }
  if (t.disabled) return;
  if (d.nav) { location.hash = d.nav; return; }
  if (d.room) { await setRoom(d.room, d.room && t.getAttribute("aria-pressed") !== "true"); return; }
  if ("teach" in d) { openTeachConfirm(); return; }
  if ("teachGo" in d) { await startTeach(false); return; }
  if ("rehearse" in d) { await startTeach(true); return; }
  if ("remark" in d) { lessonEpoch++; const r = await post("/api/lessons/remark"); lessonEpoch++; if (r.lessons) S.lessons = r.lessons; if (!r.ok) toast(r.error); render(); return; }
  if ("clearMarks" in d) { lessonEpoch++; const r = await post("/api/lessons/clear"); lessonEpoch++; if (r.lessons) S.lessons = r.lessons; if (!r.ok) toast(r.error); render(); return; }
  if ("stop" in d) { const r = await post("/api/stop"); toast(r.ok ? "Stopping" : r.error); return; }
  if ("launch" in d) { await startRun("launch"); return; }
  if (d.autologin !== undefined) {
    const r = await post("/api/autologin", { on: d.autologin === "1" });
    if (!r.ok) toast(r.error || "Couldn't change auto login");
    S.state = await api("/api/state").catch(() => S.state);
    render();
    return;
  }
  if (d.run) { await startRun(d.run); return; }
  if (d.open) { const r = await post("/api/open", { what: d.open }); toast(r.ok ? "Opening" : r.error); return; }
  if (d.overlay !== undefined) { const r = await post("/api/overlay", { on: d.overlay === "1" }); if (!r.ok) toast(r.error); return; }
  if ("quit" in d) { if (window.confirm("Quit BAClient? F8 and ctl stop working until it's opened again.")) { const r = await post("/api/quit"); toast(r.ok ? "Closing" : r.error); } return; }
  if ("pick" in d) { if (!S.art) S.art = await api("/api/art").catch(() => null); openPicker(); return; }
  if ("follow" in d) { await feature("follow"); return; }
  if (d.feature) { await feature(d.feature); return; }
  if (d.frame !== undefined) { S.frame = Number(d.frame); render(); }
});
document.addEventListener("input", (e) => {
  const id = e.target.id;
  if (id === "pickq") {
    const q = e.target.value.toLowerCase();
    $("picks").innerHTML = pickTiles(((S.art && S.art.students) || []).filter((s) => s.name.toLowerCase().includes(q)));
  }
});
document.addEventListener("change", (e) => {
  const id = e.target.id;
  if (id === "armor" || id === "attack") { S[id] = e.target.value; advise(); }
  if (id === "boss") { S.boss = e.target.value; render(); }
  if (id === "day") { S.day = e.target.value; S.frame = null; S.journal = null; loadSection(); }
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
window.addEventListener("hashchange", () => { closeModal(); render(); loadSection(); });

(async function start() {
  try { S.state = await api("/api/state"); } catch (e) { /* shown by the poll */ }
  await loadSection();
  poll();
})();
