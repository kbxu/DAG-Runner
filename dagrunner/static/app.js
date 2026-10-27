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
function normalizeYamlNewlines(value) {
  const source = String(value ?? "");
  return source.includes("\r") ? source.replace(/\r\n?/g, "\n") : source;
}
function namedId(name, id) { return name && name !== id ? (currentLanguage === "en" ? `${name} (${id})` : `${name}（${id}）`) : id; }
function taskNamedId(workflow, taskId) { const task = workflow?.tasks.find(item => item.name === taskId); return namedId(task?.description, taskId); }
const usefulTopKeys = new Set(["description", "setup", "timeout", "notification", "tasks"]);
const usefulTaskKeys = new Set(["type", "description", "shell", "command", "depends", "condition", "success", "failure", "args", "cwd", "timeout", "enabled"]);
function yamlEditor(id, value, includeSchedule=false) {
  const source=normalizeYamlNewlines(value);
  return `<div class="yaml-editor"><div class="yaml-gutter" id="${id}Gutter" aria-hidden="true">${yamlGutter(source)}</div><div class="yaml-code"><pre id="${id}Highlight" aria-hidden="true">${highlightUsefulYaml(source,includeSchedule)}\n</pre><textarea id="${id}" data-include-schedule="${includeSchedule}" spellcheck="false" oninput="syncYamlHighlight('${id}')" onkeydown="yamlEditorKeydown(event,'${id}')" onscroll="syncYamlScroll('${id}')">${esc(source)}</textarea></div></div>`;
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
function syncYamlHighlight(id) {
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
  queueWorkflowPreview(id);
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
function yamlEditorKeydown(event, id) {
  const input = document.getElementById(id);
  if (!input) return;
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
  for (const match of String(workflowTitle(workflow)).matchAll(/【([^】]*)】/g)) {
    const tag = match[1].trim();
    if (tag && !tags.includes(tag)) tags.push(tag);
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
  box.innerHTML = workflowTagOptions.map((option, index) => `<button type="button" class="workflow-tag${option.key === state.workflowTag ? " active" : ""}" onclick="setWorkflowTag(${index})"><span class="workflow-tag-label">${esc(option.label)}</span><span class="workflow-tag-count">${option.count}</span></button>`).join("");
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

function renderWorkflows() {
  hideSchedulePopover();
  renderWorkflowTags();
  const workflows = [...state.workflows.values()].filter(workflowMatchesTag);
  const { key, direction } = state.workflowSort;
  if (key) {
    const multiplier = direction === "asc" ? 1 : -1;
    workflows.sort((left, right) => {
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
  const totalPages = Math.max(1, Math.ceil(workflows.length / state.workflowPageSize));
  state.workflowPage = Math.min(Math.max(state.workflowPage, 1), totalPages);
  const pageStart = (state.workflowPage - 1) * state.workflowPageSize;
  const visibleWorkflows = workflows.slice(pageStart, pageStart + state.workflowPageSize);
  document.getElementById("lastRunSort").textContent = key === "last_run_time" ? (direction === "asc" ? "▲" : "▼") : "";
  document.getElementById("nextRunSort").textContent = key === "next_run_time" ? (direction === "asc" ? "▲" : "▼") : "";
  document.getElementById("workflowRows").innerHTML = visibleWorkflows.map(w => {
      const s = w.schedule;
      const emailNotification = w.notification?.email || {send_on:"disabled", to:[]};
      const notificationClass = emailNotification.send_on === "disabled" ? "" : " notification-active";
      return `<tr>
        <td><div class="workflow-name-cell"><button class="workflow-link" title="${esc(w.description || w.name)}" onclick="showDag('${esc(w.name)}')">${esc(w.description || w.name)}</button>${runnerTag(w.runner)}</div><div class="workflow-id mono" title="${esc(w.name)}">${esc(w.name)}</div></td>
        <td>${scheduleSummary(s)}</td>
        <td>${fmt(w.last_run_time)}</td>
        <td>${fmt(s.next_run_time)}</td>
        <td><div class="actions"><button class="button" onclick="runWorkflow('${esc(w.name)}')">${t("运行")}</button><button class="button ghost${s.enabled ? ' schedule-active' : ''}" title="${t('定时')} · ${s.enabled ? t('已启用') : t('未启用')}" aria-label="${t('定时')} · ${s.enabled ? t('已启用') : t('未启用')}" onclick="editSchedule('${esc(w.name)}')">${s.enabled ? '● ' : ''}${t("定时")}</button><button class="button ghost${notificationClass}" onclick='editNotification(${jsArg(w.name)})'>✉ ${t("通知")}</button>${s.enabled ? "" : `<button class="button ghost" onclick="editWorkflow('${esc(w.name)}')">${t("编辑")}</button>`}<button class="button ghost" onclick="exportYaml('${esc(w.name)}')">${t("导出 YAML")}</button>${s.enabled ? "" : `<button class="button danger" onclick="deleteWorkflow('${esc(w.name)}')">${t("删除")}</button>`}</div></td>
      </tr>`;
    }).join("") || `<tr><td colspan="5" class="empty">${t("还没有导入工作流")}</td></tr>`;
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
      const actions = [];
      if (r.status === "RUNNING") actions.push(`<button class="button danger" onclick="runAction('${r.run_id}','stop')">${t("停止")}</button>`);
      else {
        actions.push(`<button class="button ghost" onclick="runAction('${r.run_id}','rerun')">${t("重跑")}</button>`);
        if (r.status === "FAILED" && r.error_message !== "stopped by user") actions.push(`<button class="button" onclick="runAction('${r.run_id}','resume')">${t("失败续跑")}</button>`);
        actions.push(`<button class="button danger" onclick="deleteRun('${r.run_id}')">${t("删除")}</button>`);
      }
      const handled = r.handled_count ? ` · ${t("已处理 {count}", {count:r.handled_count})}` : "";
      const triggerLabels = {manual:t("手动"),schedule:t("定时"),rerun:t("重跑"),resume:t("失败续跑")};
      return `<tr><td><div class="workflow-name-cell"><button class="workflow-link" title="${esc(r.workflow_description || r.workflow_name)}" onclick='showRun(${jsArg(r.run_id)})'>${esc(r.workflow_description || r.workflow_name)}</button>${runnerTag(r.runner)}</div><div class="workflow-id mono" title="${esc(r.workflow_name)}">${esc(r.workflow_name)}</div></td><td>${esc(triggerLabels[r.trigger_type] || r.trigger_type || "manual")}</td><td>${statusBadge(r.status)}</td><td><div>${fmt(r.start_time)}</div><div class="secondary mono" title="${esc(r.run_id)}">${esc(r.run_id)}</div></td><td><div class="progress"><span style="width:${percent}%"></span></div><div class="progress-label">${complete}/${r.task_count || 0} · ${t("成功")} ${r.success_count || 0}${handled}</div></td><td>${duration(r.start_time, r.end_time)}</td><td><div class="actions">${actions.join("")}</div></td></tr>`;
    }).join("") || `<tr><td colspan="7" class="empty">${t("没有符合条件的运行记录")}</td></tr>`;
    renderRunPagination();
    loadRunStats();
  } catch (error) { if (!error.offline) toast(error.message, true); }
}

async function loadRunStats() {
  try {
    const stats = await api("/api/runs/stats?hours=48");
    const item = (label, value, className) => `<span class="run-stat ${className}"><b>${value}</b>${t(label)}</span>`;
    document.getElementById("runStats").innerHTML =
      item("成功", stats.success, "run-stat-success") +
      item("运行中", stats.running, "run-stat-running") +
      item("失败", stats.failed, "run-stat-failed");
  } catch (error) { if (!error.offline) document.getElementById("runStats").innerHTML = ""; }
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
async function logout() { try { const result=await api("/api/auth/logout",{method:"POST",body:"{}"}); window.location.replace(result.redirect || "/login"); } catch(e) { if (!e.loginRequired) toast(e.message,true); } }

async function openNotifierSettings() {
  try {
    const data=await api("/api/notifier"), email=data.email;
    const securityOption=(value,label)=>`<option value="${value}" ${email.security===value?"selected":""}>${t(label)}</option>`;
    const passwordHint=email.has_password?t("已保存密码；留空不会修改"):t("请输入 SMTP 密码或授权码");
    openModal(t("通知渠道配置"), `<div class="channel-editor"><div class="channel-card"><div class="notification-icon">✉</div><div><strong>${t("邮件 SMTP")}</strong><p>${t("配置所有工作流共用的发件账号。收件人与发送时机仍在各工作流的通知面板中配置。")}</p></div><span class="channel-status ${email.configured?"configured":""}">${email.configured?t("已配置"):t("未配置")}</span></div><div class="channel-form-grid"><label>${t("SMTP 主机")}<input id="smtpHost" required value="${esc(email.host)}" placeholder="smtp.example.com"></label><label>${t("端口")}<input id="smtpPort" required type="number" min="1" max="65535" value="${esc(email.port)}"></label><label>${t("用户名")}<input id="smtpUsername" value="${esc(email.username)}" autocomplete="username"></label><label>${t("密码 / 授权码")}<input id="smtpPassword" type="password" autocomplete="new-password" placeholder="${esc(passwordHint)}"></label><label class="channel-wide">${t("发件邮箱")}<input id="smtpFrom" required type="email" value="${esc(email.from)}" placeholder="sender@example.com"></label><label>${t("连接安全")}<select id="smtpSecurity" class="ui-select">${securityOption("ssl","SSL/TLS")}${securityOption("starttls","STARTTLS")}${securityOption("plain","无加密")}</select></label><label>${t("超时（秒）")}<input id="smtpTimeout" required type="number" min="1" step="0.1" value="${esc(email.timeout)}"></label><label class="channel-wide">${t("邮件主题前缀")}<input id="smtpSubjectPrefix" value="${esc(email.subject_prefix)}" placeholder="[DAG Runner]"></label></div><span class="form-hint">${t("当前仅支持邮件渠道；此页面后续可继续增加飞书等通知渠道。")}</span><button class="button" onclick="saveNotifierSettings()">${t("保存发件配置")}</button></div>`, false, "compact");
  } catch(e) { toast(e.message,true); }
}

async function saveNotifierSettings() {
  const ids=["smtpHost","smtpPort","smtpFrom","smtpTimeout"];
  for (const id of ids) { const input=document.getElementById(id); if (!input.checkValidity()) { input.reportValidity(); input.focus(); return; } }
  const email={
    host:document.getElementById("smtpHost").value.trim(),
    port:Number(document.getElementById("smtpPort").value),
    username:document.getElementById("smtpUsername").value.trim(),
    password:document.getElementById("smtpPassword").value,
    from:document.getElementById("smtpFrom").value.trim(),
    security:document.getElementById("smtpSecurity").value,
    timeout:Number(document.getElementById("smtpTimeout").value),
    subject_prefix:document.getElementById("smtpSubjectPrefix").value.trim() || "[DAG Runner]",
  };
  try {
    await api("/api/notifier",{method:"PUT",body:JSON.stringify({email})});
    closeModal(); toast(t("发件配置已保存"));
  } catch(e) { toast(e.message,true); }
}

let remoteConfigs = [];
let remoteSettings = {ssh_path:"", key_path:"", host_key_policy:"accept-new", connect_timeout:10};
let remoteDefaults = {ssh_path:"", key_path:""};

async function loadRemoteStatus() {
  try {
    const data = await api("/api/remotes");
    remoteConfigs = data.remotes || [];
    const dot = document.getElementById("remoteStatusDot");
    if (!dot) return;
    if (!remoteConfigs.length) { dot.className = "remote-dot none"; dot.title = t("未配置远程"); return; }
    const reachable = remoteConfigs.filter(r => r.status?.reachable).length;
    dot.className = "remote-dot " + (reachable === remoteConfigs.length ? "ok" : reachable === 0 ? "bad" : "warn");
    dot.title = t("{ok}/{total} 台远程可达", {ok: reachable, total: remoteConfigs.length});
  } catch(e) { /* status is best-effort */ }
}

async function openRemoteSettings() {
  try {
    const data = await api("/api/remotes");
    remoteConfigs = data.remotes || [];
    remoteSettings = data.settings || remoteSettings;
    remoteDefaults = {ssh_path: remoteSettings.detected_ssh_path || "", key_path: remoteSettings.detected_key_path || ""};
    renderRemoteSettings();
  } catch(e) { toast(e.message, true); }
}

function renderRemoteSettings() {
  const policyOption = (value, label) => `<option value="${value}" ${remoteSettings.host_key_policy===value?"selected":""}>${esc(label)}</option>`;
  const panels = remoteConfigs.map(remote => remotePanelHtml(remote, false)).join("");
  openModal(t("远程执行配置"), `<div class="remote-editor">
    <div class="remote-global">
      <div class="remote-global-head"><strong>${t("全局设置")}</strong><span class="form-hint">${t("远程仅支持 Linux，使用私钥认证，不支持密码。")}</span></div>
      <div class="remote-form-grid">
        <label class="remote-wide">${t("本地 ssh 路径")}<input id="remoteSshPath" value="${esc(remoteSettings.ssh_path)}" placeholder="${esc(remoteDefaults.ssh_path ? t("已探测：{path}",{path:remoteDefaults.ssh_path}) : t("留空使用系统 PATH 中的 ssh"))}"></label>
        <label class="remote-wide">${t("私钥路径")}<input id="remoteKeyPath" value="${esc(remoteSettings.key_path)}" placeholder="${esc(remoteDefaults.key_path ? t("已探测：{path}",{path:remoteDefaults.key_path}) : t("留空使用默认密钥"))}"></label>
        <label>${t("主机密钥策略")}<select id="remoteHostKeyPolicy" class="ui-select">${policyOption("accept-new","accept-new（推荐）")}${policyOption("yes","yes（自动接受，不安全）")}${policyOption("no","no（仅已知主机）")}${policyOption("ask","ask（交互确认）")}</select></label>
        <label>${t("连接超时（秒）")}<input id="remoteConnectTimeout" type="number" min="1" max="300" value="${esc(remoteSettings.connect_timeout)}"></label>
      </div>
      <button class="button" onclick="saveRemoteSettings()">${t("保存全局设置")}</button>
    </div>
    <div class="remote-list-head"><strong>${t("远程机器")}</strong><button class="button ghost" type="button" onclick="addRemotePanel()">＋ ${t("新增远程")}</button></div>
    <div id="remoteList" class="remote-list">${panels || `<p class="form-hint">${t("还没有配置远程机器")}</p>`}</div>
    <button class="button" onclick="closeModal()">${t("关闭")}</button>
  </div>`, false, "compact");
}

function remotePanelHtml(remote, isNew) {
  const r = remote || {alias:"", username:"", host:"", port:22, workdir:""};
  const status = remote?.status || {};
  const statusHtml = isNew ? `<span class="remote-status">${t("未测试")}</span>` : `<span class="remote-status ${status.reachable ? "ok" : "bad"}">${status.reachable ? t("在线") : t("离线")}</span>`;
  const buttons = isNew
    ? `<button class="button ghost" type="button" onclick="testRemotePanel(this)">${t("检查连通")}</button><button class="button" type="button" onclick="saveRemotePanel(this)">${t("确认")}</button><button class="button ghost" type="button" onclick="cancelRemotePanel(this)">${t("取消")}</button>`
    : `<button class="button ghost" type="button" onclick="testRemotePanel(this)">${t("检查连通")}</button><button class="button" type="button" onclick="saveRemotePanel(this)">${t("保存")}</button><button class="button danger" type="button" onclick="deleteRemotePanel(this)">${t("删除")}</button>`;
  return `<div class="remote-panel${isNew ? " remote-panel-new" : ""}">
    <div class="remote-panel-head"><input class="remote-alias" value="${esc(r.alias)}" placeholder="${t("别名（唯一）")}">${statusHtml}</div>
    <div class="remote-form-grid">
      <label>${t("用户名")}<input class="remote-username" value="${esc(r.username)}" placeholder="root"></label>
      <label>${t("主机")}<input class="remote-host" value="${esc(r.host)}" placeholder="192.168.1.10"></label>
      <label>${t("端口")}<input class="remote-port" type="number" min="1" max="65535" value="${esc(r.port)}"></label>
      <label class="remote-wide">${t("远程工作目录")}<input class="remote-workdir" value="${esc(r.workdir)}" placeholder="${t("留空使用远程 HOME")}"></label>
    </div>
    <div class="remote-panel-actions">${buttons}</div>
  </div>`;
}

function addRemotePanel() {
  const list = document.getElementById("remoteList");
  const hint = list.querySelector(".form-hint");
  if (hint) hint.remove();
  const temp = document.createElement("div");
  temp.innerHTML = remotePanelHtml(null, true);
  list.appendChild(temp.firstElementChild);
}

function cancelRemotePanel(button) { button.closest(".remote-panel").remove(); }

function readRemotePanel(panel) {
  return {
    alias: panel.querySelector(".remote-alias").value.trim(),
    username: panel.querySelector(".remote-username").value.trim(),
    host: panel.querySelector(".remote-host").value.trim(),
    port: panel.querySelector(".remote-port").value.trim(),
    workdir: panel.querySelector(".remote-workdir").value.trim(),
  };
}

function markRemoteFieldErrors(panel, errors) {
  panel.querySelectorAll(".remote-field-error").forEach(element => element.classList.remove("remote-field-error"));
  const map = {alias:".remote-alias", username:".remote-username", host:".remote-host", port:".remote-port", workdir:".remote-workdir"};
  for (const [field, selector] of Object.entries(map)) {
    if (errors?.[field]) { const input = panel.querySelector(selector); if (input) input.classList.add("remote-field-error"); }
  }
}

async function saveRemotePanel(button) {
  const panel = button.closest(".remote-panel");
  const data = readRemotePanel(panel);
  const isNew = panel.classList.contains("remote-panel-new");
  if (!data.alias) { toast(t("别名不能为空"), true); return; }
  try {
    await api(`/api/remotes/${encodeURIComponent(data.alias)}`, {method:"PUT", body:JSON.stringify(data)});
    toast(t("远程配置已保存"));
    if (isNew && panel.dataset.tested === "1") {
      try { await api("/api/remotes/test", {method:"POST", body:JSON.stringify(data)}); } catch(_) {}
    }
    await openRemoteSettings();
  } catch(e) { markRemoteFieldErrors(panel, e.data?.field_errors); toast(e.message, true); }
}

async function testRemotePanel(button) {
  const panel = button.closest(".remote-panel");
  const data = readRemotePanel(panel);
  if (!data.alias) { toast(t("请先填写别名"), true); return; }
  const badge = panel.querySelector(".remote-status");
  const original = button.textContent;
  button.disabled = true; button.textContent = t("检查中…");
  try {
    const result = await api("/api/remotes/test", {method:"POST", body:JSON.stringify(data)});
    if (result.reachable) { toast(t("连接成功")); panel.dataset.tested = "1"; }
    else { toast(t("连接失败：{error}", {error: result.error_message || ""}), true); delete panel.dataset.tested; }
    if (badge) { badge.className = "remote-status " + (result.reachable ? "ok" : "bad"); badge.textContent = result.reachable ? t("在线") : t("离线"); }
  } catch(e) { toast(e.message, true); }
  finally { button.disabled = false; button.textContent = original; }
}

async function deleteRemotePanel(button) {
  const panel = button.closest(".remote-panel");
  const alias = readRemotePanel(panel).alias;
  if (!await confirmDelete(t("删除远程配置“{alias}”？",{alias}), t("删除后无法恢复。"))) return;
  try {
    await api(`/api/remotes/${encodeURIComponent(alias)}`, {method:"DELETE"});
    toast(t("已删除远程配置 {alias}",{alias}));
    await openRemoteSettings();
  } catch(e) { toast(e.message, true); }
}

async function saveRemoteSettings() {
  const payload = {
    ssh_path: document.getElementById("remoteSshPath").value.trim(),
    key_path: document.getElementById("remoteKeyPath").value.trim(),
    host_key_policy: document.getElementById("remoteHostKeyPolicy").value,
    connect_timeout: Number(document.getElementById("remoteConnectTimeout").value),
  };
  try {
    await api("/api/remotes/settings", {method:"PUT", body:JSON.stringify(payload)});
    toast(t("全局设置已保存"));
    await openRemoteSettings();
  } catch(e) {
    for (const id of ["remoteSshPath","remoteKeyPath","remoteHostKeyPolicy","remoteConnectTimeout"]) document.getElementById(id)?.classList.remove("remote-field-error");
    const map = {ssh_path:"remoteSshPath", key_path:"remoteKeyPath", host_key_policy:"remoteHostKeyPolicy", connect_timeout:"remoteConnectTimeout"};
    for (const [field, id] of Object.entries(map)) if (e.data?.field_errors?.[field]) document.getElementById(id)?.classList.add("remote-field-error");
    toast(e.message, true);
  }
}

function runnerOptions(selected) {
  const options = [`<option value="local"${selected==="local"?" selected":""}>${t("本机")}</option>`];
  for (const remote of remoteConfigs) options.push(`<option value="${esc(remote.alias)}"${selected===remote.alias?" selected":""}>${esc(remote.alias)}</option>`);
  return options.join("");
}
function runnerTag(runner) {
  return runner && runner !== "local" ? `<span class="runner-tag">${esc(runner)}</span>` : "";
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
  openModal(title, `<div class="form-stack">${source}${warnings}<label>${t("拟分配 ID")}<input value="${esc(nextId)}" readonly></label>${workflowNameInput("importWorkflowName",split.name)}<label>${t("运行位置")}<select id="importRunner" class="ui-select">${runnerOptions("local")}</select></label><label>${t("工作流 YAML")}${yamlEditor("importDefinition",split.definition,true)}</label><span class="form-hint"><i class="yaml-key-sample">${t("高亮字段")}</i>${t("会在导入或运行时使用；migration 仅作来源信息，导入后的定时由数据库单独管理。")}</span><button class="button" onclick="submitImportWorkflow()">${esc(submitLabel)}</button></div>`, false);
}
async function openNewWorkflow() {
  pendingImportFilename = "dagr_example_pipeline.yaml";
  pendingImportSuccessVerb = "新增";
  try {
    const [definition,nextId]=await Promise.all([api("/api/workflows/example-definition"),api("/api/workflows/next-id")]);
    openWorkflowImportEditor(t("新增工作流"),definition,nextId.id,t("新增"));
  } catch(e) { toast(t("加载示例工作流失败：{error}",{error:e.message}),true); }
}
function openImportWorkflow() {
  openModal(t("导入工作流"), `<div class="import-choice">
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
    openWorkflowImportEditor(t("新增工作流"),preview.definition,nextId.id,t("新增"),preview);
  } catch(e) { toast(t("识别或转换 crontab 失败：{error}",{error:e.message}),true); }
}
async function submitImportWorkflow() {
  if (!workflowEditorCanSave()) return;
  const definition=normalizeYamlNewlines(document.getElementById("importDefinition").value);
  const file=new File([definition],pendingImportFilename,{type:"application/yaml"});
  const form=new FormData(); form.append("file",file); form.append("workflow_id",pendingWorkflowId); form.append("name",document.getElementById("importWorkflowName").value.trim()); form.append("runner",document.getElementById("importRunner")?.value || "local");
  try { const result=await api("/api/workflows/import",{method:"POST",body:form}); closeModal(); const message=pendingImportSuccessVerb === "新增" ? "已新增 {name}（{id}）" : "已导入 {name}（{id}）"; toast(t(message,{name:result.name,id:result.id})); await loadWorkflows(); } catch(e) { toast(e.message,true); }
}
async function editWorkflow(name) {
  const workflow=state.workflows.get(name); if (!workflow || workflow.schedule.enabled) return toast(t("请先下线定时"),true);
  try {
    const definition=await api(`/api/workflows/${encodeURIComponent(name)}/yaml`);
    const split=splitWorkflowName(definition);
    openModal(`${namedId(workflow.description,name)} · ${t("编辑")}`, `<div class="form-stack"><label>${t("工作流 ID")}<input value="${esc(name)}" readonly></label>${workflowNameInput("workflowDisplayName",workflow.description || split.name)}<label>${t("运行位置")}<select id="workflowRunner" class="ui-select">${runnerOptions(workflow.runner)}</select></label><label>${t("工作流 YAML")}${yamlEditor("workflowDefinition",split.definition)}</label><span class="form-hint"><i class="yaml-key-sample">${t("高亮字段")}</i>${t("会被运行时使用；migration 和 YAML schedule 不参与后续执行。保存后立即写入数据库。")}</span><button class="button" onclick='saveWorkflow(${jsArg(name)})'>${t("保存")}</button></div>`, false);
  } catch(e) { toast(e.message,true); }
}
async function saveWorkflow(name) {
  if (!workflowEditorCanSave()) return;
  try {
    await api(`/api/workflows/${encodeURIComponent(name)}`,{method:"PUT",body:JSON.stringify({definition:normalizeYamlNewlines(document.getElementById("workflowDefinition").value),name:document.getElementById("workflowDisplayName").value.trim(),runner:document.getElementById("workflowRunner")?.value || "local"})});
    closeModal(); toast(t("工作流已更新")); await loadWorkflows();
  } catch(e) { toast(e.message,true); }
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
  openModal(`${namedId(w.description, w.name)} · ${t("DAG 任务")}`, `<div class="table-wrap"><table><thead><tr><th>${t("任务")}</th><th>${t("依赖")}</th><th>${t("命令 / 类型")}</th><th>${t("启用")}</th></tr></thead><tbody>${w.tasks.map(task => `<tr><td><div class="primary">${esc(task.description || task.name)}</div><div class="workflow-id mono">${esc(task.name)}</div></td><td>${esc(task.depends.map(id => taskNamedId(w, id)).join(", ") || "—")}</td><td class="mono">${esc(task.type === "condition" ? t("条件分支") : task.command)}</td><td>${task.enabled ? t("是") : t("否")}</td></tr>`).join("")}</tbody></table></div>`);
}

function showDag(name) {
  openWorkflowViewer('workflow', name);
}

function buildDagSvg(tasks, workflowName, showStatuses=false, preview=false, direction='LR', viewer=false) {
  const byName = new Map(tasks.map(task => [task.name, task]));
  const levels = new Map();
  function getLevel(task) {
    if (levels.has(task.name)) return levels.get(task.name);
    const parents = task.depends.filter(name => byName.has(name));
    const level = parents.length ? 1 + Math.max(...parents.map(name => getLevel(byName.get(name)))) : 0;
    levels.set(task.name, level); return level;
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
  tasks.forEach(task => task.depends.forEach(parentName => {
    const from=positions.get(parentName), to=positions.get(task.name);
    if (!from || !to) return;
    const x1=from.x+(vertical ? nodeWidth/2 : nodeWidth), y1=from.y+(vertical ? nodeHeight : nodeHeight/2);
    const x2=to.x+(vertical ? nodeWidth/2 : 0), y2=to.y+(vertical ? 0 : nodeHeight/2), mid=(x1+x2)/2, midY=(y1+y2)/2;
    const curve=vertical ? `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}` : `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
    edges.push(`<path class="dag-edge" d="${curve}" marker-end="url(#dagArrow)"/>`);
    const parent = byName.get(parentName);
    const branch = parent.success?.includes(task.name) ? '成立' : parent.failure?.includes(task.name) ? '不成立' : '';
    if (branch) edges.push(`<text class="dag-branch-label" x="${mid}" y="${(y1+y2)/2-8}" text-anchor="middle">${branch}</text>`);
  }));
  const nodes=tasks.map(task => {
    const p=positions.get(task.name), title=(task.description || task.name).slice(0,22), taskId=task.name.slice(0,27);
    const status=showStatuses ? (task.status || "PENDING") : "";
    const click=viewer ? `data-task="${esc(task.name)}" onclick='selectViewerTask(${jsArg(task.name)})'` : preview ? `data-task="${esc(task.name)}" onclick='selectPreviewTask(${jsArg(task.name)})'` : showStatuses ? "" : `onclick="selectDagTask('${esc(workflowName)}','${esc(task.name)}')"`;
    const statusText=task.type === "condition" && task.condition_result ? `→ ${task.condition_result.toUpperCase()}` : status;
    return `<g class="dag-node ${task.type === "condition" ? "dag-condition" : ""} ${showStatuses ? "dag-run-node" : ""} ${task.enabled ? "dag-enabled" : "dag-disabled"} ${status ? `dag-status-${status.toLowerCase()}` : ""}" transform="translate(${p.x},${p.y})" ${click}><rect width="${nodeWidth}" height="${nodeHeight}" rx="8"></rect><circle cx="17" cy="19" r="5"></circle><text class="dag-node-title" x="29" y="24">${esc(title)}</text>${statusText ? `<text class="dag-node-status" x="176" y="23" text-anchor="end">${esc(statusText)}</text>` : ""}<text class="dag-node-id" x="15" y="49">${esc(taskId)}</text><title>${esc(task.description || task.name)} · ${esc(task.name)}${statusText ? ` · ${esc(statusText)}` : ""}</title></g>`;
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

function editNotification(name) {
  const workflow = state.workflows.get(name);
  if (!workflow) return;
  const email = workflow.notification?.email || {send_on:"disabled", to:[]};
  const option = (value, label) => `<option value="${value}" ${email.send_on === value ? "selected" : ""}>${t(label)}</option>`;
  openModal(`${namedId(workflow.description, workflow.name)} · ${t("邮件通知")}`, `<div class="notification-editor"><div class="notification-card"><div class="notification-icon">✉</div><div><strong>${t("工作流结果邮件")}</strong><p>${t("每次工作流结束后最多发送一封邮件，发件账号取自 var 通知配置。")}</p></div></div><label>${t("发送时机")}<select id="notificationSendOn" class="ui-select">${option("disabled","不发送")}${option("success","仅成功时发送")}${option("failure","仅失败时发送")}${option("always","成功或失败都发送")}</select></label><label>${t("收件邮箱")}<textarea id="notificationRecipients" class="notification-recipients" placeholder="ops@example.com&#10;owner@example.com">${esc((email.to || []).join("\n"))}</textarea></label><span class="form-hint">${t("每行填写一个邮箱，也可以使用逗号或分号分隔。保存后会更新完整的工作流 YAML 配置。")}</span><button class="button" onclick='saveNotification(${jsArg(name)})'>${t("保存通知配置")}</button></div>`, false, "compact");
}

async function saveNotification(name) {
  const sendOn = document.getElementById("notificationSendOn").value;
  const input = document.getElementById("notificationRecipients");
  const recipients = [...new Set(input.value.split(/[\n,;]+/).map(value => value.trim()).filter(Boolean))];
  input.setCustomValidity("");
  if (sendOn !== "disabled" && !recipients.length) input.setCustomValidity(t("启用邮件通知时至少填写一个收件邮箱"));
  else if (recipients.some(value => !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value))) input.setCustomValidity(t("收件邮箱格式不正确"));
  if (!input.checkValidity()) { input.reportValidity(); input.focus(); return; }
  try {
    await api(`/api/workflows/${encodeURIComponent(name)}/notification`, {method:"PUT", body:JSON.stringify({send_on:sendOn, to:recipients})});
    closeModal();
    toast(t("通知配置已保存"));
    await loadWorkflows();
  } catch(e) { toast(e.message, true); }
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
function openModal(title, body, closeOnBackdrop=true, size="default") {
  disposeWorkflowViewer();
  disposeWorkflowEditor();
  modalBackdropClosable=closeOnBackdrop;
  const modal=document.getElementById("modal");
  modal.querySelector(".modal-card").classList.toggle("compact-modal-card", size === "compact");
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
  renderWorkflows();
  loadRuns();
});

let resourceLoading = false;
let resourceSnapshot = null;
function renderResources(data) {
  const bar = document.getElementById("resourceBar");
  bar.classList.toggle("unavailable", !data);
  bar.querySelector(".resource-refresh span").textContent = t(data ? "10s 刷新" : "暂不可用");
  bar.querySelector(".resource-refresh span").dataset.i18nKey = data ? "10s 刷新" : "暂不可用";
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
async function loadResources() {
  if (resourceLoading) return;
  resourceLoading = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    resourceSnapshot = await api("/api/system/resources", {signal: controller.signal});
    renderResources(resourceSnapshot);
  } catch (_) {
    resourceSnapshot = null;
    renderResources(null);
  } finally {
    clearTimeout(timeout);
    resourceLoading = false;
  }
}
window.addEventListener("dagrunner-language-change", () => renderResources(resourceSnapshot));
loadResources(); setInterval(loadResources, 10000);
loadWorkflows(); loadRuns(); setInterval(loadRuns, 3000); setInterval(loadWorkflows, 15000);
loadRemoteStatus(); setInterval(loadRemoteStatus, 10000);
window.addEventListener("resize", positionWorkflowTags);
