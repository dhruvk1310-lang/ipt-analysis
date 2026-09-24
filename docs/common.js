// Shared helpers for IPT, Explained (index.html and predictions.html).
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x, d = 0) => (x * 100).toFixed(d) + "%";
const fmt = n => Number(n).toLocaleString("en-IN");
// date-only strings ("2026-09-24") are calendar dates: format them in UTC so no timezone shifts the day
const day = (s, o = { day: "numeric", month: "long", year: "numeric" }) =>
  new Date(s + "T00:00:00Z").toLocaleDateString("en-GB", { ...o, timeZone: "UTC" });
const pair = t => String(t).replace(" / ", " & ");
const NS = "http://www.w3.org/2000/svg";

// a probability in everyday words: "about 3 in 5", "about 1 in 50"
function odds(p) {
  if (p >= 0.995) return "almost certain";
  if (p < 0.005) return "less than 1 in 200";
  if (p >= 0.45) {
    for (const [a, b] of [[1, 2], [3, 5], [2, 3], [3, 4], [4, 5], [9, 10], [19, 20]]) if (Math.abs(p - a / b) < 0.04) return `about ${a} in ${b}`;
    return `about ${Math.round(p * 10)} in 10`;
  }
  const n = 1 / p, nice = n < 12 ? Math.round(n) : n < 100 ? Math.round(n / 5) * 5 : Math.round(n / 10) * 10;
  return `about 1 in ${nice}`;
}
// how clear a favourite is
function strength(p) {
  const f = Math.max(p, 1 - p);
  if (f < 0.55) return ["Toss-up", ""];
  if (f < 0.65) return ["Slight favourite", "fav"];
  if (f < 0.8) return ["Favourite", "fav"];
  return ["Strong favourite", "strong"];
}

// ---------- tooltips
const tip = document.getElementById("tip");
function showTip(e, html) {
  tip.innerHTML = html; tip.style.opacity = 1;
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > innerWidth - 8) x = e.clientX - w - pad;
  if (y + h > innerHeight - 8) y = e.clientY - h - pad;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const hideTip = () => { tip.style.opacity = 0; };
function tipOn(node, fn) {
  ["pointermove", "mousemove", "click"].forEach(t => node.addEventListener(t, fn));
  ["pointerleave", "mouseleave"].forEach(t => node.addEventListener(t, hideTip));
}
addEventListener("scroll", hideTip, { passive: true });
const row = (k, v) => `<div class="r"><span>${esc(k)}</span><span>${esc(v)}</span></div>`;

// ---------- svg
function el(tag, attrs = {}, parent) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}
const txt = (parent, attrs, s) => { const t = el("text", attrs, parent); t.textContent = s; return t; };
function niceMax(v) { const p = Math.pow(10, Math.floor(Math.log10(v || 1))); return Math.ceil(v / p) * p; }
const bar = (x, y, w, h, r = 4) => w <= r ? `M${x},${y} h${Math.max(w, 0)} v${h} h${-Math.max(w, 0)} z`
  : `M${x},${y} h${w - r} q${r},0 ${r},${r} v${h - 2 * r} q0,${r} ${-r},${r} h${-(w - r)} z`;
function segmented(host, items, onPick, labels) {
  host.innerHTML = "";
  items.forEach((it, i) => {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = labels ? labels[i] : it; b.dataset.v = it; b.setAttribute("aria-pressed", i === 0);
    b.onclick = () => { host.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", x === b)); onPick(it); };
    host.appendChild(b);
  });
}

// ---------- repo link when served from <user>.github.io/<repo>/
function linkRepo(node) {
  const gh = location.hostname.match(/^([^.]+)\.github\.io$/);
  const seg = location.pathname.split("/")[1];
  if (gh && seg && node) { node.href = `https://github.com/${gh[1]}/${seg}`; node.hidden = false; }
}

// ---------- padel maths (same formulas as analysis/padel_model.py)
const sigmoid = x => 1 / (1 + Math.exp(-x));
function comb(n, k) { let r = 1; for (let i = 1; i <= k; i++) r = r * (n - k + i) / i; return r; }
// P(first to n games) when each game is won with probability q
function pRace(q, n) { let s = 0; for (let k = 0; k < n; k++) s += comb(n - 1 + k, k) * q ** n * (1 - q) ** k; return s; }
// distribution of final race scores: [{a, b, p}]
function raceScores(q, n) {
  const out = [];
  for (let k = 0; k < n; k++) {
    out.push({ a: n, b: k, p: comb(n - 1 + k, k) * q ** n * (1 - q) ** k });
    out.push({ a: k, b: n, p: comb(n - 1 + k, k) * (1 - q) ** n * q ** k });
  }
  return out;
}
// point-win probability that gives game-win probability q
function pointProb(q) {
  q = Math.min(Math.max(q, 1e-6), 1 - 1e-6);
  const game = p => p ** 4 * (1 + 4 * (1 - p) + 10 * (1 - p) ** 2) + 20 * p ** 3 * (1 - p) ** 3 * p ** 2 / (1 - 2 * p * (1 - p));
  let lo = 1e-6, hi = 1 - 1e-6;
  for (let i = 0; i < 60; i++) { const mid = (lo + hi) / 2; if (game(mid) < q) lo = mid; else hi = mid; }
  return (lo + hi) / 2;
}
function pFirstTo(p, n) {
  let below = 0; for (let k = 0; k < n - 1; k++) below += comb(n - 1 + k, k) * p ** n * (1 - p) ** k;
  const deuce = comb(2 * (n - 1), n - 1) * p ** (n - 1) * (1 - p) ** (n - 1);
  return below + deuce * p * p / (1 - 2 * p * (1 - p));
}
function pSet(q) {
  const tb = pFirstTo(pointProb(q), 7);
  let below = 0; for (let k = 0; k < 5; k++) below += comb(5 + k, k) * q ** 6 * (1 - q) ** k;
  return below + comb(10, 5) * q ** 5 * (1 - q) ** 5 * (q * q + 2 * q * (1 - q) * tb);
}
// best of 3 sets with a super-tiebreak decider: {p, s20, s21, s12, s02}
function bestOf3(q) {
  const s = pSet(q), stb = pFirstTo(pointProb(q), 10);
  const r = { s20: s * s, s21: 2 * s * (1 - s) * stb, s12: 2 * s * (1 - s) * (1 - stb), s02: (1 - s) ** 2 };
  return { ...r, p: r.s20 + r.s21 };
}
function pMatch(q, fmt) {
  const m = /Race to (\d+)/.exec(fmt || "");
  if (m) return pRace(q, +m[1]);
  if (/^Best of 3/.test(fmt || "")) return bestOf3(q).p;
  return pRace(q, 8);
}
