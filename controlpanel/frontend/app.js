"use strict";

const $ = (id) => document.getElementById(id);
let evtSource = null;
let PROFILES = null;

async function loadConfig() {
  try {
    const c = await (await fetch("/api/config")).json();
    const parts = [];
    if (c.region) parts.push(`region <b>${c.region}</b>`);
    if (c.destination_db) parts.push(`destination db <b>${c.destination_db}</b>`);
    if (c.target_url) parts.push(`<a href="${c.target_url}" target="_blank">masked&nbsp;UI</a>`);
    $("infobar").innerHTML = parts.join(" &nbsp;·&nbsp; ");
  } catch (e) {
    $("infobar").textContent = "config unavailable";
  }
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

function setBadge(status) {
  const b = $("status-badge");
  b.className = "badge " + status;
  b.textContent = status;
}

function appendLog(text) {
  const el = $("log");
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.textContent += text + "\n";
  if (atBottom) el.scrollTop = el.scrollHeight;
}

function showResult(run) {
  const r = run.result || {};
  const el = $("result");
  const lines = [];
  if (r.target_url) lines.push(`Masked UI: <a href="${r.target_url}" target="_blank">${r.target_url}</a>`);
  if (r.masked_dump_url) lines.push(`Masked dump: <a href="${r.masked_dump_url}" target="_blank">download pg_dump</a>`);
  if (r.error) lines.push(`<span class="st-failed">Error: ${r.error}</span>`);
  if (typeof r.exit_code === "number") lines.push(`Exit code: <b>${r.exit_code}</b>`);
  if (lines.length) { el.innerHTML = lines.join("<br>"); el.classList.remove("hidden"); }
}

function streamLogs(runId) {
  if (evtSource) evtSource.close();
  evtSource = new EventSource(`/api/runs/${runId}/logs`);
  evtSource.onmessage = (e) => appendLog(e.data);
  evtSource.addEventListener("end", async (e) => {
    setBadge(e.data);
    evtSource.close(); evtSource = null;
    const run = await (await fetch(`/api/runs/${runId}`)).json();
    showResult(run);
    $("start-btn").disabled = false;
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
      tr.innerHTML =
        `<td class="mono">${r.id}</td><td>${r.operation}</td>` +
        `<td class="st-${r.status}">${r.status}</td>` +
        `<td>${r.exit_code ?? "—"}</td><td>${started}</td>`;
      tr.onclick = () => openRun(r.id);
      tb.appendChild(tr);
    }
  } catch (e) {}
}

async function openRun(runId) {
  $("log").textContent = "";
  $("result").classList.add("hidden");
  const run = await (await fetch(`/api/runs/${runId}`)).json();
  setBadge(run.status);
  if (run.status === "running" || run.status === "queued") {
    streamLogs(runId);
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
          if (ln.startsWith("data: ")) appendLog(ln.slice(6));
          if (ln.startsWith("event: end")) { reader.cancel(); break; }
        }
      }
    }
    showResult(run);
  }
}

function buildPayload() {
  return {
    operation: "mask",
    source_dsn: $("source_dsn").value.trim(),
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

$("run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = buildPayload();
  if (!body.source_dsn) {
    appendLog("[panel] enter a source database URL (postgresql://…)");
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

loadConfig();
loadProfiles();
loadRuns();
setInterval(loadRuns, 10000);
