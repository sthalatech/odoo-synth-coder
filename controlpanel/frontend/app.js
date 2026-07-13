"use strict";

const $ = (id) => document.getElementById(id);
let evtSource = null;
let PROFILES = null;
let CONFIG = null;
let overviewTable = null;
let runsTable = null;
let envTable = null;
let ENV_CONFIG = null;

/* ===== Router ===== */
const ROUTES = ["overview", "new", "profiles", "runs", "environments"];
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
  if (route === "profiles") loadProfilesList();
  if (route === "runs") loadRuns();
  if (route === "environments") loadEnvironments();
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
function linkCell(url, label) {
  return url
    ? `<a href="${url}" target="_blank" onclick="event.stopPropagation()">${label} ↗</a>`
    : "—";
}

function runColumns(withOp) {
  const cols = [
    { title: "id", field: "id", width: 120, formatter: (c) => `<span class="mono">${c.getValue()}</span>` },
  ];
  if (withOp) cols.push({ title: "op", field: "operation", width: 90 });
  cols.push(
    { title: "status", field: "status", width: 110, formatter: (c) => `<span class="st-${c.getValue()}">${c.getValue()}</span>` },
    { title: "exit", field: "exit_code", width: 70, formatter: (c) => (c.getValue() ?? "—") },
    { title: "started", field: "started_at", widthGrow: 2,
      formatter: (c) => (c.getValue() ? new Date(c.getValue() * 1000).toLocaleString() : "—") },
    { title: "masked UI", field: "target_url", hozAlign: "center", width: 110,
      formatter: (c) => linkCell(c.getValue(), "open") },
    { title: "dump", field: "masked_dump_url", hozAlign: "center", width: 100,
      formatter: (c) => linkCell(c.getValue(), "download") },
    { title: "vscode", field: "vscode_url", hozAlign: "center", width: 120,
      formatter: (c) => {
        const d = c.getRow().getData();
        if (d.vscode_url) return linkCell(d.vscode_url, "open");
        if (d.env_status) return `<span class="muted">${d.env_status}…</span>`;
        if (d.masked_dump_url) {
          return `<a href="#" class="mk-env" data-run="${d.id}" onclick="event.stopPropagation()">create env</a>`;
        }
        return "—";
      } },
  );
  return cols;
}

function runRow(r) {
  const res = r.result || {};
  return {
    id: r.id, operation: r.operation, status: r.status, exit_code: r.exit_code,
    started_at: r.started_at, target_url: res.target_url,
    masked_dump_url: res.masked_dump_url, vscode_url: res.vscode_url,
    env_status: r.environment && r.environment.status !== "running"
      ? r.environment.status : null,
  };
}

function makeRunsTable(el, withOp, pageSize) {
  return new Tabulator(el, {
    layout: "fitColumns",
    height: "auto",
    pagination: true,
    paginationSize: pageSize,
    paginationCounter: "rows",
    placeholder: "No runs yet",
    columns: runColumns(withOp),
    rowFormatter: (row) => { row.getElement().style.cursor = "pointer"; },
  });
}

async function renderOverview() {
  const c = CONFIG || {};
  let running = 0, total = 0, lastOk = "—";
  try {
    const { runs } = await (await fetch("/api/runs")).json();
    total = runs.length;
    running = runs.filter((r) => r.status === "running" || r.status === "queued").length;
    const ok = runs.find((r) => r.status === "succeeded");
    if (ok && ok.started_at) lastOk = new Date(ok.started_at * 1000).toLocaleString();

    const rows = runs.map(runRow);
    if (!overviewTable) {
      overviewTable = makeRunsTable("#overview-runs", false, 5);
      overviewTable.on("rowClick", (e, row) => {
        location.hash = "#/runs";
        setTimeout(() => openRun(row.getData().id), 0);
      });
      overviewTable.on("tableBuilt", () => overviewTable.setData(rows));
    } else {
      overviewTable.setData(rows);
    }
  } catch (e) {}
  $("overview-cards").innerHTML =
    card("Region", c.region || "—") +
    card("Destination db", c.destination_db || "—") +
    card("Total runs", total) +
    card("Running", running) +
    card("Last success", lastOk);
}


function opt(value, label) {
  const o = document.createElement("option");
  o.value = value; o.textContent = label;
  return o;
}

async function loadProfiles() {
  PROFILES = await (await fetch("/api/mask-config")).json();

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
  if (r.vscode_url) lines.push(`VS Code: <a href="${r.vscode_url}" target="_blank">open editor</a>`);
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
    const rows = runs.map(runRow);
    if (!runsTable) {
      runsTable = makeRunsTable("#runs", true, 10);
      runsTable.on("rowClick", (e, row) => openRun(row.getData().id));
      runsTable.on("tableBuilt", () => runsTable.setData(rows));
    } else {
      runsTable.setData(rows);
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
    ssh_key: $("ssh_key").value.trim() || null,
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
    const missing = [];
    if (!body.ssh_bastion) missing.push("Bastion (user@host[:port]) — the grey text is only a placeholder, type the value");
    if (!body.ssh_key) missing.push("SSH private key (paste the full PEM, incl. the BEGIN/END lines)");
    appendLog("[panel] SSH tunnel on, still missing: " + missing.join("; "));
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

/* ===== Profiles ===== */
let profilesTable = null;

function profileStatusBadge(s) {
  return `<span class="st-${s || "draft"}">${s || "draft"}</span>`;
}

function profileColumns() {
  return [
    { title: "label", field: "label", widthGrow: 2,
      formatter: (c) => `${c.getValue()}<br><span class="muted mono">${c.getRow().getData().id}</span>` },
    { title: "source", field: "source", widthGrow: 2,
      formatter: (c) => c.getValue() || "—" },
    { title: "series", field: "odoo_series", width: 80,
      formatter: (c) => c.getValue() || "—" },
    { title: "ent", field: "needs_enterprise", width: 60, hozAlign: "center",
      formatter: (c) => (c.getValue() ? "✓" : "—") },
    { title: "image", field: "image_status", width: 110,
      formatter: (c) => profileStatusBadge(c.getValue()) },
    { title: "", field: "id", hozAlign: "center", width: 260, headerSort: false,
      formatter: (c) => {
        const d = c.getRow().getData();
        const raw = d._raw || {};
        const hasSource = !!(raw.source_conn && raw.source_conn.host);
        const discover = hasSource
          ? `<a href="#" class="pf-discover" data-id="${d.id}">discover</a>`
          : `<span class="muted" title="add a source DB URL first">discover</span>`;
        const canBuild = !!raw.discovery_hash;
        const build = canBuild
          ? `<a href="#" class="pf-build" data-id="${d.id}">build</a>`
          : `<span class="muted" title="run discovery first">build</span>`;
        const runnable = d.image_status === "ready";
        const run = runnable
          ? `<a href="#" class="pf-run" data-id="${d.id}">run mask</a>`
          : `<span class="muted" title="build an image first">run mask</span>`;
        return `${discover} · ${build} · ${run} · <a href="#" class="pf-edit" data-id="${d.id}">edit</a>`;
      } },  ];
}

function profileRow(p) {
  const c = p.source_conn || {};
  const src = c.host ? `${c.user || ""}@${c.host}:${c.port || 5432}/${c.dbname || ""}` : "";
  return {
    id: p.id, label: p.label, source: src, odoo_series: p.odoo_series,
    needs_enterprise: p.needs_enterprise, image_status: p.image_status,
    _raw: p,
  };
}

async function loadProfilesList() {
  // ensure the mask-profile dropdown in the form is populated
  if (PROFILES && PROFILES.mask_profiles) fillProfileMaskSelect();
  try {
    const { profiles } = await (await fetch("/api/profiles")).json();
    const rows = profiles.map(profileRow);
    if (!profilesTable) {
      profilesTable = new Tabulator("#profiles-table", {
        layout: "fitColumns", height: "auto",
        pagination: true, paginationSize: 10, paginationCounter: "rows",
        placeholder: "No profiles yet — create one on the right.",
        columns: profileColumns(),
      });
      profilesTable.on("tableBuilt", () => profilesTable.setData(rows));
    } else {
      profilesTable.setData(rows);
    }
  } catch (e) {}
}

function fillProfileMaskSelect() {
  const mp = $("pf_mask_profile");
  if (!mp || mp.dataset.filled) return;
  (PROFILES.mask_profiles || []).forEach((p) => mp.appendChild(opt(p.id, p.label)));
  mp.dataset.filled = "1";
}

function resetProfileForm() {
  $("profile-form").reset();
  $("pf_id").value = "";
  $("profile-form-title").textContent = "New profile";
  $("pf-delete-btn").style.display = "none";
  $("pf_source_set").textContent = "";
  $("pf_ssh_set").textContent = "";
  $("pf_token_set").textContent = "";
  $("pf-ssh-fields").classList.add("hidden");
  $("pf-images").classList.add("hidden");
}

function fillProfileForm(p) {
  fillProfileMaskSelect();
  const c = p.source_conn || {};
  const mi = p.mask_inputs || {};
  $("pf_id").value = p.id;
  $("pf_label").value = p.label || "";
  $("pf_description").value = p.description || "";
  $("pf_source_dsn").value = "";
  $("pf_source_set").textContent = p.source_conn && c.host
    ? `current: ${c.user || ""}@${c.host}:${c.port || 5432}/${c.dbname || ""} — leave blank to keep` : "";
  $("pf_ssh_enabled").checked = !!c.ssh_enabled;
  $("pf-ssh-fields").classList.toggle("hidden", !c.ssh_enabled);
  $("pf_ssh_bastion").value = c.ssh_bastion || "";
  $("pf_ssh_key").value = "";
  $("pf_ssh_set").textContent = p.ssh_key_secret_set ? "a key is stored — leave blank to keep" : "";
  $("pf_odoo_series").value = p.odoo_series || "";
  $("pf_odoo_git_ref").value = p.odoo_git_ref || "";
  $("pf_addons_git_url").value = p.addons_git_url || "";
  $("pf_addons_git_ref").value = p.addons_git_ref || "";
  $("pf_git_token").value = "";
  $("pf_token_set").textContent = p.git_token_secret_set ? "a token is stored — leave blank to keep" : "";
  $("pf_needs_enterprise").checked = !!p.needs_enterprise;
  $("pf_enterprise_source").value = p.enterprise_source || "";
  $("pf_mask_profile").value = mi.mask_profile || "";
  $("pf_gm_jobs").value = mi.gm_jobs || "";
  $("profile-form-title").textContent = "Edit profile";
  $("pf-delete-btn").style.display = "";
  loadProfileImages(p.id);
}

async function loadProfileImages(id) {
  const wrap = $("pf-images");
  const list = $("pf-images-list");
  wrap.classList.remove("hidden");
  list.innerHTML = `<p class="muted small">loading images…</p>`;
  try {
    const data = await (await fetch(`/api/profiles/${id}/images`)).json();
    const imgs = data.images || [];
    if (!imgs.length) {
      list.innerHTML = `<p class="muted small">No images built yet. Run <b>discover</b> then <b>build</b>.</p>`;
      return;
    }
    list.innerHTML = imgs.map((im) => {
      const tag = im.uri.split(":").pop();
      const when = im.pushed_at ? new Date(im.pushed_at * 1000).toLocaleString() : "—";
      const size = im.size_mb ? `${im.size_mb} MB` : "";
      const gone = !im.exists ? ` <span class="muted">(deleted from ECR)</span>` : "";
      const badge = im.current
        ? `<span class="st-ready">current</span>`
        : `<a href="#" class="pf-img-del" data-id="${id}" data-uri="${im.uri}">delete</a>`;
      return `<div class="img-row"><span class="mono">${tag}</span>` +
        `<span class="muted small">${when} ${size}${gone}</span>${badge}</div>`;
    }).join("");
  } catch (e) {
    list.innerHTML = `<p class="muted small st-failed">could not load images</p>`;
  }
}

function buildProfilePayload() {
  const body = {
    label: $("pf_label").value.trim() || null,
    description: $("pf_description").value.trim() || null,
    ssh_enabled: $("pf_ssh_enabled").checked,
    ssh_bastion: $("pf_ssh_bastion").value.trim() || null,
    odoo_series: $("pf_odoo_series").value.trim() || null,
    odoo_git_ref: $("pf_odoo_git_ref").value.trim() || null,
    addons_git_url: $("pf_addons_git_url").value.trim() || null,
    addons_git_ref: $("pf_addons_git_ref").value.trim() || null,
    needs_enterprise: $("pf_needs_enterprise").checked,
    enterprise_source: $("pf_enterprise_source").value.trim() || null,
    mask_profile: $("pf_mask_profile").value || null,
    gm_jobs: parseInt($("pf_gm_jobs").value, 10) || null,
  };
  const dsn = $("pf_source_dsn").value.trim();
  if (dsn) body.source_dsn = dsn;
  const key = $("pf_ssh_key").value.trim();
  if (key) body.ssh_key = key;
  const tok = $("pf_git_token").value.trim();
  if (tok) body.git_token = tok;
  return body;
}

$("pf_ssh_enabled").addEventListener("change", () => {
  $("pf-ssh-fields").classList.toggle("hidden", !$("pf_ssh_enabled").checked);
});
$("profile-new-btn").addEventListener("click", resetProfileForm);
$("profiles-refresh").addEventListener("click", loadProfilesList);

$("profile-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("pf_id").value;
  const body = buildProfilePayload();
  if (!id && !body.label) { alert("A profile label is required."); return; }
  $("pf-save-btn").disabled = true;
  try {
    const url = id ? `/api/profiles/${id}` : "/api/profiles";
    const method = id ? "PATCH" : "POST";
    const resp = await fetch(url, {
      method, headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`Could not save profile: ${err.detail || resp.status}`);
    } else {
      resetProfileForm();
      loadProfilesList();
    }
  } finally {
    $("pf-save-btn").disabled = false;
  }
});

$("pf-delete-btn").addEventListener("click", async () => {
  const id = $("pf_id").value;
  if (!id) return;
  if (!confirm("Delete this profile and its stored secrets?")) return;
  await fetch(`/api/profiles/${id}`, { method: "DELETE" });
  resetProfileForm();
  loadProfilesList();
});

// delegated clicks on the profiles table: edit + run mask
document.addEventListener("click", async (e) => {
  const ed = e.target.closest(".pf-edit");
  if (ed) {
    e.preventDefault();
    const p = await (await fetch(`/api/profiles/${ed.dataset.id}`)).json();
    fillProfileForm(p);
    window.scrollTo({ top: 0, behavior: "smooth" });
    return;
  }
  const run = e.target.closest(".pf-run");
  if (run) {
    e.preventDefault();
    const id = run.dataset.id;
    if (!confirm(`Start a mask run from profile ${id}?`)) return;
    const resp = await fetch("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operation: "mask", profile_id: id }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`Could not start run: ${err.detail || resp.status}`);
      return;
    }
    const { run_id } = await resp.json();
    location.hash = "#/runs";
    setTimeout(() => openRun(run_id), 0);
  }

  const disc = e.target.closest(".pf-discover");
  if (disc) {
    e.preventDefault();
    const id = disc.dataset.id;
    if (!confirm(`Run provenance discovery for profile ${id}?\nThis inspects the live source DB + addons repo.`)) return;
    const resp = await fetch(`/api/profiles/${id}/discover`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`Could not start discovery: ${err.detail || resp.status}`);
      return;
    }
    const { run_id } = await resp.json();
    location.hash = "#/runs";
    setTimeout(() => openRun(run_id), 0);
  }

  const bld = e.target.closest(".pf-build");
  if (bld) {
    e.preventDefault();
    const id = bld.dataset.id;
    if (!confirm(`Build the provenance image for profile ${id}?\nThis launches an ephemeral EC2 builder and pushes an immutable image to ECR.`)) return;
    const resp = await fetch(`/api/profiles/${id}/build`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`Could not start build: ${err.detail || resp.status}`);
      return;
    }
    const { run_id } = await resp.json();
    location.hash = "#/runs";
    setTimeout(() => openRun(run_id), 0);
  }

  const imgDel = e.target.closest(".pf-img-del");
  if (imgDel) {
    e.preventDefault();
    const id = imgDel.dataset.id;
    const uri = imgDel.dataset.uri;
    if (!confirm(`Delete image ${uri.split(":").pop()} from ECR?\nThis is permanent.`)) return;
    const resp = await fetch(`/api/profiles/${id}/images/delete`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image_uri: uri }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`Could not delete image: ${err.detail || resp.status}`);
      return;
    }
    loadProfileImages(id);
  }
});

/* ===== Developer environments ===== */
function envColumns() {
  return [
    { title: "id", field: "id", width: 110, formatter: (c) => `<span class="mono">${c.getValue()}</span>` },
    { title: "issue", field: "issue", widthGrow: 1, formatter: (c) => c.getValue() || "—" },
    { title: "source run", field: "source_run_id", width: 120,
      formatter: (c) => (c.getValue() ? `<span class="mono">${c.getValue()}</span>` : "—") },
    { title: "status", field: "status", width: 120,
      formatter: (c) => `<span class="st-${c.getValue()}">${c.getValue()}</span>` },
    { title: "vscode", field: "vscode_url", hozAlign: "center", width: 100,
      formatter: (c) => linkCell(c.getValue(), "open") },
    { title: "odoo", field: "odoo_url", hozAlign: "center", width: 100,
      formatter: (c) => linkCell(c.getValue(), "open") },
    { title: "created", field: "created_at", width: 170,
      formatter: (c) => (c.getValue() ? new Date(c.getValue() * 1000).toLocaleString() : "—") },
    { title: "", field: "id", hozAlign: "center", width: 110, headerSort: false,
      formatter: (c) => {
        const d = c.getRow().getData();
        if (d.status === "terminated" || d.status === "failed") return "—";
        return `<a href="#" class="rm-env" data-env="${d.id}">tear down</a>`;
      } },
  ];
}

async function loadEnvironments() {
  if (!ENV_CONFIG) {
    try { ENV_CONFIG = await (await fetch("/api/environments/config")).json(); }
    catch (e) { ENV_CONFIG = { configured: false }; }
  }
  const note = $("env-config-note");
  if (!ENV_CONFIG.configured) {
    note.innerHTML = "⚠ Environments are not configured. Set <code>ENV_AMI_ID</code>, " +
      "<code>ENV_SG_ID</code> and the other <code>environments.*</code> values.";
    $("env-create-btn").disabled = true;
  } else {
    note.textContent = `Ready · ${ENV_CONFIG.instance_type} · code-server :${ENV_CONFIG.code_port} · odoo :${ENV_CONFIG.odoo_port}`;
    $("env-create-btn").disabled = false;
    if (ENV_CONFIG.repo_url && !$("env-repo-url").value) {
      $("env-repo-url").placeholder = ENV_CONFIG.repo_url;
    }
    if (ENV_CONFIG.repo_branch && !$("env-repo-branch").value) {
      $("env-repo-branch").placeholder = ENV_CONFIG.repo_branch;
    }
  }

  // populate the "seed from run" select with runs that produced a dump
  try {
    const { runs } = await (await fetch("/api/runs")).json();
    const sel = $("env-source-run");
    const cur = sel.value;
    sel.innerHTML = "";
    sel.appendChild(opt("", "— none (empty environment) —"));
    for (const r of runs) {
      const res = r.result || {};
      if (res.masked_dump_url || res.masked_dump_s3_uri) {
        sel.appendChild(opt(r.id, `${r.id} · ${new Date((r.started_at || 0) * 1000).toLocaleString()}`));
      }
    }
    if (cur) sel.value = cur;
  } catch (e) {}

  // populate the "profile" select with profiles that have a built (ready) image
  try {
    const { profiles } = await (await fetch("/api/profiles")).json();
    const sel = $("env-profile");
    const cur = sel.value;
    sel.innerHTML = "";
    sel.appendChild(opt("", "— none (use configured default image) —"));
    for (const p of profiles) {
      if (p.image_status === "ready" && p.image_uri) {
        sel.appendChild(opt(p.id, `${p.label} · ${p.image_uri.split(":").pop()}`));
      }
    }
    if (cur) sel.value = cur;
  } catch (e) {}

  try {
    const { environments } = await (await fetch("/api/environments")).json();
    if (!envTable) {
      envTable = new Tabulator("#environments", {
        layout: "fitColumns", height: "auto",
        pagination: true, paginationSize: 10, paginationCounter: "rows",
        placeholder: "No environments yet", columns: envColumns(),
      });
      envTable.on("tableBuilt", () => envTable.setData(environments));
    } else {
      envTable.setData(environments);
    }
  } catch (e) {}
}

async function createEnvironment(body) {
  const resp = await fetch("/api/environments", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    alert(`Could not create environment: ${err.detail || resp.status}`);
    return false;
  }
  return true;
}

$("env-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("env-create-btn").disabled = true;
  const ok = await createEnvironment({
    profile_id: $("env-profile").value || null,
    source_run_id: $("env-source-run").value || null,
    issue: $("env-issue").value.trim() || null,
    repo_url: $("env-repo-url").value.trim() || null,
    repo_branch: $("env-repo-branch").value.trim() || null,
  });
  $("env-create-btn").disabled = false;
  if (ok) { $("env-issue").value = ""; loadEnvironments(); }
});

$("envs-refresh").addEventListener("click", loadEnvironments);

// delegated clicks: "create env" (runs table) + "tear down" (env table)
document.addEventListener("click", async (e) => {
  const mk = e.target.closest(".mk-env");
  if (mk) {
    e.preventDefault();
    const runId = mk.dataset.run;
    if (!confirm(`Launch a developer environment seeded from run ${runId}?`)) return;
    const ok = await createEnvironment({ source_run_id: runId });
    if (ok) { location.hash = "#/environments"; }
    return;
  }
  const rm = e.target.closest(".rm-env");
  if (rm) {
    e.preventDefault();
    const envId = rm.dataset.env;
    if (!confirm(`Tear down environment ${envId}? The instance is terminated.`)) return;
    await fetch(`/api/environments/${envId}`, { method: "DELETE" });
    loadEnvironments();
  }
});

loadConfig().then(navigate);
loadProfiles();
loadRuns();
setInterval(() => {
  loadRuns();
  if (currentRoute() === "overview") renderOverview();
  if (currentRoute() === "environments") loadEnvironments();
}, 10000);
