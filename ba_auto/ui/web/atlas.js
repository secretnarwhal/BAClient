"use strict";
/* The Lesson Atlas inside the client: every location's real map with the scanned students on it.
   Clicking a student marks their whole room, because one lesson teaches everyone in it. */
const LessonAtlas = (() => {
  const GAP = 520, TITLE = 330, BALLOON = 178, SLANT = Math.tan(10 * Math.PI / 180);
  const STATE_COLOR = { queued: "#2fb0f4", opening: "#1583cc", teaching: "#1583cc", done: "#2fc08b", failed: "#e8485c", skipped: "#94a9c0" };
  const STATE_NAME = { queued: "Queued", opening: "Opening", teaching: "Teaching", done: "Done", failed: "Failed", skipped: "Skipped" };
  const MARK = "#ffbe2e", HOVER = "#ffe14d";
  const FONT = "'Segoe UI Variable Text','Segoe UI',sans-serif";
  const images = new Map();
  let host = null, canvas, g, tip, places, opts = {}, raf = 0, observer = null;
  let maps = [], mapsRef = null, live = new Map(), cells = [], size = { w: 1, h: 1 };
  let cam = { x: 0, y: 0, z: 0.1 }, want = null, W = 0, H = 0, DPR = 1, dirty = true, fitted = false;
  let needle = "", onlyOwned = false, bigIcons = true, hover = null, drag = null, moved = false, pulsing = false, lastPulse = 0, placesKey = "";
  let userMoved = false, ticker = 0;

  function image(src) {
    let im = images.get(src);
    if (!im) {
      im = new Image();
      im.onload = () => { dirty = true; };
      im.onerror = () => { im.failed = true; };
      im.src = src;
      images.set(src, im);
    }
    return im.complete && im.naturalWidth && !im.failed ? im : null;
  }
  const sprite = (n) => image(`/art/maps/ui/${n}.png`);
  const roomsOf = (c) => (live.get(c.loc.key) || {}).rooms || [];
  const toX = (x) => (x - cam.x) * cam.z;
  const toY = (y) => (y - cam.y) * cam.z;
  const seen = (s) => !onlyOwned || s.owned;
  const matches = (s) => seen(s) && (!needle || String(s.name || "").toLowerCase().includes(needle));
  const balloonScale = () => (bigIcons ? Math.max(1, 30 / (BALLOON * cam.z)) : 1);

  const insetLeft = () => (W > 700 ? 196 : 10);
  function arrange(sized, cols) {
    const cw = Math.max(1, ...sized.map((c) => c.w)) + GAP, placed = [];
    let y = 0;
    for (let r = 0; r * cols < sized.length; r++) {
      const row = sized.slice(r * cols, (r + 1) * cols), tall = Math.max(...row.map((c) => c.h));
      row.forEach((c, i) => placed.push({ ...c, x: i * cw + (cw - GAP - c.w) / 2, y: y + TITLE + (tall - c.h) / 2 }));
      y += tall + TITLE + GAP;
    }
    return { placed, w: Math.max(1, cols * cw - GAP), h: Math.max(1, y - GAP) };
  }
  function layout() {                           // the column count that makes the maps largest in this space
    const sized = maps.map((loc) => {
      const left = Math.min(...loc.layers.map((l) => l.x - l.w / 2)), right = Math.max(...loc.layers.map((l) => l.x + l.w / 2));
      const top = Math.max(...loc.layers.map((l) => l.y + l.h / 2)), bottom = Math.min(...loc.layers.map((l) => l.y - l.h / 2));
      return { loc, w: right - left, h: top - bottom, mx: -left, my: top };
    });
    const roomW = Math.max(1, W - insetLeft() - 14), roomH = Math.max(1, H - 72);
    let best = null;
    for (let cols = 1; cols <= Math.max(1, sized.length); cols++) {
      const a = arrange(sized, cols), z = Math.min(roomW / a.w, roomH / a.h);
      if (!best || z > best.z) best = { ...a, z, cols };
    }
    cells = best ? best.placed : [];
    size = best ? { w: best.w, h: best.h } : { w: 1, h: 1 };
  }
  function fit(box, animate = true) {
    const left = insetLeft(), top = 58;
    const z = Math.min((W - left - 14) / box.w, (H - top - 14) / box.h);
    const next = { z, x: box.x + box.w / 2 - (left + (W - left - 14) / 2) / z, y: box.y + box.h / 2 - (top + (H - top - 14) / 2) / z };
    if (animate && !matchMedia("(prefers-reduced-motion: reduce)").matches) want = next;
    else { cam = next; want = null; }
    dirty = true;
  }
  const fitAll = (animate) => fit({ x: 0, y: 0, w: size.w, h: size.h }, animate);
  const fitCell = (c) => { userMoved = true; fit({ x: c.x - 60, y: c.y - TITLE, w: c.w + 120, h: c.h + TITLE + 60 }); };

  function slantPath(cx, cy, w, h, r) {
    const x0 = -w / 2, x1 = w / 2, y0 = -h / 2, y1 = h / 2;
    const p = (x, y) => [cx + x - y * SLANT, cy + y];
    g.beginPath();
    g.moveTo(...p(x0 + r, y0)); g.lineTo(...p(x1 - r, y0)); g.quadraticCurveTo(...p(x1, y0), ...p(x1, y0 + r));
    g.lineTo(...p(x1, y1 - r)); g.quadraticCurveTo(...p(x1, y1), ...p(x1 - r, y1));
    g.lineTo(...p(x0 + r, y1)); g.quadraticCurveTo(...p(x0, y1), ...p(x0, y1 - r));
    g.lineTo(...p(x0, y0 + r)); g.quadraticCurveTo(...p(x0, y0), ...p(x0 + r, y0));
    g.closePath();
  }
  function badge(state) {                       // drawn in balloon units at the top-left corner
    const x = -74, y = -88;
    g.fillStyle = state ? STATE_COLOR[state] : MARK;
    g.strokeStyle = "#fff"; g.lineWidth = 7;
    g.beginPath(); g.arc(x, y, 31, 0, 7); g.fill(); g.stroke();
    g.strokeStyle = state ? "#fff" : "#5b4300"; g.lineWidth = 9; g.lineCap = "round"; g.lineJoin = "round";
    g.beginPath();
    if (state === "failed") { g.moveTo(x - 12, y - 12); g.lineTo(x + 12, y + 12); g.moveTo(x + 12, y - 12); g.lineTo(x - 12, y + 12); }
    else if (state === "skipped") { g.moveTo(x - 13, y); g.lineTo(x + 13, y); }
    else if (state === "queued") { g.moveTo(x, y - 15); g.lineTo(x, y); g.lineTo(x + 11, y + 7); }
    else if (state === "opening" || state === "teaching") { const a = performance.now() / 180; g.arc(x, y, 15, a, a + 4.4); }
    else { g.moveTo(x - 14, y + 1); g.lineTo(x - 4, y + 12); g.lineTo(x + 15, y - 11); }
    g.stroke();
  }
  function drawBalloon(px, py, s, room, faded) {
    const u = cam.z * balloonScale(), cx = toX(px), cy = toY(py);
    if (cx < -200 || cy < -200 || cx > W + 200 || cy > H + 200) return;
    g.save();
    g.globalAlpha = faded ? 0.22 : 1;
    g.translate(cx, cy); g.scale(u, u);
    const ring = room.state ? STATE_COLOR[room.state] : room.marked ? MARK : null;
    if (ring) {
      slantPath(-2, -14, 188, 176, 28); g.fillStyle = ring; g.globalAlpha *= 0.32; g.fill();
      g.globalAlpha = faded ? 0.22 : 1;
    }
    const frame = sprite(s.owned ? "balloon_owned" : "balloon_other");
    if (frame) g.drawImage(frame, -frame.width / 2, -frame.height / 2);
    else { slantPath(-2, -14, 160, 150, 22); g.fillStyle = "#fff"; g.fill(); }
    const face = s.icon ? image(s.icon) : null;
    g.save(); slantPath(-2, -14, 146, 134, 18); g.clip();
    if (face) g.drawImage(face, -122, -111, 240, 194);
    else { g.fillStyle = "#b5c3cc"; g.fillRect(-90, -90, 180, 160); }
    g.restore();
    const line = sprite(s.owned ? "line_owned" : "line_other");
    if (line) g.drawImage(line, -line.width / 2, -line.height / 2);
    else { slantPath(-2, -14, 150, 138, 20); g.lineWidth = 5; g.strokeStyle = s.owned ? "#ff80c5" : "#fff"; g.stroke(); }
    if (s.owned) {
      const heart = sprite("heart");
      if (heart) g.drawImage(heart, 24, -10, 85, 80);
      else { g.fillStyle = "#ff9ec4"; g.beginPath(); g.arc(66, 30, 36, 0, 7); g.fill(); }
      if (s.bond !== null && s.bond !== undefined) {
        g.fillStyle = "#2d4664"; g.font = `700 36px ${FONT}`;
        g.textAlign = "center"; g.textBaseline = "middle"; g.fillText(s.bond, 66, 30);
      }
    }
    if (ring) { slantPath(-2, -14, 176, 164, 26); g.lineWidth = 11; g.strokeStyle = ring; g.stroke(); }
    if (hover && hover.room.id === room.id) { slantPath(-2, -14, 190, 178, 30); g.lineWidth = 7; g.strokeStyle = HOVER; g.stroke(); }
    if (room.state || room.marked) badge(room.state);
    g.restore();
  }
  function drawTitle(c) {
    const x = toX(c.x), y = toY(c.y - 40), px = Math.max(11, Math.min(40, 150 * cam.z));
    if (x > W || x + c.w * cam.z < 0 || y < -60 || y > H + 60) return;
    const info = live.get(c.loc.key) || {}, name = info.title || c.loc.title;
    const space = (c.w + GAP * .82) * cam.z;    // never run into the next map's title
    g.font = `700 ${px}px ${FONT}`;
    let text = name + (info.rank ? `   Rank ${info.rank}` : "");
    if (g.measureText(text).width + px * 1.5 > space) text = name;
    while (text.length > 4 && g.measureText(text).width + px * 1.5 > space) text = `${text.slice(0, -2).trimEnd()}…`;
    const tw = g.measureText(text).width, h = px * 1.7;
    g.fillStyle = "#1f3a5a"; g.beginPath();
    g.moveTo(x, y - h); g.lineTo(x + tw + px * 1.5, y - h); g.lineTo(x + tw + px, y); g.lineTo(x, y); g.closePath(); g.fill();
    g.fillStyle = "#fff"; g.textAlign = "left"; g.textBaseline = "middle"; g.fillText(text, x + px * .55, y - h / 2 + 1);
    if (!info.scanned) {
      g.font = `600 ${px * .62}px ${FONT}`;
      if (tw + px * 1.8 + g.measureText("not scanned yet").width < space) {
        g.fillStyle = "#1f3a5a";
        g.fillText("not scanned yet", x + tw + px * 1.8, y - h / 2 + 1);
      }
    }
  }
  function shadeFor(c) {                        // the game's darkening of locked buildings, cut to the map's shape
    const locked = roomsOf(c).filter((r) => r.locked && r.zone !== null).map((r) => c.loc.zones[r.zone]).filter((z) => z && z.shade);
    if (!locked.length) return null;
    const base = image(`/art/maps/${c.loc.layers[0].file}`);
    const key = locked.map((z) => z.id).join() + (base ? "+" : "-");
    if (c.loc.shadeKey === key) return c.loc.shadeImage;
    if (!base) return null;
    const k = 1600 / c.w, off = document.createElement("canvas");
    off.width = 1600; off.height = Math.round(c.h * k);
    const o = off.getContext("2d");
    o.setTransform(k, 0, 0, k, k * c.mx, k * c.my);
    o.fillStyle = "#182030"; o.beginPath();
    for (const z of locked) for (const [a, b, d] of z.shade.t) {
      const p = (i) => z.shade.v[i];
      o.moveTo(p(a)[0], -p(a)[1]); o.lineTo(p(b)[0], -p(b)[1]); o.lineTo(p(d)[0], -p(d)[1]); o.closePath();
    }
    o.fill("nonzero");
    o.setTransform(1, 0, 0, 1, 0, 0); o.globalCompositeOperation = "destination-in";
    const l = c.loc.layers[0];
    o.drawImage(base, (c.mx + l.x - l.w / 2) * k, (c.my - l.y - l.h / 2) * k, l.w * k, l.h * k);
    c.loc.shadeKey = key; c.loc.shadeImage = off;
    return off;
  }
  function drawLock(c, z) {
    if (!z.lock || cam.z <= .09) return;
    const lx = toX(c.x + c.mx + z.lock.x), ly = toY(c.y + c.my - z.lock.y), r = Math.max(6, 34 * cam.z);
    g.fillStyle = "rgba(31,52,80,.82)"; g.beginPath(); g.arc(lx, ly, r * 1.5, 0, 7); g.fill();
    g.fillStyle = "#fff"; g.fillRect(lx - r * .62, ly - r * .1, r * 1.24, r * .95);
    g.strokeStyle = "#fff"; g.lineWidth = Math.max(1.4, r * .2); g.beginPath(); g.arc(lx, ly - r * .1, r * .42, Math.PI, 0); g.stroke();
  }
  function draw() {
    g.setTransform(DPR, 0, 0, DPR, 0, 0);
    const sky = g.createLinearGradient(0, 0, W, H);
    sky.addColorStop(0, "#e6f6fd"); sky.addColorStop(1, "#c6e8f8");
    g.fillStyle = sky; g.fillRect(0, 0, W, H);
    g.imageSmoothingQuality = "high";
    for (const c of cells) {
      const x = toX(c.x), y = toY(c.y), w = c.w * cam.z, h = c.h * cam.z;
      if (x > W || y > H || x + w < 0 || y + h < 0) continue;
      g.fillStyle = "rgba(45,70,99,.16)"; g.fillRect(x + 5, y + 7, w, h);
      g.fillStyle = "#eaf6fb"; g.fillRect(x, y, w, h);
      for (const l of c.loc.layers) {
        const full = `/art/maps/${l.file}`, small = full.replace(".webp", "_s.webp");
        const im = (w > 620 && image(full)) || image(small) || (w > 620 ? null : image(small));
        if (im) g.drawImage(im, toX(c.x + c.mx + l.x - l.w / 2), toY(c.y + c.my - l.y - l.h / 2), l.w * cam.z, l.h * cam.z);
      }
      const shade = shadeFor(c);
      if (shade) { g.save(); g.globalAlpha = .6; g.drawImage(shade, x, y, w, h); g.restore(); }
      g.strokeStyle = "rgba(255,255,255,.9)"; g.lineWidth = 2; g.strokeRect(x, y, w, h);
      drawTitle(c);
    }
    pulsing = false;
    const labels = [];
    for (const c of cells) {
      for (const room of roomsOf(c)) {
        if (room.zone === null) continue;
        if (room.locked) drawLock(c, c.loc.zones[room.zone] || {});
        if (room.state === "opening" || room.state === "teaching") pulsing = true;
        let first = null;
        for (const s of room.students) {
          if (!seen(s)) continue;
          const px = c.x + c.mx + s.x, py = c.y + c.my - s.y;
          drawBalloon(px, py, s, room, !matches(s));
          if (!first || py < first.py) first = { px, py };
        }
        if (first && (room.marked || room.state)) labels.push({ room, ...first });
      }
    }
    for (const { room, px, py } of labels) {       // a readable room name above each marked room
      const u = cam.z * balloonScale(), x = toX(px), y = toY(py) - 112 * u - 8;
      if (x < -120 || y < -30 || x > W + 120 || y > H + 30) continue;
      const text = room.state ? `${room.name} · ${STATE_NAME[room.state]}` : room.name;
      g.font = `700 11.5px ${FONT}`;
      const tw = g.measureText(text).width + 14;
      g.fillStyle = room.state ? STATE_COLOR[room.state] : MARK;
      g.beginPath(); g.roundRect(x - tw / 2, y - 10, tw, 20, 6); g.fill();
      g.fillStyle = room.state ? "#fff" : "#4c3800"; g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(text, x, y + 1);
    }
  }
  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (want) {
      const k = 0.2;
      cam.x += (want.x - cam.x) * k; cam.y += (want.y - cam.y) * k; cam.z += (want.z - cam.z) * k; dirty = true;
      if (Math.abs(want.z - cam.z) < want.z * .002 && Math.abs(want.x - cam.x) * cam.z < .5) { cam = want; want = null; }
    }
    if (pulsing && now - lastPulse > 70) { lastPulse = now; dirty = true; }
    if (dirty) { dirty = false; draw(); }
  }
  function pick(mx, my) {
    const half = 88 * cam.z * balloonScale();
    let found = null;
    for (const c of cells) for (const room of roomsOf(c)) {
      if (room.zone === null) continue;
      for (const s of room.students) {
        if (!seen(s)) continue;
        const x = toX(c.x + c.mx + s.x), y = toY(c.y + c.my - s.y);
        if (Math.abs(mx - x) < half && Math.abs(my - y) < half) found = { s, room, c };
      }
    }
    return found;
  }
  const cellAt = (mx, my) => cells.find((c) => mx >= toX(c.x) && mx <= toX(c.x + c.w) && my >= toY(c.y - TITLE) && my <= toY(c.y + c.h));
  const local = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };

  function showTip(hit, mx, my) {
    if (!hit) { tip.style.display = "none"; return; }
    const { s, room, c } = hit, info = live.get(c.loc.key) || {};
    tip.textContent = "";
    tip.append(s.name || "Unknown student");
    const where = document.createElement("small");
    where.textContent = `${info.title || c.loc.title} > ${room.name}${s.owned ? ` · yours${s.bond !== null && s.bond !== undefined ? `, rank ${s.bond}` : ""}` : ""}`;
    const action = document.createElement("small");
    action.className = "act";
    action.textContent = room.state ? `${STATE_NAME[room.state]}${room.note ? `: ${room.note}` : ""}` : room.marked ? "Marked. Click to unmark the room." : room.markable ? "Click to mark the whole room." : room.why || "Can't be marked right now.";
    tip.append(where, action);
    tip.style.display = "block";
    tip.style.left = `${Math.min(W - 260, mx + 14)}px`;
    tip.style.top = `${Math.min(H - 84, my + 14)}px`;
  }
  function refreshPlaces() {
    const key = cells.map((c) => { const l = live.get(c.loc.key) || {}; return `${c.loc.key}:${l.scanned ? 1 : 0}:${(l.rooms || []).reduce((a, r) => a + r.students.length, 0)}:${(l.rooms || []).filter((r) => r.marked).length}`; }).join();
    if (key === placesKey) return;
    placesKey = key;
    places.textContent = "";
    for (const c of cells) {
      const l = live.get(c.loc.key) || {}, rooms = l.rooms || [];
      const b = document.createElement("button");
      b.className = l.scanned ? "" : "off";
      b.append(l.title || c.loc.title);
      const marked = rooms.filter((r) => r.marked).length;
      const tag = document.createElement("span");
      tag.textContent = marked ? `${marked} marked` : l.scanned ? rooms.reduce((a, r) => a + r.students.length, 0) : "–";
      if (marked) tag.className = "m";
      b.append(tag);
      b.onclick = () => fitCell(c);
      places.append(b);
    }
  }
  function resize() {
    if (!host) return;
    const box = host.getBoundingClientRect();
    if (box.width < 2 || box.height < 2) return;
    DPR = window.devicePixelRatio || 1; W = box.width; H = box.height;
    canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
    canvas.style.width = `${W}px`; canvas.style.height = `${H}px`;
    if (maps.length && !userMoved) { layout(); fitAll(false); fitted = true; }
    else if (!fitted && cells.length) { fitAll(false); fitted = true; }
    draw();                                     // setting the canvas size clears it, so never leave it blank
    dirty = false;
  }

  function mount(el, options) {
    unmount();
    host = el; opts = options || {}; fitted = false; placesKey = ""; userMoved = false;
    el.innerHTML = `<canvas class="atlas-canvas" aria-label="Map of every lesson location. Click a student to mark their room. Every room is also listed in the side panel."></canvas>
      <div class="atlas-bar"><label class="search">${opts.searchIcon || ""}<input id="atlas-find" type="search" placeholder="Find a student" aria-label="Find a student"></label>
      <button class="btn small" id="atlas-owned" aria-pressed="false">Only mine</button><button class="btn small on" id="atlas-big" aria-pressed="true" title="Keep icons readable when zoomed out">Big icons</button><button class="btn small" id="atlas-all">See all</button></div>
      <nav class="atlas-places" aria-label="Locations"></nav><div class="atlas-tip" role="status"></div>`;
    canvas = el.querySelector("canvas"); g = canvas.getContext("2d");
    tip = el.querySelector(".atlas-tip"); places = el.querySelector(".atlas-places");
    if (needle) el.querySelector("#atlas-find").value = needle;
    const owned = el.querySelector("#atlas-owned"), big = el.querySelector("#atlas-big");
    const press = (b, on) => { b.setAttribute("aria-pressed", on); b.classList.toggle("on", on); dirty = true; };
    press(owned, onlyOwned); press(big, bigIcons);
    owned.onclick = () => { onlyOwned = !onlyOwned; press(owned, onlyOwned); };
    big.onclick = () => { bigIcons = !bigIcons; press(big, bigIcons); };
    el.querySelector("#atlas-all").onclick = () => { userMoved = false; fitAll(true); };
    const find = el.querySelector("#atlas-find");
    find.oninput = () => { needle = find.value.trim().toLowerCase(); dirty = true; };
    find.onkeydown = (e) => {
      if (e.key !== "Enter" || !needle) return;
      for (const c of cells) for (const r of roomsOf(c)) for (const s of r.students) if (matches(s) && r.zone !== null) {
        const x = c.x + c.mx + s.x, y = c.y + c.my - s.y;
        userMoved = true;
        fit({ x: x - 700, y: y - 450, w: 1400, h: 900 });
        return;
      }
    };
    canvas.addEventListener("pointerdown", (e) => {
      canvas.setPointerCapture(e.pointerId);
      drag = { x: e.clientX, y: e.clientY, cx: cam.x, cy: cam.y }; moved = false; want = null;
    });
    canvas.addEventListener("pointermove", (e) => {
      const [mx, my] = local(e);
      if (drag) {
        if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) > 4) { moved = true; userMoved = true; canvas.classList.add("drag"); }
        if (moved) { cam.x = drag.cx - (e.clientX - drag.x) / cam.z; cam.y = drag.cy - (e.clientY - drag.y) / cam.z; dirty = true; }
      }
      const hit = moved ? null : pick(mx, my);
      if ((hit && hit.room.id) !== (hover && hover.room.id)) dirty = true;
      hover = hit;
      canvas.style.cursor = moved ? "grabbing" : hit ? (hit.room.markable || hit.room.marked ? "pointer" : "not-allowed") : "grab";
      showTip(hit, mx, my);
    });
    const end = () => { drag = null; canvas.classList.remove("drag"); };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
    canvas.addEventListener("pointerleave", () => { if (hover) dirty = true; hover = null; tip.style.display = "none"; });
    canvas.addEventListener("click", (e) => {
      if (moved || e.detail > 1) return;
      const hit = pick(...local(e));
      if (hit && opts.onRoom) opts.onRoom(hit.room);
    });
    canvas.addEventListener("dblclick", (e) => {
      const [mx, my] = local(e);
      if (pick(mx, my)) return;
      const c = cellAt(mx, my);
      if (c) fitCell(c);
    });
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault(); want = null; userMoved = true;
      const [mx, my] = local(e), k = Math.exp(-e.deltaY * .0016), z = Math.max(.012, Math.min(1.6, cam.z * k));
      cam.x += mx / cam.z - mx / z; cam.y += my / cam.z - my / z; cam.z = z; dirty = true;
    }, { passive: false });
    observer = new ResizeObserver(resize);
    observer.observe(el);
    resize();
    raf = requestAnimationFrame(frame);
    ticker = setInterval(() => { if (dirty) { dirty = false; draw(); } }, 250);   // art still appears if animation is throttled
  }
  function unmount() {
    if (raf) cancelAnimationFrame(raf);
    if (ticker) clearInterval(ticker);
    if (observer) observer.disconnect();
    raf = 0; ticker = 0; observer = null; host = null; hover = null; drag = null; want = null;
  }
  function setMaps(list) {
    if (list === mapsRef) return;
    mapsRef = list; maps = list.map((l) => ({ ...l }));
    layout();
    fitted = false;
    if (host) resize();
    dirty = true;
  }
  function setLive(view) {
    live = new Map((view.locations || []).map((l) => [l.key, l]));
    if (hover) {                                // keep hover on the fresh copy of its room
      const c = hover.c, r = roomsOf(c).find((x) => x.id === hover.room.id);
      hover = r ? { ...hover, room: r } : null;
    }
    if (host && cells.length) refreshPlaces();
    dirty = true;
  }
  /* For tests and previews: where a room's first student is on screen, or null. */
  function roomPoint(id) {
    for (const c of cells) for (const r of roomsOf(c)) {
      if (r.id === id && r.students.length && r.zone !== null) {
        const s = r.students[0];
        return [toX(c.x + c.mx + s.x), toY(c.y + c.my - s.y)];
      }
    }
    return null;
  }
  const focusRoom = (id) => {
    for (const c of cells) if (roomsOf(c).some((r) => r.id === id)) { fitCell(c); return; }
  };
  return { mount, unmount, setMaps, setLive, focusRoom, roomPoint };
})();
