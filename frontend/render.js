// Pure HTML builders. Every model-supplied string goes through esc(); links through safeUrl().

export const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const money = n => "$" + Number(n || 0).toLocaleString("en-US");
export const shortDate = iso => new Date(iso + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
export const safeUrl = u => /^https:\/\//.test(String(u)) ? String(u) : "#";
const perNight = (total, nights) => nights > 0 ? `~${money(Math.round(total / nights))}/day all-in` : "";

export const CREW = {
  brief: ["📝", "Brief"], scout: ["🧭", "Scout"], judge: ["⚖️", "Judge"], itinerary: ["🗺️", "Itinerary"],
  logistics: ["🚌", "Logistics"], budget: ["💵", "Budget"], reviewer: ["🔍", "Reviewer"],
};
const TICKET_COLORS = ["#2f5d50", "#c4643f", "#3f6f8f"];

export function threadHTML(turns, { typing, error }) {
  const items = turns.map(t => {
    if (t.role === "user") {
      const note = t.kind === "note" ? `<span class="meta">${esc(t.meta || "Added to your trip")}</span>` : "";
      return `<div class="bubble me${t.kind === "note" ? " note" : ""}">${esc(t.text)}${note}</div>`;
    }
    return `<div class="bubble them">${t.html}</div>`;
  });
  if (typing) items.push(`<div class="typing" aria-label="Tradewinds is typing"><i></i><i></i><i></i></div>`);
  if (error) items.push(`<div class="bubble them">${esc(error)}<div style="margin-top:8px"><button class="btn ghost small" data-action="retry-chat">Try again</button></div></div>`);
  return items.join("");
}

export function assistantHTML(reply, list, intro) {
  const items = (list || []).length ? `${intro ? `<div class="label" style="margin-top:8px">${esc(intro)}</div>` : ""}<ul>${list.map(q => `<li>${esc(q)}</li>`).join("")}</ul>` : "";
  return `${esc(reply)}${items}`;
}

export function ringHTML(agents, crewState) {
  const n = agents.length || 7;
  const done = agents.filter(a => crewState[a.name] === "done").length;
  const nodes = agents.map((a, i) => {
    const ang = (-90 + i * 360 / n) * Math.PI / 180;
    const [icon] = CREW[a.name] || ["•"];
    return `<div class="node ${crewState[a.name] || ""}" style="left:${50 + 41 * Math.cos(ang)}%;top:${50 + 41 * Math.sin(ang)}%" title="${esc(a.role)}">${icon}</div>`;
  }).join("");
  return `<div class="ring" aria-hidden="true"><svg viewBox="0 0 100 100"><circle class="track" cx="50" cy="50" r="41"/>
    <circle class="arc" cx="50" cy="50" r="41" pathLength="100" stroke-dasharray="${100 * done / n} 100"/></svg>
    <div class="globe"></div>${nodes}</div>`;
}

export function crewHTML(agents, crewState, status) {
  const steps = agents.map(a => {
    const [, label] = CREW[a.name] || ["", a.role];
    return `<span class="${crewState[a.name] || ""}">${esc(label)}</span>`;
  }).join(`<span>·</span>`);
  return `<div class="crew" aria-live="polite">${ringHTML(agents, crewState)}
    <div class="crew-text"><div class="label">Your crew is on it</div><div class="status">${esc(status || "Getting started…")}</div>
    <div class="steps">${steps}</div></div></div>`;
}

export function summaryHTML(brief, reply) {
  if (!brief) return "";
  const tags = [
    `📅 ${esc(shortDate(brief.start_date))} – ${esc(shortDate(brief.end_date))}`, `🌙 ${brief.nights} nights`,
    `✈️ from ${esc(brief.origin)}`, `💰 ${money(brief.budget_usd)} all-in`,
    ...brief.vibe.slice(0, 4).map(v => `✦ ${esc(v)}`),
  ];
  return `<div class="summary"><h2>${esc(reply || "Your trip")}</h2>${tags.map(t => `<span class="tag">${t}</span>`).join("")}</div>`;
}

export function skeletonTicketsHTML() {
  const one = `<div class="ticket"><div class="top skeleton" style="--c:transparent"></div><div class="body">
    <div class="skeleton" style="height:14px"></div><div class="skeleton" style="height:14px;width:70%"></div>
    <div class="skeleton" style="height:26px;width:40%"></div></div></div>`;
  return `<div class="cards" aria-busy="true">${one}${one}${one}</div>`;
}

export function ticketsHTML(entries, { nights, selected, plans, busy, ranked }) {
  return `<div class="cards">${entries.map((r, i) => {
    const o = r.option;
    const planned = Boolean(plans[i] && plans[i].final);
    const label = planned ? (selected === i ? "Your plan is below ↓" : "See this plan") : busy === i ? "Planning…" : "Plan this trip";
    const pitch = ranked ? esc(r.pitch) : `<span class="muted">${esc(o.why)}</span>`;
    return `<article class="ticket${selected === i ? " selected" : ""}" style="--c:${TICKET_COLORS[i % 3]}">
      ${r.best ? `<span class="stamp">Best value</span>` : ""}
      <div class="top"><span class="name">${esc(o.name)}</span><span class="where">${o.countries.map(esc).join(" · ")} · ${nights} nights</span></div>
      <div class="perf"></div>
      <div class="body"><p class="pitch">${pitch}</p>
        <div><div class="price">about ${money(o.rough_cost.total_usd)}</div><small class="muted">${perNight(o.rough_cost.total_usd, nights)} · estimate</small></div>
        <button class="btn ${planned ? "ghost" : "primary"}" data-plan="${i}" ${busy !== null && busy !== i ? "disabled" : ""}>${label}</button>
      </div></article>`;
  }).join("")}</div>`;
}

export function dayHTML(d) {
  return `<details class="day" data-day="${Number(d.day)}"><summary><span class="dn">Day ${d.day}</span>
    <span class="dt">${esc(d.title)}<small>${esc(shortDate(d.date))} · ${esc(d.base)}, ${esc(d.country)} · ${esc(d.pace)}</small></span></summary>
    <div class="parts"><p><b>Morning</b><br>${esc(d.morning)}</p><p><b>Afternoon</b><br>${esc(d.afternoon)}</p><p><b>Evening</b><br>${esc(d.evening)}</p></div></details>`;
}

const waitingHTML = text => `<div class="waiting"><div class="muted" style="font-size:13px">${esc(text)}</div><div class="skeleton"></div><div class="skeleton" style="width:70%"></div></div>`;

function bookHTML(p) {
  if (!p.links) return waitingHTML("Finding the cheapest flights and beds…");
  const lg = p.logistics;
  const transfers = lg.transfers.length ? `<p class="note">${lg.transfers.map(t => `${esc(t.from_base)} → ${esc(t.to_base)} by ${esc(t.mode)} (~${money(t.est_usd)})`).join("<br>")}</p>` : "";
  return `<div class="book"><a class="btn primary small" href="${esc(safeUrl(p.links.flights))}" target="_blank" rel="noopener">✈️ Flights</a>
    ${p.links.stays.map(s => `<a class="btn ghost small" href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener">🛏 ${esc(s.base)}</a>`).join("")}</div>
    <p class="note">${lg.stays.map(s => `${esc(s.base)}: ${esc(s.kind)}, ~${money(s.est_per_night_usd)}/night`).join("<br>")}</p>
    <p class="note">${esc(lg.getting_around)}</p>${transfers}`;
}

function costsHTML(p) {
  if (!p.budget) return waitingHTML("Adding it all up…");
  const b = p.budget;
  const tips = (b.saving_tips || []).length ? `<div class="label" style="margin-top:12px">Ways to save</div><ul class="tips">${b.saving_tips.map(t => `<li>${esc(t)}</li>`).join("")}</ul>` : "";
  return `<table class="budget">${b.lines.map(l => `<tr><td>${esc(l.category)}</td><td>${money(l.usd)}</td></tr>`).join("")}
    <tr class="total"><td>Total</td><td>${money(b.total_usd)}</td></tr></table>
    <p class="note">${b.within_budget ? "Within your budget." : "Over your budget."} ${esc(b.notes)}</p>${tips}`;
}

export function planHTML(p, { notesPending }) {
  const it = p.itinerary;
  const head = it ? esc(it.headline) : "Sketching your days…";
  const update = notesPending ? `<button class="btn ghost small" data-action="replan">Update plan with your notes</button>` : "";
  const note = p.note ? `<div class="banner">${esc(p.note)}</div>` : "";
  return `<section class="plan" id="plan" aria-label="Trip plan">${note}
    <div class="plan-head"><h3>${head}</h3>${p.final && p.review && p.review.approved ? `<span class="reviewed">Reviewed ✓</span>` : ""}${update}</div>
    ${it ? `<div style="margin-top:8px">${it.highlights.map(h => `<span class="tag" style="margin:0 6px 6px 0">✦ ${esc(h)}</span>`).join("")}</div>` : ""}
    <div class="plan-grid">
      <div class="section"><div class="label">Day by day</div>${it ? it.days.map(dayHTML).join("") : waitingHTML("Planning each day…")}</div>
      <div>
        <div class="section">${it ? `<div class="map" id="map"></div>` : waitingHTML("Mapping your route…")}</div>
        <div class="section"><div class="label">Book it</div>${bookHTML(p)}</div>
        <div class="section"><div class="label">What it costs</div>${costsHTML(p)}</div>
      </div>
    </div></section>`;
}

export function emptyCanvasHTML() {
  return `<div class="empty"><div class="globe"></div><p class="serif" style="font-size:20px;color:var(--ink);margin:0 0 4px">Your trips will appear here</p>
    <p>Tell me where you'd like to go in the chat. I'll start searching as soon as I know the basics.</p></div>`;
}
