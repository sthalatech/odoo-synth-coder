"use strict";

const $ = (id) => document.getElementById(id);
let evtSource = null;
let PROFILES = null;
let CONFIG = null;

/* ===== Router ===== */
const ROUTES = ["overview", "new", "runs"];
function currentRoute() {
  const h = (location.hash || "").replace(/^#\/?/, "").split("/")[0];
  return ROUTES.includes(h) ? h : "overview";
}
function navigate() {
  const route = currentRoute();
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
  const page = $("page-" + route);
  if (page) page.classList.add("active");
  document.querySelectorAll(".nav-item").forEach((a) =>
    a.classList.toggle("active", a.dataset.route === route));
  if (route === "overview") renderOverview();
  if (route === "runs") loadRuns();
}
window.addEventListener("hashchange", navigate);

/* ===== Panes: 'new' (live) and 'detail' (runs page) ===== */
const PANE = {
  new: { log: "log", badge: "status-badge", result: "result" },
  detail: { log: "detail-log", badge: "detail-badge", result: "detail-result" },
};

async function loadConfig() {
  try {
    CONFIG = await (await fetch("/api/config")).json();
    const c = CONFIG;
    const parts = [];
    if (c.region) parts.push(`region <b>${c.region}</b>`);
    if (c.destination_db) parts.push(`destination <b>${c.destination_db}</b>`);
    $("infobar").innerHTML = parts.join("<br>");
  } catch (e) {
    $("infobar").textContent = "config unavailable";
  }
}

function card(k, v) {
  return `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`;
}
async function renderOverview() {
  const c = CONFIG || {};
  let running = 0, total = 0, lastOk = "—", lastMaskedUi = "—";
  try {
    const { runs } = await (await fetch("/api/runs")).json();
    total = runs.length;
    running = runs.filter((r) => r.status === "running" || r.status === "queued").length;
    const ok = runs.find((r) => r.status === "succeeded");
    if (ok && ok.started_at) lastOk = new Date(ok.started_at * 1000).toLocaleString();
    const okUrl = ok && ok.result && ok.result.target_url;
    if (okUrl) lastMaskedUi = `<a href="${okUrl}" target="_blank">open ↗</a>`;
    const tb = document.querySelector("#overview-runs tbody");
    tb.innerHTML = "";
    for (const r of runs.slice(0, 6)) {
      const started = r.started_at ? new Date(r.started_at * 1000).toLocaleString() : "—";
      const url = r.result && r.result.target_url;
      const tr = document.createElement("tr");
      tr.className = "clickable";
      tr.innerHTML = `<td class="mono">${r.id}</td><td class="st-${r.status}">${r.status}</td>` +
        `<td>${r.exit_code ?? "—"}</td><td>${started}</td>` +
        `<td>${url ? `<a href="${url}" target="_blank" onclick="event.stopPropagation()">open ↗</a>` : "—"}</td>`;
      tr.onclick = () => { location.hash = "#/runs"; setTimeout(() => openRun(r.id), 0); };
      tb.appendChild(tr);
    }
  } catch (e) {}
  $("overview-cards").innerHTML =
    card("Region", c.region || "—") +
    card("Destination db", c.destination_db || "—") +
    card("Total runs", total) +
    card("Running", running) +
    card("Last success", lastOk) +
    card("Latest masked UI", lastMaskedUi);
}


function opt(value, label) {
  const o = document.createElement("option");
  o.value = value; o.textContent = label;
  return o;
}

async function loadProfiles() {
  PROFILES = await (await fetch("/api/profiles")).json();

  const mp = $("mask_profile");
  mp.innerHTML = "";
  (PROFILES.mask_profiles || []).forEach((p) => mp.appendChild(opt(p.id, p.label)));
  mp.addEventListener("change", renderMaskProfileHint);
  renderMaskProfileHint();

  const nd = PROFILES.neutralize_defaults || {};
  $("neutralize_mail").checked = nd.mail !== false;
  $("neutralize_fetchmail").checked = nd.fetchmail !== false;
  $("neutralize_payment").checked = nd.payment !== false;
  $("neutralize_smtp_param").checked = nd.smtp_param !== false;
  $("reset_admin_login").checked = PROFILES.reset_admin_login !== false;
  $("gm_jobs").value = PROFILES.gm_jobs || 4;

  // dump download availability
  if (!PROFILES.dump_download_enabled) {
    $("produce_dump").checked = false;
    $("produce_dump").disabled = true;
    $("dump-note").classList.remove("hidden");
  }

  const dbl = PROFILES.destination_label || "managed";
  const ddb = PROFILES.destination_db || "";
  $("dest-note").innerHTML =
    `Destination is created automatically: <b>${dbl}</b>${ddb ? ` (db <b>${ddb}</b>)` : ""}. It is dropped &amp; recreated on each run.`;
}

function renderMaskProfileHint() {
  const p = (PROFILES.mask_profiles || []).find((x) => x.id === $("mask_profile").value);
  $("mask_profile_hint").textContent = p ? (p.description || "") : "";
}

function setBadge(status, pane = "new") {
  const b = $(PANE[pane].badge);
  b.className = "badge " + status;
  b.textContent = status;
}

function appendLog(text, pane = "new") {
  const el = $(PANE[pane].log);
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.textContent += text + "\n";
  if (atBottom) el.scrollTop = el.scrollHeight;
}

function showResult(run, pane = "new") {
  const r = run.result || {};
  const el = $(PANE[pane].result);
  const lines = [];
  if (r.target_url) lines.push(`Masked UI: <a href="${r.target_url}" target="_blank">${r.target_url}</a>`);
  if (r.masked_dump_url) lines.push(`Masked dump: <a href="${r.masked_dump_url}" target="_blank">download pg_dump</a>`);
  if (r.error) lines.push(`<span class="st-failed">Error: ${r.error}</span>`);
  if (typeof r.exit_code === "number") lines.push(`Exit code: <b>${r.exit_code}</b>`);
  if (lines.length) { el.innerHTML = lines.join("<br>"); el.classList.remove("hidden"); }
  else el.classList.add("hidden");
}

function streamLogs(runId, pane = "new") {
  if (evtSource) evtSource.close();
  evtSource = new EventSource(`/api/runs/${runId}/logs`);
  evtSource.onmessage = (e) => appendLog(e.data, pane);
  evtSource.addEventListener("end", async (e) => {
    setBadge(e.data, pane);
    evtSource.close(); evtSource = null;
    const run = await (await fetch(`/api/runs/${runId}`)).json();
    showResult(run, pane);
    if (pane === "new") $("start-btn").disabled = false;
    loadRuns();
  });
  evtSource.onerror = () => {};
}

async function loadRuns() {
  try {
    const { runs } = await (await fetch("/api/runs")).json();
    const tb = document.querySelector("#runs tbody");
    tb.innerHTML = "";
    for (const r of runs) {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      const started = r.started_at ? new Date(r.started_at * 1000).toLocaleString() : "—";
      const url = r.result && r.result.target_url;
      tr.innerHTML =
        `<td class="mono">${r.id}</td><td>${r.operation}</td>` +
        `<td class="st-${r.status}">${r.status}</td>` +
        `<td>${r.exit_code ?? "—"}</td><td>${started}</td>` +
        `<td>${url ? `<a href="${url}" target="_blank" onclick="event.stopPropagation()">open ↗</a>` : "—"}</td>`;
      tr.onclick = () => openRun(r.id);
      tb.appendChild(tr);
    }
  } catch (e) {}
}

async function openRun(runId) {
  $("detail-title").textContent = `Run ${runId}`;
  $("detail-log").textContent = "";
  $("detail-result").classList.add("hidden");
  const run = await (await fetch(`/api/runs/${runId}`)).json();
  setBadge(run.status, "detail");
  if (run.status === "running" || run.status === "queued") {
    streamLogs(runId, "detail");
  } else {
    const res = await fetch(`/api/runs/${runId}/logs`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
        for (const ln of frame.split("\n")) {
          if (ln.startsWith("data: ")) appendLog(ln.slice(6), "detail");
          if (ln.startsWith("event: end")) { reader.cancel(); break; }
        }
      }
    }
    showResult(run, "detail");
  }
}

function buildPayload() {
  return {
    operation: "mask",
    source_dsn: $("source_dsn").value.trim(),
    ssh_enabled: $("ssh_enabled").checked,
    ssh_bastion: $("ssh_bastion").value.trim() || null,
    ssh_key: $("ssh_key").value || null,
    mask_profile: $("mask_profile").value,
    admin_password: $("admin_password").value || null,
    gm_jobs: parseInt($("gm_jobs").value, 10) || null,
    neutralize_mail: $("neutralize_mail").checked,
    neutralize_fetchmail: $("neutralize_fetchmail").checked,
    neutralize_payment: $("neutralize_payment").checked,
    neutralize_smtp_param: $("neutralize_smtp_param").checked,
    reset_admin_login: $("reset_admin_login").checked,
    produce_dump: $("produce_dump").checked,
  };
}

// SSH toggle shows/hides bastion fields
$("ssh_enabled").addEventListener("change", () => {
  $("ssh-fields").classList.toggle("hidden", !$("ssh_enabled").checked);
});

$("run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = buildPayload();
  if (!body.source_dsn) {
    appendLog("[panel] enter a source database URL (postgresql://…)");
    return;
  }
  if (body.ssh_enabled && (!body.ssh_bastion || !body.ssh_key)) {
    appendLog("[panel] SSH tunnel on: provide the bastion (user@host[:port]) and the private key");
    return;
  }
  $("start-btn").disabled = true;
  $("log").textContent = "";
  $("result").classList.add("hidden");
  setBadge("running");

  const resp = await fetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    appendLog(`[panel] failed to start: ${err.detail || resp.status}`);
    setBadge("failed");
    $("start-btn").disabled = false;
    return;
  }
  const { run_id } = await resp.json();
  appendLog(`[panel] run ${run_id} started`);
  streamLogs(run_id);
  loadRuns();
});

$("runs-refresh").addEventListener("click", loadRuns);

loadConfig().then(navigate);
loadProfiles();
loadRuns();
setInterval(() => {
  loadRuns();
  if (currentRoute() === "overview") renderOverview();
}, 10000);
