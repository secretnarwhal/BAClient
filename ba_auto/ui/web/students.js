"use strict";
/* The Students page. The grid keeps a small pool of recycled cards, so its cost follows the screen and
   not the roster. The art viewer stays mounted, so opening it is one paint over a thumbnail that's
   already decoded. Uses ic, esc, known and UNREAD from app.js. */
const Students = (() => {
  const ART_RATIO = 456 / 404;
  const PAD = 4;
  const DENSITIES = [
    { name: "Tiny", cell: 76, gap: 5, thumb: "thumb_small", foot: "none" },
    { name: "Small", cell: 104, gap: 6, thumb: "thumb_small", foot: "name" },
    { name: "Medium", cell: 140, gap: 8, thumb: "thumb", foot: "full" },
    { name: "Large", cell: 190, gap: 10, thumb: "thumb", foot: "full" },
  ];
  const GROUPS = [
    ["attack", "Attack", ["Explosive", "Piercing", "Mystic", "Sonic"]],
    ["armor", "Armor", ["Light", "Heavy", "Special", "Elastic"]],
    ["role", "Role", ["Tank", "Dealer", "Healer", "Support", "T.S."]],
    ["position", "Position", ["Front", "Middle", "Back"]],
    ["class", "Class", ["Striker", "Special"]],
    ["stars", "Stars", [1, 2, 3, 4, 5]],
  ];
  const SORTS = [["smart", "Yours first"], ["level", "Level"], ["stars", "Stars"], ["bond", "Bond"], ["name", "Name"],
    ["role", "Role"], ["position", "Position"], ["attack", "Attack"], ["armor", "Armor"]];
  const RANK = Object.fromEntries(GROUPS.filter((g) => g[0] !== "stars").map(([k, , list]) => [k, new Map(list.map((v, i) => [v, i]))]));
  const SCOPES = [["all", "All"], ["yes", "Yours"], ["no", "Not owned"], ["unknown", "Unknown"]];
  const SKILLS = [["ex", "EX"], ["basic", "Basic"], ["enhanced", "Enhanced"], ["sub", "Sub"]];
  const STATS = [["hp", "HP"], ["atk", "ATK"], ["def", "DEF"], ["healing", "Healing"]];
  const FULL_CACHE = 10;            // decoded portraits kept for instant reopening
  const FREE_CARDS = 48;            // spare cards kept after scrolling or changing density
  const IMAGES_PER_FRAME = 16, CALM_IMAGES_PER_FRAME = 5;
  const collator = new Intl.Collator("en", { sensitivity: "base", numeric: true });

  const F = { q: "", scope: "all", min: { level: null, bond: null }, sets: Object.fromEntries(GROUPS.map(([k]) => [k, new Set()])),
    sort: "smart", dir: 1, density: 1 };
  try {
    const saved = JSON.parse(localStorage.getItem("ba.students") || "{}");
    if (DENSITIES[saved.density]) F.density = saved.density;
    if (SORTS.some(([k]) => k === saved.sort)) F.sort = saved.sort;
    if (saved.dir === -1) F.dir = -1;
  } catch (e) { /* defaults are fine */ }
  const remember = () => { try { localStorage.setItem("ba.students", JSON.stringify({ density: F.density, sort: F.sort, dir: F.dir })); } catch (e) { /* optional */ } };

  let data = null, rev = null, all = [], view = [], opts = {}, calm = false, featuredDev = null, scanHtml = "";
  let root = null, scroller = null, space = null, ro = null;
  let cols = 1, cellW = 0, cellH = 0, gap = 0, rowH = 1, cursor = 0, frame = 0, pumpFrame = 0, hoverTimer = 0;
  const live = new Map(), free = new Map(), byEl = new WeakMap(), queue = [];

  /* ---------- filtering and sorting ---------- */
  const sortValue = (s, key) => {
    const v = s[key];
    if (v === undefined || v === null) return null;
    return RANK[key] ? (RANK[key].has(v) ? RANK[key].get(v) : null) : v;
  };
  const byName = (a, b) => (a._code - b._code) || collator.compare(a.name, b.name);
  function smart(a, b) {
    const owned = { yes: 0, unknown: 1, no: 2 };
    return (owned[a.owned] - owned[b.owned]) || (b.read - a.read) || ((b.level ?? -1) - (a.level ?? -1)) || byName(a, b);
  }
  function comparator() {
    const { sort: key, dir } = F;
    if (key === "smart") return dir === 1 ? smart : (a, b) => smart(b, a);
    if (key === "name") return (a, b) => byName(a, b) * dir;
    const high = key === "level" || key === "stars" || key === "bond" ? -1 : 1;   // numbers start highest
    return (a, b) => {
      const x = sortValue(a, key), y = sortValue(b, key);
      if (x === null || y === null) return (x === null) - (y === null) || byName(a, b);   // unread is always last
      return (x - y) * high * dir || byName(a, b);
    };
  }
  function passes(s, terms) {
    if (F.scope !== "all" && s.owned !== F.scope) return false;
    for (const t of terms) if (!s._q.includes(t)) return false;
    return true;
  }
  function apply(toTop) {
    const terms = F.q.toLowerCase().split(/\s+/).filter(Boolean);
    const active = GROUPS.map((g) => g[0]).filter((k) => F.sets[k].size);
    const { level, bond } = F.min;
    let blind = 0;
    const out = [];
    for (const s of all) {
      if (!passes(s, terms)) continue;
      let ok = true, unread = false;
      for (const k of active) {
        if (s[k] === undefined || s[k] === null) unread = true;
        else if (!F.sets[k].has(s[k])) ok = false;
      }
      if (level !== null) { if (s.level === undefined) unread = true; else if (s.level < level) ok = false; }
      if (bond !== null) { if (s.bond === undefined) unread = true; else if (s.bond < bond) ok = false; }
      if (!ok) continue;
      if (unread) { blind++; continue; }
      out.push(s);
    }
    out.sort(comparator());
    const keep = view[cursor] && view[cursor].id;
    view = out;
    const at = keep ? view.findIndex((s) => s.id === keep) : -1;
    cursor = at >= 0 && !toTop ? at : 0;
    renderCount(blind);
    relayout();
    if (toTop && scroller) scroller.scrollTop = 0;
    paint(true);
    if (viewer.open) syncViewer();
  }

  /* ---------- toolbar ---------- */
  function shell() {
    const scopes = SCOPES.map(([k, l]) => `<button type="button" data-scope="${k}"><span>${l}</span><b></b></button>`).join("");
    const sorts = SORTS.map(([k, l]) => `<option value="${k}">${l}</option>`).join("");
    const groups = GROUPS.map(([k, label, list]) => `<div class="fgroup" role="group" aria-label="${label}"><small>${label}</small>${list.map((v) =>
      `<button type="button" class="fchip ${k === "attack" || k === "armor" ? `t-${String(v).toLowerCase()}` : ""}" data-f="${k}" data-v="${esc(v)}" aria-pressed="false">${k === "attack" || k === "armor" ? "<i></i>" : ""}${k === "stars" ? `${v}${ic("star", "solid")}` : esc(v)}</button>`).join("")}</div>`).join("");
    return `<div class="sb">
      <div class="card sb-bar">
        <label class="search sb-search">${ic("search")}<input id="sb-q" placeholder="Search name, type or role" aria-label="Search students" autocomplete="off" spellcheck="false"><kbd>/</kbd></label>
        <div class="seg" role="group" aria-label="Ownership">${scopes}</div>
        <div class="sb-sort"><select id="sb-sort" aria-label="Sort by">${sorts}</select><button type="button" class="iconbtn" id="sb-dir" aria-label="Reverse the order" title="Reverse the order"></button></div>
        <label class="sb-density" title="How many students fit on screen"><span id="sb-dname"></span><input id="sb-density" type="range" min="0" max="${DENSITIES.length - 1}" step="1" aria-label="Card size"></label>
        <span class="sb-count" id="sb-count" role="status"></span>
      </div>
      <div class="card sb-filters">${groups}
        <div class="fgroup"><small>At least</small><label class="fnum">Lv<input id="sb-minlevel" type="number" min="1" max="99" inputmode="numeric" aria-label="Minimum level"></label><label class="fnum">${ic("heart")}<input id="sb-minbond" type="number" min="1" max="99" inputmode="numeric" aria-label="Minimum bond"></label></div>
        <button type="button" class="linkbtn" id="sb-clear" hidden>Clear filters</button>
        <span class="sb-scan" id="sb-scan"></span>
      </div>
      <div class="sb-scroll" id="sb-scroll" tabindex="0" role="grid" aria-label="Students. Arrow keys move, Enter opens."><div class="sb-space" id="sb-space"></div><div class="sb-none" id="sb-none" hidden></div></div>
    </div>`;
  }
  function syncControls() {
    root.querySelector("#sb-q").value = F.q;
    root.querySelector("#sb-sort").value = F.sort;
    root.querySelector("#sb-density").value = F.density;
    root.querySelector("#sb-dname").textContent = DENSITIES[F.density].name;
    root.querySelector("#sb-minlevel").value = F.min.level ?? "";
    root.querySelector("#sb-minbond").value = F.min.bond ?? "";
    root.querySelector("#sb-dir").innerHTML = `<span aria-hidden="true">${F.dir === 1 ? "↓" : "↑"}</span>`;
    root.querySelectorAll("[data-scope]").forEach((b) => b.classList.toggle("on", b.dataset.scope === F.scope));
    root.querySelectorAll("[data-f]").forEach((b) => {
      const v = b.dataset.f === "stars" ? Number(b.dataset.v) : b.dataset.v;
      const on = F.sets[b.dataset.f].has(v);
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
    });
    const any = F.q || F.scope !== "all" || F.min.level !== null || F.min.bond !== null || GROUPS.some(([k]) => F.sets[k].size);
    root.querySelector("#sb-clear").hidden = !any;
  }
  function renderCount(blind) {
    if (!root) return;
    const count = root.querySelector("#sb-count");
    count.textContent = `${view.length.toLocaleString("en-US")} of ${all.length.toLocaleString("en-US")}`;
    count.title = blind ? `${blind} more can't be checked against this filter because that value hasn't been read` : "";
    count.classList.toggle("blind", blind > 0);
    if (blind) count.textContent += ` · ${blind} unread`;
    const none = root.querySelector("#sb-none");
    none.hidden = view.length > 0;
    if (!view.length) {
      none.innerHTML = all.length
        ? `<div class="empty-state"><div class="empty-ic">${ic("search")}</div><h3>No students match</h3><p>${blind ? `${blind} more haven't had this value read yet, so they can't be matched.` : "Try fewer filters."}</p><button type="button" class="btn small" data-clear-all>Clear filters</button></div>`
        : `<div class="empty-state"><div class="empty-ic">${ic("students")}</div><h3>No students yet</h3><p>A roster scan reads each student from the game. It spends nothing.</p>${opts.runBtn ? opts.runBtn("roster-scan", "Scan roster") : ""}</div>`;
    }
    if (data) {
      const c = data.counts;
      root.querySelectorAll("[data-scope]").forEach((b) => { b.querySelector("b").textContent = c[b.dataset.scope] ?? 0; });
    }
  }
  function renderScan() {
    if (!root || !data) return;
    const when = data.scanned_at ? String(data.scanned_at).slice(5, 16) : null;
    const text = data.error ? "Roster couldn't be read"
      : !data.counts.read ? "No roster scan yet"
      : `${data.counts.read} read${data.complete ? "" : " · partial scan"}${data.counts.no_art ? ` · ${data.counts.no_art} without art` : ""}${when ? ` · ${when}` : ""}`;
    const html = `<span class="tiny muted">${esc(text)}</span>${opts.runBtn ? opts.runBtn("roster-scan", data.counts.read ? "Scan" : "Scan roster", "small") : ""}`;
    if (html !== scanHtml) { root.querySelector("#sb-scan").innerHTML = html; scanHtml = html; }
  }

  /* ---------- grid ---------- */
  function newCard() {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "sc";
    el.tabIndex = -1;
    el.innerHTML = `<span class="sc-ph"></span><img alt="" decoding="async" draggable="false"><span class="sc-types"><i></i><i></i></span><span class="sc-lv"></span><span class="sc-foot"><b></b><span class="sc-meta"></span></span>`;
    const [ph, img, types, lv, foot] = el.children;
    const card = { el, ph, img, a: types.children[0], d: types.children[1], lv, name: foot.children[0], meta: foot.children[1], id: null, want: null, i: -1 };
    img.onload = () => { if (img.getAttribute("src") === card.want) el.classList.add("ready"); };
    byEl.set(el, card);
    space.appendChild(el);
    return card;
  }
  function take(id) {
    let card = free.get(id);
    if (card) free.delete(id);
    else {
      const first = free.entries().next();
      if (!first.done) { card = first.value[1]; free.delete(first.value[0]); } else card = newCard();
    }
    card.el.hidden = false;
    return card;
  }
  function wantImage(card) {
    if (!card.want || card.queued || card.img.getAttribute("src") === card.want) return;
    card.queued = true;
    queue.push(card);
    pump();
  }
  function release(card) {
    card.el.hidden = true;
    card.el.classList.remove("cur");
    free.set(card.id ?? card, card);
  }
  function fill(card, s) {
    const density = DENSITIES[F.density];
    const key = `${s.id}|${density.thumb}`;
    if (card.key === key && card.rev === rev) return;
    card.key = key;
    card.rev = rev;
    card.id = s.id;
    const el = card.el;
    el.title = s.name;
    el.setAttribute("aria-label", cardLabel(s));
    el.classList.toggle("no", s.owned === "no");
    el.classList.toggle("yes", s.owned === "yes");
    card.ph.textContent = String(s.name).replace(/\(.*\)/, "").trim().slice(0, 2);
    card.a.className = s.attack ? `t-${s.attack.toLowerCase()}` : "off";
    card.d.className = s.armor ? `t-${s.armor.toLowerCase()}` : "off";
    card.lv.textContent = s.level !== undefined ? s.level : UNREAD;
    card.lv.classList.toggle("unk", s.level === undefined);
    card.name.textContent = s.name;
    card.meta.textContent = `★${s.stars ?? UNREAD}  ♥${s.bond ?? UNREAD}`;
    const url = s.art[density.thumb] || s.art.small || s.art.icon || null;
    if (url !== card.want) {
      el.classList.remove("ready");
      card.img.removeAttribute("src");
      card.want = url;
    }
  }
  const cardLabel = (s) => `${s.name}, ${ownedText(s)}, level ${s.level ?? "unread"}, ${s.stars ?? "unread"} stars, bond ${s.bond ?? "unread"}`;
  function pump() {
    if (pumpFrame) return;
    pumpFrame = requestAnimationFrame(() => {
      pumpFrame = 0;
      let budget = calm ? CALM_IMAGES_PER_FRAME : IMAGES_PER_FRAME;
      while (queue.length && budget > 0) {
        const card = queue.shift();
        card.queued = false;
        if (card.el.hidden || !card.want || card.img.getAttribute("src") === card.want) continue;
        card.img.src = card.want;
        budget--;
      }
      if (queue.length) pump();
    });
  }
  function relayout() {
    if (!scroller) return;
    const d = DENSITIES[F.density];
    const width = Math.max(1, scroller.clientWidth - PAD * 2);
    gap = d.gap;
    cols = Math.max(1, Math.floor((width + gap) / (d.cell + gap)));
    cellW = (width - gap * (cols - 1)) / cols;
    cellH = Math.round(cellW * ART_RATIO);
    rowH = cellH + gap;
    const rows = Math.ceil(view.length / cols);
    space.style.height = `${rows ? rows * rowH - gap + PAD * 2 : 0}px`;
    space.style.setProperty("--cw", `${cellW}px`);
    space.style.setProperty("--ch", `${cellH}px`);
    space.dataset.foot = d.foot;
  }
  function paint(rebind) {
    if (!scroller) return;
    const top = scroller.scrollTop, height = scroller.clientHeight;
    const overscan = 1;
    const firstRow = Math.max(0, Math.floor((top - PAD) / rowH) - overscan);
    const lastRow = Math.ceil((top + height) / rowH) + overscan;
    const first = firstRow * cols, last = Math.min(view.length, lastRow * cols);
    if (rebind) { for (const card of live.values()) release(card); live.clear(); }
    else for (const [i, card] of live) if (i < first || i >= last) { release(card); live.delete(i); }
    for (let i = first; i < last; i++) {
      let card = live.get(i);
      const s = view[i];
      if (!card) {
        card = take(s.id);
        live.set(i, card);
        card.i = i;
        fill(card, s);
        wantImage(card);
        const col = i % cols, row = Math.floor(i / cols);
        card.el.style.transform = `translate(${PAD + col * (cellW + gap)}px,${PAD + row * rowH}px)`;
      }
      card.el.classList.toggle("cur", i === cursor);
    }
    while (free.size > FREE_CARDS) {
      const [key, card] = free.entries().next().value;
      free.delete(key);
      card.el.remove();
    }
  }
  function schedule() {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; paint(false); });
  }
  function setCursor(i) {
    if (!view.length) return;
    cursor = Math.max(0, Math.min(view.length - 1, i));
    const y = PAD + Math.floor(cursor / cols) * rowH;
    if (y < scroller.scrollTop) scroller.scrollTop = y - PAD;
    else if (y + cellH > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = y + cellH + PAD - scroller.clientHeight;
    paint(false);
    if (!calm) preload(fullUrl(view[cursor]));
  }

  /* ---------- decoded portraits ---------- */
  const decoded = new Map();
  const fullUrl = (s) => s.art.full || s.art.portrait || s.art.icon || null;
  function preload(url) {
    if (!url) return Promise.resolve(false);
    const hit = decoded.get(url);
    if (hit) { decoded.delete(url); decoded.set(url, hit); return hit.done; }
    const image = new Image();
    image.decoding = "async";
    image.src = url;
    const rec = { image, done: image.decode().then(() => true, () => { decoded.delete(url); return false; }) };
    decoded.set(url, rec);
    while (decoded.size > FULL_CACHE) decoded.delete(decoded.keys().next().value);
    return rec.done;
  }

  /* ---------- the art viewer ---------- */
  const viewer = { el: null, art: null, thumb: null, full: null, ph: null, info: null, bg: null, open: false, token: 0, id: null, leaving: null };
  function buildViewer() {
    if (viewer.el) return;
    const el = document.createElement("div");
    el.id = "sv";
    el.hidden = true;
    el.tabIndex = -1;
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.innerHTML = `<div class="sv-bg" data-sv-close></div>
      <div class="sv-stage" data-sv-close>
        <button type="button" class="sv-nav prev" data-sv-step="-1" aria-label="Previous student"><span aria-hidden="true">‹</span></button>
        <div class="sv-art"><span class="sv-ph"></span><img class="sv-thumb" alt="" decoding="sync" draggable="false"><img class="sv-full" alt="" decoding="async" draggable="false"></div>
        <button type="button" class="sv-nav next" data-sv-step="1" aria-label="Next student"><span aria-hidden="true">›</span></button>
      </div>
      <aside class="sv-info" aria-live="polite"></aside>
      <button type="button" class="sv-close" data-sv-close aria-label="Close">${ic("close")}</button>`;
    document.body.appendChild(el);
    Object.assign(viewer, { el, bg: el.querySelector(".sv-bg"), art: el.querySelector(".sv-art"), thumb: el.querySelector(".sv-thumb"),
      full: el.querySelector(".sv-full"), ph: el.querySelector(".sv-ph"), info: el.querySelector(".sv-info") });
    el.addEventListener("click", (e) => {
      const step = e.target.closest("[data-sv-step]");
      if (step) { stepViewer(Number(step.dataset.svStep)); return; }
      if (e.target.closest("[data-sv-close]") && (e.target.hasAttribute("data-sv-close") || e.target.closest("button"))) closeViewer();
    });
  }
  function ownedText(s) {
    if (s.owned_note) return s.owned_note;
    if (s.owned === "yes") return s.owned_from === "lessons" ? "Yours, seen in Lessons" : "Yours";
    if (s.owned === "no") return s.owned_from === "lessons" ? "Not owned at the last lesson scan" : "Not owned";
    return "Not scanned";
  }
  const cell = (label, value, cls = "") => `<div class="sv-cell ${cls}"><small>${label}</small><b class="${known(value) ? "" : "unk"}">${known(value) ? esc(typeof value === "number" ? value.toLocaleString("en-US") : value) : UNREAD}</b></div>`;
  const gearText = (g) => (typeof g === "number" ? `T${g}` : g);
  const weaponText = (v) => (v === true ? "Locked" : v === false ? "Unlocked" : null);
  function infoHtml(s) {
    const chips = [s.class, s.role, s.position, s.weapon_type].filter(known).map((v) => `<span class="chip">${esc(v)}</span>`).join("");
    const types = `${s.attack ? `<span class="chip type t-${s.attack.toLowerCase()}">${ic("target")}${esc(s.attack)}</span>` : ""}${s.armor ? `<span class="chip type t-${s.armor.toLowerCase()}">${ic("shield")}${esc(s.armor)}</span>` : ""}`;
    const feature = s.dev && s.art.portrait ? (featuredDev === s.dev ? `<span class="chip sv-featured">${ic("sparkle")}Featured</span>` : `<button type="button" class="btn small" data-feature="${esc(s.dev)}">${ic("sparkle")}Feature</button>`) : "";
    const head = `<div class="sv-top"><span class="sv-owned ${s.owned}">${esc(ownedText(s))}</span>${feature}</div><h2>${esc(s.name)}</h2><div class="sv-chips">${types}${chips}</div>`;
    if (!s.read) {
      const coded = /^CH\d+$/.test(s.name);
      const why = coded ? `This art only has a code so far, so it can't be matched to a name in your roster.${s.owned === "no" ? "" : " Its stats may be under another card."}`
        : "Level, stars, bond, stats, skills and gear appear after a roster scan reads this student. A scan spends nothing.";
      return `${head}<div class="sv-empty">${ic("eye")}<div><b>${coded ? "Name not linked yet" : "Not read from the game yet"}</b><p>${why}</p>${!coded && opts.runBtn ? opts.runBtn("roster-scan", "Scan roster", "small") : ""}</div></div>`;
    }
    const stats = s.stats || {}, skills = s.skills || {}, gear = s.gear || [], terrain = s.terrain || {};
    const stars = known(s.stars) ? `<span class="stars" title="${esc(s.stars)} stars">${[1, 2, 3, 4, 5].map((n) => ic("star", n <= s.stars ? "" : "off")).join("")}</span>` : `<b class="unk">${UNREAD}</b>`;
    const land = Object.keys(terrain).length ? `<section><h3>Terrain</h3><div class="sv-row">${Object.entries(terrain).map(([k, v]) => `<div class="sv-cell"><small>${esc(k[0].toUpperCase() + k.slice(1))}</small><b class="land ${known(v) ? esc(v) : "unk"}">${known(v) ? `<i></i>${esc(v)}` : UNREAD}</b></div>`).join("")}</div></section>` : "";
    const unread = (s.unread || []).filter((u) => u !== "name");
    return `${head}<div class="sv-big">${cell("Level", s.level)}<div class="sv-cell"><small>Stars</small>${stars}</div><div class="sv-cell bondc"><small>Bond</small><b class="${known(s.bond) ? "" : "unk"}">${ic("heart")}${known(s.bond) ? esc(s.bond) : UNREAD}</b></div></div>
      <section><h3>Stats</h3><div class="sv-row">${STATS.map(([k, l]) => cell(l, stats[k])).join("")}</div></section>
      <section><h3>Skills</h3><div class="sv-row">${SKILLS.map(([k, l]) => cell(l, skills[k])).join("")}</div></section>
      <section><h3>Gear</h3><div class="sv-row">${[0, 1, 2].map((i) => cell(`Slot ${i + 1}`, gear[i] === undefined || gear[i] === null ? null : gearText(gear[i]))).join("")}${cell("Weapon", weaponText(s.weapon_locked))}</div></section>
      ${land}
      <p class="sv-foot">${s.art.portrait ? "" : "No art is linked to this name yet.<br>"}${s.scanned_at ? `Read ${esc(s.scanned_at)}` : ""}${unread.length ? `<br>Not read: ${esc(unread.join(", "))}` : ""}</p>`;
  }
  function showArt(s) {
    const token = ++viewer.token;
    const url = fullUrl(s), small = s.art[DENSITIES[F.density].thumb] || s.art.thumb || s.art.small || s.art.icon;
    viewer.ph.textContent = String(s.name).replace(/\(.*\)/, "").trim().slice(0, 2);
    viewer.art.classList.toggle("has-art", Boolean(url || small));
    viewer.full.classList.remove("on");
    if (small) { if (viewer.thumb.getAttribute("src") !== small) viewer.thumb.src = small; viewer.thumb.hidden = false; }
    else { viewer.thumb.removeAttribute("src"); viewer.thumb.hidden = true; }
    if (!url || url === small) return;
    preload(url).then(async (ok) => {
      if (!ok || token !== viewer.token) return;
      if (viewer.full.getAttribute("src") !== url) viewer.full.src = url;
      try { await viewer.full.decode(); } catch (e) { return; }
      if (token === viewer.token) viewer.full.classList.add("on");
    });
  }
  function neighbours() {
    if (calm) return;
    const go = () => { [cursor - 1, cursor + 1].forEach((i) => { if (view[i]) preload(fullUrl(view[i])); }); };
    if (window.requestIdleCallback) requestIdleCallback(go, { timeout: 400 }); else setTimeout(go, 120);
  }
  function showAt(i) {
    if (!view.length) return;
    cursor = Math.max(0, Math.min(view.length - 1, i));
    const s = view[cursor];
    viewer.id = s.id;
    viewer.el.setAttribute("aria-label", s.name);
    viewer.info.innerHTML = infoHtml(s);
    viewer.info.scrollTop = 0;
    showArt(s);
    neighbours();
  }
  function stepViewer(by) {
    const next = cursor + by;
    if (next >= 0 && next < view.length) showAt(next);
  }
  const moving = () => !calm && !matchMedia("(prefers-reduced-motion: reduce)").matches;
  function openAt(i, from) {
    if (!view[i]) return;
    buildViewer();
    if (viewer.leaving) { viewer.leaving.cancel(); viewer.leaving = null; }
    viewer.open = true;
    viewer.el.hidden = false;
    document.getElementById("app").inert = true;
    viewer.el.classList.toggle("calm", calm);
    showAt(i);
    viewer.el.focus({ preventScroll: true });
    if (from && moving()) {
      const a = from.getBoundingClientRect(), b = viewer.art.getBoundingClientRect();
      viewer.art.animate([{ transform: `translate(${a.left - b.left}px,${a.top - b.top}px) scale(${a.width / b.width})` }, { transform: "none" }], { duration: 170, easing: "cubic-bezier(.2,.8,.2,1)" });
      viewer.bg.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 130 });
      viewer.info.animate([{ opacity: 0, transform: "translateX(16px)" }, { opacity: 1, transform: "none" }], { duration: 170, easing: "ease-out" });
    }
  }
  function closeViewer(animate = true) {
    if (!viewer.open) return;
    viewer.open = false;
    viewer.token++;
    const app = document.getElementById("app");
    if (app) app.inert = false;
    const finish = () => { viewer.el.hidden = true; viewer.leaving = null; };
    if (scroller) { setCursor(cursor); scroller.focus({ preventScroll: true }); }
    const card = live.get(cursor);
    if (animate && card && moving()) {
      const a = card.el.getBoundingClientRect(), b = viewer.art.getBoundingClientRect();
      const anim = viewer.art.animate([{ transform: "none" }, { transform: `translate(${a.left - b.left}px,${a.top - b.top}px) scale(${a.width / b.width})` }], { duration: 120, easing: "ease-in" });
      viewer.bg.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 120, fill: "forwards" }).finished.then((x) => x.cancel(), () => {});
      viewer.info.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 80, fill: "forwards" }).finished.then((x) => x.cancel(), () => {});
      viewer.leaving = anim;
      anim.finished.then(finish, () => {});
    } else finish();
  }
  function syncViewer() {
    const at = view.findIndex((s) => s.id === viewer.id);
    if (at < 0) { closeViewer(false); return; }
    cursor = at;
    viewer.info.innerHTML = infoHtml(view[at]);
  }

  /* ---------- events ---------- */
  let searchTimer = 0;
  function onInput(e) {
    const id = e.target.id;
    if (id === "sb-q") {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { F.q = e.target.value; syncControls(); apply(true); }, 0);
    } else if (id === "sb-density") setDensity(Number(e.target.value));
    else if (id === "sb-minlevel" || id === "sb-minbond") {
      const n = e.target.value === "" ? null : Number(e.target.value);
      F.min[id === "sb-minlevel" ? "level" : "bond"] = Number.isFinite(n) && n > 0 ? n : null;
      syncControls();
      apply(true);
    }
  }
  function onChange(e) {
    if (e.target.id === "sb-sort") { F.sort = e.target.value; remember(); apply(true); }
  }
  function setDensity(i) {
    if (!DENSITIES[i] || i === F.density) return;
    const anchor = Math.floor((scroller.scrollTop + PAD) / rowH) * cols;
    F.density = i;
    remember();
    syncControls();
    relayout();
    scroller.scrollTop = Math.floor(anchor / cols) * rowH;
    paint(true);
  }
  function clearFilters() {
    F.q = ""; F.scope = "all"; F.min.level = F.min.bond = null;
    GROUPS.forEach(([k]) => F.sets[k].clear());
    syncControls();
    apply(true);
  }
  function onClick(e) {
    const t = e.target.closest("[data-scope],[data-f],#sb-dir,#sb-clear,[data-clear-all],.sc");
    if (!t) return;
    if (t.classList.contains("sc")) {
      const card = byEl.get(t);
      if (card) openAt(card.i, t);
    } else if (t.dataset.scope) { F.scope = t.dataset.scope; syncControls(); apply(true); }
    else if (t.dataset.f) {
      const v = t.dataset.f === "stars" ? Number(t.dataset.v) : t.dataset.v, set = F.sets[t.dataset.f];
      if (set.has(v)) set.delete(v); else set.add(v);
      syncControls();
      apply(true);
    } else if (t.id === "sb-dir") { F.dir = -F.dir; remember(); syncControls(); apply(true); }
    else clearFilters();
  }
  function onHover(e) {
    if (calm) return;
    const t = e.target.closest(".sc");
    clearTimeout(hoverTimer);
    if (!t) return;
    hoverTimer = setTimeout(() => { const card = byEl.get(t); if (card && view[card.i]) preload(fullUrl(view[card.i])); }, 70);
  }
  function onGridKey(e) {
    if (e.target !== scroller) return;
    const page = Math.max(1, Math.floor(scroller.clientHeight / rowH)) * cols;
    const moves = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols, PageDown: page, PageUp: -page };
    if (e.key in moves) { e.preventDefault(); setCursor(cursor + moves[e.key]); }
    else if (e.key === "Home") { e.preventDefault(); setCursor(0); }
    else if (e.key === "End") { e.preventDefault(); setCursor(view.length - 1); }
    else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const card = live.get(cursor);
      openAt(cursor, card && card.el);
    }
  }
  function onDocKey(e) {
    if (viewer.open) {
      const keys = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 };
      if (e.key === "Escape") closeViewer();
      else if (keys[e.key]) { e.preventDefault(); stepViewer(keys[e.key]); }
      else if (e.key === "Home") { e.preventDefault(); showAt(0); }
      else if (e.key === "End") { e.preventDefault(); showAt(view.length - 1); }
      return;
    }
    if (e.key === "/" && !viewer.open && !/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) {
      e.preventDefault();
      const q = root && root.querySelector("#sb-q");
      if (q) { q.focus(); q.select(); }
    } else if (e.key === "Escape" && e.target.id === "sb-q") {
      e.target.value = "";
      F.q = "";
      syncControls();
      apply(true);
      scroller.focus({ preventScroll: true });
    }
  }

  /* ---------- lifetime ---------- */
  function mount(host, options) {
    opts = options || {};
    host.innerHTML = shell();
    root = host.firstElementChild;
    scroller = root.querySelector("#sb-scroll");
    space = root.querySelector("#sb-space");
    scanHtml = "";
    root.addEventListener("input", onInput);
    root.addEventListener("change", onChange);
    root.addEventListener("click", onClick);
    space.addEventListener("pointerover", onHover, { passive: true });
    scroller.addEventListener("keydown", onGridKey);
    scroller.addEventListener("scroll", schedule, { passive: true });
    document.addEventListener("keydown", onDocKey);
    ro = new ResizeObserver(() => { relayout(); paint(true); });
    ro.observe(scroller);
    syncControls();
    if (data) apply(false);
  }
  function unmount() {
    if (!root) return;
    closeViewer(false);
    document.removeEventListener("keydown", onDocKey);
    ro.disconnect();
    clearTimeout(hoverTimer);
    if (frame) cancelAnimationFrame(frame);
    if (pumpFrame) cancelAnimationFrame(pumpFrame);
    frame = pumpFrame = 0;
    live.clear();
    free.clear();
    queue.length = 0;
    decoded.clear();
    root = scroller = space = null;
  }
  function update(next, state) {
    const busy = Boolean(state && state.busy);
    const dev = state && state.featured ? state.featured.dev : null;
    if (busy !== calm) {
      calm = busy;
      root && root.classList.toggle("calm", calm);
      viewer.el && viewer.el.classList.toggle("calm", calm);
    }
    if (dev !== featuredDev) {
      featuredDev = dev;
      if (viewer.open && view[cursor]) viewer.info.innerHTML = infoHtml(view[cursor]);
    }
    if (next && next.rev !== rev) {
      data = next;
      rev = next.rev;
      all = next.students.map((s) => Object.assign(s, {
        _code: /^CH\d/.test(s.name) ? 1 : 0,
        _q: [s.name, s.dev, s.attack, s.armor, s.role, s.position, s.class].filter(Boolean).join(" ").toLowerCase(),
      }));
      if (root) apply(false);
    }
    renderScan();
  }
  return { mount, unmount, update, get rev() { return rev; } };
})();
