// Tradewinds client: chat on the left, trips appearing on the right as the crew works.
import {
  assistantHTML, crewHTML, emptyCanvasHTML, esc, planHTML, skeletonTicketsHTML, summaryHTML, threadHTML, ticketsHTML,
} from "/static/render.js";

const $ = sel => document.querySelector(sel);
const QS = new URLSearchParams(location.search).get("demo") ? "?demo=1" : "";
const FALLBACK_ERROR = "Something went wrong. Please try again.";
const TERMINAL = new Set(["questions", "options", "plan", "error"]);
const MAX_NOTES = 5;
const MAX_TURN_CHARS = 1000; // matches the server's ChatTurn limit
const TILES = { url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", attribution: "&copy; OpenStreetMap contributors" };
const newSessionId = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`);

// ---------- state (replaced, never mutated) ----------
const INITIAL = Object.freeze({
  session: "", turns: [], searching: false, typing: false, chatError: "",
  agents: [], crew: {}, status: "", crewFor: null, // "options" | plan index
  brief: null, reply: "", candidates: [], options: [], notes: [],
  plans: {}, selected: null, planning: null, planError: null, tab: "chat", tripsDot: false,
});
let state = INITIAL;
let controller = null;
let map = null;
const setState = patch => { state = Object.freeze({ ...state, ...patch }); render(); };
const decided = () => state.brief !== null;

// ---------- SSE over fetch (EventSource can't POST) ----------
async function streamSSE(url, body, onEvent, signal) {
  let resp;
  try {
    resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  } catch (e) {
    if (e.name !== "AbortError") onEvent({ type: "error", message: FALLBACK_ERROR });
    return;
  }
  if (!resp.ok || !resp.body) {
    let message = FALLBACK_ERROR;
    try { const j = await resp.json(); if (typeof j.detail === "string") message = j.detail; } catch (_) { /* not JSON */ }
    onEvent({ type: "error", message });
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let finished = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true }).replace(/\r\n?/g, "\n");
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const data = buf.slice(0, cut).split("\n").filter(l => l.startsWith("data:")).map(l => l.slice(5).trimStart()).join("\n");
        buf = buf.slice(cut + 2);
        if (!data) continue;
        const ev = JSON.parse(data);
        if (TERMINAL.has(ev.type)) finished = true;
        onEvent(ev);
      }
    }
  } catch (e) {
    if (e.name === "AbortError") return;
  }
  // A stream that closes without a result (proxy timeout, crash) must not leave the page spinning.
  if (!finished && !(signal && signal.aborted)) onEvent({ type: "error", message: FALLBACK_ERROR });
}

// Crew progress events shared by both streams; returns true if handled.
function crewEvent(ev) {
  switch (ev.type) {
    case "run_start": setState({ agents: ev.agents, crew: {}, status: "" }); return true;
    case "agent_start": setState({ crew: { ...state.crew, [ev.agent]: "now" } }); return true;
    case "agent_done": case "agent_skip": setState({ crew: { ...state.crew, [ev.agent]: "done" } }); return true;
    case "status": setState({ status: ev.text }); return true;
    default: return false; // thought, tool_call, tool_result, revision, agent_error: not for travelers
  }
}

// ---------- chat ----------
function send(text) {
  const clean = text.trim();
  if (!clean) return;
  if (!state.session) startApp();
  if (decided()) return addNote(clean);
  if (state.searching) return;
  setState({ turns: [...state.turns, { role: "user", text: clean }] });
  runOptions();
}

function addNote(text) {
  if (state.notes.length >= MAX_NOTES) {
    setState({ turns: [...state.turns, { role: "user", text, kind: "note", meta: "Note limit reached: start a new trip to change more" }] });
    return;
  }
  const meta = state.planning !== null ? "Noted: I'll use this next time you plan a trip" : "Added to your trip";
  setState({ notes: [...state.notes, text.slice(0, 500)], turns: [...state.turns, { role: "user", text, kind: "note", meta }] });
}

function runOptions() {
  controller = new AbortController();
  setState({ searching: true, typing: true, chatError: "" });
  // Only the real Q&A goes to the server: notes and the app's own status messages stay local.
  const messages = state.turns.filter(t => !t.kind).map(t => ({ role: t.role, text: t.text.slice(0, MAX_TURN_CHARS) }));
  streamSSE("/api/options" + QS, { messages, session_id: state.session }, onOptionsEvent, controller.signal);
}

function say(html, text, kind) {
  return [...state.turns, { role: "assistant", html, text, ...(kind ? { kind } : {}) }];
}

function onOptionsEvent(ev) {
  if (crewEvent(ev)) return;
  switch (ev.type) {
    case "questions":
      setState({ searching: false, typing: false, turns: say(assistantHTML(ev.reply, ev.questions), [ev.reply, ...ev.questions].join("\n")) });
      break;
    case "brief":
      setState({ typing: false, brief: ev.brief, reply: ev.reply, crewFor: "options", tripsDot: state.tab === "chat",
        turns: say(assistantHTML(ev.reply, ev.followups, ev.followups.length ? "While I search, if you like:" : ""), ev.reply, "local") });
      break;
    case "candidates":
      setState({ candidates: ev.options.map(o => ({ option: o, pitch: o.why, best: false })), tripsDot: state.tab === "chat" });
      break;
    case "options":
      setState({ searching: false, crewFor: null, options: ev.options, candidates: [],
        turns: say(`Here are three best-value trips. Pick one and I'll plan it day by day.`, "", "local") });
      break;
    case "error":
      // If the search had already started, step back to the last real question so "Try again" resends it.
      setState({ searching: false, typing: false, crewFor: null, chatError: ev.message || FALLBACK_ERROR,
        brief: null, reply: "", candidates: [], options: [], turns: state.turns.filter(t => t.kind !== "local") });
      break;
  }
}

// ---------- planning ----------
function planCard(i, force = false) {
  const existing = state.plans[i];
  if (existing && existing.final && !force) { setState({ selected: i }); scrollToPlan(); return; }
  if (state.planning !== null) return;
  controller = new AbortController();
  const notes = state.notes.slice();
  setState({ planning: i, selected: i, crewFor: i, planError: null, plans: { ...state.plans, [i]: { notesUsed: notes.length } } });
  scrollToPlan();
  const body = { brief: state.brief, option: state.options[i].option, session_id: state.session, notes };
  let started = false;
  streamSSE("/api/plan" + QS, body, ev => {
    if (ev.type === "status" && !started) return patchPlan(i, { note: ev.text }); // demo note before run_start
    if (ev.type === "run_start") started = true;
    onPlanEvent(i, ev);
  }, controller.signal);
}

function patchPlan(i, patch) {
  setState({ plans: { ...state.plans, [i]: { ...state.plans[i], ...patch } } });
}

function onPlanEvent(i, ev) {
  if (crewEvent(ev)) return;
  if (ev.type === "plan_part") {
    const { type, part, ...rest } = ev;
    patchPlan(i, rest);
  } else if (ev.type === "plan") {
    const { type, ...plan } = ev;
    patchPlan(i, { ...plan, final: true });
    setState({ planning: null, crewFor: null });
  } else if (ev.type === "error") {
    setState({ planning: null, crewFor: null, planError: { index: i, message: ev.message || FALLBACK_ERROR } });
  }
}

const scrollToPlan = () => requestAnimationFrame(() => { const el = $("#plan"); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); });

// ---------- render ----------
function render() {
  renderChat();
  renderCanvas();
  $("#tabs").querySelectorAll("button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === state.tab)));
  $("#v-app").dataset.tab = state.tab;
  $("#trips-dot").hidden = !state.tripsDot;
}

let lastSpoken = 0;
function renderChat() {
  const thread = $("#thread");
  thread.innerHTML = threadHTML(state.turns, { typing: state.typing, error: state.chatError });
  if (state.turns.length > lastSpoken) { // announce only the newest message to screen readers
    const t = state.turns[state.turns.length - 1];
    if (t.role === "assistant" && t.text) $("#live").textContent = t.text;
    lastSpoken = state.turns.length;
  }
  thread.scrollTop = thread.scrollHeight;
  const input = $("#chat-input");
  const blocked = state.searching && !decided();
  input.disabled = blocked;
  $("#chat-form .send").disabled = blocked;
  input.placeholder = decided() ? "Add anything else (dorms, must-sees, pace)…" : "Type your answer…";
}

let planSignature = "";
function renderCanvas() {
  const top = $("#canvas-top");
  const parts = [];
  if (!state.brief && state.crewFor === null) {
    top.innerHTML = emptyCanvasHTML();
    renderPlan(null);
    return;
  }
  parts.push(summaryHTML(state.brief, state.reply));
  if (state.crewFor === "options") parts.push(crewHTML(state.agents, state.crew, state.status));
  const nights = state.brief ? state.brief.nights : 0;
  if (state.options.length) {
    parts.push(ticketsHTML(state.options, { nights, selected: state.selected, plans: state.plans, busy: state.planning, ranked: true }));
  } else if (state.candidates.length) {
    parts.push(`<div class="label" style="margin-bottom:8px">Found these · ranking for value…</div>`);
    parts.push(ticketsHTML(state.candidates, { nights, selected: null, plans: {}, busy: -1, ranked: false }));
  } else if (state.crewFor === "options") {
    parts.push(skeletonTicketsHTML());
  }
  if (state.planError) {
    parts.push(`<div class="banner" style="margin-top:14px"><span>${esc(state.planError.message)}</span>
      <button class="btn ghost small" data-action="retry-plan">Try again</button></div>`);
  }
  if (state.selected !== null && state.crewFor === state.selected) {
    parts.push(`<div style="margin-top:18px">${crewHTML(state.agents, state.crew, state.status)}</div>`);
  }
  top.innerHTML = parts.join("");
  renderPlan(state.selected !== null ? state.plans[state.selected] || null : null);
}

// The plan region is rebuilt only when the plan itself changes, so open days and the map survive status updates.
function renderPlan(p) {
  const region = $("#canvas-plan");
  const notesPending = Boolean(p && p.final && state.notes.length > (p.notesUsed || 0) && state.planning === null);
  const signature = p ? JSON.stringify([state.selected, p.note, p.itinerary, p.links, p.budget, p.final, p.review, notesPending]) : "";
  if (signature === planSignature) return;
  planSignature = signature;
  const open = new Set([...region.querySelectorAll("details.day[open]")].map(d => d.dataset.day));
  region.innerHTML = p ? planHTML(p, { notesPending }) : "";
  region.querySelectorAll("details.day").forEach(d => { if (open.has(d.dataset.day)) d.open = true; });
  drawMap(p && p.itinerary ? p.itinerary.days : null);
}

function baseRuns(days) {
  return days.reduce((runs, d) => {
    const last = runs[runs.length - 1];
    return last && last.base === d.base ? [...runs.slice(0, -1), { ...last, days: [...last.days, d] }]
      : [...runs, { base: d.base, lat: d.lat, lon: d.lon, days: [d] }];
  }, []);
}

function drawMap(days) {
  if (map) { map.remove(); map = null; }
  const el = document.getElementById("map");
  if (!el || !days || !window.L) return;
  const runs = baseRuns(days);
  map = L.map(el, { scrollWheelZoom: false });
  L.tileLayer(TILES.url, { attribution: TILES.attribution, maxZoom: 18 }).addTo(map);
  const pts = runs.map(r => [r.lat, r.lon]);
  L.polyline(pts, { color: "#d4673f", weight: 3, dashArray: "6 8", opacity: .9 }).addTo(map);
  runs.forEach((r, n) => {
    const icon = L.divIcon({ className: "", html: `<div class="pin"><span>${n + 1}</span></div>`, iconSize: [28, 28], iconAnchor: [14, 28] });
    L.marker([r.lat, r.lon], { icon }).addTo(map).bindPopup(`<b>${esc(r.base)}</b><br>Days ${r.days[0].day}–${r.days[r.days.length - 1].day}`);
  });
  map.fitBounds(L.latLngBounds(pts).pad(0.45));
  setTimeout(() => map && map.invalidateSize(), 60);
}

// ---------- wiring ----------
function startApp() {
  setState({ session: newSessionId() });
  $("#v-landing").hidden = true;
  $("#footer").hidden = true;
  $("#v-app").hidden = false;
  $("#tabs").hidden = false;
  $("#restart").hidden = false;
  $("#chat-input").focus();
}

function restart() {
  if (controller) controller.abort();
  if (map) { map.remove(); map = null; }
  state = INITIAL;
  planSignature = "";
  lastSpoken = 0;
  render();
  $("#v-landing").hidden = false;
  $("#footer").hidden = false;
  $("#v-app").hidden = true;
  $("#tabs").hidden = true;
  $("#restart").hidden = true;
  $("#ask-input").value = "";
  $("#ask-input").focus();
}

$("#ask-form").onsubmit = e => { e.preventDefault(); send($("#ask-input").value); };
$("#chat-form").onsubmit = e => { e.preventDefault(); const v = $("#chat-input").value; $("#chat-input").value = ""; send(v); };
$("#examples").onclick = e => { const b = e.target.closest(".chip"); if (b) send(b.textContent.replace(/^\S+\s/, "")); };
$("#restart").onclick = restart;
$("#tabs").onclick = e => { const b = e.target.closest("button"); if (b) setState({ tab: b.dataset.tab, tripsDot: b.dataset.tab === "trips" ? false : state.tripsDot }); };
document.addEventListener("click", e => {
  const plan = e.target.closest("[data-plan]");
  if (plan && !plan.disabled) return planCard(Number(plan.dataset.plan));
  const action = e.target.closest("[data-action]");
  if (!action) return;
  if (action.dataset.action === "retry-chat") runOptions();
  if (action.dataset.action === "retry-plan" && state.planError) planCard(state.planError.index, true);
  if (action.dataset.action === "replan" && state.selected !== null) planCard(state.selected, true);
});
$("#ask-input").focus();
