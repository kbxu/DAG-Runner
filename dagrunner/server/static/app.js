const state = {
  workflows: new Map(),
  workflowSort: { key: "next_run_time", direction: "asc" },
  workflowTag: "all",
  workflowPage: 1,
  workflowPageSize: 15,
  runs: [],
  runPage: 1,
  runPageSize: 15,
  runPagination: { page: 1, page_size: 15, total: 0, total_pages: 1 },
  runFilters: { workflow: "", status: "", trigger: "", tag: "" },
  auth: { username: "", is_admin: false, unrestricted: false, workflows: [] },
};
let serviceState = "checking";
let runLoadSequence = 0;
let runSearchTimer = null;

async function api(url, options = {}) {
  let response;
  try {
    const headers = {
      "X-DAGRunner-Language": currentLanguage,
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
    };
    response = await fetch(url, { headers, ...options });
    setServiceStatus("online");
  } catch (cause) {
    setServiceStatus("offline");
    const error = new Error(t("后端服务已断开"));
    error.offline = true;
    error.cause = cause;
    throw error;
  }
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : await response.text();
  if (response.status === 401 && body?.login_url) {
    window.location.assign(body.login_url);
    const error = new Error(t("登录已失效，请重新登录"));
    error.loginRequired = true;
    throw error;
  }
  if (!response.ok) { const error = new Error(t(body.error || body || `HTTP ${response.status}`)); error.data = body; throw error; }
  return body;
}

function setServiceStatus(status) {
  if (serviceState === status) return;
  serviceState = status;
  const box = document.getElementById("serviceStatus");
  if (!box) return;
  box.classList.remove("online", "offline", "checking");
  box.classList.add(status);
  box.querySelector("b").textContent = status === "online" ? t("服务在线") : status === "offline" ? t("服务断线") : t("正在连接");
}

function esc(value) { return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c])); }
function jsArg(value) { return JSON.stringify(String(value ?? "")).replace(/</g, "\\u003c").replace(/>/g, "\\u003e").replace(/&/g, "\\u0026").replace(/'/g, "\\u0027"); }
const PERMISSION_RANK = { read: 1, execute: 2, edit: 3 };
// 存储里的权限：单词沿用旧的逐级包含语义；JSON 数组表示相互独立的权限组合。
function parsePermissionFlags(value) {
  const text = String(value ?? "").trim();
  if (!text) return new Set();
  const lower = text.toLowerCase();
  if (lower === "read") return new Set(["read"]);
  if (lower === "execute") return new Set(["read", "execute"]);
  if (lower === "edit") return new Set(["read", "execute", "edit"]);
  if (lower.startsWith("[")) {
    try {
      const items = JSON.parse(text);
      if (Array.isArray(items)) return new Set(items.filter(item => item in PERMISSION_RANK));
    } catch (_) {}
    return new Set();
  }
  return new Set(lower.split(",").map(item => item.trim()).filter(item => item in PERMISSION_RANK));
}
function serializePermissionFlags(flags) {
  const ordered = Object.keys(PERMISSION_RANK).filter(perm => flags.has(perm));
  if (!ordered.length) return "";
  if (ordered.length === 3) return "edit";
  if (ordered.length === 2 && ordered[0] === "read" && ordered[1] === "execute") return "execute";
  return JSON.stringify(ordered);
}
function workflowPermissionSet(name) {
  const workflow = state.workflows.get(name);
  if (workflow?.permission) return parsePermissionFlags(workflow.permission);
  return state.auth.is_admin || state.auth.unrestricted ? new Set(["read", "execute", "edit"]) : new Set();
}
function canWorkflow(name, required) {
  return workflowPermissionSet(name).has(required);
}
function canCreateWorkflow() {
  if (state.auth.is_admin || state.auth.unrestricted) return true;
  return [...state.workflows.values()].some(workflow => parsePermissionFlags(workflow.permission).has("edit"));
}
async function loadAuth() {
  try { state.auth = await api("/api/auth/me"); }
  catch (error) { if (!error.loginRequired && !error.offline) toast(error.message, true); }
}
function normalizeYamlNewlines(value) {
  const source = String(value ?? "");
  return source.includes("\r") ? source.replace(/\r\n?/g, "\n") : source;
}
function namedId(name, id) { return name && name !== id ? (currentLanguage === "en" ? `${name} (${id})` : `${name}（${id}）`) : id; }
function taskNamedId(workflow, taskId) { const task = workflow?.tasks.find(item => item.name === taskId); return namedId(task?.description, taskId); }
const usefulTopKeys = new Set(["description", "runner", "setup", "timeout", "notification", "tasks"]);
const usefulTaskKeys = new Set(["type", "description", "shell", "command", "depends", "condition", "success", "failure", "args", "cwd", "timeout", "enabled", "runner"]);
function yamlEditor(id, value, includeSchedule=false, readOnly=false) {
  const source=normalizeYamlNewlines(value);
  return `<div class="yaml-editor"><div class="yaml-gutter" id="${id}Gutter" aria-hidden="true">${yamlGutter(source)}</div><div class="yaml-code"><pre id="${id}Highlight" aria-hidden="true">${highlightUsefulYaml(source,includeSchedule)}\n</pre><textarea id="${id}" data-include-schedule="${includeSchedule}" spellcheck="false" ${readOnly ? "readonly" : ""} oninput="syncYamlHighlight('${id}')" onkeydown="yamlEditorKeydown(event,'${id}')" onscroll="syncYamlScroll('${id}')">${esc(source)}</textarea></div></div>`;
}
function yamlGutter(value) {
  const source=String(value ?? "");
  const count=source === "" ? 1 : source.split("\n").length;
  return Array.from({length:count}, (_,index) => index+1).join("\n");
}
function highlightShellVariables(text) {
  const source=String(text), pattern=/(\$env:[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\b[A-Z_][A-Z0-9_]*(?=\s*=))/g;
  let result="", cursor=0;
  for (const match of source.matchAll(pattern)) {
    result+=esc(source.slice(cursor,match.index));
    result+=`<span class="yaml-useful-var">${esc(match[0])}</span>`;
    cursor=match.index+match[0].length;
  }
  return result+esc(source.slice(cursor));
}
function highlightUsefulYaml(value, includeSchedule=false) {
  let topSection="", taskField="";
  return String(value).split("\n").map(line => {
    const match=line.match(/^(\s*)([^#\s][^:]*):(.*)$/);
    if (!match) return highlightShellVariables(line);
    const indent=match[1].replace(/\t/g,"  ").length;
    const rawKey=match[2], key=rawKey.trim().replace(/^(['"])(.*)\1$/,"$2");
    if (indent===0) { topSection=key; taskField=""; }
    if (topSection==="tasks" && indent===4) taskField=key;
    const useful =
      (indent===0 && usefulTopKeys.has(key)) ||
      (topSection==="tasks" && indent===2) ||
      (topSection==="tasks" && indent===4 && usefulTaskKeys.has(key)) ||
      (topSection==="setup" && indent===2 && ["linux","windows","default"].includes(key)) ||
      (includeSchedule && indent===0 && key==="schedule") ||
      (includeSchedule && topSection==="schedule" && indent===2 && ["cron","crons","timezone","enabled"].includes(key));
    if (!useful) return highlightShellVariables(line);
    return `${esc(match[1])}<span class="yaml-useful-key">${esc(rawKey)}</span>:${highlightShellVariables(match[3])}`;
  }).join("\n");
}
function syncYamlHighlight(id, skipPreview=false) {
  const input=document.getElementById(id), highlight=document.getElementById(`${id}Highlight`);
  if (input) {
    const normalized=normalizeYamlNewlines(input.value);
    if (normalized !== input.value) {
      const start=input.selectionStart, end=input.selectionEnd;
      input.value=normalized;
      input.selectionStart=start; input.selectionEnd=end;
    }
  }
  if (input && highlight) { highlight.innerHTML=`${highlightUsefulYaml(input.value,input.dataset.includeSchedule==="true")}\n`; syncYamlScroll(id); }
  syncYamlGutter(id);
  if (!skipPreview) queueWorkflowPreview(id);
}
function syncYamlGutter(id) {
  const input=document.getElementById(id), gutter=document.getElementById(`${id}Gutter`);
  if (!input || !gutter) return;
  const count=input.value === "" ? 1 : input.value.split("\n").length;
  if (gutter.dataset.lines !== String(count)) {
    gutter.dataset.lines=String(count);
    gutter.textContent=Array.from({length:count}, (_,index) => index+1).join("\n");
  }
}
function syncYamlScroll(id) {
  const input=document.getElementById(id), highlight=document.getElementById(`${id}Highlight`), gutter=document.getElementById(`${id}Gutter`);
  if (input && highlight) { highlight.scrollTop=input.scrollTop; highlight.scrollLeft=input.scrollLeft; }
  if (input && gutter) {
    const inputMax=input.scrollHeight-input.clientHeight, gutterMax=gutter.scrollHeight-gutter.clientHeight;
    gutter.scrollTop = inputMax > 0 && gutterMax > 0 ? Math.round(input.scrollTop*gutterMax/inputMax) : 0;
  }
}
function yamlTaskOffset(definition, name) {
  const escaped = String(name).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const tasksMatch = /^tasks\s*:\s*(?:#.*)?$/m.exec(definition);
  const from = tasksMatch ? tasksMatch.index + tasksMatch[0].length : 0;
  const match = new RegExp(`(?:^|\\n)([ \\t]*)(['"]?)${escaped}\\2\\s*:`, "m").exec(definition.slice(from));
  if (!match) return null;
  return from + match.index + (match[0].startsWith("\n") ? 1 : 0) + match[1].length;
}
function scrollYamlToTask(textarea, definition, name) {
  const offset = yamlTaskOffset(definition, name);
  const start = offset ?? 0;
  textarea.focus();
  textarea.setSelectionRange(start, start);
  const line = definition.slice(0, start).split("\n").length - 1;
  const lineHeight = parseFloat(getComputedStyle(textarea).lineHeight) || 18.6;
  const targetTop = Math.max(0, (line - 2) * lineHeight);
  const maxScrollTop = Math.max(0, textarea.scrollHeight - textarea.clientHeight);
  textarea.scrollTop = Math.min(targetTop, maxScrollTop);
  syncYamlScroll(textarea.id);
  return line;
}
function ensureYamlFlash(textarea) {
  const code = textarea?.closest(".yaml-code");
  if (!code) return null;
  let flash = code.querySelector(".yaml-flash");
  if (!flash) {
    flash = document.createElement("div");
    flash.className = "yaml-flash";
    flash.style.display = "none";
    code.appendChild(flash);
    textarea.addEventListener("scroll", () => positionYamlFlash(textarea, flash));
  }
  return flash;
}
function positionYamlFlash(textarea, flash) {
  if (!textarea.dataset.flashLine) { flash.style.display = "none"; return; }
  const lineHeight = parseFloat(getComputedStyle(textarea).lineHeight) || 18.6;
  const paddingTop = parseFloat(getComputedStyle(textarea).paddingTop) || 12;
  flash.style.display = "block";
  flash.style.top = `${paddingTop + Number(textarea.dataset.flashLine) * lineHeight - textarea.scrollTop}px`;
  flash.style.height = `${lineHeight}px`;
}
function flashYamlLine(textarea, line) {
  const flash = ensureYamlFlash(textarea);
  if (!flash) return;
  textarea.dataset.flashLine = String(line);
  positionYamlFlash(textarea, flash);
  flash.classList.remove("active");
  void flash.offsetWidth;
  flash.classList.add("active");
  clearTimeout(textarea._flashTimer);
  textarea._flashTimer = setTimeout(() => {
    flash.classList.remove("active");
    setTimeout(() => { delete textarea.dataset.flashLine; flash.style.display = "none"; }, 500);
  }, 600);
}
function clearYamlFlash(textarea) {
  const flash = textarea?.closest(".yaml-code")?.querySelector(".yaml-flash");
  if (flash) { flash.classList.remove("active"); flash.style.display = "none"; }
  if (textarea) { delete textarea.dataset.flashLine; clearTimeout(textarea._flashTimer); }
}
function yamlEditorKeydown(event, id) {
  const input = document.getElementById(id);
  if (!input || input.readOnly) return;
  const value = input.value;
  const start = input.selectionStart, end = input.selectionEnd;
  if (event.key === 'Tab') {
    event.preventDefault();
    const blockStart = value.lastIndexOf('\n', start - 1) + 1;
    const blockEndIndex = value.indexOf('\n', end);
    const blockEnd = blockEndIndex === -1 ? value.length : blockEndIndex;
    const block = value.slice(blockStart, blockEnd);
    if (event.shiftKey) {
      const dedented = block.split('\n').map(line => line.replace(/^ {1,2}/, '')).join('\n');
      if (dedented !== block) {
        const collapsed = start === end;
        const removed = collapsed ? block.length - block.replace(/^ {1,2}/, '').length : 0;
        input.setRangeText(dedented, blockStart, blockEnd, 'end');
        if (collapsed) input.selectionStart = input.selectionEnd = Math.max(blockStart, start - removed);
        input.dispatchEvent(new Event('input'));
      }
      return;
    }
    if (start === end) input.setRangeText('  ', start, end, 'end');
    else input.setRangeText(block.split('\n').map(line => line ? '  ' + line : line).join('\n'), blockStart, blockEnd, 'end');
    input.dispatchEvent(new Event('input'));
    return;
  }
  if (event.key === 'Enter' && !event.shiftKey) {
    const before = value.slice(0, start);
    const lineStart = before.lastIndexOf('\n') + 1;
    const indent = (before.slice(lineStart).match(/^[ \t]*/) || [''])[0];
    if (!indent) return;
    event.preventDefault();
    input.setRangeText('\n' + indent, start, end, 'end');
    input.dispatchEvent(new Event('input'));
  }
}
function fmt(value) {
  if (!value) return "—";
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/);
  return match ? `${match[1]}/${match[2]}/${match[3]} ${match[4]}:${match[5]}:${match[6]}` : esc(value);
}
function duration(start, end) { if (!start) return "—"; const seconds = Math.max(0, (new Date(end || Date.now()) - new Date(start)) / 1000); return seconds < 60 ? `${Math.round(seconds)} ${t("秒")}` : `${Math.floor(seconds / 60)} ${t("分")} ${Math.round(seconds % 60)} ${t("秒")}`; }
function statusBadge(status) { return `<span class="badge ${esc(status)}">${esc(status)}</span>`; }

async function loadWorkflows() {
  try {
    const data = await api("/api/workflows");
    state.workflows = new Map(data.workflows.map(w => [w.name, w]));
    renderWorkflows();
    const errors = Object.entries(data.config_errors || {});
    const box = document.getElementById("configErrors");
    box.classList.toggle("hidden", !errors.length);
    box.textContent = errors.map(([file, error]) => `${file}: ${error}`).join("\n");
  } catch (error) { if (!error.offline) toast(error.message, true); }
}

let workflowTagOptions = [];
function workflowTitle(workflow) { return workflow.description || workflow.name; }
function extractWorkflowTags(workflow) {
  const tags = [];
  let rest = String(workflowTitle(workflow)).trim();
  for (;;) {
    const match = rest.match(/^(?:【([^】]+)】|\[([^\]]+)\])\s*/);
    if (!match) break;
    const tag = (match[1] ?? match[2] ?? "").trim();
    if (tag && !tags.includes(tag)) tags.push(tag);
    rest = rest.slice(match[0].length);
  }
  return tags;
}
function workflowMatchesTag(workflow) {
  if (state.workflowTag === "all") return true;
  const tags = extractWorkflowTags(workflow);
  if (state.workflowTag === "other") return tags.length === 0;
  return state.workflowTag.startsWith("tag:") && tags.includes(state.workflowTag.slice(4));
}
function renderWorkflowTags() {
  const workflows = [...state.workflows.values()];
  const counts = new Map();
  let otherCount = 0;
  for (const workflow of workflows) {
    const tags = extractWorkflowTags(workflow);
    if (!tags.length) { otherCount += 1; continue; }
    for (const tag of tags) counts.set(tag, (counts.get(tag) || 0) + 1);
  }
  const extracted = [...counts.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0], "zh"))
    .map(([label, count]) => ({ key: `tag:${label}`, label, count }));
  workflowTagOptions = [
    { key: "all", label: t("全部"), count: workflows.length },
    ...extracted,
    { key: "other", label: t("其他"), count: otherCount },
  ];
  if (!workflowTagOptions.some(option => option.key === state.workflowTag)) state.workflowTag = "all";
  const box = document.getElementById("workflowTags");
  if (!box) return;
  const allTagHint = t("在工作流名称开头添加【标签】或 [标签] 即可自动归类，如：【运维】日报");
  box.innerHTML = workflowTagOptions.map((option, index) => `<button type="button" class="workflow-tag${option.key === state.workflowTag ? " active" : ""}"${option.key === "all" ? ` title="${esc(allTagHint)}"` : ""} onclick="setWorkflowTag(${index})"><span class="workflow-tag-label">${esc(option.label)}</span><span class="workflow-tag-count">${option.count}</span></button>`).join("");
}
function setWorkflowTag(index) {
  const option = workflowTagOptions[index];
  if (!option || option.key === state.workflowTag) return;
  state.workflowTag = option.key;
  state.workflowPage = 1;
  renderWorkflows();
  state.runFilters.tag = option.key;
  loadRuns(true);
}

let workflowResortInterval = null;
let workflowResortDeadline = 0;

function sortWorkflowList(list) {
  const { key, direction } = state.workflowSort;
  if (!key) return list;
  const multiplier = direction === "asc" ? 1 : -1;
  return list.sort((left, right) => {
    const leftValue = key === "next_run_time" ? left.schedule?.next_run_time : left[key];
    const rightValue = key === "next_run_time" ? right.schedule?.next_run_time : right[key];
    const leftTime = leftValue ? Date.parse(leftValue) : null;
    const rightTime = rightValue ? Date.parse(rightValue) : null;
    if (leftTime === null && rightTime === null) return left.db_id - right.db_id;
    if (leftTime === null) return 1;
    if (rightTime === null) return -1;
    return (leftTime - rightTime) * multiplier || left.db_id - right.db_id;
  });
}

function renderWorkflowResortBadge() {
  const badge = document.getElementById("workflowResortBadge");
  if (!badge) return;
  if (!state.frozenWorkflowOrder || !workflowResortDeadline) {
    badge.classList.add("hidden");
    badge.innerHTML = "";
    return;
  }
  const remain = Math.max(0, Math.ceil((workflowResortDeadline - Date.now()) / 1000));
  if (remain <= 0) {
    flushWorkflowResort();
    return;
  }
  badge.classList.remove("hidden");
  badge.innerHTML = `<span>${esc(t("{seconds}s 后恢复排序", {seconds: remain}))}</span><button type="button" class="resort-now-btn" onclick="flushWorkflowResort()">${esc(t("立即刷新"))}</button>`;
}

function startWorkflowResortCountdown(seconds = 30) {
  if (!state.frozenWorkflowOrder) {
    const current = sortWorkflowList([...state.workflows.values()]);
    state.frozenWorkflowOrder = current.map(item => item.name);
  }
  workflowResortDeadline = Date.now() + seconds * 1000;
  clearInterval(workflowResortInterval);
  renderWorkflowResortBadge();
  workflowResortInterval = setInterval(renderWorkflowResortBadge, 1000);
}

function flushWorkflowResort(rerender = true) {
  clearInterval(workflowResortInterval);
  workflowResortInterval = null;
  workflowResortDeadline = 0;
  state.frozenWorkflowOrder = null;
  const badge = document.getElementById("workflowResortBadge");
  if (badge) {
    badge.classList.add("hidden");
    badge.innerHTML = "";
  }
  if (rerender) renderWorkflows();
}

function setWorkflowSearch(value) {
  state.workflowSearch = String(value || "").trim().toLowerCase();
  state.workflowPage = 1;
  renderWorkflows();
}

function renderWorkflows() {
  hideSchedulePopover();
  renderWorkflowTags();
  const query = state.workflowSearch || "";
  const workflows = [...state.workflows.values()].filter(w => {
    if (!workflowMatchesTag(w)) return false;
    if (!query) return true;
    return String(w.name || "").toLowerCase().includes(query)
      || String(w.description || "").toLowerCase().includes(query);
  });
  const { key, direction } = state.workflowSort;
  if (state.frozenWorkflowOrder) {
    const rank = new Map(state.frozenWorkflowOrder.map((name, idx) => [name, idx]));
    workflows.sort((left, right) => {
      const a = rank.has(left.name) ? rank.get(left.name) : Number.MAX_SAFE_INTEGER;
      const b = rank.has(right.name) ? rank.get(right.name) : Number.MAX_SAFE_INTEGER;
      return a - b || left.db_id - right.db_id;
    });
  } else {
    sortWorkflowList(workflows);
  }
  const totalPages = Math.max(1, Math.ceil(workflows.length / state.workflowPageSize));
  state.workflowPage = Math.min(Math.max(state.workflowPage, 1), totalPages);
  const pageStart = (state.workflowPage - 1) * state.workflowPageSize;
  const visibleWorkflows = workflows.slice(pageStart, pageStart + state.workflowPageSize);
  document.getElementById("lastRunSort").textContent = key === "last_run_time" ? (direction === "asc" ? "▲" : "▼") : "";
  document.getElementById("nextRunSort").textContent = key === "next_run_time" ? (direction === "asc" ? "▲" : "▼") : "";
  const mobileSort = document.getElementById("workflowSortMobile");
  if (mobileSort) {
    const signature = `${currentLanguage}:${key}:${direction}`;
    if (mobileSort.dataset.sortSignature !== signature) {
      mobileSort.dataset.sortSignature = signature;
      const options = [`<option value=""${key ? "" : " selected"}>${t("默认")}</option>`];
      for (const sortKey of ["last_run_time", "next_run_time"]) {
        const label = t(sortKey === "last_run_time" ? "上次运行" : "下次运行");
        options.push(`<option value="${sortKey}:desc"${key === sortKey && direction === "desc" ? " selected" : ""}>${label} ↓</option>`);
        options.push(`<option value="${sortKey}:asc"${key === sortKey && direction === "asc" ? " selected" : ""}>${label} ↑</option>`);
      }
      mobileSort.innerHTML = options.join("");
    }
  }
  document.getElementById("workflowRows").innerHTML = visibleWorkflows.map(w => {
      const s = w.schedule;
      const notificationClass = (w.notification_count || 0) > 0 ? " notification-active" : "";
      const canExecute = canWorkflow(w.name, "execute");
      const canEdit = canWorkflow(w.name, "edit");
      const actions = [];
      if (canExecute) actions.push(`<button class="button" onclick="runWorkflow('${esc(w.name)}')">${t("运行")}</button>`);
      if (canEdit) {
        if (s.enabled) {
          actions.push(`<button class="button ghost schedule-active" onclick='setWorkflowOnline(${jsArg(w.name)}, false)'>${t("下线")}</button>`);
        } else {
          actions.push(`<button class="button ghost" onclick='setWorkflowOnline(${jsArg(w.name)}, true)'>${t("上线")}</button>`);
          actions.push(`<button class="button ghost" onclick="editWorkflow('${esc(w.name)}')">${t("编辑")}</button>`);
        }
        actions.push(`<button class="button ghost${notificationClass}" onclick='editNotification(${jsArg(w.name)})'>${t("通知")}</button>`);
      }
      actions.push(`<button class="button ghost" onclick="exportYaml('${esc(w.name)}')">${t("导出")}</button>`);
      if (canEdit && !s.enabled) actions.push(`<button class="button danger" onclick="deleteWorkflow('${esc(w.name)}')">${t("删除")}</button>`);
      return `<tr>
        <td class="card-cell-name"><div class="workflow-name-cell"><button class="workflow-link" title="${esc(w.description || w.name)}" onclick="showDag('${esc(w.name)}')">${esc(w.description || w.name)}</button>${runnerTag(w.runner, w.runner_locations)}</div><div class="workflow-id mono" title="${esc(w.name)}">${esc(w.name)}</div></td>
        <td data-label="${esc(t("Cron / 时区"))}">${scheduleSummary(s)}</td>
        <td data-label="${esc(t("上次运行"))}">${fmt(w.last_run_time)}</td>
        <td data-label="${esc(t("下次运行"))}">${fmt(s.next_run_time)}</td>
        <td class="card-cell-actions"><div class="actions">${actions.join("")}</div></td>
      </tr>`;
    }).join("") || `<tr><td colspan="5" class="empty">${state.workflowSearch ? t("没有匹配的工作流") : t("还没有导入工作流")}</td></tr>`;
  document.getElementById("workflowPagination").innerHTML = `<span>${t("共 {total} 条 · 第 {page} / {pages} 页", {total:workflows.length,page:state.workflowPage,pages:totalPages})}</span><div><button class="button ghost" ${state.workflowPage <= 1 ? "disabled" : ""} onclick="changeWorkflowPage(${state.workflowPage - 1})">${t("上一页")}</button><button class="button ghost" ${state.workflowPage >= totalPages ? "disabled" : ""} onclick="changeWorkflowPage(${state.workflowPage + 1})">${t("下一页")}</button></div>`;
  positionWorkflowTags();
}

function positionWorkflowTags() {
  const rail = document.getElementById("workflowTags");
  const wrap = document.querySelector(".workflow-panel-wrap");
  if (!rail || !wrap) return;
  if (getComputedStyle(rail).position !== "absolute") {
    rail.style.top = "";
    rail.style.maxHeight = "";
    return;
  }
  const anchor = wrap.querySelector(".workflow-list-table thead") || wrap.querySelector(".panel-head");
  if (!anchor) return;
  const wrapRect = wrap.getBoundingClientRect();
  const top = Math.max(0, anchor.getBoundingClientRect().bottom - wrapRect.top);
  rail.style.top = `${top}px`;
  rail.style.maxHeight = `${Math.max(120, wrapRect.height - top - 8)}px`;
}

function scheduleSummary(schedule) {
  const entries = schedule.entries?.length ? schedule.entries : (schedule.crons || []).map(cron => ({cron, next_run_time:null}));
  if (!entries.length) return `<div class="mono">${t("未配置")}</div><div class="secondary">${esc(schedule.timezone)}</div>`;
  const details = entries.map(entry => `<div class="schedule-popover-row"><span class="mono">${esc(entry.cron)}</span><span>${entry.next_run_time ? fmt(entry.next_run_time) : t("未启用时间")}</span></div>`).join("");
  return `<div class="schedule-summary" tabindex="0" onmouseenter="showSchedulePopover(this)" onmouseleave="hideSchedulePopover()" onfocus="showSchedulePopover(this)" onblur="hideSchedulePopover()"><div class="mono schedule-current">${esc(schedule.cron || entries[0].cron)}</div><div class="secondary">${esc(schedule.timezone)}${entries.length > 1 ? ` · ${t("{count} 个定时", {count:entries.length})}` : ""}</div><div class="schedule-popover"><b>${t("全部定时（统一时区：{timezone}）", {timezone:esc(schedule.timezone)})}</b>${details}</div></div>`;
}

function hideSchedulePopover() {
  document.getElementById("scheduleFloatingPopover")?.remove();
}

function showSchedulePopover(summary) {
  hideSchedulePopover();
  const source = summary.querySelector(".schedule-popover");
  if (!source) return;
  const popup = source.cloneNode(true);
  popup.id = "scheduleFloatingPopover";
  popup.classList.add("schedule-floating");
  popup.style.visibility = "hidden";
  document.body.appendChild(popup);
  const anchor = summary.getBoundingClientRect(), margin = 8, gap = 7;
  const popupRect = popup.getBoundingClientRect();
  const left = Math.min(Math.max(anchor.left - 10, margin), Math.max(margin, window.innerWidth - popupRect.width - margin));
  const below = anchor.bottom + gap;
  const above = anchor.top - popupRect.height - gap;
  const top = below + popupRect.height <= window.innerHeight - margin || above < margin ? below : above;
  popup.style.left = `${left}px`;
  popup.style.top = `${Math.max(margin, top)}px`;
  popup.style.maxHeight = `${Math.max(80, window.innerHeight - Math.max(margin, top) - margin)}px`;
  popup.style.visibility = "visible";
}

function setWorkflowSort(key) {
  if (state.workflowSort.key !== key) {
    state.workflowSort = { key, direction: "desc" };
  } else if (state.workflowSort.direction === "desc") {
    state.workflowSort.direction = "asc";
  } else {
    state.workflowSort = { key: "", direction: "asc" };
  }
  state.workflowPage = 1;
  renderWorkflows();
}

function setWorkflowSortFromSelect(value) {
  const [key, direction] = value.split(":");
  state.workflowSort = key ? { key, direction: direction === "asc" ? "asc" : "desc" } : { key: "", direction: "asc" };
  state.workflowPage = 1;
  renderWorkflows();
}

function changeWorkflowPage(page) {
  const totalPages = Math.max(1, Math.ceil(state.workflows.size / state.workflowPageSize));
  if (page < 1 || page > totalPages || page === state.workflowPage) return;
  state.workflowPage = page;
  renderWorkflows();
}

async function loadRuns(resetPage=false) {
  if (resetPage === true) state.runPage = 1;
  const sequence = ++runLoadSequence;
  try {
    const query = new URLSearchParams({ page: state.runPage, page_size: state.runPageSize });
    for (const [key, value] of Object.entries(state.runFilters)) if (value) query.set(key, value);
    const data = await api(`/api/runs?${query}`);
    if (sequence !== runLoadSequence) return;
    state.runs = data.runs;
    state.runPagination = data.pagination;
    state.runPage = data.pagination.page;
    document.getElementById("runRows").innerHTML = data.runs.map(r => {
      const complete = (r.success_count || 0) + (r.failed_count || 0) + (r.handled_count || 0) + (r.skipped_count || 0);
      const percent = r.task_count ? Math.round(complete * 100 / r.task_count) : 0;
      const canExecute = canWorkflow(r.workflow_name, "execute");
      const canEdit = canWorkflow(r.workflow_name, "edit");
      const actions = [];
      if (r.status === "RUNNING") {
        // 停止不是新的执行动作，executor 也可以操作。
        if (canExecute) actions.push(`<button class="button danger" onclick="runAction('${r.run_id}','stop')">${t("停止")}</button>`);
      } else {
        // 重跑/续跑会产生额外执行，只有 maintainer（edit）可用。
        if (canEdit) actions.push(`<button class="button ghost" onclick="runAction('${r.run_id}','rerun')">${t("重跑")}</button>`);
        if (canEdit && r.status === "FAILED" && r.error_message !== "stopped by user") actions.push(`<button class="button" title="${esc(t("按最新配置续跑：从上次失败处继续，已成功的任务不重跑，运行位置以当前配置为准"))}" onclick="runAction('${r.run_id}','resume')">${t("失败续跑")}</button>`);
        if (canEdit) actions.push(`<button class="button danger" onclick="deleteRun('${r.run_id}')">${t("删除")}</button>`);
      }
      const handled = r.handled_count ? ` · ${t("已处理 {count}", {count:r.handled_count})}` : "";
      const triggerLabels = {manual:t("手动"),schedule:t("定时"),rerun:t("重跑"),resume:t("失败续跑")};
      const triggerText = esc(triggerLabels[r.trigger_type] || r.trigger_type || "manual");
      return `<tr><td class="card-cell-name"><div class="workflow-name-cell"><button class="workflow-link" title="${esc(r.workflow_description || r.workflow_name)}" onclick='showRun(${jsArg(r.run_id)})'>${esc(r.workflow_description || r.workflow_name)}</button>${runnerTag(r.runner, r.runner_locations)}</div><div class="workflow-id mono" title="${esc(r.workflow_name)}">${esc(r.workflow_name)}</div></td><td data-label="${esc(t("触发方式"))}">${triggerText}</td><td data-label="${esc(t("状态"))}">${statusBadge(r.status)}</td><td data-label="${esc(t("开始时间"))}"><div>${fmt(r.start_time)}</div><div class="secondary mono" title="${esc(r.run_id)}">${esc(r.run_id)}</div></td><td data-label="${esc(t("任务进度"))}"><div class="progress"><span style="width:${percent}%"></span></div><div class="progress-label">${complete}/${r.task_count || 0} · ${t("成功")} ${r.success_count || 0}${handled}</div></td><td data-label="${esc(t("耗时"))}">${duration(r.start_time, r.end_time)}</td><td class="card-cell-actions"><div class="actions">${actions.join("")}</div></td></tr>`;
    }).join("") || `<tr><td colspan="7" class="empty">${t("没有符合条件的运行记录")}</td></tr>`;
    renderRunPagination();
    renderRunStats(data.stats);
  } catch (error) { if (!error.offline) toast(error.message, true); }
}

function toggleRunStatusFromStat(status) {
  const next = state.runFilters.status === status ? "" : status;
  state.runFilters.status = next;
  const select = document.getElementById("runStatusFilter");
  if (select) select.value = next;
  loadRuns(true);
}

function renderRunStats(stats) {
  const current = state.runFilters.status || "";
  const item = (label, value, className, status) => {
    const active = current === status ? " active" : "";
    return `<button type="button" class="run-stat ${className}${active}" onclick="toggleRunStatusFromStat('${status}')" aria-pressed="${current === status}"><b>${value}</b>${t(label)}</button>`;
  };
  document.getElementById("runStats").innerHTML = stats
    ? item("成功", stats.success, "run-stat-success", "SUCCESS") +
      item("运行中", stats.running, "run-stat-running", "RUNNING") +
      item("失败", stats.failed, "run-stat-failed", "FAILED")
    : "";
}

function renderRunPagination() {
  const p = state.runPagination;
  document.getElementById("runPagination").innerHTML = `<span>${t("共 {total} 条 · 第 {page} / {pages} 页", {total:p.total,page:p.page,pages:p.total_pages})}</span><div><button class="button ghost" ${p.page <= 1 ? "disabled" : ""} onclick="changeRunPage(${p.page - 1})">${t("上一页")}</button><button class="button ghost" ${p.page >= p.total_pages ? "disabled" : ""} onclick="changeRunPage(${p.page + 1})">${t("下一页")}</button></div>`;
}

function changeRunPage(page) {
  if (page < 1 || page > state.runPagination.total_pages || page === state.runPage) return;
  state.runPage = page;
  loadRuns();
}

function setRunFilter(name, value) {
  state.runFilters[name] = value.trim();
  loadRuns(true);
}

function scheduleRunSearch(value) {
  state.runFilters.workflow = value.trim();
  clearTimeout(runSearchTimer);
  runSearchTimer = setTimeout(() => loadRuns(true), 300);
}

async function runWorkflow(name) { try { const r = await api(`/api/workflows/${encodeURIComponent(name)}/run`, {method:"POST", body:"{}"}); toast(t("已提交 {id}",{id:r.run_id})); setTimeout(loadRuns, 250); } catch(e) { toast(e.message, true); } }

let taskRunMenu = null;
function closeTaskRunMenu() { taskRunMenu?.remove(); taskRunMenu = null; }

async function startTaskRun(name, task, scope) {
  try {
    const r = await api(`/api/workflows/${encodeURIComponent(name)}/run`, {method:"POST", body:JSON.stringify({from_task:task, only_task:scope === "only"})});
    toast(t("已提交 {id}",{id:r.run_id}));
    setTimeout(loadRuns, 250);
  } catch(e) { toast(e.message, true); }
}

function openTaskRunMenu(name, task, event) {
  closeTaskRunMenu();
  const menu = document.createElement('div');
  menu.id = 'taskRunMenu';
  menu.className = 'task-run-menu';
  menu.innerHTML = `<strong class="task-run-menu-title" title="${esc(task)}">${esc(task)}</strong><button type="button" data-run-scope="only">${t("执行当前任务")}</button><button type="button" data-run-scope="descendants">${t("执行当前任务及后续")}</button>`;
  document.body.append(menu);
  taskRunMenu = menu;
  menu.querySelectorAll('button[data-run-scope]').forEach(button => {
    button.addEventListener('click', () => { const scope=button.dataset.runScope; closeTaskRunMenu(); startTaskRun(name, task, scope); });
  });
  menu.addEventListener('contextmenu', event => event.preventDefault());
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(event.clientX, window.innerWidth - rect.width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(event.clientY, window.innerHeight - rect.height - 8))}px`;
}

document.addEventListener('mousedown', event => {
  if (taskRunMenu && !taskRunMenu.contains(event.target)) closeTaskRunMenu();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') closeTaskRunMenu();
});

function applySavedTheme() {
  let saved = "default";
  try { saved = localStorage.getItem("dagrunner.theme") || "default"; } catch (_) {}
  if (!["default", "blue", "red", "dark"].includes(saved)) saved = "default";
  setTheme(saved, false);
}

function setTheme(theme, persist = true) {
  const valid = ["default", "blue", "red", "dark"].includes(theme) ? theme : "default";
  if (valid === "default") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", valid);
  if (persist) {
    try { localStorage.setItem("dagrunner.theme", valid); } catch (_) {}
  }
  document.querySelectorAll(".theme-swatch").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.themeChoice === valid);
  });
}

applySavedTheme();

function toggleGearMenu(event) {
  event.stopPropagation();
  const dropdown = document.getElementById("gearDropdown");
  const button = document.getElementById("gearButton");
  if (!dropdown || !button) return;
  const willOpen = dropdown.classList.contains("hidden");
  dropdown.classList.toggle("hidden", !willOpen);
  button.setAttribute("aria-expanded", String(willOpen));
}

function closeGearMenu() {
  const dropdown = document.getElementById("gearDropdown");
  const button = document.getElementById("gearButton");
  if (!dropdown || dropdown.classList.contains("hidden")) return;
  dropdown.classList.add("hidden");
  if (button) button.setAttribute("aria-expanded", "false");
}

document.addEventListener("click", event => {
  const menu = document.getElementById("gearMenu");
  if (menu && !menu.contains(event.target)) closeGearMenu();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") closeGearMenu();
});

function updateGearLanguageMeta() {
  const meta = document.getElementById("gearLanguageMeta");
  if (meta) meta.textContent = currentLanguage === "zh-CN" ? "English" : "中文";
}

async function openAbout() {
  closeGearMenu();
  try {
    const data = await api("/api/system/overview");
    const version = data?.version || "";
    const rows = (data?.satellites || []).map(item => {
      const state = item.status?.status === "online" ? "online" : item.status?.status === "error" ? "error" : "offline";
      const label = state === "online" ? t("在线") : state === "error" ? t("异常") : t("离线");
      const agent = item.status?.agent_version;
      return `<div class="about-satellite-row"><span class="mono">${esc(item.alias)}</span><span class="secondary">${esc(agent ? `v${agent}` : "—")}</span><span class="satellite-status ${state}">${label}</span></div>`;
    }).join("");
    openModal(t("关于"), `<div class="about-panel">
      <div class="about-brand"><img src="/static/dagrunner-logo-refined.svg" alt=""><div><strong>DAG Runner Console</strong><span class="form-hint">${t("工作流、定时计划和运行状态集中管理")}</span></div></div>
      <div class="about-version"><span>${t("当前版本")}</span><b>${esc(version ? `v${version}` : "—")}</b></div>
      ${rows ? `<div class="about-satellites">${rows}</div>` : ""}
    </div>`, false, "compact");
  } catch (e) { toast(e.message, true); }
}

updateGearLanguageMeta();

async function logout() { try { const result=await api("/api/auth/logout",{method:"POST",body:"{}"}); window.location.replace(result.redirect || "/login"); } catch(e) { if (!e.loginRequired) toast(e.message,true); } }

let notifierChannelsCache = [];
let notifierEditing = null;

async function openNotifierSettings() {
  notifierEditing = null;
  try {
    const data = await api("/api/notifier/channels");
    notifierChannelsCache = data.channels || [];
    renderNotifierSettingsModal();
  } catch (e) { toast(e.message, true); }
}

function notifierChannelSummary(ch) {
  const cfg = ch.config || {};
  if (ch.channel_type === "email") {
    return `${cfg.from || "—"} · ${cfg.host || "—"}:${cfg.port || 465}`;
  }
  const url = String(cfg.webhook_url || "");
  return url.length > 48 ? `${url.slice(0, 48)}…` : (url || "—");
}

function renderNotifierChannelForm(editing) {
  const isNew = !editing.id;
  const type = editing.channel_type;
  const cfg = editing.config || {};
  const fields = type === "email"
    ? `<div class="channel-form-grid">
        <label>${t("SMTP 主机")}<input id="chHost" required value="${esc(cfg.host || "")}" placeholder="smtp.example.com"></label>
        <label>${t("端口")}<input id="chPort" required type="number" min="1" max="65535" value="${esc(cfg.port ?? 465)}"></label>
        <label>${t("用户名")}<input id="chUser" value="${esc(cfg.username || "")}" autocomplete="username"></label>
        <label>${t("密码 / 授权码")}<input id="chPass" type="password" autocomplete="new-password" placeholder="${esc(cfg.has_password ? t("已保存；留空不修改") : t("请输入密码或授权码"))}"></label>
        <label class="channel-wide">${t("发件邮箱")}<input id="chFrom" required type="email" value="${esc(cfg.from || "")}" placeholder="sender@example.com"></label>
        <label>${t("连接安全")}<select id="chSecurity" class="ui-select">
          <option value="ssl"${(cfg.security || "ssl") === "ssl" ? " selected" : ""}>SSL/TLS</option>
          <option value="starttls"${cfg.security === "starttls" ? " selected" : ""}>STARTTLS</option>
          <option value="plain"${cfg.security === "plain" ? " selected" : ""}>无加密</option>
        </select></label>
        <label>${t("超时（秒）")}<input id="chTimeout" required type="number" min="1" step="0.1" value="${esc(cfg.timeout ?? 10)}"></label>
        <label class="channel-wide">${t("主题前缀")}<input id="chPrefix" value="${esc(cfg.subject_prefix || "[DAG Runner]")}"></label>
      </div>`
    : `<div class="channel-form-grid">
        <label class="channel-wide">Webhook URL<input id="chWebhook" required type="url" value="${esc(cfg.webhook_url || "")}" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..."></label>
        <label class="channel-wide">签名密钥 Secret（可选）<input id="chSecret" type="password" autocomplete="new-password" placeholder="${esc(cfg.has_secret ? "已保存；留空不修改" : "开启签名校验时填写")}"></label>
        <label>${t("主题前缀")}<input id="chPrefix" value="${esc(cfg.subject_prefix || "[DAG Runner]")}"></label>
        <label>${t("超时（秒）")}<input id="chTimeout" required type="number" min="1" step="0.1" value="${esc(cfg.timeout ?? 10)}"></label>
        <label class="channel-wide">默认 @手机号（可选，逗号分隔）<input id="chMobiles" value="${esc((cfg.mention_mobiles || []).join(", "))}" placeholder="13800138000"></label>
      </div>`;
  return `<div class="notif-form-card">
    <div class="notif-form-head">
      <strong>${isNew ? (type === "feishu" ? "新增飞书渠道" : "新增邮箱渠道") : `编辑渠道 · ${esc(editing.name)}`}</strong>
      <button type="button" class="button ghost small" onclick="cancelNotifierEdit()">返回列表</button>
    </div>
    <div class="channel-form-grid">
      <label>渠道名称<input id="chName" required value="${esc(editing.name || "")}" placeholder="${type === "feishu" ? "如：运维告警飞书群" : "如：公司发件邮箱"}"></label>
      <label class="notif-enable-check"><input id="chEnabled" type="checkbox"${editing.enabled !== false ? " checked" : ""}> 启用该渠道</label>
    </div>
    ${fields}
    <div class="notif-form-actions">
      <button type="button" class="button" onclick="saveNotifierChannel()">${t("保存")}</button>
      <button type="button" class="button ghost" onclick="cancelNotifierEdit()">${t("取消")}</button>
    </div>
  </div>`;
}

function renderNotifierSettingsModal() {
  const listHtml = notifierChannelsCache.length
    ? notifierChannelsCache.map(ch => `
      <div class="notif-channel-row">
        <div class="notif-channel-info">
          <div class="notif-channel-title">
            <span class="notif-type-tag ${esc(ch.channel_type)}">${ch.channel_type === "feishu" ? "飞书" : "邮箱"}</span>
            <strong>${esc(ch.name)}</strong>
            <span class="badge ${ch.enabled ? "enabled" : "disabled"}">${ch.enabled ? "启用" : "停用"}</span>
          </div>
          <div class="notif-channel-sub mono">${esc(notifierChannelSummary(ch))}</div>
        </div>
        <div class="notif-channel-actions">
          <button type="button" class="button ghost small" onclick='testNotifierChannel(${jsArg(ch.id)})'>测试</button>
          <button type="button" class="button ghost small" onclick='startEditNotifierChannel(${jsArg(ch.id)})'>编辑</button>
          <button type="button" class="button danger small" onclick='removeNotifierChannel(${jsArg(ch.id)})'>删除</button>
        </div>
      </div>`).join("")
    : `<div class="notif-empty">暂无通知渠道，请点击上方按钮添加邮箱或飞书渠道（配置保存在数据库中）。</div>`;

  const body = notifierEditing
    ? `<div class="channel-editor">${renderNotifierChannelForm(notifierEditing)}</div>`
    : `<div class="channel-editor">
        <div class="notif-toolbar">
          <span class="form-hint" style="margin:0">支持配置任意数量的邮箱 SMTP 与飞书自定义机器人渠道。</span>
          <div class="notif-toolbar-btns">
            <button type="button" class="button small" onclick="startAddNotifierChannel('email')">＋ 新增邮箱</button>
            <button type="button" class="button small" onclick="startAddNotifierChannel('feishu')">＋ 新增飞书</button>
          </div>
        </div>
        <div class="notif-channel-list">${listHtml}</div>
      </div>`;

  const existing = document.querySelector("#modalBody .channel-editor");
  const modal = document.getElementById("modal");
  if (existing && modal && !modal.classList.contains("hidden")) {
    existing.outerHTML = body;
    return;
  }
  openModal(t("通知渠道管理"), body, true, "compact");
}

function startAddNotifierChannel(type) {
  notifierEditing = {
    id: null,
    name: "",
    channel_type: type,
    enabled: true,
    config: type === "email"
      ? { host: "", port: 465, username: "", from: "", security: "ssl", timeout: 10, subject_prefix: "[DAG Runner]" }
      : { webhook_url: "", secret: "", mention_mobiles: [], timeout: 10, subject_prefix: "[DAG Runner]" },
  };
  renderNotifierSettingsModal();
}

function startEditNotifierChannel(id) {
  const found = notifierChannelsCache.find(item => item.id === id);
  if (!found) return;
  notifierEditing = JSON.parse(JSON.stringify(found));
  renderNotifierSettingsModal();
}

function cancelNotifierEdit() {
  notifierEditing = null;
  renderNotifierSettingsModal();
}

async function saveNotifierChannel() {
  if (!notifierEditing) return;
  const type = notifierEditing.channel_type;
  const nameInput = document.getElementById("chName");
  if (!nameInput.checkValidity()) { nameInput.reportValidity(); nameInput.focus(); return; }
  let config;
  if (type === "email") {
    for (const id of ["chHost", "chPort", "chFrom", "chTimeout"]) {
      const el = document.getElementById(id);
      if (!el.checkValidity()) { el.reportValidity(); el.focus(); return; }
    }
    config = {
      host: document.getElementById("chHost").value.trim(),
      port: Number(document.getElementById("chPort").value),
      username: document.getElementById("chUser").value.trim(),
      password: document.getElementById("chPass").value,
      from: document.getElementById("chFrom").value.trim(),
      security: document.getElementById("chSecurity").value,
      timeout: Number(document.getElementById("chTimeout").value),
      subject_prefix: document.getElementById("chPrefix").value.trim() || "[DAG Runner]",
    };
  } else {
    for (const id of ["chWebhook", "chTimeout"]) {
      const el = document.getElementById(id);
      if (!el.checkValidity()) { el.reportValidity(); el.focus(); return; }
    }
    const mobiles = document.getElementById("chMobiles").value.split(/[\n,;]+/).map(v => v.trim()).filter(Boolean);
    config = {
      webhook_url: document.getElementById("chWebhook").value.trim(),
      secret: document.getElementById("chSecret").value.trim(),
      mention_mobiles: mobiles,
      timeout: Number(document.getElementById("chTimeout").value),
      subject_prefix: document.getElementById("chPrefix").value.trim() || "[DAG Runner]",
    };
  }
  const payload = {
    name: nameInput.value.trim(),
    channel_type: type,
    enabled: document.getElementById("chEnabled").checked,
    config,
  };
  try {
    if (notifierEditing.id) {
      await api(`/api/notifier/channels/${encodeURIComponent(notifierEditing.id)}`, { method: "PUT", body: JSON.stringify(payload) });
      toast("通知渠道已更新");
    } else {
      await api("/api/notifier/channels", { method: "POST", body: JSON.stringify(payload) });
      toast("通知渠道已添加");
    }
    notifierEditing = null;
    const data = await api("/api/notifier/channels");
    notifierChannelsCache = data.channels || [];
    renderNotifierSettingsModal();
    loadWorkflows();
  } catch (e) { toast(e.message, true); }
}

async function removeNotifierChannel(id) {
  const found = notifierChannelsCache.find(item => item.id === id);
  if (!await confirmDelete(`删除通知渠道「${found?.name || id}」？`, "已绑定该渠道的工作流通知规则也会同步移除。")) return;
  try {
    await api(`/api/notifier/channels/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("通知渠道已删除");
    const data = await api("/api/notifier/channels");
    notifierChannelsCache = data.channels || [];
    renderNotifierSettingsModal();
    loadWorkflows();
  } catch (e) { toast(e.message, true); }
}

async function testNotifierChannel(id) {
  const found = notifierChannelsCache.find(item => item.id === id);
  if (!found) return;
  let target = "";
  if (found.channel_type === "email") {
    const input = prompt("请输入测试收件邮箱（留空默认发给发件邮箱）：", found.config?.from || "");
    if (input === null) return;
    target = input.trim();
  }
  try {
    await api(`/api/notifier/channels/${encodeURIComponent(id)}/test`, { method: "POST", body: JSON.stringify({ target }) });
    toast("测试消息已发送");
  } catch (e) { toast(e.message, true); }
}

let satelliteConfigs = [];
let satelliteServiceEnabled = true;
let satelliteEditing = null;

function renderSatelliteStatus(satellites, enabled) {
  satelliteConfigs = satellites || [];
  satelliteServiceEnabled = enabled !== false;
  const dot = document.getElementById("satelliteStatusDot");
  const button = document.getElementById("satelliteSettingsButton");
  if (button) {
    button.disabled = !satelliteServiceEnabled;
    button.title = satelliteServiceEnabled ? "" : t("卫星服务未启用：请在主程序配置中把 satellite.enabled 设为 true，然后重启主程序");
  }
  if (!dot) return;
  if (!satelliteServiceEnabled) { dot.className = "satellite-dot none"; dot.title = t("卫星服务未启用"); return; }
  if (!satelliteConfigs.length) { dot.className = "satellite-dot none"; dot.title = t("未配置卫星"); return; }
  const online = satelliteConfigs.filter(s => s.status?.status === "online").length;
  dot.className = "satellite-dot " + (online === satelliteConfigs.length ? "ok" : online === 0 ? "bad" : "warn");
  dot.title = t("{ok}/{total} 台卫星在线", {ok: online, total: satelliteConfigs.length});
}

async function openSatelliteSettings() {
  const button = document.getElementById("satelliteSettingsButton");
  if (button && button.disabled) return;
  try {
    const data = await api("/api/satellites");
    satelliteConfigs = data.satellites || [];
    satelliteServiceEnabled = data.enabled !== false;
    satelliteEditing = null;
    renderSatelliteSettings();
  } catch(e) { toast(e.message, true); }
}

function satelliteStatusBadge(satellite) {
  const status = satellite.status || {};
  const state = status.status === "online" ? "online" : status.status === "error" ? "error" : "offline";
  const label = state === "online" ? t("在线") : state === "error" ? t("异常") : t("离线");
  return `<span class="satellite-status ${state}">${label}</span>`;
}

function satelliteRowHtml(satellite) {
  const status = satellite.status || {};
  const meta = [
    satellite.mode || "",
    status.platform || "",
    status.hostname || "",
    status.agent_version || "",
    status.last_seen ? String(status.last_seen).replace("T", " ") : "",
  ].filter(Boolean).join(" · ");
  const disabled = satelliteEditing !== null ? "disabled" : "";
  return `<div class="satellite-row" data-alias="${esc(satellite.alias)}">
    <span class="satellite-row-alias">${esc(satellite.alias)}</span>
    ${satelliteStatusBadge(satellite)}
    <span class="satellite-row-meta" title="${esc(meta)}">${esc(meta)}</span>
    <span class="satellite-row-actions">
      <button class="button ghost" type="button" onclick='editSatellitePanel(${jsArg(satellite.alias)})' ${disabled}>${t("编辑")}</button>
      <button class="button ghost" type="button" onclick='testSatelliteByAlias(${jsArg(satellite.alias)})' ${disabled}>${t("检查连通")}</button>
      <button class="button ghost" type="button" onclick='downloadSatelliteConfigByAlias(${jsArg(satellite.alias)})' ${disabled}>${t("下载配置")}</button>
      <button class="button danger" type="button" onclick='deleteSatellitePanelByAlias(${jsArg(satellite.alias)})' ${disabled}>${t("删除")}</button>
    </span>
  </div>`;
}

function satellitePanelHtml(satellite, isNew) {
  const s = satellite || {alias:"", name:"", mode:"auto", url:"", host_key_policy:"accept-new", cert_fingerprint:"", has_token:false, status:{}};
  const status = s.status || {};
  const badge = isNew ? `<span class="satellite-status">${t("未测试")}</span>` : satelliteStatusBadge(s);
  const meta = isNew ? "" : `<span class="satellite-meta">${esc(status.platform || "")}${status.hostname ? " · " + esc(status.hostname) : ""}${status.agent_version ? " · " + esc(status.agent_version) : ""}${status.last_seen ? " · " + esc(status.last_seen) : ""}</span>`;
  const buttons = isNew
    ? `<button class="button ghost" type="button" onclick="testSatellitePanel(this)">${t("检查连通")}</button><button class="button" type="button" onclick="saveSatellitePanel(this)">${t("确认")}</button><button class="button ghost" type="button" onclick="cancelSatellitePanel()">${t("取消")}</button>`
    : `<button class="button" type="button" onclick="saveSatellitePanel(this)">${t("保存")}</button><button class="button ghost" type="button" onclick="cancelSatellitePanel()">${t("取消")}</button>`;
  return `<div class="satellite-panel${isNew ? " satellite-panel-new" : ""}">
    <div class="satellite-panel-head"><input class="satellite-alias" value="${esc(s.alias)}" placeholder="${t("别名（唯一）")}" ${isNew ? "" : "readonly"}>${badge}${meta}</div>
    <div class="satellite-form-grid">
      <label>${t("名称")}<input class="satellite-name" value="${esc(s.name)}" placeholder="${t("可选")}"></label>
      <label>${t("模式")}<select class="satellite-mode ui-select">
        <option value="auto"${s.mode==="auto"?" selected":""}>${t("自动（推荐）")}</option>
        <option value="forward"${s.mode==="forward"?" selected":""}>${t("前向（主程序连接卫星）")}</option>
        <option value="reverse"${s.mode==="reverse"?" selected":""}>${t("反向（卫星连接主程序）")}</option>
      </select></label>
      <label class="satellite-wide">${t("卫星 URL（前向模式）")}<input class="satellite-url" value="${esc(s.url)}" placeholder="https://192.168.1.10:7121"></label>
      <label class="satellite-wide">${t("共享令牌")}<input class="satellite-token" value="" placeholder="${s.has_token ? t("已保存，留空保持不变") : t("留空自动生成")}"></label>
      <label>${t("证书信任")}<select class="satellite-policy ui-select">
        <option value="accept-new"${s.host_key_policy!=="pinned"?" selected":""}>accept-new（${t("自动固定指纹")}）</option>
        <option value="pinned"${s.host_key_policy==="pinned"?" selected":""}>pinned（${t("校验固定指纹")}）</option>
      </select></label>
      <label class="satellite-wide">${t("证书指纹（pinned 时填写）")}<input class="satellite-fingerprint" value="${esc(s.cert_fingerprint)}" placeholder="64 位十六进制"></label>
    </div>
    <div class="satellite-panel-actions">${buttons}</div>
  </div>`;
}

function renderSatelliteSettings() {
  const editing = satelliteEditing;
  const rows = satelliteConfigs.filter(item => item.alias !== editing).map(satelliteRowHtml).join("");
  let editingPanel = "";
  if (editing === "__new__") editingPanel = satellitePanelHtml(null, true);
  else if (editing) {
    const found = satelliteConfigs.find(item => item.alias === editing);
    if (found) editingPanel = satellitePanelHtml(found, false);
  }
  const empty = !satelliteConfigs.length && editing !== "__new__"
    ? `<p class="form-hint">${t("还没有配置卫星节点")}</p>` : "";
  const locked = editing !== null;
  openModal(t("卫星节点管理"), `<div class="satellite-editor">
    <div class="satellite-global">
      <div class="satellite-global-head"><strong>${t("加密通信")}</strong><span class="form-hint">${t("主程序与卫星版之间使用 HTTPS 加密通信，并通过共享令牌做挑战-响应认证；首次连接自动固定证书指纹，也可以切换到 pinned 模式填写指纹。")}</span></div>
    </div>
    <div class="satellite-list-head"><strong>${t("卫星节点")}</strong><button class="button ghost" type="button" onclick="addSatellitePanel()" ${locked ? "disabled" : ""}>＋ ${t("新增卫星")}</button></div>
    <div id="satelliteList" class="satellite-list">${rows}${editingPanel}${empty}</div>
    <button class="button" onclick="closeModal()">${t("关闭")}</button>
  </div>`, false, "compact");
}

function addSatellitePanel() {
  if (satelliteEditing !== null) { toast(t("请先保存或取消当前编辑"), true); return; }
  satelliteEditing = "__new__";
  renderSatelliteSettings();
}

function editSatellitePanel(alias) {
  if (satelliteEditing !== null && satelliteEditing !== alias) { toast(t("请先保存或取消当前编辑"), true); return; }
  satelliteEditing = alias;
  renderSatelliteSettings();
}

function cancelSatellitePanel() {
  satelliteEditing = null;
  renderSatelliteSettings();
}

function readSatellitePanel(panel) {
  return {
    alias: panel.querySelector(".satellite-alias").value.trim(),
    name: panel.querySelector(".satellite-name").value.trim(),
    mode: panel.querySelector(".satellite-mode").value,
    url: panel.querySelector(".satellite-url").value.trim(),
    token: panel.querySelector(".satellite-token").value.trim(),
    host_key_policy: panel.querySelector(".satellite-policy").value,
    cert_fingerprint: panel.querySelector(".satellite-fingerprint").value.trim(),
  };
}

function satellitePayload(values) {
  return {
    name: values.name,
    mode: values.mode,
    url: values.url,
    token: values.token,
    host_key_policy: values.host_key_policy,
    cert_fingerprint: values.cert_fingerprint,
    generate_token: false,
  };
}

function markSatelliteFieldErrors(panel, errors) {
  panel.querySelectorAll(".satellite-field-error").forEach(element => element.classList.remove("satellite-field-error"));
  const map = {alias:".satellite-alias", name:".satellite-name", mode:".satellite-mode", url:".satellite-url", host_key_policy:".satellite-policy", cert_fingerprint:".satellite-fingerprint"};
  for (const [field, selector] of Object.entries(map)) {
    if (errors?.[field]) { const input = panel.querySelector(selector); if (input) input.classList.add("satellite-field-error"); }
  }
}

async function saveSatellitePanel(button) {
  const panel = button.closest(".satellite-panel");
  const values = readSatellitePanel(panel);
  if (!values.alias) { toast(t("别名不能为空"), true); return; }
  try {
    const result = await api(`/api/satellites/${encodeURIComponent(values.alias)}`, {method:"PUT", body:JSON.stringify(satellitePayload(values))});
    toast(result.token ? t("卫星配置已保存，并已生成新的共享令牌") : t("卫星配置已保存"));
    satelliteEditing = null;
    await openSatelliteSettings();
  } catch(e) { markSatelliteFieldErrors(panel, e.data?.field_errors); toast(e.message, true); }
}

async function testSatellitePanel(button) {
  const panel = button.closest(".satellite-panel");
  const values = readSatellitePanel(panel);
  if (!values.alias) { toast(t("请先填写别名"), true); return; }
  const badge = panel.querySelector(".satellite-status");
  const original = button.textContent;
  button.disabled = true; button.textContent = t("检查中…");
  try {
    // Persist first so the check reflects exactly what the form shows.
    await api(`/api/satellites/${encodeURIComponent(values.alias)}`, {method:"PUT", body:JSON.stringify(satellitePayload(values))});
    const result = await api(`/api/satellites/${encodeURIComponent(values.alias)}/test`, {method:"POST", body:"{}"});
    if (result.reachable) { toast(t("连接成功（{mode}）", {mode: result.mode})); }
    else { toast(t("连接失败：{error}", {error: result.error_message || ""}), true); }
    if (badge) { badge.className = "satellite-status " + (result.reachable ? "online" : "offline"); badge.textContent = result.reachable ? t("在线") : t("离线"); }
  } catch(e) { toast(e.message, true); }
  finally { button.disabled = false; button.textContent = original; }
}

async function testSatelliteByAlias(alias) {
  try {
    const result = await api(`/api/satellites/${encodeURIComponent(alias)}/test`, {method:"POST", body:"{}"});
    if (result.reachable) { toast(t("连接成功（{mode}）", {mode: result.mode})); }
    else { toast(t("连接失败：{error}", {error: result.error_message || ""}), true); }
    await openSatelliteSettings();
  } catch(e) { toast(e.message, true); }
}

async function deleteSatellitePanelByAlias(alias) {
  if (!await confirmDelete(t("删除卫星节点“{alias}”？",{alias}), t("删除后无法恢复。"))) return;
  try {
    await api(`/api/satellites/${encodeURIComponent(alias)}`, {method:"DELETE"});
    toast(t("已删除卫星节点 {alias}",{alias}));
    satelliteEditing = null;
    await openSatelliteSettings();
  } catch(e) { toast(e.message, true); }
}

function downloadSatelliteConfigByAlias(alias) {
  window.location.href = `/api/satellites/${encodeURIComponent(alias)}/config`;
}

function setTopLevelRunner(definition, runner) {
  const lines = String(definition ?? "").split(/\r?\n/);
  const out = [];
  let replaced = false;
  for (const line of lines) {
    if (!replaced && /^runner\s*:/.test(line)) {
      replaced = true;
      if (runner && runner !== "local") out.push(`runner: ${runner}`);
      continue;
    }
    out.push(line);
  }
  if (!replaced && runner && runner !== "local") out.unshift(`runner: ${runner}`);
  return out.join("\n");
}

function onRunnerSelectChange(editorId, runner) {
  const textarea = document.getElementById(editorId);
  if (!textarea) return;
  if (typeof workflowEditor !== "undefined" && workflowEditor && workflowEditor.input?.id === editorId && workflowEditor.mode === "form" && workflowEditor.formModel) {
    workflowEditor.formModel.runner = runner === "local" ? "" : runner;
    if (typeof renderTaskForm === "function") renderTaskForm();
    if (typeof renderDraftPreviewFromModel === "function") renderDraftPreviewFromModel(workflowEditor);
    if (typeof scheduleFormSync === "function") scheduleFormSync();
    return;
  }
  textarea.value = setTopLevelRunner(textarea.value, runner);
  syncYamlHighlight(editorId);
  queueWorkflowPreview(editorId, 0);
}

function detectTopLevelRunner(definition) {
  const match = /^runner\s*:\s*(\S+)\s*$/m.exec(String(definition ?? ""));
  return match ? match[1] : "";
}

function runnerOptions(selected) {
  const options = [`<option value="local"${selected==="local"?" selected":""}>${t("本机")}</option>`];
  const known = new Set(["local"]);
  for (const satellite of satelliteConfigs) {
    known.add(satellite.alias);
    options.push(`<option value="${esc(satellite.alias)}"${selected===satellite.alias?" selected":""}>${esc(satellite.alias)}</option>`);
  }
  // Keep a runner that is not in the current satellite list selectable,
  // otherwise saving would silently reset it to 本机.
  if (selected && selected !== "local" && !known.has(selected)) {
    options.push(`<option value="${esc(selected)}" selected>${esc(selected)}（${t("未配置")}）</option>`);
  }
  return options.join("");
}
function runnerTag(runner, locations) {
  const list = locations && locations.length ? locations : [{alias: runner || "local", count: null}];
  const satellites = list.filter(item => item.alias && item.alias !== "local");
  const hasLocal = list.some(item => item.alias === "local" || !item.alias);
  let label = "";
  if (satellites.length === 1 && !hasLocal) label = satellites[0].alias;
  else if (satellites.length > 0) label = "mixed";
  if (!label) return "";
  const parts = list.map(item => `${item.alias === "local" || !item.alias ? t("本机") : esc(item.alias)}${item.count == null ? "" : ` (${item.count})`}`);
  return `<span class="runner-tag" title="${esc(parts.join(", "))}">${esc(label)}</span>`;
}

let pendingImportFilename = "workflow.yaml";
let pendingImportSuccessVerb = "导入";
let pendingWorkflowId = "";
function splitWorkflowName(definition) {
  const source = String(definition ?? "");
  const match = source.match(/^name:[ \t]*(.*?)[ \t]*$/m);
  if (!match) return {name: "", definition: source};
  let value = match[1];
  if (value.length > 1 && ((value[0] === '"' && value.endsWith('"')) || (value[0] === "'" && value.endsWith("'")))) value = value.slice(1, -1);
  return {name: value, definition: source.replace(/^name:[^\n]*\n?/m, "")};
}
function workflowNameInput(id, value) {
  return `<label>${t("工作流名称")}<input id="${id}" value="${esc(value)}" placeholder="${t("用于列表和通知显示")}"></label>`;
}
function openWorkflowImportEditor(title, definition, nextId, submitLabel, sourceInfo=null) {
  pendingWorkflowId = nextId;
  const split = splitWorkflowName(definition);
  const source = sourceInfo ? `<div class="import-source"><span>${t("识别来源")}</span><b>${esc(sourceInfo.source_label)}</b></div>` : "";
  const warnings = sourceInfo?.warnings?.length ? `<div class="import-warnings"><b>${t("转换提示")}</b>${sourceInfo.warnings.map(warning => `<span>${esc(warning)}</span>`).join("")}</div>` : "";
  openModal(title, `<div class="form-stack">${source}${warnings}<div class="form-grid"><label>${t("拟分配 ID")}<input value="${esc(nextId)}" readonly></label>${workflowNameInput("importWorkflowName",split.name)}</div><label>${t("运行位置")}<select id="importRunner" class="ui-select" onchange="onRunnerSelectChange('importDefinition', this.value)">${runnerOptions(detectTopLevelRunner(split.definition) || "local")}</select></label><label class="yaml-editor-label">${t("工作流 YAML")}${yamlEditor("importDefinition",split.definition,true)}</label><span class="form-hint"><i class="yaml-key-sample">${t("高亮字段")}</i>${t("会在导入或运行时使用；migration 仅作来源信息，导入后的定时由数据库单独管理。")}</span><button class="button" onclick="submitImportWorkflow()">${esc(submitLabel)}</button></div>`, false);
}
async function openNewWorkflow() {
  pendingImportFilename = "dagr_example_pipeline.yaml";
  pendingImportSuccessVerb = "新增";
  try {
    const [definition,nextId]=await Promise.all([api("/api/workflows/example-definition"),api("/api/workflows/next-id")]);
    openWorkflowImportEditor(t("创建"),definition,nextId.id,t("新增"));
  } catch(e) { toast(t("加载示例工作流失败：{error}",{error:e.message}),true); }
}
function openImportWorkflow() {
  openModal(t("新建工作流"), `<div class="import-choice">
    <button type="button" class="import-choice-option" onclick="openNewWorkflow()"><strong>${t("创建")}</strong><span>${t("从示例工作流开始创建，可编辑 YAML")}</span></button>
    <button type="button" class="import-choice-option" onclick="openImportWorkflowFile('.yaml,.yml,text/yaml,application/yaml')"><strong>${t("DAG Runner YAML 文件")}</strong><span>${t("选择「导出 YAML」生成的 .yaml 文件")}</span></button>
    <button type="button" class="import-choice-option" onclick="openImportWorkflowFile('.xml,text/xml,application/xml')"><strong>${t("Windows 计划任务文件")}</strong><span>${t("选择导出的 .xml 计划任务文件")}</span></button>
    <button type="button" class="import-choice-option" onclick="openImportWorkflowFile('.json,application/json')"><strong>${t("DolphinScheduler 导出文件")}</strong><span>${t("选择导出的 .json 工作流定义")}</span></button>
    <div class="import-choice-option">
      <label>${t("Crontab 任务")}<input id="crontabInput" class="crontab-input" placeholder="${t("30 16 * * mon-fri /path/to/command arg")}" spellcheck="false"></label>
      <span class="form-hint">${t("粘贴单行 crontab：5 个时间字段 + 要执行的命令")}</span>
      <button type="button" class="button" onclick="submitCrontabImport()">${t("确认")}</button>
    </div>
  </div>`, false, "compact");
}
function openImportWorkflowFile(accept=".yaml,.yml,.json,.xml,text/yaml,application/json,text/xml,application/xml") {
  closeModal();
  const input=document.createElement("input");
  input.type="file"; input.accept=accept;
  input.onchange=async () => {
    const file=input.files[0]; if (!file) return;
    pendingImportSuccessVerb="导入";
    try {
      const form=new FormData(); form.append("file",file);
      const [preview,nextId]=await Promise.all([api("/api/workflows/import-preview",{method:"POST",body:form}),api("/api/workflows/next-id")]);
      pendingImportFilename=preview.filename || file.name;
      openWorkflowImportEditor(t("导入并编辑工作流"),preview.definition,nextId.id,t("导入"),preview);
    } catch(e) { toast(t("识别或转换文件失败：{error}",{error:e.message}),true); }
  };
  input.click();
}
async function submitCrontabImport() {
  const line=document.getElementById("crontabInput").value.trim();
  if (!line) { toast(t("请粘贴 crontab 任务"),true); return; }
  try {
    const [preview,nextId]=await Promise.all([api("/api/workflows/crontab-preview",{method:"POST",body:JSON.stringify({line})}),api("/api/workflows/next-id")]);
    pendingImportFilename=preview.filename || "dagr_crontab.yaml";
    pendingImportSuccessVerb="新增";
    openWorkflowImportEditor(t("创建"),preview.definition,nextId.id,t("新增"),preview);
  } catch(e) { toast(t("识别或转换 crontab 失败：{error}",{error:e.message}),true); }
}
async function submitImportWorkflow() {
  if (typeof clearFormError === "function") clearFormError();
  const saveError = typeof workflowEditorSaveError === "function" ? workflowEditorSaveError() : null;
  if (saveError) { showFormError(saveError); return; }
  try { await flushWorkflowModelSync(); } catch (e) { showFormError(e.message); return; }
  const postSyncError = typeof workflowEditorSaveError === "function" ? workflowEditorSaveError(true) : null;
  if (postSyncError) { showFormError(postSyncError); return; }
  const definition=normalizeYamlNewlines(document.getElementById("importDefinition").value);
  const file=new File([definition],pendingImportFilename,{type:"application/yaml"});
  const form=new FormData(); form.append("file",file); form.append("workflow_id",pendingWorkflowId); form.append("name",document.getElementById("importWorkflowName").value.trim()); form.append("runner",document.getElementById("importRunner")?.value || "local");
  try { const result=await api("/api/workflows/import",{method:"POST",body:form}); closeModal(); const message=pendingImportSuccessVerb === "新增" ? "已新增 {name}（{id}）" : "已导入 {name}（{id}）"; toast(t(message,{name:result.name,id:result.id})); await loadWorkflows(); } catch(e) { showFormError(e.message); toast(e.message,true); }
}
function openWorkflowFormModal(name, definition, description, readOnly) {
  const split = splitWorkflowName(definition);
  const title = readOnly ? namedId(description, name) : `${namedId(description, name)} · ${t("编辑")}`;
  const disabled = readOnly ? " disabled" : "";
  const submit = readOnly ? "" : `<button class="button" onclick='saveWorkflow(${jsArg(name)})'>${t("保存")}</button>`;
  openModal(title, `<div class="form-stack">
    <div class="form-grid">
      <label>${t("工作流 ID")}<input value="${esc(name)}" readonly></label>
      <label>${t("工作流名称")}<input id="workflowDisplayName" value="${esc(description || split.name)}"${disabled} placeholder="${t("用于列表和通知显示")}"></label>
    </div>
    <label>${t("运行位置")}<select id="workflowRunner" class="ui-select"${disabled} onchange="onRunnerSelectChange('workflowDefinition', this.value)">${runnerOptions(detectTopLevelRunner(split.definition) || "local")}</select></label>
    <label class="yaml-editor-label">${t("工作流 YAML")}${yamlEditor("workflowDefinition",split.definition,false,readOnly)}</label>
    <span class="form-hint"><i class="yaml-key-sample">${t("高亮字段")}</i>${t("会被运行时使用；migration 和 YAML schedule 不参与后续执行。保存后立即写入数据库。")}</span>
    ${submit}
  </div>`, Boolean(readOnly));
}

async function editWorkflow(name) {
  const workflow=state.workflows.get(name); if (!workflow) return toast(t("工作流不存在"),true);
  try {
    const definition=await api(`/api/workflows/${encodeURIComponent(name)}/yaml`);
    openWorkflowFormModal(name, definition, workflow.description || "", false);
  } catch(e) { toast(e.message,true); }
}

async function openReadOnlyWorkflow(name) {
  const workflow=state.workflows.get(name); if (!workflow) return toast(t("工作流不存在"),true);
  try {
    const definition=await api(`/api/workflows/${encodeURIComponent(name)}/yaml`);
    openWorkflowFormModal(name, definition, workflow.description || "", true);
  } catch(e) { toast(e.message,true); }
}

async function setWorkflowOnline(name, online) {
  try {
    const model = await api(`/api/workflows/${encodeURIComponent(name)}/model`);
    const schedule = model.schedule || {};
    const crons = (schedule.crons || []).filter(Boolean);
    if (!crons.length) {
      toast(t("请先配置定时后再上线"), true);
      return;
    }
    await api(`/api/workflows/${encodeURIComponent(name)}/schedule`, {
      method: "PUT",
      body: JSON.stringify({crons, timezone: schedule.timezone || "Asia/Shanghai", enabled: online}),
    });
    startWorkflowResortCountdown(30);
    toast(t(online ? "已上线，定时调度已启用" : "已下线，定时调度已停用"));
    await loadWorkflows();
  } catch (e) { toast(e.message, true); }
}
async function saveWorkflow(name) {
  if (typeof clearFormError === "function") clearFormError();
  const saveError = typeof workflowEditorSaveError === "function" ? workflowEditorSaveError() : null;
  if (saveError) { showFormError(saveError); return; }
  try { await flushWorkflowModelSync(); } catch (e) { showFormError(e.message); return; }
  const postSyncError = typeof workflowEditorSaveError === "function" ? workflowEditorSaveError(true) : null;
  if (postSyncError) { showFormError(postSyncError); return; }
  try {
    await api(`/api/workflows/${encodeURIComponent(name)}`,{method:"PUT",body:JSON.stringify({definition:normalizeYamlNewlines(document.getElementById("workflowDefinition").value),name:document.getElementById("workflowDisplayName").value.trim(),runner:document.getElementById("workflowRunner")?.value || "local"})});
    if (typeof flushScheduleServerSync === "function") await flushScheduleServerSync();
    closeModal(); toast(t("工作流已更新")); await loadWorkflows();
  } catch(e) { showFormError(e.message); toast(e.message,true); }
}
async function runAction(id, action) { if (action === "stop" && !confirm(t("确定停止这个运行实例？当前子进程会被终止。"))) return; try { const r = await api(`/api/runs/${encodeURIComponent(id)}/${action}`, {method:"POST", body:"{}"}); toast(action === "stop" ? t("已发送停止请求") : t("已提交新运行 {id}",{id:r.run_id})); setTimeout(loadRuns, 300); } catch(e) { toast(e.message, true); } }
async function deleteWorkflow(name) {
  const workflow = state.workflows.get(name), label = namedId(workflow?.description, name);
  if (!await confirmDelete(t("删除工作流“{name}”？",{name:label}), t("将同时删除工作流正文、定时配置、全部历史运行记录、任务记录和对应日志，此操作不可恢复。"))) return;
  try {
    await api(`/api/workflows/${encodeURIComponent(name)}`, {method:"DELETE"});
    toast(t("已删除工作流 {name}",{name:label}));
    await loadWorkflows();
  } catch(e) { toast(e.message, true); }
}
async function deleteRun(id) {
  if (!await confirmDelete(t("删除这条运行记录？"), `${id}\n${t("对应的任务日志也会一并删除。")}`)) return;
  try {
    await api(`/api/runs/${encodeURIComponent(id)}`, {method:"DELETE"});
    toast(t("已删除运行记录 {id}",{id}));
    await loadRuns();
  } catch(e) { toast(e.message, true); }
}

let deleteConfirmResolver = null;
function confirmDelete(title, message) {
  if (deleteConfirmResolver) deleteConfirmResolver(false);
  document.getElementById("deleteConfirmTitle").textContent = title;
  document.getElementById("deleteConfirmMessage").textContent = message;
  document.getElementById("deleteConfirmModal").classList.remove("hidden");
  return new Promise(resolve => { deleteConfirmResolver = resolve; });
}
function resolveDeleteConfirm(confirmed) {
  document.getElementById("deleteConfirmModal").classList.add("hidden");
  const resolve = deleteConfirmResolver;
  deleteConfirmResolver = null;
  if (resolve) resolve(confirmed);
}
function closeDeleteConfirmFromBackdrop(event) {
  if (event.target.id === "deleteConfirmModal") resolveDeleteConfirm(false);
}

function showTasks(name) {
  const w = state.workflows.get(name); if (!w) return;
  openModal(`${namedId(w.description, w.name)} · ${t("DAG 任务")}`, `<div class="table-wrap"><table class="card-table tasks-table"><thead><tr><th>${t("任务")}</th><th>${t("依赖")}</th><th>${t("命令 / 类型")}</th><th>${t("启用")}</th></tr></thead><tbody>${w.tasks.map(task => `<tr><td class="card-cell-name"><div class="primary">${esc(task.description || task.name)}</div><div class="workflow-id mono">${esc(task.name)}</div></td><td data-label="${esc(t("依赖"))}">${esc(task.depends.map(id => taskNamedId(w, id)).join(", ") || "—")}</td><td data-label="${esc(t("命令 / 类型"))}" class="mono">${esc(task.type === "condition" ? t("条件分支") : task.command)}</td><td data-label="${esc(t("启用"))}">${task.enabled ? t("是") : t("否")}</td></tr>`).join("")}</tbody></table></div>`);
}

function showDag(name) {
  openWorkflowViewer('workflow', name);
}

const svgTextMeasureContext = document.createElement('canvas').getContext('2d');
function truncateSvgText(text, maxWidth, font) {
  const value=String(text ?? "");
  if (!value) return "";
  svgTextMeasureContext.font=font;
  if (svgTextMeasureContext.measureText(value).width <= maxWidth) return value;
  const ellipsis="…";
  let truncated=value;
  while (truncated.length > 1 && svgTextMeasureContext.measureText(truncated + ellipsis).width > maxWidth) {
    truncated=truncated.slice(0, -1);
  }
  return truncated + ellipsis;
}

function buildDagSvg(tasks, workflowName, showStatuses=false, preview=false, direction='LR', viewer=false, issueMap=null) {
  if (!tasks || !tasks.length) {
    return `<div class="dag-empty">${esc(t("暂无任务节点"))}</div>`;
  }
  const byName = new Map(tasks.map(task => [task.name, task]));
  const levels = new Map();
  const visiting = new Set();
  const cyclicNodes = new Set();
  function getLevel(task) {
    if (levels.has(task.name)) return levels.get(task.name);
    if (visiting.has(task.name)) {
      cyclicNodes.add(task.name);
      return 0;
    }
    visiting.add(task.name);
    const parents = (task.depends || []).filter(name => name !== task.name && byName.has(name));
    let level = 0;
    for (const parentName of parents) {
      if (visiting.has(parentName)) {
        cyclicNodes.add(task.name);
        cyclicNodes.add(parentName);
        continue;
      }
      level = Math.max(level, 1 + getLevel(byName.get(parentName)));
    }
    visiting.delete(task.name);
    levels.set(task.name, level);
    return level;
  }
  tasks.forEach(getLevel);
  const columns = new Map();
  tasks.forEach(task => { const level=levels.get(task.name); if (!columns.has(level)) columns.set(level, []); columns.get(level).push(task); });
  const nodeWidth=190, nodeHeight=68, columnGap=85, rowGap=42, margin=45;
  const maxLevel=Math.max(0, ...levels.values());
  const maxRows=Math.max(1, ...[...columns.values()].map(items => items.length));
  const vertical = direction === 'TB';
  const width=vertical ? margin*2+maxRows*nodeWidth+(maxRows-1)*rowGap : margin*2+(maxLevel+1)*nodeWidth+maxLevel*columnGap;
  const height=vertical ? margin*2+(maxLevel+1)*nodeHeight+maxLevel*columnGap : margin*2+maxRows*nodeHeight+(maxRows-1)*rowGap;
  const positions=new Map();
  for (const [level, items] of columns) {
    const contentHeight=items.length*nodeHeight+(items.length-1)*rowGap;
    const startY=(height-contentHeight)/2;
    const startX=(width-(items.length*nodeWidth+(items.length-1)*rowGap))/2;
    items.forEach((task,index) => positions.set(task.name,vertical
      ? {x:startX+index*(nodeWidth+rowGap),y:margin+level*(nodeHeight+columnGap)}
      : {x:margin+level*(nodeWidth+columnGap),y:startY+index*(nodeHeight+rowGap)}));
  }
  const edges=[];
  tasks.forEach(task => (task.depends || []).forEach(parentName => {
    const from=positions.get(parentName), to=positions.get(task.name);
    if (!from || !to) return;
    const x1=from.x+(vertical ? nodeWidth/2 : nodeWidth), y1=from.y+(vertical ? nodeHeight : nodeHeight/2);
    const x2=to.x+(vertical ? nodeWidth/2 : 0), y2=to.y+(vertical ? 0 : nodeHeight/2), mid=(x1+x2)/2, midY=(y1+y2)/2;
    const curve=vertical ? `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}` : `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
    const isCyclicEdge = cyclicNodes.has(parentName) && cyclicNodes.has(task.name);
    edges.push(`<path class="dag-edge${isCyclicEdge ? ' dag-edge-cycle' : ''}" d="${curve}" marker-end="url(#dagArrow)"/>`);
    const parent = byName.get(parentName);
    const branch = parent.success?.includes(task.name) ? '成立' : parent.failure?.includes(task.name) ? '不成立' : '';
    if (branch) edges.push(`<text class="dag-branch-label" x="${mid}" y="${(y1+y2)/2-8}" text-anchor="middle">${branch}</text>`);
  }));
  const nodes=tasks.map(task => {
    const p=positions.get(task.name);
    const runnerLabel=task.runner && task.runner !== "local" ? task.runner : "";
    const runnerText=runnerLabel.length > 10 ? runnerLabel.slice(0,7)+"..." : runnerLabel;
    const pillWidth=runnerText ? Math.ceil(runnerText.length*6.4)+14 : 0;
    // 标题/ID 按节点可用宽度测量截断，超出显示省略号，避免溢出节点框。
    const title=truncateSvgText(task.description || task.name, nodeWidth-37, '700 12px Inter, "Microsoft YaHei", "Segoe UI", sans-serif');
    const taskId=truncateSvgText(task.name, nodeWidth-21, '400 10.5px "Cascadia Code", Consolas, monospace');
    const status=showStatuses ? (task.status || "PENDING") : "";
    const click=viewer ? `data-task="${esc(task.name)}" onclick='selectViewerTask(${jsArg(task.name)})'` : preview ? `data-task="${esc(task.name)}" onclick='selectPreviewTask(${jsArg(task.name)})'` : showStatuses ? "" : `onclick="selectDagTask('${esc(workflowName)}','${esc(task.name)}')"`;
    // Status is conveyed by the node color only; hover shows the status word.
    const conditionText=task.type === "condition" && task.condition_result ? `→ ${task.condition_result.toUpperCase()}` : "";
    const issues = issueMap && typeof issueMap.get === "function" ? (issueMap.get(task.name) || []) : [];
    const hasIssue = Boolean(issues.length || cyclicNodes.has(task.name));
    const hoverStatus=[status, conditionText, issues.length ? `待完善：${issues.join('；')}` : ""].filter(Boolean).join(" · ");
    // The satellite badge hangs on the outer top-right corner of the node so
    // it never covers the task title inside the box.
    const pill=runnerText ? `<rect class="dag-node-runner" x="${nodeWidth + 14 - pillWidth}" y="-15" width="${pillWidth}" height="15" rx="7"></rect><text class="dag-node-runner-text" x="${nodeWidth + 14 - pillWidth/2}" y="-4" text-anchor="middle">${esc(runnerText)}</text>` : "";
    const issueBadge = hasIssue && preview ? `<g class="dag-node-issue-badge" transform="translate(${nodeWidth - 10}, -6)"><circle cx="0" cy="0" r="8"></circle><text x="0" y="3.5" text-anchor="middle">!</text></g>` : "";
    return `<g class="dag-node ${task.type === "condition" ? "dag-condition" : ""} ${showStatuses ? "dag-run-node" : ""} ${task.enabled ? "dag-enabled" : "dag-disabled"} ${status ? `dag-status-${status.toLowerCase()}` : ""} ${hasIssue && preview ? "dag-node-issue" : ""}" transform="translate(${p.x},${p.y})" ${click}><rect width="${nodeWidth}" height="${nodeHeight}" rx="8"></rect><circle cx="17" cy="19" r="5"></circle><text class="dag-node-title" x="29" y="24">${esc(title)}</text>${pill}${issueBadge}<text class="dag-node-id" x="15" y="49">${esc(taskId)}</text><title>${esc(task.description || task.name)} · ${esc(task.name)}${runnerLabel ? ` · ${esc(runnerLabel)}` : ""}${hoverStatus ? ` · ${esc(hoverStatus)}` : ""}</title></g>`;
  }).join("");
  return `<svg class="dag-canvas" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(workflowName)} DAG"><defs><marker id="dagArrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z"></path></marker></defs>${edges.join("")}${nodes}</svg>`;
}

function selectDagTask(workflowName, taskName) {
  const workflow=state.workflows.get(workflowName), task=workflow?.tasks.find(item => item.name===taskName), target=document.getElementById("dagTaskDetail");
  if (!task || !target) return;
  const detail=task.type === "condition" ? `<div class="dag-meta"><b>${t("类型：")}</b><code>${t("条件分支")}</code></div><div class="dag-meta"><b>${t("成功：")}</b><span>${esc(task.success.map(id => taskNamedId(workflow, id)).join(", ") || t("无"))}</span></div><div class="dag-meta"><b>${t("失败：")}</b><span>${esc(task.failure.map(id => taskNamedId(workflow, id)).join(", ") || t("无"))}</span></div>` : `<div class="dag-meta"><b>${t("命令：")}</b><code>${esc(task.command)}</code></div>`;
  target.innerHTML=`<div><span class="badge ${task.enabled ? "enabled" : "disabled"}">${task.enabled ? t("已启用") : t("已禁用")}</span> <strong>${esc(task.description || task.name)}</strong> <span class="secondary dag-inline mono">${esc(task.name)}</span></div><div class="dag-meta"><b>${t("依赖：")}</b><span>${esc(task.depends.map(id => taskNamedId(workflow, id)).join(", ") || t("无"))}</span></div>${detail}`;
}

function exportYaml(name) { window.location.assign(`/api/workflows/${encodeURIComponent(name)}/yaml`); }

function editSchedule(name) {
  const workflow = state.workflows.get(name), s = workflow.schedule;
  const crons = s.crons?.length ? s.crons : [s.cron || "0 18 * * mon-fri"];
  const rows = crons.map(cron => scheduleInputRow(cron)).join("");
  openModal(`${namedId(workflow.description, workflow.name)} · ${t("定时配置")}`, `<div class="schedule-editor"><div class="schedule-editor-head"><label>${t("统一时区")}<input id="timezoneInput" value="${esc(s.timezone || "Asia/Shanghai")}" oninput="this.setCustomValidity('')"></label><button class="button ghost" type="button" onclick="addScheduleRow()">＋ ${t("增加定时")}</button></div><div id="scheduleInputs" class="schedule-inputs">${rows}</div><span class="form-hint">${t("Cron 使用 5 个字段。每小时第 10 分钟：10 * * * *　每天 10:00：0 10 * * *")}</span><label class="check"><input id="enabledInput" type="checkbox" ${s.enabled ? "checked" : ""}>${t("启用全部自动调度")}</label><button class="button" onclick='saveSchedule(${jsArg(name)})'>${t("保存配置")}</button></div>`, false, "compact");
}

let wfNotifChannels = [];

function wfNotificationRuleRowHtml(rule, channels) {
  const chOptions = channels.map(ch => `<option value="${esc(ch.id)}"${rule.channel_id === ch.id ? " selected" : ""}>[${ch.channel_type === "feishu" ? "飞书" : "邮箱"}] ${esc(ch.name)}${ch.enabled ? "" : "（已停用）"}</option>`).join("");
  const selectedCh = channels.find(ch => ch.id === rule.channel_id) || channels[0];
  const placeholder = selectedCh?.channel_type === "feishu" ? "可选 @手机号，逗号分隔（留空直接发群）" : "收件邮箱，逗号或分号分隔";
  return `<div class="wf-notif-row">
    <select class="ui-select wf-notif-channel" onchange="onWfNotifChannelChange(this)">${chOptions}</select>
    <select class="ui-select wf-notif-sendon">
      <option value="failure"${rule.send_on === "failure" ? " selected" : ""}>仅失败时</option>
      <option value="success"${rule.send_on === "success" ? " selected" : ""}>仅成功时</option>
      <option value="always"${rule.send_on === "always" ? " selected" : ""}>成功或失败</option>
    </select>
    <input class="wf-notif-targets" type="text" value="${esc((rule.targets || []).join(", "))}" placeholder="${esc(placeholder)}">
    <button type="button" class="icon-button wf-notif-del" title="删除规则" onclick="this.closest('.wf-notif-row').remove()">×</button>
  </div>`;
}

function onWfNotifChannelChange(select) {
  const row = select.closest(".wf-notif-row");
  const ch = wfNotifChannels.find(item => item.id === select.value);
  const input = row?.querySelector(".wf-notif-targets");
  if (input) {
    input.placeholder = ch?.channel_type === "feishu" ? "可选 @手机号，逗号分隔（留空直接发群）" : "收件邮箱，逗号或分号分隔";
  }
}

function addWfNotificationRule() {
  if (!wfNotifChannels.length) return;
  const host = document.getElementById("wfNotifRules");
  if (!host) return;
  host.querySelector(".notif-empty")?.remove();
  host.insertAdjacentHTML("beforeend", wfNotificationRuleRowHtml({ channel_id: wfNotifChannels[0].id, send_on: "failure", targets: [] }, wfNotifChannels));
}

async function editNotification(name) {
  const workflow = state.workflows.get(name);
  if (!workflow) return;
  try {
    const data = await api(`/api/workflows/${encodeURIComponent(name)}/notifications`);
    wfNotifChannels = data.channels || [];
    const rules = data.rules || [];
    const body = !wfNotifChannels.length
      ? `<div class="notification-editor"><div class="notif-empty">暂无可用通知渠道。请管理员先在右上角「⚙ 通知」中配置邮箱或飞书渠道。</div></div>`
      : `<div class="notification-editor">
          <div class="notif-toolbar">
            <span class="form-hint" style="margin:0">可为当前工作流绑定多条邮箱或飞书通知规则，保存后写入数据库。</span>
            <button type="button" class="button ghost small" onclick="addWfNotificationRule()">＋ 添加规则</button>
          </div>
          <div id="wfNotifRules" class="wf-notif-rules">${rules.length ? rules.map(rule => wfNotificationRuleRowHtml(rule, wfNotifChannels)).join("") : '<div class="notif-empty">暂未绑定通知规则，点击上方「＋ 添加规则」配置。</div>'}</div>
          <button class="button" onclick='saveNotification(${jsArg(name)})'>${t("保存通知配置")}</button>
        </div>`;
    openModal(`${namedId(workflow.description, workflow.name)} · ${t("通知配置")}`, body, true, "compact");
  } catch (e) { toast(e.message, true); }
}

async function saveNotification(name) {
  const rows = [...document.querySelectorAll("#wfNotifRules .wf-notif-row")];
  const rules = rows.map(row => ({
    channel_id: row.querySelector(".wf-notif-channel").value,
    send_on: row.querySelector(".wf-notif-sendon").value,
    targets: row.querySelector(".wf-notif-targets").value.split(/[\n,;]+/).map(v => v.trim()).filter(Boolean),
    enabled: true,
  }));
  try {
    await api(`/api/workflows/${encodeURIComponent(name)}/notifications`, { method: "PUT", body: JSON.stringify({ rules }) });
    closeModal();
    toast(t("通知配置已保存"));
    await loadWorkflows();
  } catch (e) { toast(e.message, true); }
}
function scheduleInputRow(cron="") {
  return `<div class="schedule-input-row"><label>${t("Cron（5 字段）")}<input class="cron-input" value="${esc(cron)}" oninput="this.setCustomValidity('')"></label><button class="button danger schedule-remove" type="button" onclick="removeScheduleRow(this)" aria-label="${t("删除此定时")}">${t("删除")}</button></div>`;
}
function addScheduleRow() {
  document.getElementById("scheduleInputs").insertAdjacentHTML("beforeend", scheduleInputRow());
}
function removeScheduleRow(button) {
  const rows = document.querySelectorAll("#scheduleInputs .schedule-input-row");
  if (rows.length <= 1) return toast(t("至少保留一个定时任务"), true);
  button.closest(".schedule-input-row").remove();
}
function cronFieldIsValid(field, index) {
  const bounds = [[0,59], [0,23], [1,31], [1,12], [0,7]], [minimum, maximum] = bounds[index];
  const component = "(?:\\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)(?:/\\d+)?";
  if (!(new RegExp(`^${component}(?:,${component})*$`)).test(field)) return false;
  let namesRemoved = field.toLowerCase();
  if (index === 2) namesRemoved = namesRemoved.replaceAll("last", "");
  if (index === 3) namesRemoved = namesRemoved.replace(/jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec/g, "");
  if (index === 4) namesRemoved = namesRemoved.replace(/mon|tue|wed|thu|fri|sat|sun/g, "");
  if (/[a-z]/i.test(namesRemoved)) return false;
  const numbers = [...field.matchAll(/\d+/g)].map(match => Number(match[0]));
  if (numbers.some(value => value < minimum || value > maximum)) return false;
  const steps = [...field.matchAll(/\/(\d+)/g)].map(match => Number(match[1]));
  return steps.every(step => step > 0 && step <= maximum - minimum);
}
function validateScheduleForm() {
  const timezoneInput = document.getElementById("timezoneInput");
  const timezone = timezoneInput.value.trim();
  timezoneInput.setCustomValidity("");
  if (!timezone) timezoneInput.setCustomValidity(t("时区不能为空"));
  else {
    try { new Intl.DateTimeFormat("zh-CN", {timeZone:timezone}).format(); }
    catch (_) { timezoneInput.setCustomValidity(t("请输入有效的 IANA 时区，例如 Asia/Shanghai")); }
  }
  if (!timezoneInput.checkValidity()) { timezoneInput.reportValidity(); timezoneInput.focus(); return null; }

  const inputs = [...document.querySelectorAll("#scheduleInputs .cron-input")], crons = [], seen = new Set();
  for (const input of inputs) {
    const cron = input.value.trim().replace(/\s+/g, " "), fields = cron.split(" ");
    input.setCustomValidity("");
    if (!cron) input.setCustomValidity(t("Cron 不能为空"));
    else if (fields.length !== 5) input.setCustomValidity(t("Cron 必须包含 5 个字段"));
    else if (fields.some(field => !/^[A-Za-z0-9*\/,-]+$/.test(field))) input.setCustomValidity(t("Cron 含有不支持的字符"));
    else if (fields.some((field, index) => !cronFieldIsValid(field, index))) input.setCustomValidity(t("Cron 字段的范围或写法不正确"));
    else if (seen.has(cron)) input.setCustomValidity(t("请删除重复的 Cron"));
    if (!input.checkValidity()) { input.reportValidity(); input.focus(); return null; }
    seen.add(cron); crons.push(cron);
  }
  return {crons, timezone};
}
async function saveSchedule(name) { const values=validateScheduleForm(); if (!values) return; try { await api(`/api/workflows/${encodeURIComponent(name)}/schedule`, {method:"PUT", body:JSON.stringify({...values, enabled:document.getElementById("enabledInput").checked})}); closeModal(); toast(t("定时配置已保存")); loadWorkflows(); } catch(e) { toast(e.message, true); } }

async function showRun(id) {
  openWorkflowViewer('run', id);
}
const LOG_CHUNK_SIZE = 200;
const LOG_SCROLL_THRESHOLD = 40;
let logState = null;
let logScrollBound = false;

function setLogStatus(message) {
  const status = document.getElementById("logStatus");
  if (status) status.textContent = message;
}

function isLogScrollNearBottom() {
  const scroll = document.getElementById("logScroll");
  if (!scroll) return false;
  return scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - LOG_SCROLL_THRESHOLD;
}

function onLogScroll() {
  if (!logState || logState.loading || !logState.hasMore) return;
  if (isLogScrollNearBottom()) loadLogChunk();
}

function bindLogScroll() {
  if (logScrollBound) return;
  const scroll = document.getElementById("logScroll");
  if (!scroll) return;
  scroll.addEventListener("scroll", onLogScroll);
  logScrollBound = true;
}

function appendLogLines(lines, startLine) {
  const content = document.getElementById("logContent");
  if (!content) return;
  const html = lines.map((line, index) => `<div class="log-line"><span class="log-line-number">${startLine + index + 1}</span><span class="log-line-text">${esc(line)}</span></div>`).join("");
  content.insertAdjacentHTML("beforeend", html);
}

async function loadLogChunk() {
  const state = logState;
  if (!state || state.loading || !state.hasMore) return;
  state.loading = true;
  setLogStatus(t("正在加载…"));
  try {
    const data = await api(`/api/runs/${encodeURIComponent(state.runId)}/tasks/${encodeURIComponent(state.task)}/log?offset=${state.offset}&limit=${LOG_CHUNK_SIZE}`);
    if (logState !== state) return;
    appendLogLines(data.lines, state.offset);
    state.offset += data.lines.length;
    state.hasMore = data.has_more;
    setLogStatus(state.hasMore ? "" : t("已加载全部 {total} 行",{total:data.total}));
  } catch(e) {
    if (logState === state) toast(e.message, true);
  } finally {
    if (logState === state) {
      state.loading = false;
      if (state.hasMore && isLogScrollNearBottom()) loadLogChunk();
    }
  }
}

async function showLog(runId, task, label) {
  const modal = document.getElementById("logModal");
  document.getElementById("logModalTitle").textContent = currentLanguage === "en" ? `${label || task} (${task})` : `${label || task}（${task}）`;
  document.getElementById("logContent").innerHTML = "";
  setLogStatus("");
  logState = {runId, task, offset: 0, hasMore: true, loading: false};
  bindLogScroll();
  modal.classList.remove("hidden");
  await loadLogChunk();
}

function downloadLog() {
  const state = logState;
  if (!state) return;
  const url = `/api/runs/${encodeURIComponent(state.runId)}/tasks/${encodeURIComponent(state.task)}/log?download=1`;
  const link = document.createElement("a");
  link.href = url;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function copyLog() {
  const state = logState;
  if (!state) return;
  let text = "";
  try {
    text = await api(`/api/runs/${encodeURIComponent(state.runId)}/tasks/${encodeURIComponent(state.task)}/log`);
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const input = document.createElement("textarea");
      input.value = text;
      input.className = "clipboard-fallback";
      document.body.appendChild(input);
      input.select();
      const copied = document.execCommand("copy");
      input.remove();
      if (!copied) throw new Error(t("浏览器不支持自动复制"));
    }
    toast(t("日志已复制"));
  } catch(e) { toast(t("复制失败：{error}",{error:e.message}), true); }
}

let modalBackdropClosable = true;
function resetMaximizedCard(card) {
  card.style.width = "";
  card.style.maxWidth = "";
  const layout = card.querySelector(".workflow-editor-layout");
  if (layout) layout.style.gridTemplateColumns = "";
}

// 最大化时如果示意图宽度不够，就把弹窗加宽（不超过页面宽度），
// 让 .dag-scroll 不再出现左右滚动条。
function fitMaximizedCardWidth() {
  const card = document.querySelector("#modal .modal-card");
  if (!card || !card.classList.contains("maximized")) return;
  const layout = card.querySelector(".workflow-editor-layout");
  const vertical = Boolean(layout?.classList.contains("workflow-editor-vertical"));
  const limit = Math.max(320, Math.floor(window.innerWidth - 40));
  if (card.style.width && card.getBoundingClientRect().width > limit + 1) {
    // 视口变窄时先回到样式表基准宽度，再按内容重新适配。
    resetMaximizedCard(card);
  }
  for (let attempt = 0; attempt < 4; attempt++) {
    const scroller = card.querySelector(".dag-scroll");
    if (!scroller) return;
    const overflow = scroller.scrollWidth - scroller.clientWidth;
    if (overflow <= 1) return;
    const rect = card.getBoundingClientRect();
    // 先按当前网格比例记录左右两列宽度，再改卡片宽度，避免左栏跟着变宽。
    const cols = (vertical && layout)
      ? getComputedStyle(layout).gridTemplateColumns.split(/\s+/).filter(Boolean).map(parseFloat)
      : [];
    const grow = Math.min(overflow, limit - Math.round(rect.width));
    if (grow <= 0) return;
    const next = Math.round(rect.width + grow);
    if (vertical && cols.length > 1) {
      layout.style.gridTemplateColumns = `${Math.round(cols[0])}px ${Math.round(cols[1] + grow)}px`;
    }
    card.style.width = `${next}px`;
    card.style.maxWidth = `${next}px`;
  }
}

function toggleModalMaximize() {
  const card = document.querySelector("#modal .modal-card");
  if (!card) return;
  card.classList.toggle("maximized");
  const button = document.getElementById("modalMaximize");
  if (button) {
    const maximized = card.classList.contains("maximized");
    button.textContent = maximized ? "⤡" : "⤢";
    button.title = t(maximized ? "还原" : "最大化");
  }
  if (card.classList.contains("maximized")) fitMaximizedCardWidth();
  else resetMaximizedCard(card);
}
function openModal(title, body, closeOnBackdrop=true, size="default") {
  disposeWorkflowViewer();
  disposeWorkflowEditor();
  modalBackdropClosable=closeOnBackdrop;
  const modal=document.getElementById("modal");
  const card = modal.querySelector(".modal-card");
  card.classList.remove("maximized");
  card.classList.remove("form-mode");
  resetMaximizedCard(card);
  const maximizeButton = document.getElementById("modalMaximize");
  if (maximizeButton) { maximizeButton.textContent = "⤢"; maximizeButton.title = t("最大化"); }
  card.classList.toggle("compact-modal-card", size === "compact");
  card.classList.toggle("account-modal-card", size === "account");
  document.getElementById("modalTitle").textContent=title;
  document.getElementById("modalBody").innerHTML=body;
  modal.classList.remove("hidden");
  initWorkflowEditor();
}
function closeModal() { disposeWorkflowViewer(); disposeWorkflowEditor(); closeLogModal(); closeTaskRunMenu(); document.getElementById("modal").classList.add("hidden"); }
function closeModalFromBackdrop(event) { if (event.target.id === "modal" && modalBackdropClosable) closeModal(); }
function closeLogModal() { logState = null; setLogStatus(""); document.getElementById("logModal").classList.add("hidden"); }
function closeLogModalFromBackdrop(event) { if (event.target.id === "logModal") closeLogModal(); }
let toastTimer; function toast(message, error=false) { const box=document.getElementById("toast"); box.textContent=message; box.style.background=error?"#b42318":"#182230"; box.classList.remove("hidden"); clearTimeout(toastTimer); toastTimer=setTimeout(()=>box.classList.add("hidden"),4000); }

window.addEventListener("dagrunner-language-change", () => {
  const previousState = serviceState;
  serviceState = "";
  setServiceStatus(previousState);
  renderSatelliteStatus(satelliteConfigs, satelliteServiceEnabled);
  renderWorkflows();
  loadRuns();
  updateGearLanguageMeta();
});

let resourceSnapshot = null;
function renderResources(data) {
  const bar = document.getElementById("resourceBar");
  bar.classList.toggle("unavailable", !data);
  for (const metric of bar.querySelectorAll("[data-resource]")) {
    const key = metric.dataset.resource, value = data?.[key];
    const percent = value && Number.isFinite(value.percent) ? Math.max(0, Math.min(100, value.percent)) : null;
    metric.querySelector("b").textContent = percent === null ? "—" : `${Math.round(percent)}%`;
    metric.dataset.level = percent >= 90 ? "danger" : percent >= 75 ? "warning" : "normal";
    const track = metric.querySelector(".resource-track");
    track.querySelector("i").style.width = `${percent ?? 0}%`;
    if (percent === null) track.removeAttribute("aria-valuenow");
    else track.setAttribute("aria-valuenow", percent);
    const capacity = value?.total ? `${(value.used / 1073741824).toFixed(1)} / ${(value.total / 1073741824).toFixed(1)} GiB` : "";
    metric.title = percent === null ? t("暂不可用") : `${t(key === "disk" ? "数据库所在磁盘" : "服务器资源")} · ${percent.toFixed(1)}%${capacity ? ` · ${capacity}` : ""}`;
  }
}
async function loadSystemStatus() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const data = await api("/api/system/overview", {signal: controller.signal});
    resourceSnapshot = data.resources;
    renderResources(data.resources);
    renderSatelliteStatus(data.satellites, data.satellite_enabled);
  } catch (_) {
    resourceSnapshot = null;
    renderResources(null);
  } finally {
    clearTimeout(timeout);
  }
}
window.addEventListener("dagrunner-language-change", () => renderResources(resourceSnapshot));

const pollers = new Map();
function stopPoller(key) {
  const poller = pollers.get(key);
  if (!poller) return;
  poller.stop();
  pollers.delete(key);
}
function startPoller(key, task, activeInterval, hiddenInterval) {
  stopPoller(key);
  let timer = null;
  let running = false;
  let stopped = false;
  const delay = () => (document.hidden ? hiddenInterval : activeInterval);
  function schedule(wait) {
    if (stopped) return;
    clearTimeout(timer);
    timer = setTimeout(tick, wait ?? delay());
  }
  async function tick() {
    if (stopped || running) return;
    running = true;
    try { await task(); } finally {
      running = false;
      schedule();
    }
  }
  tick();
  pollers.set(key, {
    reschedule: () => schedule(),
    kick: () => schedule(0),
    stop: () => { stopped = true; clearTimeout(timer); },
  });
}
document.addEventListener("visibilitychange", () => {
  for (const poller of pollers.values()) poller.reschedule();
  if (!document.hidden) for (const poller of pollers.values()) poller.kick();
});

let lastAccounts = [];
let accountEditing = null;
let accountWorkflowKeys = [];
let accountWorkflowTitles = {};

function permissionLabel(permission) {
  const flags = parsePermissionFlags(permission);
  if (flags.has("edit")) return "Maintainer";
  if (flags.has("execute")) return "Executor";
  if (flags.has("read")) return "Reviewer";
  return t("无权限");
}

function accountWorkflowLabel(key) {
  const title = accountWorkflowTitles[key];
  return title && title !== key ? `${title}（${key}）` : key;
}

function accountScopeText(account) {
  if (account.is_admin) return t("全部（admin）");
  if (account.unrestricted) return t("全部工作流");
  if (!account.workflows.length) return t("无权限");
  return account.workflows.map(item => `${accountWorkflowLabel(item.key)}：${permissionLabel(item.permission)}`).join("，");
}

async function openAccountSettings() {
  try {
    const [accountsData, workflowsData] = await Promise.all([
      api("/api/accounts"), api("/api/workflows"),
    ]);
    lastAccounts = accountsData.accounts;
    accountWorkflowKeys = workflowsData.workflows.map(w => w.name);
    accountWorkflowTitles = Object.fromEntries(workflowsData.workflows.map(w => [w.name, w.description]));
    renderAccountSettings(lastAccounts, null);
  } catch (e) { toast(e.message, true); }
}

function renderAccountSettings(accounts, editing) {
  accountEditing = editing;
  if (editing) {
    const account = editing === "new" ? null : accounts.find(item => item.username === editing);
    if (editing === "new" || account) {
      const body = `<div class="account-settings">${accountEditorHtml(account)}</div>`;
      openModal(t("账号管理"), body, false, "account");
      syncAccountUnrestrictedView();
      return;
    }
  }
  const rows = accounts.map(account => {
    let actions;
    if (account.is_admin) {
      actions = `<span class="secondary">${t("内置管理员")}</span>`;
    } else {
      actions = `<button class="button ghost small" onclick='editAccountPanel(${jsArg(account.username)})'>${t("编辑")}</button>`
        + `<button class="button ghost small" onclick='resetAccountPasswordPrompt(${jsArg(account.username)})'>${t("重置密码")}</button>`
        + `<button class="button danger small" onclick='deleteAccountByUsername(${jsArg(account.username)})'>${t("删除")}</button>`;
    }
    const badge = account.enabled ? "" : ` <span class="badge disabled">${t("已禁用")}</span>`;
    return `<tr><td class="card-cell-name" data-label="${t("账号")}"><div class="account-name">${esc(account.username)}${badge}</div></td>`
      + `<td data-label="${t("权限")}"><div class="account-scope">${esc(accountScopeText(account))}</div></td>`
      + `<td class="secondary" data-label="${t("最后登录")}">${esc(account.last_login_time || "-")}</td>`
      + `<td class="card-cell-actions" data-label="${t("操作")}"><div class="actions">${actions}</div></td></tr>`;
  }).join("");
  const toolbar = `<div class="account-toolbar"><span class="form-hint" style="margin:0">${t("管理登录账号及各工作流的细粒度访问权限")}</span><button class="button" onclick="addAccountPanel()">＋ ${t("新建账号")}</button></div>`;
  const body = `<div class="account-settings">${toolbar}`
    + `<table class="account-table card-table"><thead><tr><th>${t("账号")}</th><th>${t("权限")}</th><th>${t("最后登录")}</th><th>${t("操作")}</th></tr></thead><tbody>${rows || `<tr><td colspan="4" class="empty">${t("暂无账号")}</td></tr>`}</tbody></table>`
    + `</div>`;
  openModal(t("账号管理"), body, true, "account");
}

function generateStrongPassword(targetId) {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*";
  const bytes = new Uint8Array(20);
  crypto.getRandomValues(bytes);
  const pwd = Array.from(bytes, b => chars[b % chars.length]).join("");
  const input = document.getElementById(targetId);
  if (input) {
    input.type = "text";
    input.value = pwd;
    input.focus();
    input.select();
  }
}

function filterAccountWorkflowRows(query) {
  const q = String(query || "").trim().toLowerCase();
  document.querySelectorAll("#modalBody .account-workflow-row").forEach(row => {
    const hay = row.dataset.search || "";
    row.style.display = !q || hay.includes(q) ? "" : "none";
  });
}

function syncAccountUnrestrictedView() {
  const box = document.getElementById("accountUnrestricted");
  const section = document.getElementById("accountWorkflowsSection");
  if (!box || !section) return;
  section.classList.toggle("is-unrestricted", box.checked);
}

function accountEditorHtml(account) {
  const isNew = !account;
  const username = account?.username || "";
  const enabled = account ? account.enabled : true;
  const unrestricted = Boolean(account?.unrestricted);
  const grants = {};
  for (const item of account?.workflows || []) grants[item.key] = item.permission;
  const ROLE_OPTIONS = [
    { value: "", name: t("无权限"), desc: t("不可见") },
    { value: "read", name: "Reviewer", desc: t("只读") },
    { value: "execute", name: "Executor", desc: t("只读 + 执行") },
    { value: "edit", name: "Maintainer", desc: t("只读 + 执行 + 编辑") },
  ];
  const permissionCells = (key, granted) => {
    const flags = parsePermissionFlags(granted);
    const current = flags.has("edit") ? "edit" : flags.has("execute") ? "execute" : flags.has("read") ? "read" : "";
    return ROLE_OPTIONS.map(option => `
      <label class="account-role-cell"><input type="radio" name="account-perm-${esc(key)}" value="${option.value}" data-account-workflow="${esc(key)}"${current === option.value ? " checked" : ""} aria-label="${esc(option.name)} · ${esc(key)}"></label>`).join("");
  };
  const workflowRows = accountWorkflowKeys.map(key => {
    const title = accountWorkflowTitles[key] || "";
    const search = `${title} ${key}`.toLowerCase();
    const name = title && title !== key
      ? `<strong title="${esc(title)}">${esc(title)}</strong><span class="mono" title="${esc(key)}">${esc(key)}</span>`
      : `<strong class="mono" title="${esc(key)}">${esc(key)}</strong>`;
    return `<div class="account-workflow-row" data-search="${esc(search)}"><div class="account-workflow-name">${name}</div>${permissionCells(key, grants[key])}</div>`;
  }).join("");
  return `<form class="account-editor" autocomplete="off" onsubmit="event.preventDefault()">
    <div class="account-editor-head">
      <div>
        <h4>${isNew ? t("新建账号") : t("编辑账号：{name}", {name: username})}</h4>
        <span class="form-hint" style="margin:0">${isNew ? t("创建登录凭据并分配工作流权限") : t("调整账号启停状态或工作流角色权限")}</span>
      </div>
      <button type="button" class="button ghost small" onclick="cancelAccountPanel()">← ${t("返回列表")}</button>
    </div>

    <div class="account-card-section">
      <div class="account-form-grid">
        <label${isNew ? "" : ` class="account-full"`}>${t("用户名")}<input id="accountUsername" name="new_account_user" autocomplete="off" value="${esc(username)}" ${isNew ? "" : "disabled"} maxlength="64" placeholder="如：ops_user"></label>
        ${isNew ? `<label>${t("密码")}（至少 16 位）<div class="account-pwd-wrap"><input id="accountPassword" name="new_account_pass" type="password" autocomplete="new-password" minlength="16" placeholder="至少 16 位字符"><button type="button" class="button ghost small" onclick="generateStrongPassword('accountPassword')">${t("随机生成")}</button></div></label>` : ""}
      </div>
      <div class="account-toggles-grid">
        <label class="account-toggle-card">
          <input id="accountEnabled" type="checkbox" ${enabled ? "checked" : ""}>
          <div><strong>${t("启用账号")}</strong><span>${t("允许登录控制台；禁用后现有会话立即失效")}</span></div>
        </label>
        <label class="account-toggle-card">
          <input id="accountUnrestricted" type="checkbox" ${unrestricted ? "checked" : ""} onchange="syncAccountUnrestrictedView()">
          <div><strong>${t("全部工作流权限")}</strong><span>${t("拥有所有工作流的完整权限，不受下方逐项授权限制")}</span></div>
        </label>
      </div>
    </div>

    <div id="accountWorkflowsSection" class="account-workflows${unrestricted ? " is-unrestricted" : ""}">
      <div class="account-workflows-head">
        <div class="account-workflows-title">
          <span>${t("工作流逐项授权")}</span>
          <span class="tf-count-badge">${accountWorkflowKeys.length}</span>
        </div>
        <input class="account-wf-search" type="search" autocomplete="off" placeholder="${esc(t("搜索名称或 ID"))}" oninput="filterAccountWorkflowRows(this.value)">
      </div>
      <div class="account-unrestricted-banner">${t("已开启「全部工作流权限」，无需逐项勾选。")}</div>
      <div class="account-workflows-bulk">
        <span class="bulk-label">${t("批量设置")}</span>
        <button type="button" class="button ghost small" onclick="setAllAccountRoles('')">${t("全部清空")}</button>
        <button type="button" class="button ghost small" onclick="setAllAccountRoles('read')">Reviewer · ${t("只读")}</button>
        <button type="button" class="button ghost small" onclick="setAllAccountRoles('execute')">Executor · ${t("只读 + 执行")}</button>
        <button type="button" class="button ghost small" onclick="setAllAccountRoles('edit')">Maintainer · ${t("只读 + 执行 + 编辑")}</button>
      </div>
      <div class="account-workflows-list">
        <div class="account-workflow-head">
          <span class="account-workflow-head-label">${t("工作流")}</span>
          ${ROLE_OPTIONS.map(option => `<div class="account-role-head"><span class="role-name">${esc(option.name)}</span><span class="role-desc">${esc(option.desc)}</span></div>`).join("")}
        </div>
        <div class="account-workflow-scroll">
          ${workflowRows || `<div class="notif-empty">${t("暂无工作流")}</div>`}
        </div>
      </div>
    </div>

    <div class="account-editor-actions">
      <button type="button" class="button ghost" onclick="cancelAccountPanel()">${t("取消")}</button>
      <button type="button" class="button" onclick="saveAccountPanel()">${t("保存")}</button>
    </div>
  </form>`;
}

function addAccountPanel() { renderAccountSettings(lastAccounts, "new"); }
function editAccountPanel(username) { renderAccountSettings(lastAccounts, username); }
function cancelAccountPanel() { renderAccountSettings(lastAccounts, null); }

function setAllAccountRoles(role) {
  document.querySelectorAll("#modalBody [name^='account-perm-']").forEach(radio => {
    radio.checked = radio.value === role;
  });
}

function readAccountPanel() {
  const payload = {
    enabled: document.getElementById("accountEnabled").checked,
    unrestricted: document.getElementById("accountUnrestricted").checked,
    workflows: {},
  };
  const keys = [...new Set(
    [...document.querySelectorAll("#modalBody [data-account-workflow]")].map(item => item.dataset.accountWorkflow)
  )];
  for (const key of keys) {
    const checked = document.querySelector(`#modalBody [data-account-workflow="${CSS.escape(key)}"]:checked`);
    if (checked && checked.value) payload.workflows[key] = checked.value;
  }
  if (accountEditing === "new") {
    payload.username = document.getElementById("accountUsername").value.trim();
    payload.password = document.getElementById("accountPassword").value;
  }
  return payload;
}

async function saveAccountPanel() {
  const payload = readAccountPanel();
  try {
    if (accountEditing === "new") {
      if (!payload.username) { toast(t("请输入用户名"), true); return; }
      if (payload.password.length < 16) { toast(t("密码至少 16 位"), true); return; }
      await api("/api/accounts", {method: "POST", body: JSON.stringify(payload)});
      toast(t("账号已创建"));
    } else {
      await api(`/api/accounts/${encodeURIComponent(accountEditing)}`, {method: "PUT", body: JSON.stringify(payload)});
      toast(t("账号已保存"));
    }
    await openAccountSettings();
  } catch (e) { toast(e.message, true); }
}

async function deleteAccountByUsername(username) {
  if (!await confirmDelete(t("删除账号：{name}？", {name: username}), t("该账号的登录会话会立即失效。"))) return;
  try {
    await api(`/api/accounts/${encodeURIComponent(username)}`, {method: "DELETE"});
    toast(t("账号已删除"));
    await openAccountSettings();
  } catch (e) { toast(e.message, true); }
}

async function resetAccountPasswordPrompt(username) {
  openModal(t("重置密码：{name}", {name: username}), `<form class="form-stack" autocomplete="off" onsubmit="event.preventDefault()">
    <input type="text" name="username" autocomplete="username" value="${esc(username)}" style="display:none">
    <label>${t("新密码")}<div class="account-pwd-wrap"><input id="accountResetPassword" type="password" autocomplete="new-password" minlength="16" placeholder="至少 16 位字符"><button type="button" class="button ghost small" onclick="generateStrongPassword('accountResetPassword')">${t("随机生成")}</button></div></label>
    <span class="form-hint">${t("密码至少 16 位；该账号现有会话会立即失效。")}</span>
    <div class="form-actions"><button type="button" class="button ghost" onclick="openAccountSettings()">${t("返回列表")}</button><button type="button" class="button" onclick='saveAccountPasswordReset(${jsArg(username)})'>${t("保存")}</button></div>
  </form>`, false, "compact");
}

async function saveAccountPasswordReset(username) {
  const password = document.getElementById("accountResetPassword").value;
  if (password.length < 16) { toast(t("密码至少 16 位"), true); return; }
  try {
    await api(`/api/accounts/${encodeURIComponent(username)}/password`, {method: "POST", body: JSON.stringify({password})});
    closeModal();
    toast(t("密码已重置"));
  } catch (e) { toast(e.message, true); }
}

async function openChangePassword() {
  const currentUser = state.auth?.username || "";
  openModal(t("修改密码"), `<form class="form-stack" autocomplete="off" onsubmit="event.preventDefault()">
    <input type="text" name="username" autocomplete="username" value="${esc(currentUser)}" tabindex="-1" aria-hidden="true" style="position:absolute;opacity:0;pointer-events:none;height:0;width:0;">
    <label>${t("当前密码")}<input id="oldPassword" type="password" autocomplete="current-password"></label>
    <label>${t("新密码")}<input id="newPassword" type="password" autocomplete="new-password" minlength="16"></label>
    <label>${t("确认新密码")}<input id="confirmPassword" type="password" autocomplete="new-password" minlength="16"></label>
    <span class="form-hint">${t("密码至少 16 位；修改后会退出登录。")}</span>
    <div class="form-actions"><button type="button" class="button" onclick="saveChangePassword()">${t("保存")}</button></div>
  </form>`, false, "compact");
}

async function saveChangePassword() {
  const oldPassword = document.getElementById("oldPassword").value;
  const newPassword = document.getElementById("newPassword").value;
  const confirm = document.getElementById("confirmPassword").value;
  if (newPassword.length < 16) { toast(t("密码至少 16 位"), true); return; }
  if (newPassword !== confirm) { toast(t("两次输入的新密码不一致"), true); return; }
  try {
    const oldHash = await sha256Hex(oldPassword);
    const result = await api("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({old_password_hash: oldHash, new_password: newPassword}),
    });
    toast(t("密码已修改，请重新登录"));
    setTimeout(() => window.location.replace(result.redirect || "/login"), 800);
  } catch (e) { toast(e.message, true); }
}

window.addEventListener("resize", () => {
  positionWorkflowTags();
  fitMaximizedCardWidth();
});
loadAuth().finally(() => {
  startPoller("workflows", loadWorkflows, 15000, 60000);
  startPoller("runs", () => loadRuns(), 3000, 30000);
  startPoller("system", loadSystemStatus, 10000, 30000);
  if (state.auth.is_admin && typeof refreshUpdateStatus === "function") {
    refreshUpdateStatus();
  }
});
