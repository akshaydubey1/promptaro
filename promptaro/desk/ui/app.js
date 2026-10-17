// Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
// Promptaro — Author: Akshay Dubey
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const S = { sessions: [], current: null, session: null, models: [], tiers: {}, running: false, pendingApproval: null, trackTier: 0 };

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "content-type": "application/json", "x-promptaro": "1", ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  const text = await r.text();
  const data = text ? JSON.parse(text) : {};
  if (!r.ok) { const e = new Error(typeof data.detail === "string" ? data.detail : data.detail?.message || "Request failed"); e.detail = data.detail; e.status = r.status; throw e; }
  return data;
}
function toast(msg, ms = 3200) { const t = $("toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ms); }

/* ---------------- markdown (small, safe: escapes first) ---------------- */
function md(src) {
  const blocks = []; let s = String(src ?? "");
  s = s.replace(/```([\w+-]*)[^\n]*\n([\s\S]*?)```/g, (_, lang, code) => { blocks.push(`<pre><button class="btn copy-code" data-copy>Copy</button><code>${esc(code.replace(/\n$/, ""))}</code></pre>`); return `\u0000${blocks.length - 1}\u0000`; });
  s = esc(s);
  const inline = (t) => t.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>").replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = []; let list = null;
  for (const line of s.split("\n")) {
    const b = line.match(/^\u0000(\d+)\u0000$/);
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    const li = line.match(/^\s*([-*]|\d+[.)])\s+(.*)$/);
    if (li) { const kind = /\d/.test(li[1]) ? "ol" : "ul"; if (!list || list !== kind) { if (list) out.push(`</${list}>`); out.push(`<${kind}>`); list = kind; } out.push(`<li>${inline(li[2])}</li>`); continue; }
    if (list) { out.push(`</${list}>`); list = null; }
    if (b) out.push(blocks[+b[1]]);
    else if (h) out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`</${list}>`);
  return out.join("").replace(/<\/p><p>/g, "<br>");
}
document.addEventListener("click", (e) => {
  if (e.target.matches("[data-copy]")) { navigator.clipboard.writeText(e.target.nextElementSibling.textContent); e.target.textContent = "Copied"; setTimeout(() => (e.target.textContent = "Copy"), 1200); }
});

/* ---------------- sidebar ---------------- */
async function loadSessions() {
  [S.sessions, S.projNames] = await Promise.all([api("/api/sessions"), api("/api/project-names")]);
  renderSide();
}
const projBase = (p) => (p || "").split(/[\\/]/).filter(Boolean).pop() || p;
const projName = (p) => (S.projNames || {})[p] || projBase(p);
function collapsedProjs() {
  try { return new Set(JSON.parse(localStorage.getItem("promptaro:projCollapsed") || "[]")); } catch { return new Set(); }
}
function renderSide() {
  const row = (s, inProj) => `<li class="item ${s.id === S.current ? "active" : ""}" data-sid="${s.id}"><span class="status ${esc(s.status)}" title="${esc(statusText(s.status))}"></span><span class="name">${esc(s.title)}${!inProj && s.project ? `<span class="sub"> · ${esc(projName(s.project))}</span>` : ""}${inProj && s.kind === "agent" ? `<span class="sub tag">agent</span>` : ""}</span>${s.kind === "agent" && s.cost ? `<span class="sub">$${(+s.cost).toFixed(2)}</span>` : ""}<button class="x ren" data-ren="${s.id}" title="Rename" aria-label="Rename conversation">✏</button><button class="x" data-del="${s.id}" title="Delete" aria-label="Delete conversation">✕</button></li>`;
  const unbound = S.sessions.filter((s) => !s.project);
  const projects = new Map();                                  // path -> sessions, first-seen order
  for (const s of S.sessions) if (s.project) (projects.get(s.project) || projects.set(s.project, []).get(s.project)).push(s);
  const collapsed = collapsedProjs();
  let html = `<div class="group"><h3>Chats</h3><ul>${unbound.map((s) => row(s)).join("") || `<li class="empty-note">No chats yet</li>`}</ul></div>`;
  if (projects.size) {
    html += `<div class="group"><h3>Projects</h3>`;
    for (const [p, items] of projects) {
      const shut = collapsed.has(p);
      html += `<div class="proj"><h4 class="proj-head" data-proj="${esc(p)}" title="${esc(p)}"><span class="chev${shut ? " shut" : ""}">▾</span>📁 ${esc(projName(p))}<span class="count">${items.length}</span><button class="x ren" data-ren-proj="${esc(p)}" title="Rename project" aria-label="Rename project">✏</button><button class="x add" data-add-chat="${esc(p)}" title="New chat in ${esc(projName(p))}" aria-label="New chat in this project">＋</button></h4><ul${shut ? ' class="hidden"' : ""}>${items.map((s) => row(s, true)).join("")}</ul></div>`;
    }
    html += `</div>`;
  }
  $("lists").innerHTML = html;
}
const statusText = (s) => ({ running: "Working", waiting: "Needs your approval", review: "Ready for review", done: "Merged", discarded: "Discarded", stopped: "Stopped", idle: "Idle" }[s] || s);
$("side").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    const s = S.sessions.find((x) => x.id === del.dataset.del);
    if (!confirm(s?.kind === "agent" ? "Delete this agent task and throw away its unmerged changes?" : "Delete this chat?")) return;
    try { await deleteSession(del.dataset.del); } catch (err) { toast(err.message); }
    return;
  }
  const renProj = e.target.closest("[data-ren-proj]");
  if (renProj) {
    const p = renProj.dataset.renProj;
    const name = prompt("Project name", projName(p));
    if (name !== null) {
      try { await api("/api/projects/rename", { method: "POST", body: { path: p, name } }); await loadSessions(); if (S.session) renderTop(); } catch (err) { toast(err.message); }
    }
    return;
  }
  const ren = e.target.closest("[data-ren]");
  if (ren) {
    const s = S.sessions.find((x) => x.id === ren.dataset.ren);
    const title = prompt(s?.kind === "agent" ? "Rename this agent task" : "Rename this chat", s?.title || "");
    if (title !== null && title.trim()) {
      try { await api(`/api/sessions/${ren.dataset.ren}`, { method: "PATCH", body: { title: title.trim() } }); await loadSessions(); if (S.current === ren.dataset.ren) openSession(S.current); } catch (err) { toast(err.message); }
    }
    return;
  }
  const add = e.target.closest("[data-add-chat]");
  if (add) {
    try {
      const s = await api("/api/sessions", { method: "POST", body: { project: add.dataset.addChat } });
      await loadSessions(); openSession(s.id);
      toast("New chat in this project — the model reads its files, and can't change them.", 4000);
    } catch (err) { toast(err.message); }
    return;
  }
  const head = e.target.closest("[data-proj]");
  if (head) {
    const p = head.dataset.proj, c = collapsedProjs();
    c.has(p) ? c.delete(p) : c.add(p);
    try { localStorage.setItem("promptaro:projCollapsed", JSON.stringify([...c])); } catch { }
    renderSide();
    return;
  }
  const li = e.target.closest("[data-sid]"); if (li) { openSession(li.dataset.sid); $("side").classList.remove("open"); }
});
async function deleteSession(sid) {
  await api(`/api/sessions/${sid}`, { method: "DELETE" });
  if (sid === S.current) closeView();
  loadSessions();
}
function closeView() {
  S.current = null; S.session = null; S.running = false;
  $("title").textContent = "Promptaro"; inputHint(); renderSide(); renderTop(); closeReview(); welcome();
}
$("menu").onclick = () => $("side").classList.toggle("open");

/* ---------------- models ---------------- */
async function loadModels() {
  try { S.models = await api("/api/models"); } catch { S.models = []; }
  S.tiers = Object.fromEntries(S.models.map((m) => [m.id, m.tier]));
  const opts = `<option value="">Auto (router decides)</option>` + S.models.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}${m.subscription ? "" : m.free ? " · free" : m.cost_note ? " · " + esc(m.cost_note) : m.via ? " · " + esc(m.via) : ""}</option>`).join("");
  $("modelPick").innerHTML = opts; $("agentModel").innerHTML = opts;
  const on = S.models.length > 0;
  $("connDot").classList.toggle("on", on);
  $("connLabel").textContent = on ? `${S.models.length} models connected` : "Connect AI accounts";
  inputHint();
}
/* The composer never invites input it can't serve. */
function inputHint() {
  $("input").placeholder = S.models.length ? "Ask anything" : "Connect an AI account to start chatting";
}
const fmtTokens = (n) => (n >= 10000 ? (n / 1000).toFixed(1) + "k" : String(n));
const money = (v) => (!v ? "$0" : v < 0.01 ? "$" + v.toFixed(4) : "$" + v.toFixed(2));
const pct = (q) => Math.round((q || 0) * 100) + "%";
const plural = (n, w) => `${n || 0} ${w}${n === 1 ? "" : "s"}`;
const cap = (t) => String(t || "").charAt(0).toUpperCase() + String(t || "").slice(1);
const ICON = {
  up: '<svg viewBox="0 0 20 20" width="15" height="15" aria-hidden="true"><path d="M6 9l3.2-6c1.3 0 2.1 1 1.8 2.3L10.4 8H15a1.6 1.6 0 0 1 1.6 1.9l-1.1 5.6A1.8 1.8 0 0 1 13.7 17H6zM3 9h2v8H3z" fill="currentColor"/></svg>',
  down: '<svg viewBox="0 0 20 20" width="15" height="15" aria-hidden="true"><path d="M14 11l-3.2 6c-1.3 0-2.1-1-1.8-2.3l.6-2.7H5a1.6 1.6 0 0 1-1.6-1.9l1.1-5.6A1.8 1.8 0 0 1 6.3 3H14zM17 11h-2V3h2z" fill="currentColor"/></svg>',
  copy: '<svg viewBox="0 0 20 20" width="15" height="15" aria-hidden="true"><path d="M7 3h8a2 2 0 0 1 2 2v8h-2V5H7zM3 7a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zm2 0v9h7V7z" fill="currentColor"/></svg>',
};

/* "Why this model?" — difficulty, the bar, the options weighed, and what it saved */
function whyPanel(x, compact = false) {
  if (!x) return "";
  const lvl = { easy: "Easy", medium: "Medium", hard: "Hard" }[x.level] || "";
  const body = [];
  if (x.difficulty !== undefined) body.push(`<div class="meter"><span>Difficulty</span><div class="bar"><i style="width:${Math.max(4, Math.round(x.difficulty * 100))}%"></i></div><b>${lvl}</b></div>`);
  let line = cap(x.because);
  if (x.quality_needed) line += ` It needed an expected quality of ${pct(x.quality_needed)} for ${esc(x.task_type || "this")} work, and ${x.models_able} of ${x.models_considered} connected models were expected to get there.`;
  body.push(`<p>${esc(line)}</p>`);
  if (x.free_skipped) body.push(`<p class="note">Free models weren't used: ${esc(x.free_skipped)}.</p>`);
  if (x.escalations) body.push(`<p class="note">The first answer didn't pass the checks, so it moved up ${x.escalations} level${x.escalations > 1 ? "s" : ""}.</p>`);
  if (x.saved_usd > 0 && x.strongest) body.push(`<p class="saved">About ${money(x.saved_usd)} less than ${esc(x.strongest.name)} would have cost for the same answer.</p>`);
  if (x.options && x.options.length && !compact) {
    body.push(`<table class="opts-table"><thead><tr><th>Model</th><th title="Expected chance of a good answer, learned over time">Quality</th><th>Est. cost</th><th>Good enough?</th></tr></thead><tbody>${
      x.options.map((o) => `<tr class="${o.chosen ? "chosen" : ""}"><td>${esc(o.name)}${o.free ? ' <span class="tag free">Free</span>' : ""}${o.chosen ? ' <span class="tag pick">Chosen</span>' : ""}</td><td>${pct(o.expected_quality)}</td><td>${o.free ? "$0" : money(o.est_cost_usd)}</td><td>${o.able ? "Yes" : "No"}</td></tr>`).join("")}</tbody></table>
      <p class="fine">Quality scores start from each model family's track record and are adjusted by checks on real answers and by your thumbs up and down.</p>`);
  }
  return `<details class="why"><summary>Why this model?</summary><div class="why-body">${body.join("")}</div></details>`;
}
const tierOf = (id) => (id === "offline" ? 0 : S.tiers[id] || 2);

/* ---------------- thread ---------------- */
const SUGGEST = ["Explain this error message and how to fix it", "Write a SQL query that finds duplicate emails",
  "Summarize the pros and cons of Postgres vs MySQL", "Draft a polite follow-up email to a recruiter"];
async function welcome() {
  const th = $("thread");
  if (!S.models.length) return onboarding();
  th.innerHTML = `<div class="welcome">
    <h2>What are we working on?</h2>
    <p>Ask anything, or hand a coding task to an agent. Easy questions go to free models, everyday work to the cheapest model that does it well, and hard work to your subscription or the strongest model.</p>
    <div class="starters">
      <button class="starter" data-start="agent"><b>Start an agent on a repo</b><span>It reads the code, makes the change, runs your tests, and waits for your review.</span></button>
      <button class="starter" data-start="chat"><b>Ask a question</b><span>Every answer shows which model answered and why.</span></button>
      <button class="starter" data-start="projchat"><b>Chat about a project</b><span>Bind a folder — the model reads your code to answer, and can't change it.</span></button>
    </div>
    <div class="suggest">${SUGGEST.map((q) => `<button class="chip" data-suggest="${esc(q)}">${esc(q)}</button>`).join("")}</div>
    <div class="legend" aria-label="Track colors"><span><i style="background:var(--t1)"></i>Small or free model</span><span><i style="background:var(--t2)"></i>Medium model</span><span><i style="background:var(--t3)"></i>Large model or your subscription</span></div>
  </div>`;
}

/* first run: three short steps, each showing its live status */
async function onboarding() {
  let st = { connections: {} }, subs = { claude_code: {}, codex: {} };
  try { [st, subs] = await Promise.all([api("/connect/status"), api("/api/subscriptions")]); } catch {}
  if (S.current) return;
  const or = st.connections?.openrouter?.connected;
  const anyKey = ["anthropic", "openai", "google"].some((v) => st.connections?.[v]?.connected);
  const freeCount = (st.free_providers || []).filter((p) => p.connected).length;
  const plan = (x, name, tool, how) => x.subscription ? `<span class="ok">✓ ${name} plan found — hard tasks will use it.</span>`
    : x.installed ? `${tool} is installed but not signed in with your ${name} plan. In Terminal run <code>${how}</code>.`
    : `Install ${tool} and sign in with your ${name} plan.`;
  $("thread").innerHTML = `<div class="welcome onboard">
    <h2>Welcome to Promptaro</h2>
    <p>One place for every AI model. Promptaro picks the right model for each question, so easy things cost nothing and hard things get the best. Setup takes about a minute.</p>
    <ol class="steps">
      <li class="${or ? "done" : ""}"><div><b>Sign in with OpenRouter</b><span>A free account that unlocks every model family, including 50 free requests a day.</span></div>
        ${or ? '<span class="ok">✓ Connected</span>' : '<a class="btn primary" href="/connect/openrouter/start">Sign in</a>'}</li>
      <li class="${freeCount ? "done" : ""}"><div><b>Add free providers</b><span>Groq, Cerebras and NVIDIA give free keys with generous daily allowances and don't train on your prompts. Each takes a minute; together they cover millions of tokens a day.</span>${freeCount ? `<span class="ok">✓ ${freeCount} connected</span>` : ""}</div>
        <button class="btn" data-start="free">${freeCount ? "Add more" : "Add free keys"}</button></li>
      <li class="${subs.claude_code?.subscription || subs.codex?.subscription ? "done" : ""}"><div><b>Already pay for Claude or ChatGPT?</b>
        <span>Hard agent tasks run on your plan, with no extra cost. Promptaro finds them on its own.</span>
        <span class="plan">Claude: ${plan(subs.claude_code || {}, "Claude", "Claude Code", "claude")}</span>
        <span class="plan">ChatGPT: ${plan(subs.codex || {}, "ChatGPT", "Codex CLI", "codex login")}</span></div>
        <button class="btn" data-start="recheck">Check again</button></li>
      <li class="${anyKey ? "done" : ""}"><div><b>Optional: paid API keys</b><span>Anthropic, OpenAI or Google keys connect directly, without OpenRouter's fee.</span></div>
        <button class="btn" data-start="connect">Add keys</button></li>
    </ol>
    <p class="fine">Keys are stored in your system keychain. Nothing leaves this computer except the questions you send to the model that answers them.</p>
  </div>`;
}
$("thread").addEventListener("click", async (e) => {
  const sg = e.target.closest("[data-suggest]");
  if (sg) { input.value = sg.dataset.suggest; input.focus(); input.dispatchEvent(new Event("input")); return; }
  const st = e.target.closest("[data-start]"); if (!st) return;
  if (st.dataset.start === "agent") openAgentDialog();
  if (st.dataset.start === "projchat") openProjChat();
  if (st.dataset.start === "chat") $("input").focus();
  if (st.dataset.start === "connect") openConnections();
  if (st.dataset.start === "free") openConnections().then(() => $("freeHead")?.scrollIntoView({ block: "start" }));
  if (st.dataset.start === "recheck") { st.textContent = "Checking…"; await api("/api/subscriptions?refresh=1"); await loadModels(); welcome(); }
});

/* ---------------- savings ---------------- */
async function loadSavings() {
  try {
    const d = await api("/api/insights"); S.insights = d;
    const t = d.totals || {};
    $("savingsBtn").classList.toggle("hidden", !(t.answers || t.agent_steps || t.subscription_runs || t.gateway_requests));
    $("savingsAmt").textContent = money(t.saved || 0);
  } catch {}
}
async function openSavings() {
  await loadSavings();
  try { S.freeUsage = (await api("/api/free")).usage; } catch { S.freeUsage = []; }
  const d = S.insights || { totals: {} }, t = d.totals || {};
  const q = d.free_quota;
  const subs = d.subscriptions || {};
  const plans = [subs.claude_code?.subscription && "Claude", subs.codex?.subscription && "ChatGPT"].filter(Boolean);
  $("savingsBody").innerHTML = `
    <div class="big-row">
      <div class="big"><b>${money(t.saved || 0)}</b><span>saved by smart routing</span></div>
      <div class="big"><b>${money(t.spent || 0)}</b><span>spent on paid models</span></div>
      <div class="big"><b>${fmtTokens(Math.round(t.tokens || 0))}</b><span>tokens used</span></div>
    </div>
    <p class="fine">"Saved" compares each answer with what the strongest connected model would have charged for the same work${t.plan_value ? `, including ${money(t.plan_value)} of work done on your subscription` : ""}.</p>
    <div class="split">
      <div><span class="tag free">Free</span> ${plural(t.answers_free, "answer")}</div>
      <div><span class="tag paid">Paid</span> ${plural(t.answers_paid, "answer")} · ${plural(t.agent_steps, "agent step")}</div>
      <div><span class="tag sub">Subscription</span> ${plural(t.subscription_runs, "agent run")}${plans.length ? ` (${plans.join(" and ")})` : ""}</div>
      <div><span class="tag paid">Reused</span> ${plural(t.answers_cached, "repeated question")} answered instantly</div>
    </div>
    ${q ? `<p>${q.available ? `Free requests left today: <b>${q.remaining ?? "—"}</b>${q.limit ? ` of ${q.limit}` : ""}.` : q.pause_reason === "daily" ? "Today's free requests are used up; free models come back at midnight UTC. Paid models fill in until then." : "Free models are rate-limited for a minute; paid models fill in."}${q.limit === 50 ? ' <span class="fine">Buying $10 of OpenRouter credit once raises this to 1000 a day.</span>' : ""}</p>` : ""}
    ${S.freeUsage && S.freeUsage.length ? `<h3>Free providers today</h3><table class="opts-table"><thead><tr><th>Provider</th><th>Requests</th><th>Tokens</th><th>Status</th></tr></thead><tbody>${
      S.freeUsage.map((u) => `<tr><td>${esc(u.label)}</td><td>${k(u.requests_today)}${u.requests_left_today ? ` <span class="fine">(${k(u.requests_left_today)} left)</span>` : ""}</td><td>${k(u.tokens_today)}${u.tokens_left_today ? ` <span class="fine">(${k(u.tokens_left_today)} left)</span>` : ""}</td><td>${u.paused ? (u.paused === "daily" ? "Done for today" : "Cooling down") : "Available"}</td></tr>`).join("")}</tbody></table>` : ""}
    ${t.gateway_requests ? `<p>Coding tools: ${plural(t.gateway_requests, "request")}, ${t.gateway_free || 0} on free models.</p>` : ""}
    <h3>What Promptaro has learned</h3>
    ${d.learned && d.learned.length ? `<table class="opts-table"><thead><tr><th>Model</th><th>Uses</th><th>Passed checks</th><th>👍 / 👎</th></tr></thead><tbody>${
      d.learned.map((x) => `<tr><td>${esc(x.name)}</td><td>${x.runs}</td><td>${x.pass_rate == null ? "—" : pct(x.pass_rate)}</td><td>${x.thumbs_up} / ${x.thumbs_down}</td></tr>`).join("")}</tbody></table>`
      : '<p class="note">Nothing yet. Every answer, check and thumbs up or down makes the next choice better.</p>'}`;
  $("savingsDlg").showModal();
}
$("savingsBtn").onclick = openSavings;
$("savingsClose").onclick = () => $("savingsDlg").close();

function track(tier, inner, quiet = false) {
  const d = document.createElement("div");
  d.className = `track t${tier}`;
  d.innerHTML = `<div class="station ${quiet ? "quiet" : ""}">${inner}</div>`;
  $("thread").appendChild(d); return d;
}
function plain(cls, html) { const d = document.createElement("div"); d.className = cls; d.innerHTML = html; $("thread").appendChild(d); return d; }

const VERBS = { list_files: "Listed", read_file: "Read", search: "Searched", write_file: "Wrote", edit_file: "Edited", run: "Ran" };
function target(a, args) { return a === "run" ? args.command : a === "search" ? `${args.pattern}${args.path && args.path !== "." ? " in " + args.path : ""}` : args.path || "."; }

function renderEvent(ev) {
  const d = ev.data || {}, th = $("thread");
  th.querySelector(".welcome")?.remove();
  th.querySelector(".thinking-wrap")?.remove();
  switch (ev.kind) {
    case "user": plain("msg-user", `<div class="bubble">${esc(d.text)}</div>`); break;
    case "assistant": {
      th.querySelector(".live")?.remove();
      const t = tierOf(d.model);
      const tokens = (d.tokens_in || 0) + (d.tokens_out || 0);
      const el = track(t, `<div class="answer">${md(d.text)}</div>
        <div class="route">
          <span class="model">${esc(d.name || d.model)}</span>
          ${d.offline ? "" : d.free ? '<span class="tag free">Free</span>' : '<span class="tag paid">Paid</span>'}
          <span>${d.cached ? "reused an earlier answer" : `${fmtTokens(tokens)} tokens`}${tokens && !d.cached ? ` <span class="sub">(${fmtTokens(d.tokens_in || 0)} in · ${fmtTokens(d.tokens_out || 0)} out)</span>` : ""}</span>
          ${!d.free && +d.cost > 0 ? `<span>${money(+d.cost)}</span>` : d.cost_note ? `<span class="sub">${esc(d.cost_note)}</span>` : ""}
          ${d.retry ? '<span class="auto">Stronger model</span>' : S.session && S.session.model ? "" : '<span class="auto">Auto-selected</span>'}
          ${d.passed === false && !d.offline ? '<span class="flag-bad">did not pass checks</span>' : ""}
        </div>
        ${d.offline ? "" : `<div class="acts" data-seq="${ev.seq}">
          <button class="btn ghost icon" data-fb="1" title="Good answer" aria-label="Good answer">${ICON.up}</button>
          <button class="btn ghost icon" data-fb="0" title="Not good — teach the router" aria-label="Not a good answer">${ICON.down}</button>
          <button class="btn ghost icon" data-copyans title="Copy answer" aria-label="Copy answer">${ICON.copy}</button>
          ${whyPanel(d.explain)}
        </div>`}`);
      el._text = d.text;
      break;
    }
    case "delta": {
      let live = th.querySelector(".live");
      if (d.restart && live) { live.remove(); live = null; plain("track", '<div class="note">That answer didn\'t pass the checks — asking a stronger model.</div>'); }
      if (!live) { live = track(2, '<div class="answer"></div>'); live.classList.add("live"); live._buf = ""; }
      live._buf += d.text || "";
      if (!live._raf) live._raf = requestAnimationFrame(() => { live._raf = 0; let b = live._buf; if ((b.match(/```/g) || []).length % 2) b += "\n```";   // close a half-written code block
        live.querySelector(".answer").innerHTML = md(b) + '<span class="caret"></span>'; });
      th.appendChild(live);
      break;
    }
    case "feedback": {
      const acts = th.querySelector(`.acts[data-seq="${d.seq}"]`);
      if (!acts) break;
      acts.querySelectorAll("[data-fb]").forEach((b) => b.classList.toggle("on", (b.dataset.fb === "1") === d.good));
      if (!d.good && S.session?.kind === "chat" && !acts.querySelector("[data-retry]")) acts.insertAdjacentHTML("beforeend", `<button class="btn" data-retry="${d.seq}">Try a stronger model</button>`);
      break;
    }
    case "model": S.trackTier = d.subscription ? 3 : tierOf(d.model); track(S.trackTier, `<div class="model-switch">Using <b>${esc(d.name || d.model)}</b>${d.subscription ? ' <span class="tag sub">Your subscription</span>' : d.free ? ' <span class="tag free">Free</span>' : d.via ? ` via ${esc(d.via)}` : ""}${whyPanel(d.explain, false)}</div>`, true); break;
    case "thought": track(S.trackTier, `<div class="thought">${esc(d.text)}</div>`, true); break;
    case "tool_call": {
      const el = track(S.trackTier, `<details class="step pending"><summary><span class="verb">${d.manual ? "You ran" : VERBS[d.action] || d.action}</span><span class="target">${esc(target(d.action, d.args || {}))}</span><span class="res">…</span></summary>${d.action === "write_file" || d.action === "edit_file" ? `<div class="out">${esc(d.args.new ?? d.args.content ?? "")}</div>` : ""}</details>`, true);
      el.dataset.pending = "1"; break;
    }
    case "tool_result": {
      const el = [...th.querySelectorAll('[data-pending="1"]')].pop();
      if (!el) break; el.dataset.pending = "0";
      const det = el.querySelector("details");
      const code = d.meta && d.meta.exit_code !== undefined && d.meta.exit_code !== null ? ` (exit ${d.meta.exit_code})` : "";
      det.querySelector(".res").outerHTML = `<span class="res ${d.ok ? "ok" : "fail"}">${d.ok ? "ok" : "failed"}${code}</span>`;
      if (d.action !== "write_file" && d.action !== "edit_file" || !d.ok) det.insertAdjacentHTML("beforeend", `<div class="out">${esc(d.output)}</div>`);
      if (!d.ok && d.action === "run") det.open = true;
      break;
    }
    case "approval": {
      const el = track(S.trackTier, `<div class="approval" data-aid="${esc(d.id)}"><strong>Allow this command?</strong><code>${esc(d.command)}</code><div class="row"><button class="btn primary" data-approve="1">Allow</button><button class="btn" data-approve="0">Don't allow</button></div></div>`);
      el.querySelector(".approval").dataset.open = "1"; break;
    }
    case "approval_result": {
      const a = th.querySelector(`.approval[data-aid="${CSS.escape(d.id)}"]`);
      if (a) { a.dataset.open = "0"; a.querySelector(".row").outerHTML = `<div class="note">${d.approved ? "Allowed" : "Not allowed"}</div>`; }
      break;
    }
    case "escalate": track(S.trackTier, `<div class="note">Moving to a stronger model: ${esc(d.reason)}.</div>`, true); break;
    case "finish": {
      const tests = { passed: "Tests passed", failed: "Tests failed", not_run: "Tests not run" }[d.tests] || "";
      track(S.trackTier, `<div class="finish"><h4>Done${tests ? ` — ${tests.toLowerCase()}` : ""}</h4><div class="answer">${md(d.summary)}</div>${S.session?.kind === "agent" ? `<div class="row"><button class="btn primary" data-act="review">Review changes</button><button class="btn" data-act="open-code">Open in VS Code</button></div>` : ""}</div>`);
      break;
    }
    case "workspace": plain("track", `<div class="note">Working on branch <code>${esc(d.branch)}</code>${d.base ? ` from <code>${esc(d.base)}</code>` : ""}. ${esc(d.note || "")}</div>`); break;
    case "git": plain("track", `<div class="note">${esc(d.text)}${d.url ? ` <a href="${esc(d.url)}" target="_blank" rel="noopener">Open</a>` : ""}</div>`); break;
    case "note": plain("track", `<div class="note">${esc(d.text)}</div>`); break;
    case "error": plain("track", `<div class="err">${esc(d.text)}</div>`); break;
  }
}
function showThinking() { if (!$("thread").querySelector(".thinking-wrap")) plain("track thinking-wrap", `<div class="thinking">${S.session?.kind === "agent" ? "Working" : "Thinking"}</div>`); }
function scrollDown() { const t = $("thread"); t.scrollTop = t.scrollHeight; }

$("thread").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-approve]");
  if (b) {
    const card = b.closest(".approval");
    try { await api(`/api/sessions/${S.current}/approve`, { method: "POST", body: { id: card.dataset.aid, approved: b.dataset.approve === "1" } }); }
    catch (err) { toast(err.message); }
  }
  const fb = e.target.closest("[data-fb]");
  if (fb) {
    const seq = +fb.closest(".acts").dataset.seq;
    try { await api(`/api/sessions/${S.current}/feedback`, { method: "POST", body: { seq, good: fb.dataset.fb === "1" } }); toast(fb.dataset.fb === "1" ? "Thanks — the router will lean on this model for work like this." : "Got it — the router will try other models for work like this."); }
    catch (err) { toast(err.message); }
  }
  const rt = e.target.closest("[data-retry]");
  if (rt) {
    rt.disabled = true;
    try { await api(`/api/sessions/${S.current}/retry`, { method: "POST", body: { seq: +rt.dataset.retry } }); S.running = true; showThinking(); }
    catch (err) { toast(err.message); rt.disabled = false; }
  }
  const cp = e.target.closest("[data-copyans]");
  if (cp) { navigator.clipboard.writeText(cp.closest(".track")._text || ""); toast("Copied"); }
  const act = e.target.closest("[data-act]");
  if (act?.dataset.act === "review") openReview();
  if (act?.dataset.act === "open-code") openIn("code");
});

async function openSession(sid) {
  S.current = sid; renderSide();
  const r = await api(`/api/sessions/${sid}`);
  S.session = r.session; S.running = r.running || r.session.status === "running"; S.trackTier = 0;
  $("title").textContent = r.session.title;
  $("thread").innerHTML = "";
  r.events.forEach(renderEvent);
  if (S.running) showThinking();
  $("modelPick").value = r.session.model || ""; $("preferPick").value = r.session.prefer || "balanced";
  $("input").placeholder = r.session.kind === "agent" ? "Tell the agent something (it reads it at its next step)"
    : r.session.project ? "Ask about this project — the model can read its files" : "Message";
  renderTop(); updateComposer(); scrollDown();
  if (r.session.kind !== "agent") closeReview();
  else if (!$("review").classList.contains("hidden")) openReview();
}
function renderTop() {
  const s = S.session; if (!s) { $("topTools").innerHTML = ""; return; }
  $("topTools").innerHTML = (s.kind === "agent" ? `<span>${esc(statusText(s.status))}</span><button class="btn" data-top="review">Changes</button>` : "") +
    (s.project ? `<span title="${esc(s.project)}">📁 ${esc(projName(s.project))}</span>` : "") +
    ((s.tokens_in || s.tokens_out) ? `<span title="Tokens used in this conversation (in · out)">${fmtTokens((s.tokens_in || 0) + (s.tokens_out || 0))} tokens</span>` : "") +
    (s.cost ? `<span title="Total spent in this conversation">$${(+s.cost).toFixed(4)}</span>` : (s.tokens_in ? `<span>$0</span>` : "")) +
    `<button class="btn ghost icon" data-top="export" title="Download this conversation as Markdown" aria-label="Download conversation">⤓</button>` +
    `<button class="btn ghost icon" data-top="close" title="Close this view (keeps the conversation in the list)" aria-label="Close view">✕</button>` +
    `<button class="btn ghost icon" data-top="delete" title="Delete conversation" aria-label="Delete conversation">🗑</button>`;
}
$("topTools").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-top]"); if (!b) return;
  const s = S.session;
  if (b.dataset.top === "review") openReview();
  if (b.dataset.top === "export") location.href = `/api/sessions/${s.id}/export`;
  if (b.dataset.top === "close") closeView();
  if (b.dataset.top === "delete") {
    if (!confirm(s.kind === "agent" ? "Delete this agent task and throw away its unmerged changes?" : "Delete this chat?")) return;
    try { await deleteSession(s.id); } catch (err) { toast(err.message); }
  }
});
function updateComposer() { $("stopBtn").classList.toggle("hidden", !(S.session?.kind === "agent" && S.running)); }

/* ---------------- sending ---------------- */
const input = $("input");
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px"; });
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("sendBtn").onclick = send;
async function send() {
  const text = input.value.trim(); if (!text) return;
  if (!S.models.length) {          // nothing connected — guide to the one-minute setup instead of dead-ending
    toast("Connect one AI account first — sign in with OpenRouter or paste any key.", 5000);
    connMsg(); openConnections().then(() => $("anyKey").focus());
    return;
  }
  try {
    if (!S.current) {
      const s = await api("/api/sessions", { method: "POST", body: { prefer: $("preferPick").value, model: $("modelPick").value } });
      await loadSessions(); await openSession(s.id);
    }
    input.value = ""; input.style.height = "auto";
    await api(`/api/sessions/${S.current}/messages`, { method: "POST", body: { text } });
    S.running = true; updateComposer();
  } catch (e) { input.value = text; input.dispatchEvent(new Event("input")); toast(e.message); }   // keep the draft
}
$("stopBtn").onclick = () => S.current && api(`/api/sessions/${S.current}/stop`, { method: "POST" });
$("modelPick").onchange = () => S.current && api(`/api/sessions/${S.current}`, { method: "PATCH", body: { model: $("modelPick").value } });
$("preferPick").onchange = () => S.current && api(`/api/sessions/${S.current}`, { method: "PATCH", body: { prefer: $("preferPick").value } });
$("newChat").onclick = () => { closeView(); $("title").textContent = "New chat"; input.focus(); $("side").classList.remove("open"); };

/* ---------------- live events ---------------- */
let streamKey = "";
async function connectStream() {
  if (!streamKey) {
    try { streamKey = (await api("/api/stream-key", { method: "POST" })).key; }
    catch { setTimeout(connectStream, 3000); return; }
  }
  const es = new EventSource(`/api/stream?key=${encodeURIComponent(streamKey)}`);
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) { streamKey = ""; setTimeout(connectStream, 3000); } };
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.kind === "session") {
      const s = S.sessions.find((x) => x.id === ev.session_id);
      if (s) Object.assign(s, ev.data); else loadSessions();
      renderSide();
      if (ev.session_id === S.current) {
        Object.assign(S.session, ev.data); $("title").textContent = S.session.title; S.running = ev.data.status === "running" || ev.data.status === "waiting";
        renderTop(); updateComposer();
        if (!S.running) $("thread").querySelector(".thinking-wrap")?.remove();
        if (ev.data.status === "review" && !$("review").classList.contains("hidden")) openReview();
      }
      return;
    }
    if (ev.session_id !== S.current) { if (ev.kind === "assistant" || ev.kind === "finish") loadSavings(); return; }
    const nearBottom = $("thread").scrollHeight - $("thread").scrollTop - $("thread").clientHeight < 160;
    renderEvent(ev);
    if (S.running && !["finish", "approval", "assistant", "error", "delta", "feedback"].includes(ev.kind)) showThinking();
    if (["assistant", "finish"].includes(ev.kind)) loadSavings();
    if (nearBottom) scrollDown();
  };
}

/* ---------------- review drawer ---------------- */
async function openReview() {
  if (!S.session || S.session.kind !== "agent") return;
  $("review").classList.remove("hidden"); $("app").classList.add("with-review");
  const s = S.session;
  $("reviewMeta").innerHTML = `Branch <code>${esc(s.branch)}</code>${s.base_branch ? ` from <code>${esc(s.base_branch)}</code>` : ""}`;
  let d; try { d = await api(`/api/sessions/${s.id}/diff`); } catch (e) { $("files").innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  if (d.gone) { $("reviewActions").innerHTML = ""; $("files").innerHTML = `<p class="note">This task's copy was removed.</p>`; $("patch").innerHTML = ""; return; }
  const has = d.files.length > 0, busy = S.running;
  $("reviewActions").innerHTML = has ? `
    <button class="btn primary" data-rv="merge" ${busy ? "disabled" : ""}>Merge into ${esc(s.base_branch || "your branch")}</button>
    <button class="btn" data-rv="pr" ${busy ? "disabled" : ""}>Open pull request</button>
    <button class="btn" data-rv="commit" ${busy ? "disabled" : ""} title="Commit on ${esc(s.branch)}">Commit to branch</button>
    <button class="btn" data-rv="code">Open in VS Code</button>
    <button class="btn danger" data-rv="discard">Discard</button>` : `<button class="btn" data-rv="code">Open in VS Code</button><button class="btn danger" data-rv="discard">Discard</button>`;
  $("files").innerHTML = has ? d.files.map((f, i) => `<div class="file" data-jump="${i}"><span class="n">${esc(f.path)}</span><span class="a">+${f.added}</span><span class="d">−${f.removed}</span></div>`).join("") : `<p class="note">No changes yet.</p>`;
  let fi = -1;
  $("patch").innerHTML = d.patch.split("\n").map((l) => {
    if (l.startsWith("diff --git")) { fi++; return `<div class="h" id="f${fi}">${esc(l.replace(/^diff --git a\/(.*?) b\/.*/, "$1"))}</div>`; }
    if (/^(index |--- |\+\+\+ |new file|deleted file|similarity|rename )/.test(l)) return "";
    const c = l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : "";
    return `<div class="l ${c}">${esc(l) || " "}</div>`;
  }).join("") + (d.truncated ? `<p class="note">Diff truncated — open in VS Code to see everything.</p>` : "");
}
function closeReview() { $("review").classList.add("hidden"); $("app").classList.remove("with-review"); }
$("closeReview").onclick = closeReview;
$("files").addEventListener("click", (e) => { const f = e.target.closest("[data-jump]"); if (f) $("f" + f.dataset.jump)?.scrollIntoView({ behavior: "smooth" }); });
$("reviewActions").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-rv]"); if (!b) return; const id = S.current, a = b.dataset.rv;
  try {
    if (a === "code") return openIn("code");
    if (a === "discard" && !confirm("Throw away this agent's changes and its branch?")) return;
    b.disabled = true;
    const r = await api(`/api/sessions/${id}/${a}`, { method: "POST", body: a === "commit" ? { message: "" } : undefined });
    toast(a === "merge" ? `Merged (${r.sha})` : a === "commit" ? `Committed ${r.sha}` : a === "pr" ? "Pull request opened" : "Discarded");
    if (a === "discard") closeReview(); else openReview();
  } catch (err) { toast(err.message, 6000); b.disabled = false; }
});
async function openIn(app) { try { await api(`/api/sessions/${S.current}/open`, { method: "POST", body: { app } }); } catch (e) { toast(e.message); } }
$("manualRun").onclick = async () => {
  const cmd = $("manualCmd").value.trim(); if (!cmd) return;
  $("manualRun").disabled = true;
  try { await api(`/api/sessions/${S.current}/run`, { method: "POST", body: { command: cmd } }); $("manualCmd").value = ""; openReview(); }
  catch (e) { toast(e.message); } finally { $("manualRun").disabled = false; }
};
$("manualCmd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("manualRun").click(); });

/* ---------------- new agent / project chat ---------------- */
async function fillProjects(input, box) {
  try {
    const ps = await api("/api/projects");
    box.innerHTML = ps.map((p) => `<button type="button" data-proj="${esc(p)}" title="${esc(p)}">${esc(p.split(/[\\/]/).filter(Boolean).pop() || p)}</button>`).join("");
    if (!input.value && ps[0]) input.value = ps[0];
  } catch {}
}
async function openAgentDialog() {
  $("agentErr").textContent = ""; $("agentModel").value = $("modelPick").value;
  await fillProjects($("projPath"), $("recentProjects"));
  $("agentDlg").showModal(); $("taskText").focus();
}
$("newAgent").onclick = () => { openAgentDialog(); $("side").classList.remove("open"); };
$("recentProjects").addEventListener("click", (e) => { const b = e.target.closest("[data-proj]"); if (b) $("projPath").value = b.dataset.proj; });
$("agentCancel").onclick = () => $("agentDlg").close();
$("pickFolder").onclick = async () => {
  try { const r = await api("/api/pick-folder", { method: "POST" }); if (r.path) $("projPath").value = r.path; }
  catch { $("projPath").focus(); toast("Type or paste the folder path"); }
};
$("agentForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { project: $("projPath").value.trim(), task: $("taskText").value.trim(), model: $("agentModel").value,
    prefer: $("agentPrefer").value, approval: $("agentApproval").value, max_cost: +$("agentBudget").value || 2 };
  if (!body.project || !body.task) { $("agentErr").textContent = "Choose a project folder and describe the task."; return; }
  $("agentStart").disabled = true; $("agentErr").textContent = "";
  try {
    const s = await api("/api/agents", { method: "POST", body });
    $("agentDlg").close(); $("taskText").value = "";
    await loadSessions(); await openSession(s.id); S.running = true; showThinking(); updateComposer();
  } catch (err) {
    if (err.detail?.code === "not_a_repo") {
      $("agentErr").innerHTML = `That folder isn't a git repository yet. <button type="button" class="btn" id="gitInit">Set it up with git</button>`;
      $("gitInit").onclick = async () => { try { await api("/api/repos/init", { method: "POST", body: { path: body.project } }); $("agentErr").textContent = "Done. Start the agent again."; } catch (x) { $("agentErr").textContent = x.message; } };
    } else $("agentErr").textContent = err.message;
  } finally { $("agentStart").disabled = false; }
});

/* ---------------- project chat ---------------- */
async function openProjChat() {
  $("projChatErr").textContent = "";
  await fillProjects($("chatProjPath"), $("chatRecentProjects"));
  $("projChatDlg").showModal(); $("chatProjPath").focus();
}
$("newProjChat").onclick = () => { openProjChat(); $("side").classList.remove("open"); };
$("chatRecentProjects").addEventListener("click", (e) => { const b = e.target.closest("[data-proj]"); if (b) $("chatProjPath").value = b.dataset.proj; });
$("projChatCancel").onclick = () => $("projChatDlg").close();
$("pickChatFolder").onclick = async () => {
  try { const r = await api("/api/pick-folder", { method: "POST" }); if (r.path) $("chatProjPath").value = r.path; }
  catch { $("chatProjPath").focus(); toast("Type or paste the folder path"); }
};
$("projChatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const p = $("chatProjPath").value.trim();
  if (!p) { $("projChatErr").textContent = "Choose a project folder."; return; }
  try {
    const s = await api("/api/sessions", { method: "POST", body: { project: p } });
    $("projChatDlg").close(); $("chatProjPath").value = "";
    await loadSessions(); await openSession(s.id);
    toast("Ask anything — the model reads this project to answer, and can't change it.", 5000);
  } catch (err) { $("projChatErr").textContent = err.message; }
});

/* ---------------- connections ---------------- */
async function openConnections() {
  const s = await api("/connect/status");
  $("connStorage").textContent = s.storage === "os-keychain" ? "Keys are kept in your system keychain and never shown again." : "Keys are kept in a private file in your home folder and never shown again.";
  const rows = Object.entries(s.connections).map(([v, c]) => {
    const right = c.connected ? `<span class="sub">Connected</span><button class="btn" data-off="${v}">Disconnect</button>`
      : v === "openrouter" ? `<a class="btn primary" href="/connect/openrouter/start">Sign in</a><input type="password" autocomplete="off" placeholder="or paste key" data-key="${v}"><button class="btn" data-on="${v}">Connect</button>`
      : `<a class="btn" href="${esc(c.get_key_url)}" target="_blank" rel="noopener">Get key</a><input type="password" autocomplete="off" placeholder="Paste key" data-key="${v}"><button class="btn" data-on="${v}">Connect</button>`;
    const sub = v === "openrouter" ? "One sign-in for Claude, GPT, Gemini and more" : "Optional direct connection, no OpenRouter fee";
    return `<div class="conn-row"><span class="dot ${c.connected ? "on" : ""}"></span><span class="grow"><strong>${esc(c.label)}</strong><br><span class="sub">${sub}</span></span>${right}</div>`;
  });
  const media = Object.entries(s.media_providers || {});
  if (media.length) {
    rows.push(`<div class="free-head"><h3>Image &amp; video keys</h3><span class="fine">Free accounts used by Studio → Generate. Pollinations works with no key at all.</span></div>`);
    rows.push(media.map(([v, c]) => {
      const right = c.connected ? `<span class="sub">Connected</span><button class="btn" data-off="${v}">Disconnect</button>`
        : `<a class="btn" href="${esc(c.get_key_url)}" target="_blank" rel="noopener">Get free key</a><input type="password" autocomplete="off" placeholder="Paste key" data-key="${v}"><button class="btn" data-on="${v}">Connect</button>`;
      const extra = c.needs_account_id ? `<br><input autocomplete="off" spellcheck="false" placeholder="Account ID (dash.cloudflare.com → right side)" data-acct="${v}" style="margin-top:6px">` : "";
      return `<div class="conn-row"><span class="dot ${c.connected ? "on" : ""}"></span><span class="grow"><strong>${esc(c.label)}</strong><br><span class="sub">Free tier — powers image${v === "huggingface" ? " and video" : ""} generation</span>${extra}</span>${right}</div>`;
    }).join(""));
  }
  let subs = { claude_code: {}, codex: {} };
  try { subs = await api("/api/subscriptions"); } catch {}
  const planRow = (x, title, tool, how, url) => `<div class="conn-row"><span class="dot ${x.subscription ? "on" : ""}"></span><span class="grow"><strong>${title}</strong><br><span class="sub">${
    x.subscription ? (x.resting_s ? `Signed in. Resting for ${Math.ceil(x.resting_s / 60)} min after reaching its usage limit.` : "Signed in. Hard agent tasks run on this plan automatically.")
    : x.installed ? `${tool} is installed but not signed in with your plan. Open Terminal and run <code>${how}</code>.`
    : `Not found on this computer. <a href="${url}" target="_blank" rel="noopener">Install ${tool}</a> and sign in with your plan.`}</span></span>
    <button class="btn" data-recheck="1">Check again</button></div>`;
  rows.push(planRow(subs.claude_code || {}, "Claude subscription (Claude Code)", "Claude Code", "claude", "https://docs.claude.com/en/docs/claude-code/setup"));
  rows.push(planRow(subs.codex || {}, "ChatGPT subscription (Codex CLI)", "Codex CLI", "codex login", "https://developers.openai.com/codex/cli"));
  try { rows.push(freeRows(await api("/api/free"))); } catch {}
  rows.push(`<div class="conn-row"><span class="dot ${s.ollama.length ? "on" : ""}"></span><span class="grow"><strong>Ollama (local models)</strong><br><span class="sub">${s.ollama.length ? esc(s.ollama.join(", ")) : "Start Ollama and it's picked up automatically"}</span></span></div>`);
  const eps = s.endpoints || [];
  rows.push(`<div class="free-head"><h3>Custom endpoints</h3><span class="fine">Any OpenAI-compatible API — Ollama, LM Studio, vLLM, or a company proxy. Local servers need no key.</span></div>`);
  rows.push(eps.map((e) => `<div class="conn-row"><span class="dot on"></span><span class="grow"><strong>${esc(e.label)}</strong> ${e.private ? '<span class="badge private" title="Treated as private: code may use it">Private — safe for code</span>' : '<span class="badge trains" title="Treated like an outside service: chats only">Outside service</span>'}<br><span class="sub">${esc(e.base_url)} · ${esc((e.models || []).join(", "))}${e.has_key ? " · key saved" : ""}</span></span><button class="btn" data-rmep="${esc(e.id)}">Remove</button></div>`).join(""));
  rows.push(`<div class="ep-form">
    <input id="epLabel" placeholder="Name — e.g. My Ollama" autocomplete="off">
    <input id="epBase" placeholder="Base URL — http://localhost:11434/v1" autocomplete="off" spellcheck="false">
    <input id="epModels" placeholder="Models (comma-separated, blank = auto-detect)" autocomplete="off" spellcheck="false">
    <input id="epKey" type="password" autocomplete="off" placeholder="API key (only if the endpoint needs one)">
    <label class="fine"><input type="checkbox" id="epPrivate"> Private — a local or company endpoint that can't train on prompts, so code may use it</label>
    <button class="btn primary" id="epAdd">Add endpoint</button>
  </div>`);
  const fm = (s.settings || {}).free_models || "chat";
  const q = s.free_quota;
  const quota = !q ? "" : q.available ? `<br><span class="sub quota">Free requests left today: <b>${q.remaining ?? "checking…"}</b>${q.limit ? ` of ${q.limit}` : ""}</span>`
    : `<br><span class="sub quota">${q.pause_reason === "daily" ? "Today's free requests are used up (back at midnight UTC)." : "Rate-limited for a minute."} Paid models fill in.</span>`;
  rows.push(`<div class="conn-row"><span class="grow"><strong>Free models</strong><br><span class="sub">$0, rate-limited. "Private" means providers that don't train on your prompts; code (agents and coding tools) only goes to those unless you choose otherwise.</span>${quota}</span>
    <select id="freeModels">
      <option value="off" ${fm === "off" ? "selected" : ""}>Off</option>
      <option value="chat" ${fm === "chat" ? "selected" : ""}>Chats: any · Code: private ones only</option>
      <option value="all" ${fm === "all" ? "selected" : ""}>Chats and code: any free model</option>
    </select></div>`);
  if (s.families && s.families.length) rows.unshift(`<p class="fine families">${s.families.length} model families connected: ${esc(s.families.slice(0, 12).join(", "))}${s.families.length > 12 ? "…" : ""}</p>`);
  $("connBody").innerHTML = rows.join("");
  $("freeModels").onchange = async (e) => {
    try { await api("/connect/settings", { method: "POST", body: { free_models: e.target.value } }); await loadModels(); toast("Saved"); }
    catch (err) { toast(err.message); }
  };
  if (!$("connDlg").open) $("connDlg").showModal();
  if (!S.models.length) $("anyKey").focus();          // first visit: one click, one paste, done
}
function connMsg(kind, text, small) {
  const m = $("connMsg");
  if (!kind) { m.className = "conn-msg hidden"; return; }
  m.className = `conn-msg ${kind}`;
  m.innerHTML = `${esc(text)}${small ? `<small>${esc(small)}</small>` : ""}`;
}
async function connectKey(path, key, btn, workspaceId) {
  if (!key) return;
  const label = btn.textContent; btn.disabled = true; btn.textContent = "Checking…"; connMsg();
  try {
    const r = await api(path, { method: "POST", body: workspaceId ? { key, workspace_id: workspaceId } : { key } });
    S.pendingKey = null;
    connMsg("ok", `${r.label} connected. ${r.models} models ready.`);
    await loadModels(); await openConnections();
    connMsg("ok", `${r.label} connected. ${r.models} models ready.`);
  } catch (err) {
    if (err.detail && err.detail.code === "needs_workspace") askWorkspace(path, key, err.detail);
    else connMsg("bad", err.message, err.detail && err.detail.detail ? `Provider said: ${err.detail.detail}` : "");
  } finally { btn.disabled = false; btn.textContent = label; }
}
function askWorkspace(path, key, d) {
  S.pendingKey = { path, key };                 // kept in memory only, never put back on the page
  const m = $("connMsg");
  m.className = "conn-msg bad";
  const pick = (d.workspaces || []).length
    ? `<select id="wsPick">${d.workspaces.map((w) => `<option value="${esc(w.id)}">${esc(w.name)} (${esc(w.id)})</option>`).join("")}</select>`
    : `<input id="wsPick" placeholder="wrkspc_…" autocomplete="off" spellcheck="false">`;
  m.innerHTML = `${esc(d.message)}
    <div class="row" style="margin-top:8px">${pick}<button class="btn primary" id="wsGo">Connect</button></div>
    <small>Find it in the Claude Console: <a href="${esc(d.console_url || "https://platform.claude.com/settings/workspaces")}" target="_blank" rel="noopener">Settings → Workspaces</a>, ID column. Or create a new key there and choose a workspace for it.</small>`;
  $("wsGo").onclick = () => {
    const ws = $("wsPick").value.trim();
    if (!ws) return $("wsPick").focus();
    connectKey(S.pendingKey.path, S.pendingKey.key, $("wsGo"), ws);
  };
  $("wsPick").focus();
}
$("anyKeyBtn").onclick = () => { const k = $("anyKey").value.trim(); $("anyKey").value = ""; connectKey("/connect/key", k, $("anyKeyBtn")); };
$("anyKey").addEventListener("keydown", (e) => { if (e.key === "Enter") $("anyKeyBtn").click(); });
$("openConnections").onclick = () => { connMsg(); openConnections(); };
$("connClose").onclick = async () => { $("connDlg").close(); await loadModels(); if (!S.current) welcome(); };
$("connBody").addEventListener("click", async (e) => {
  if (e.target.dataset.recheck) { e.target.textContent = "Checking…"; await api("/api/subscriptions?refresh=1"); await loadModels(); return openConnections(); }
  if (e.target.id === "epAdd") {
    const body = { label: $("epLabel").value.trim(), base_url: $("epBase").value.trim(),
                   models: $("epModels").value.trim(), key: $("epKey").value.trim() || null,
                   private: $("epPrivate").checked };
    e.target.disabled = true; connMsg();
    try {
      const r = await api("/connect/endpoints", { method: "POST", body });
      connMsg("ok", `${r.endpoint.label} added — ${r.models} model(s) ready.`);
      await loadModels(); await openConnections();
      connMsg("ok", `${r.endpoint.label} added — ${r.models} model(s) ready.`);
    } catch (err) { connMsg("bad", err.message); }
    finally { e.target.disabled = false; }
    return;
  }
  const rmep = e.target.dataset.rmep;
  if (rmep) {
    try { await api(`/connect/endpoints/${rmep}`, { method: "DELETE" }); connMsg("ok", "Endpoint removed."); }
    catch (err) { connMsg("bad", err.message); }
    await loadModels(); openConnections();
    return;
  }
  const on = e.target.dataset.on, off = e.target.dataset.off;
  if (on) {
    const inp = $("connBody").querySelector(`[data-key="${on}"]`); const k = inp.value.trim(); inp.value = "";
    const acct = $("connBody").querySelector(`[data-acct="${on}"]`);
    return connectKey(`/connect/${on}`, k, e.target, acct ? acct.value.trim() : "");
  }
  if (off) {
    try { await api(`/connect/${off}`, { method: "DELETE" }); connMsg("ok", "Disconnected and removed from storage."); }
    catch (err) { connMsg("bad", err.message); }
    await loadModels(); openConnections();
  }
});

$("quitBtn").onclick = async () => {
  const running = S.sessions.filter((s) => s.status === "running" || s.status === "waiting").length;
  if (!confirm(running ? `${running} agent task(s) are running and will be stopped. Quit Promptaro?` : "Quit Promptaro? Open it again any time from your apps.")) return;
  try { await api("/api/quit", { method: "POST" }); } catch {}
  $("connDlg").close();
  document.body.innerHTML = '<div class="welcome" style="max-width:520px;margin:20vh auto;padding:0 16px"><h2>Promptaro has stopped.</h2><p>Open it again from Applications, the Start menu or your app menu. You can close this window.</p></div>';
};

/* ---------------- free providers ---------------- */
const k = (n) => (n >= 1e6 ? +(n / 1e6).toFixed(1) + "M" : n >= 1000 ? +(n / 1000).toFixed(1) + "k" : String(n));
function limitText(l) {
  const parts = [];
  if (l.rpm) parts.push(`${k(l.rpm)} req/min`);
  if (l.rpd) parts.push(`${k(l.rpd)} req/day`);
  if (l.tpm) parts.push(`${k(l.tpm)} tok/min`);
  if (l.tpd) parts.push(`${k(l.tpd)} tok/day`);
  if (l.rpmonth) parts.push(`${k(l.rpmonth)} req/month`);
  return parts.join(" · ");
}
const privacyBadge = (p) => p.trains ? '<span class="badge trains" title="Its free tier may use your prompts for training. Used for chats only unless you allow it for code.">May train on prompts</span>'
  : '<span class="badge private" title="Its free tier does not use your prompts for training">Doesn\'t train on prompts</span>';
function freeRows(d) {
  const used = Object.fromEntries((d.usage || []).map((u) => [u.id, u]));
  return `<div class="free-head" id="freeHead"><h3>Free providers</h3><span class="fine">One free key each. Promptaro tracks every limit and moves on when one runs out.</span></div>` +
    d.providers.map((p) => {
      const u = used[p.id];
      const today = u ? `<br><span class="sub">Today: ${k(u.requests_today)} requests · ${k(u.tokens_today)} tokens${u.paused ? ` · <b>${u.paused === "daily" ? "daily limit reached" : "rate-limited, back in a minute"}</b>` : ""}</span>` : "";
      const right = p.connected ? `<span class="sub">Connected</span><button class="btn" data-off="${p.id}">Disconnect</button>`
        : `<span class="actions"><a class="btn" href="${esc(p.get_key_url)}" target="_blank" rel="noopener">Get free key</a><input type="password" autocomplete="off" placeholder="Paste key" data-key="${p.id}"><button class="btn" data-on="${p.id}">Connect</button></span>`;
      return `<div class="conn-row"><span class="dot ${p.connected ? "on" : ""}"></span><span class="grow"><strong>${esc(p.label)}</strong> ${privacyBadge(p)}<br><span class="limits" title="Free-tier limits, checked ${esc(p.checked)}">${esc(limitText(p.limits))}</span><br><span class="sub">${esc(p.note)}</span>${today}</span>${right}</div>`;
    }).join("");
}

/* ---------------- coding tools ---------------- */
async function openTools() {
  let g; try { g = await api("/api/gateway"); } catch (e) { return toast(e.message); }
  const win = g.os === "Windows";
  const copyBtn = (text) => `<button class="btn" data-copytext="${esc(text)}">Copy</button>`;
  const cmd = (text) => `<div class="cmd"><code>${esc(text)}</code>${copyBtn(text)}</div>`;
  const L = g.launchers;
  $("toolsBody").innerHTML = `
    <div class="tool-card">
      <h3>1. Add the launchers <span class="fine">(once)</span></h3>
      ${L.installed ? `<span class="ok">✓ Installed in ${esc(L.dir)}</span>` : `<div><button class="btn primary" id="installLaunchers">Add promptaro-claude, promptaro-aider and promptaro-env</button></div>`}
      ${L.installed && !L.on_path ? `<div class="warn-note">${esc(L.dir)} isn't on your PATH yet. ${win ? "Add it in System Properties → Environment Variables." : `Run <code>echo 'export PATH="$HOME/.local/bin:$PATH"' &gt;&gt; ~/.zshrc</code> and open a new Terminal.`}</div>` : ""}
    </div>
    <div class="tool-card">
      <h3>2. Start your tool through Promptaro</h3>
      <div class="kv"><span>Claude Code</span>${cmd("promptaro-claude")}<span>Aider</span>${cmd("promptaro-aider")}${win ? "" : `<span>Any other tool</span>${cmd('eval "$(promptaro-env)"')}`}</div>
      ${g.claude_installed ? "" : '<p class="fine">Claude Code isn\'t installed yet: <a href="https://docs.claude.com/en/docs/claude-code/setup" target="_blank" rel="noopener">install it</a>. When started with promptaro-claude it uses Promptaro\'s models, not your Claude subscription.</p>'}
    </div>
    <div class="tool-card">
      <h3>Or set it up by hand</h3>
      <div class="kv">
        <span>Anthropic-style tools</span>${cmd(`ANTHROPIC_BASE_URL=${g.base_url}`)}
        <span>OpenAI-style tools</span>${cmd(`${g.openai_base_url}`)}
        <span>Model</span>${cmd(g.model)}
        <span>Key / token</span><div class="cmd"><code id="tokVal">••••••••••••••••••••</code><button class="btn" id="tokShow">Show</button><button class="btn" id="tokCopy">Copy</button></div>
      </div>
      <p class="fine">Cline, Continue, OpenCode, Zed and similar: choose "OpenAI compatible", paste the base URL, token and model. Codex CLI isn't supported yet (it needs the newer Responses API). <button class="btn ghost" id="tokRotate" style="padding:2px 6px;font-size:12px">Make a new token</button></p>
    </div>
    <div class="tool-card">
      <h3>Spending</h3>
      <label class="row"><select id="gwSpend">
        <option value="free" ${g.spend === "free" ? "selected" : ""}>Free models only — never spend money</option>
        <option value="paid_ok" ${g.spend === "paid_ok" ? "selected" : ""}>Free first, paid models when free ones can't do it</option>
      </select></label>
      <p class="fine">${g.free_models === "all" ? "Free providers that may train on prompts are allowed for code (change in Connections → Free models)." : "Code only goes to free providers that don't train on your prompts. Change in Connections → Free models."}</p>
    </div>
    <div class="tool-card">
      <h3>Recent requests</h3>
      ${g.recent.length ? `<table class="opts-table"><thead><tr><th>Model</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>${
        g.recent.map((r) => `<tr><td>${esc(r.name)} ${r.free ? '<span class="tag free">Free</span>' : ""}</td><td>${fmtTokens(r.tokens_in + r.tokens_out)}</td><td>${r.free ? "$0" : money(r.cost)}</td></tr>`).join("")}</tbody></table>`
        : '<p class="note">None yet. Start a tool with promptaro-claude and its requests show up here.</p>'}
    </div>`;
  const inst = $("installLaunchers");
  if (inst) inst.onclick = async () => { inst.disabled = true; try { await api("/api/gateway/launchers", { method: "POST" }); openTools(); } catch (e) { toast(e.message); inst.disabled = false; } };
  let tok = null;
  const getTok = async () => tok || (tok = (await api("/api/gateway/token", { method: "POST" })).token);
  $("tokShow").onclick = async () => { $("tokVal").textContent = await getTok(); };
  $("tokCopy").onclick = async () => { navigator.clipboard.writeText(await getTok()); toast("Token copied"); };
  $("tokRotate").onclick = async () => { if (!confirm("Make a new token? Tools set up by hand will need the new one; promptaro-claude and friends pick it up automatically.")) return; await api("/api/gateway/rotate", { method: "POST" }); tok = null; toast("New token made"); openTools(); };
  $("gwSpend").onchange = async (e) => { try { await api("/connect/settings", { method: "POST", body: { gateway_spend: e.target.value } }); toast("Saved"); } catch (err) { toast(err.message); } };
  if (!$("toolsDlg").open) $("toolsDlg").showModal();
}
$("toolsBtn").onclick = () => { openTools(); $("side").classList.remove("open"); };
$("toolsClose").onclick = () => $("toolsDlg").close();
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copytext]");
  if (b) { navigator.clipboard.writeText(b.dataset.copytext); b.textContent = "Copied"; setTimeout(() => (b.textContent = "Copy"), 1200); }
});

/* ---------------- answer details toggle ---------------- */
/* Shows/hides the "model · Paid/Free · tokens · cost" line under each answer.
   On by default; the choice persists across launches. */
function applyDetailsPref() {
  const on = localStorage.getItem("promptaro:modelInfo") !== "off";
  $("app").classList.toggle("no-route-info", !on);
  $("detailsDot").classList.toggle("on", on);
  $("detailsBtn").classList.toggle("off", !on);
}
$("detailsBtn").onclick = () => {
  localStorage.setItem("promptaro:modelInfo", localStorage.getItem("promptaro:modelInfo") === "off" ? "on" : "off");
  applyDetailsPref();
};

/* ---------------- updates ---------------- */
async function checkUpdate() {
  try {
    const h = await (await fetch("/healthz", { cache: "no-store" })).json();
    S.build = h.build;
    $("updateBar").classList.toggle("hidden", !h.update_ready);
  } catch {}
}
$("restartBtn").onclick = async () => {
  const running = S.sessions.filter((s) => s.status === "running" || s.status === "waiting").length;
  if (running && !confirm(`${running} agent task(s) are running and will be stopped. Restart anyway?`)) return;
  $("restartBtn").disabled = true; $("restartBtn").textContent = "Restarting…";
  try { await api("/api/restart", { method: "POST" }); } catch {}
  const old = S.build;
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 500));
    try { const h = await (await fetch("/healthz", { cache: "no-store" })).json(); if (h.build !== old) return location.reload(); } catch {}
  }
  $("restartBtn").disabled = false; $("restartBtn").textContent = "Restart now";
  toast("Couldn't restart automatically. Close the Terminal window and open Promptaro again.", 8000);
};
setInterval(checkUpdate, 30000);

/* ---------------- studio (generate + file tools) ---------------- */
const CONV_FMTS = {
  "convert": [],
  "make-pdf": [],
  "merge-pdf": [],
};
function fillFormats() {
  const a = $("convAction").value, c = S.toolCaps?.convert || {};
  const groups = a === "convert"
    ? [["Audio", c.audio_formats], ["Video", c.video_formats], ["Image", c.image_formats], ["Document", c.doc_formats]]
    : [];
  $("convFmt").innerHTML = groups.map(([g, fs]) => fs && fs.length ? `<optgroup label="${g}">${fs.map((f) => `<option>${f}</option>`).join("")}</optgroup>` : "").join("");
  $("convFmt").disabled = a !== "convert";
  $("convName").style.display = a === "convert" ? "none" : "";
}
async function openStudio() {
  $("convErr").textContent = "";
  try {
    S.toolCaps = await api("/api/tools/capabilities");
    const c = S.toolCaps.convert, media = S.toolCaps.media || [];
    const usable = media.filter((m) => m.connected && m.enabled !== false);
    const gated = media.filter((m) => m.connected && m.enabled === false).map((m) => m.label);
    const video = usable.some((m) => m.kinds.includes("video"));
    $("genPaid").checked = !!S.toolCaps.paid_media;
    $("studioCaps").textContent =
      `Images: ${usable.length ? usable.map((m) => m.label).join(", ") : "no keys yet — Pollinations works without one"}` +
      `${gated.length ? ` (off until allowed: ${gated.join(", ")})` : ""}. ` +
      `Video: ${video ? "Hugging Face (free)" : "needs a free Hugging Face token (Connections → Image & video keys)"}. ` +
      `Convert: ${c.ffmpeg ? "audio+video ✓" : "audio/video needs ffmpeg"}${c.pandoc ? ", pandoc ✓" : ""}.`;
  } catch { $("studioCaps").textContent = ""; }
  fillFormats();
  if (!$("studioDlg").open) $("studioDlg").showModal();
}
$("studioBtn").onclick = openStudio;
$("studioClose").onclick = () => $("studioDlg").close();
$("convAction").onchange = fillFormats;
$("pickFiles").onclick = async () => {
  try {
    const r = await api("/api/tools/pick-files", { method: "POST" });
    if (r.paths && r.paths.length) $("convPaths").value = [$("convPaths").value, ...r.paths].filter(Boolean).join("\n");
  } catch (e) { toast(e.message); }
};
function convInputPaths() {
  return $("convPaths").value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
}
$("genGo").onclick = async () => {
  const prompt = $("genPrompt").value.trim();
  if (!prompt) return $("genPrompt").focus();
  const b = $("genGo"); b.disabled = true; b.textContent = "Generating…";
  $("genStatus").textContent = "trying the free providers — the first one that answers wins";
  $("genResult").innerHTML = "";
  try {
    const r = await api("/api/tools/generate", { method: "POST", body: { kind: $("genKind").value, prompt, size: $("genSize").value } });
    const tag = r.kind === "video" || r.file.endsWith(".mp4") || r.file.endsWith(".webm")
      ? `<video src="${r.url}" controls autoplay muted loop style="max-width:100%;border-radius:8px"></video>`
      : `<img src="${r.url}" alt="generated" style="max-width:100%;border-radius:8px">`;
    $("genResult").innerHTML = `${tag}<p class="fine">made by ${esc(r.provider)} · <a href="${r.url}" download>download</a> · <a href="#" data-reveal="${esc(r.file)}">show in folder</a></p>`;
    $("genStatus").textContent = "";
  } catch (e) { $("genStatus").textContent = ""; $("genResult").innerHTML = `<p class="form-error">${esc(e.message)}</p>`; }
  finally { b.disabled = false; b.textContent = "Generate"; }
};
$("genPaid").onchange = async (e) => {
  try { await api("/connect/settings", { method: "POST", body: { paid_media: e.target.checked } }); }
  catch (x) { e.target.checked = !e.target.checked; toast(x.message); }
};
$("convGo").onclick = async () => {
  const inputs = convInputPaths();
  if (!inputs.length) { $("convErr").textContent = "Add at least one file path."; return; }
  const b = $("convGo"); b.disabled = true; $("convErr").textContent = ""; $("convResult").innerHTML = "";
  try {
    const r = await api("/api/tools/convert", { method: "POST", body: { action: $("convAction").value, inputs, format: $("convFmt").value, name: $("convName").value.trim() } });
    $("convResult").innerHTML = r.outputs.map((o) =>
      `<p class="fine">✓ <b>${esc(o.name)}</b> (${(o.bytes / 1024).toFixed(0)} KB) — <a href="${o.url}" download>download</a> · <a href="#" data-reveal="${esc(o.name)}">show in folder</a></p>`).join("");
  } catch (e) { $("convErr").textContent = e.message; }
  finally { b.disabled = false; }
};
$("studioDlg").addEventListener("click", async (e) => {
  const rv = e.target.closest("[data-reveal]");
  if (rv) { e.preventDefault(); try { await api("/api/tools/reveal", { method: "POST", body: { path: rv.dataset.reveal } }); } catch (x) { toast(x.message); } }
});

/* ---------------- boot ---------------- */
(async function boot() {
  const q = new URLSearchParams(location.search);
  if (q.get("connected")) toast("Signed in. Models are ready.");
  if (q.get("error")) toast(q.get("error"), 6000);
  if (q.toString()) history.replaceState(null, "", "/");
  applyDetailsPref();
  // Terms of Use gate — the app stays inert until accepted (server enforces too).
  async function showTerms(review) {
    const lg = await api("/api/legal");
    $("legalTerms").textContent = lg.terms;
    $("legalAccept").classList.toggle("hidden", review);
    $("legalClose").classList.toggle("hidden", !review);
    $("legalDlg").showModal();
    if (review) return;
    await new Promise((resolve) => {
      $("legalAccept").onclick = async () => {
        $("legalAccept").disabled = true;
        try {
          await api("/api/legal/accept", { method: "POST", body: { version: lg.version } });
          $("legalDlg").close(); resolve();
        } catch (x) { $("legalAccept").disabled = false; toast(x.message); }
      };
    });
  }
  $("termsBtn").onclick = () => showTerms(true);
  $("legalClose").onclick = () => $("legalDlg").close();
  if (!(await api("/api/legal")).accepted) await showTerms(false);
  await loadModels(); await loadSessions(); welcome(); connectStream(); checkUpdate(); loadSavings();
})();
