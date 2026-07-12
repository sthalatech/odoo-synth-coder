"use strict";

const $ = (id) => document.getElementById(id);
let evtSource = null;
let PROFILES = null;
let stagedUrl = null; // presigned URL from an upload

async function loadConfig() {
  try {
    const c = await (await fetch("/api/config")).json();
    const parts = [];
    if (c.region) parts.push(`region <b>${c.region}</b>`);
    if (c.source_db) parts.push(`source db <b>${c.source_db}</b>`);
    if (c.target_db) parts.push(`target db <b>${c.target_db}</b>`);
    if (c.source_url) parts.push(`<a href="${c.source_url}" target="_blank">source&nbsp;UI</a>`);
    if (c.target_url) parts.push(`<a href="${c.target_url}" target="_blank">target&nbsp;UI</a>`);
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

  // connection selects
  const conns = PROFILES.connections || [];
  for (const sel of document.querySelectorAll(".conn-select")) {
    sel.innerHTML = "";
    conns.forEach((c) => sel.appendChild(opt(c.id, c.label)));
  }
  // sensible defaults
  if (conns.find((c) => c.id === "source")) $("mask_source_conn").value = "source";
  if (conns.find((c) => c.id === "masked")) $("mask_target_conn").value = "masked";
  if (conns.find((c) => c.id === "source")) $("restore_target_conn").value = "source";

  // restore source types
  const st = $("source_type");
  st.innerHTML = "";
  (PROFILES.restore_source_types || []).forEach((s) => {
    const isUpload = (s.needs || []).includes("file");
    if (isUpload && !PROFILES.upload_enabled) return; // hide uploads if no bucket
    st.appendChild(opt(s.id, s.label));
  });
  st.addEventListener("change", renderSourceFields);
  renderSourceFields();

  // mask profiles
  const mp = $("mask_profile");
  mp.innerHTML = "";
  (PROFILES.mask_profiles || []).forEach((p) => mp.appendChild(opt(p.id, p.label)));
  mp.addEventListener("change", renderMaskProfileHint);
  renderMaskProfileHint();

  // toggle defaults
  const nd = PROFILES.neutralize_defaults || {};
  $("neutralize_mail").checked = nd.mail !== false;
  $("neutralize_fetchmail").checked = nd.fetchmail !== false;
  $("neutralize_payment").checked = nd.payment !== false;
  $("neutralize_smtp_param").checked = nd.smtp_param !== false;
  $("reset_admin_login").checked = PROFILES.reset_admin_login !== false;
  $("gm_jobs").value = PROFILES.gm_jobs || 4;
}

function currentSourceType() {
  return (PROFILES.restore_source_types || []).find((s) => s.id === $("source_type").value);
}

function renderSourceFields() {
  const s = currentSourceType();
  $("source_hint").textContent = s ? (s.hint || "") : "";
  const needs = s ? (s.needs || []) : [];
  $("field-url").classList.toggle("hidden", !needs.includes("url"));
  $("field-file").classList.toggle("hidden", !needs.includes("file"));
  $("field-dsn").classList.toggle("hidden", !needs.includes("dsn"));
  stagedUrl = null;
  $("upload-status").textContent = "";
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
  if (r.source_url) lines.push(`Source: <a href="${r.source_url}" target="_blank">${r.source_url}</a>`);
  if (r.target_url) lines.push(`Target: <a href="${r.target_url}" target="_blank">${r.target_url}</a>`);
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

// ---- operation switch ----
$("operation").addEventListener("change", () => {
  const op = $("operation").value;
  $("restore-fields").classList.toggle("hidden", op !== "restore");
  $("mask-fields").classList.toggle("hidden", op !== "mask");
});

// ---- upload ----
$("upload-btn").addEventListener("click", async () => {
  const f = $("file").files[0];
  if (!f) { $("upload-status").textContent = "pick a file first"; return; }
  $("upload-status").textContent = `uploading ${f.name} ...`;
  const fd = new FormData();
  fd.append("file", f);
  try {
    const resp = await fetch("/api/upload", { method: "POST", body: fd });
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.status);
    const j = await resp.json();
    stagedUrl = j.url;
    $("upload-status").textContent = `staged ✓ (${f.name})`;
  } catch (e) {
    $("upload-status").textContent = `upload failed: ${e.message}`;
  }
});

// ---- submit ----
function buildPayload() {
  const op = $("operation").value;
  if (op === "restore") {
    const s = currentSourceType();
    const needs = s ? (s.needs || []) : [];
    const body = {
      operation: "restore",
      source_type: $("source_type").value,
      target_conn: $("restore_target_conn").value,
      target_db: $("restore_target_db").value || null,
    };
    if (needs.includes("url")) body.url = $("url").value || null;
    if (needs.includes("file")) body.url = stagedUrl;
    if (needs.includes("dsn")) body.dsn = $("dsn").value || null;
    return body;
  }
  return {
    operation: "mask",
    source_conn: $("mask_source_conn").value,
    target_conn: $("mask_target_conn").value,
    target_db: $("mask_target_db").value || null,
    mask_profile: $("mask_profile").value,
    admin_password: $("admin_password").value || null,
    gm_jobs: parseInt($("gm_jobs").value, 10) || null,
    neutralize_mail: $("neutralize_mail").checked,
    neutralize_fetchmail: $("neutralize_fetchmail").checked,
    neutralize_payment: $("neutralize_payment").checked,
    neutralize_smtp_param: $("neutralize_smtp_param").checked,
    reset_admin_login: $("reset_admin_login").checked,
  };
}

$("run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = buildPayload();
  if (body.operation === "restore") {
    const s = currentSourceType();
    const needs = s ? (s.needs || []) : [];
    if (needs.includes("file") && !stagedUrl) {
      appendLog("[panel] upload & stage the file first");
      return;
    }
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

// init
$("operation").dispatchEvent(new Event("change"));
loadConfig();
loadProfiles();
loadRuns();
setInterval(loadRuns, 10000);
