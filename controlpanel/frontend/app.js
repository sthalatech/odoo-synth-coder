"use strict";

const $ = (id) => document.getElementById(id);
let evtSource = null;

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
  if (lines.length) {
    el.innerHTML = lines.join("<br>");
    el.classList.remove("hidden");
  }
}

function streamLogs(runId) {
  if (evtSource) evtSource.close();
  evtSource = new EventSource(`/api/runs/${runId}/logs`);
  evtSource.onmessage = (e) => appendLog(e.data);
  evtSource.addEventListener("end", async (e) => {
    setBadge(e.data);
    evtSource.close();
    evtSource = null;
    const run = await (await fetch(`/api/runs/${runId}`)).json();
    showResult(run);
    $("start-btn").disabled = false;
    loadRuns();
  });
  evtSource.onerror = () => { /* keep the last state; browser auto-retries */ };
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
  } catch (e) { /* ignore */ }
}

async function openRun(runId) {
  $("log").textContent = "";
  $("result").classList.add("hidden");
  const run = await (await fetch(`/api/runs/${runId}`)).json();
  setBadge(run.status);
  if (run.status === "running" || run.status === "queued") {
    streamLogs(runId);
  } else {
    // replay stored logs once
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

$("operation").addEventListener("change", (e) => {
  $("dump-field").classList.toggle("hidden", e.target.value !== "restore");
});

$("run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("start-btn").disabled = true;
  $("log").textContent = "";
  $("result").classList.add("hidden");
  setBadge("running");
  const body = {
    operation: $("operation").value,
    dump_url: $("dump_url").value || null,
    admin_password: $("admin_password").value || null,
  };
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
loadRuns();
setInterval(loadRuns, 10000);
