/* wecom-forward-plus admin UI — vanilla JS, no build step, no framework. */
"use strict";

const state = { groups: [], defaults: {}, editing: null };

const $ = (id) => document.getElementById(id);

// -- API helper ---------------------------------------------------------------

async function api(path, options = {}) {
  const opts = { credentials: "same-origin", ...options };
  if (opts.method && opts.method !== "GET") {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  }
  const response = await fetch(path, opts);
  if (response.status === 401) {
    showLogin();
    throw new ApiError(401, { message: "未登录或会话已过期" });
  }
  let body = null;
  try { body = await response.json(); } catch (_) { /* empty 204 responses */ }
  if (!response.ok) {
    throw new ApiError(response.status, (body && body.error) || { message: `请求失败 (${response.status})` });
  }
  return body;
}

class ApiError extends Error {
  constructor(status, error) {
    super(error.message);
    this.status = status;
    this.fields = error.fields || {};
  }
}

// -- login flow ----------------------------------------------------------------

function showLogin() {
  $("main-section").hidden = true;
  $("logout-btn").hidden = true;
  $("login-section").hidden = false;
}

function showMain() {
  $("login-section").hidden = true;
  $("main-section").hidden = false;
  $("logout-btn").hidden = false;
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-error").hidden = true;
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password: $("password").value }) });
    $("password").value = "";
    showMain();
    await loadGroups();
  } catch (err) {
    $("login-error").textContent = err.message || "登录失败";
    $("login-error").hidden = false;
  }
});

$("logout-btn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (_) { /* session already gone */ }
  showLogin();
});

// -- groups table ----------------------------------------------------------------

async function loadGroups() {
  $("table-error").hidden = true;
  try {
    const payload = await api("/api/groups");
    state.groups = payload.groups;
    state.defaults = payload.defaults;
    renderDefaults(payload.defaults);
    renderGroups();
  } catch (err) {
    if (err.status === 401) throw err; // showLogin() already ran inside api()
    $("table-error").textContent = err.message;
    $("table-error").hidden = false;
  }
}

function renderDefaults(defaults) {
  $("dify-url").textContent = defaults.dify_base_url || "—";
  $("default-session-max").textContent = `${defaults.session_max_total}`;
  $("default-session-ttl").textContent = `${defaults.session_ttl_seconds} 秒`;
  $("defaults-panel").hidden = false;
}

function renderGroups() {
  const tbody = $("groups-body");
  tbody.replaceChildren();
  for (const group of state.groups) {
    tbody.append(rowFor(group));
  }
  const empty = state.groups.length === 0;
  $("groups-table").hidden = empty;
  $("empty-state").hidden = !empty;
}

function rowFor(group) {
  const tr = document.createElement("tr");
  if (!group.enabled) tr.classList.add("disabled");

  const cells = [
    textCell(group.name),
    enabledCell(group),
    textCell(group.wecom_robot_id, "mono"),
    textCell(group.wecom_robot_secret_masked, "mono muted"),
    textCell(group.dify_api_key_masked, "mono muted"),
    paramCell(group.session_max_total, group.effective_session_max_total),
    paramCell(group.session_ttl_seconds, group.effective_session_ttl_seconds),
    textCell((group.updated_at || "").replace("T", " ").slice(0, 19), "mono muted"),
    actionsCell(group),
  ];
  for (const cell of cells) tr.append(cell);
  return tr;
}

function textCell(value, className) {
  const td = document.createElement("td");
  if (className) td.className = className;
  td.textContent = value == null ? "" : String(value);
  return td;
}

function paramCell(value, effective) {
  const td = document.createElement("td");
  td.className = "num";
  if (value == null) {
    td.textContent = `${effective}`;
    td.title = "继承全局默认";
    td.classList.add("inherited");
  } else {
    td.textContent = `${value}`;
  }
  return td;
}

function enabledCell(group) {
  const td = document.createElement("td");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "switch";
  checkbox.setAttribute("role", "switch");
  checkbox.setAttribute("aria-label", `启用「${group.name}」`);
  checkbox.checked = group.enabled;
  checkbox.addEventListener("change", () => toggleEnabled(group, checkbox));
  td.append(checkbox);
  return td;
}

function actionsCell(group) {
  const td = document.createElement("td");
  const edit = document.createElement("button");
  edit.className = "ghost btn-sm";
  edit.textContent = "编辑";
  edit.addEventListener("click", () => openEdit(group));
  const del = document.createElement("button");
  del.className = "ghost danger btn-sm";
  del.textContent = "删除";
  del.addEventListener("click", () => deleteGroup(group));
  td.append(edit, del);
  return td;
}

async function toggleEnabled(group, checkbox) {
  try {
    await api(`/api/groups/${group.id}/enabled`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: checkbox.checked }),
    });
    group.enabled = checkbox.checked;
    await loadGroups();
  } catch (err) {
    checkbox.checked = !checkbox.checked; // revert the optimistic toggle
    if (err.status === 401) return; // login screen is already up
    $("table-error").textContent = err.message;
    $("table-error").hidden = false;
  }
}

async function deleteGroup(group) {
  if (!confirm(`删除配置组「${group.name}」？该组的客户端将断开，会话将被清空。`)) return;
  try {
    await api(`/api/groups/${group.id}`, { method: "DELETE" });
    await loadGroups();
  } catch (err) {
    if (err.status === 401) return; // login screen is already up
    $("table-error").textContent = err.message;
    $("table-error").hidden = false;
  }
}

// -- create / edit dialog ---------------------------------------------------------

const dialog = $("group-dialog");
const form = $("group-form");

$("add-group-btn").addEventListener("click", () => openCreate());

const SECRET_FIELDS = ["wecom_robot_secret", "dify_api_key"];

function openCreate() {
  state.editing = null;
  $("dialog-title").textContent = "新增配置组";
  form.reset();
  form.classList.add("mode-create");
  form.elements.enabled.checked = true;
  form.elements.wecom_robot_id.required = true;
  for (const field of SECRET_FIELDS) {
    form.elements[field].required = true;
    form.elements[field].placeholder = "必填";
  }
  $("form-error").hidden = true;
  dialog.showModal();
}

function openEdit(group) {
  state.editing = group;
  $("dialog-title").textContent = `编辑「${group.name}」`;
  form.reset();
  form.classList.remove("mode-create");
  form.elements.name.value = group.name;
  form.elements.wecom_robot_id.value = group.wecom_robot_id;
  // Secret inputs stay blank: blank = keep the stored value.
  form.elements.session_max_total.value = group.session_max_total == null ? "" : group.session_max_total;
  form.elements.session_ttl_seconds.value = group.session_ttl_seconds == null ? "" : group.session_ttl_seconds;
  form.elements.enabled.checked = group.enabled;
  form.elements.wecom_robot_id.required = false;
  for (const field of SECRET_FIELDS) {
    form.elements[field].required = false;
    form.elements[field].placeholder = "留空表示保持不变";
  }
  $("form-error").hidden = true;
  dialog.showModal();
}

$("cancel-btn").addEventListener("click", () => dialog.close());

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  $("form-error").hidden = true;

  const elements = form.elements;
  const payload = {
    name: elements.name.value.trim(),
    wecom_robot_id: elements.wecom_robot_id.value.trim(),
    wecom_robot_secret: elements.wecom_robot_secret.value,
    dify_api_key: elements.dify_api_key.value,
    session_max_total: elements.session_max_total.value === "" ? null : Number(elements.session_max_total.value),
    session_ttl_seconds: elements.session_ttl_seconds.value === "" ? null : Number(elements.session_ttl_seconds.value),
    enabled: elements.enabled.checked,
  };

  try {
    if (state.editing) {
      await api(`/api/groups/${state.editing.id}`, { method: "PUT", body: JSON.stringify(payload) });
    } else {
      await api("/api/groups", { method: "POST", body: JSON.stringify(payload) });
    }
    dialog.close();
    await loadGroups();
  } catch (err) {
    if (err.status !== 401) {
      const fieldMessages = Object.values(err.fields || {});
      $("form-error").textContent = fieldMessages.length
        ? fieldMessages.join("；")
        : err.message;
      $("form-error").hidden = false;
    }
  }
});

// -- boot ---------------------------------------------------------------------------

loadGroups().then(() => showMain()).catch(() => { /* 401: showLogin ran in api() */ });
