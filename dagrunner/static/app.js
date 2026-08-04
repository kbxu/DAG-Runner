const state = {
  workflows: new Map(),
  workflowSort: { key: "next_run_time", direction: "asc" },
  workflowPage: 1,
  workflowPageSize: 15,
  runs: [],
  runPage: 1,
  runPageSize: 15,
  runPagination: { page: 1, page_size: 15, total: 0, total_pages: 1 },
  runFilters: { workflow: "", status: "", trigger: "" },
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
  if (!response.ok) throw new Error(t(body.error || body || `HTTP ${response.status}`));
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
function namedId(name, id) { return name && name !== id ? (currentLanguage === "en" ? `${name} (${id})` : `${name}（${id}）`) : id; }
function taskNamedId(workflow, taskId) { const task = workflow?.tasks.find(item => item.name === taskId); return namedId(task?.description, taskId); }
const usefulTopKeys = new Set(["description", "setup", "tasks"]);
const usefulTaskKeys = new Set(["type", "description", "command", "depends", "condition", "success", "failure", "args", "cwd", "timeout", "enabled"]);
function yamlEditor(id, value, includeSchedule=false) {
  return `<div class="yaml-editor"><pre id="${id}Highlight" aria-hidden="true">${highlightUsefulYaml(value,includeSchedule)}\n</pre><textarea id="${id}" data-include-schedule="${includeSchedule}" spellcheck="false" oninput="syncYamlHighlight('${id}')" onscroll="syncYamlScroll('${id}')">${esc(value)}</textarea></div>`;
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
  if (input && highlight) { highlight.innerHTML=`${highlightUsefulYaml(input.value,input.dataset.includeSchedule==="true")}\n`; syncYamlScroll(id); }
}
function syncYamlScroll(id) {
  const input=document.getElementById(id), highlight=document.getElementById(`${id}Highlight`);
  if (input && highlight) { highlight.scrollTop=input.scrollTop; highlight.scrollLeft=input.scrollLeft; }
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

function renderWorkflows() {
  hideSchedulePopover();
  const workflows = [...state.workflows.values()];
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
      return `<tr>
        <td><button class="workflow-link" onclick="showDag('${esc(w.name)}')">${esc(w.description || w.name)}</button><div class="workflow-id mono" title="${esc(w.name)}">${esc(w.name)}</div></td>
        <td><span class="primary">${w.task_count}</span> ${t("个节点")}</td>
        <td><span class="badge ${s.enabled ? "enabled" : "disabled"}">${s.enabled ? t("已启用") : t("未启用")}</span></td>
        <td>${scheduleSummary(s)}</td>
        <td>${fmt(w.last_run_time)}</td>
        <td>${fmt(s.next_run_time)}</td>
        <td><div class="actions"><button class="button" onclick="runWorkflow('${esc(w.name)}')">${t("运行")}</button><button class="button ghost" onclick="showDag('${esc(w.name)}')">DAG</button><button class="button ghost" onclick="showTasks('${esc(w.name)}')">${t("任务")}</button><button class="button ghost" onclick="editSchedule('${esc(w.name)}')">${t("定时")}</button>${s.enabled ? "" : `<button class="button ghost" onclick="editWorkflow('${esc(w.name)}')">${t("编辑")}</button>`}<button class="button ghost" onclick="exportYaml('${esc(w.name)}')">${t("导出 YAML")}</button>${s.enabled ? "" : `<button class="button danger" onclick="deleteWorkflow('${esc(w.name)}')">${t("删除")}</button>`}</div></td>
      </tr>`;
    }).join("") || `<tr><td colspan="7" class="empty">${t("还没有导入工作流")}</td></tr>`;
  document.getElementById("workflowPagination").innerHTML = `<span>${t("共 {total} 条 · 第 {page} / {pages} 页", {total:workflows.length,page:state.workflowPage,pages:totalPages})}</span><div><button class="button ghost" ${state.workflowPage <= 1 ? "disabled" : ""} onclick="changeWorkflowPage(${state.workflowPage - 1})">${t("上一页")}</button><button class="button ghost" ${state.workflowPage >= totalPages ? "disabled" : ""} onclick="changeWorkflowPage(${state.workflowPage + 1})">${t("下一页")}</button></div>`;
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
      const actions = [`<button class="button ghost" onclick="showRun('${r.run_id}')">${t("详情")}</button>`];
      if (r.status === "RUNNING") actions.push(`<button class="button danger" onclick="runAction('${r.run_id}','stop')">${t("停止")}</button>`);
      else {
        actions.push(`<button class="button ghost" onclick="runAction('${r.run_id}','rerun')">${t("重跑")}</button>`);
        if (r.status === "FAILED" && r.error_message !== "stopped by user") actions.push(`<button class="button" onclick="runAction('${r.run_id}','resume')">${t("失败续跑")}</button>`);
        actions.push(`<button class="button danger" onclick="deleteRun('${r.run_id}')">${t("删除")}</button>`);
      }
      const handled = r.handled_count ? ` · ${t("已处理 {count}", {count:r.handled_count})}` : "";
      const triggerLabels = {manual:t("手动"),schedule:t("定时"),rerun:t("重跑"),resume:t("失败续跑")};
      return `<tr><td><div>${fmt(r.start_time)}</div><div class="secondary mono">${esc(r.run_id)}</div></td><td><div class="primary">${esc(r.workflow_description || r.workflow_name)}</div><div class="workflow-id mono">${esc(r.workflow_name)}</div></td><td>${esc(triggerLabels[r.trigger_type] || r.trigger_type || "manual")}</td><td>${statusBadge(r.status)}</td><td><div class="progress"><span style="width:${percent}%"></span></div><div class="progress-label">${complete}/${r.task_count || 0} · ${t("成功")} ${r.success_count || 0}${handled}</div></td><td>${duration(r.start_time, r.end_time)}</td><td><div class="actions">${actions.join("")}</div></td></tr>`;
    }).join("") || `<tr><td colspan="7" class="empty">${t("没有符合条件的运行记录")}</td></tr>`;
    renderRunPagination();
  } catch (error) { if (!error.offline) toast(error.message, true); }
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
async function logout() { try { const result=await api("/api/auth/logout",{method:"POST",body:"{}"}); window.location.replace(result.redirect || "/login"); } catch(e) { if (!e.loginRequired) toast(e.message,true); } }
let pendingImportFilename = "workflow.yaml";
let pendingImportSuccessVerb = "导入";
function openWorkflowImportEditor(title, definition, nextId, submitLabel, sourceInfo=null) {
  const source = sourceInfo ? `<div class="import-source"><span>${t("识别来源")}</span><b>${esc(sourceInfo.source_label)}</b></div>` : "";
  const warnings = sourceInfo?.warnings?.length ? `<div class="import-warnings"><b>${t("转换提示")}</b>${sourceInfo.warnings.map(warning => `<span>${esc(warning)}</span>`).join("")}</div>` : "";
  openModal(title, `<div class="form-stack">${source}${warnings}<label>${t("拟分配 ID")}<input value="${esc(nextId)}" readonly></label><label>${t("工作流 YAML")}${yamlEditor("importDefinition",definition,true)}</label><span class="form-hint"><i class="yaml-key-sample">${t("高亮字段")}</i>${t("会在导入或运行时使用；中文名称取自 description。顶层 name 和 migration 仅作来源信息，导入后的定时由数据库单独管理。")}</span><button class="button" onclick="submitImportWorkflow()">${esc(submitLabel)}</button></div>`, false);
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
  const input=document.createElement("input");
  input.type="file"; input.accept=".yaml,.yml,.json,.xml,text/yaml,application/json,text/xml,application/xml";
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
async function submitImportWorkflow() {
  const definition=document.getElementById("importDefinition").value;
  const file=new File([definition],pendingImportFilename,{type:"application/yaml"});
  const form=new FormData(); form.append("file",file);
  try { const result=await api("/api/workflows/import",{method:"POST",body:form}); closeModal(); const message=pendingImportSuccessVerb === "新增" ? "已新增 {name}（{id}）" : "已导入 {name}（{id}）"; toast(t(message,{name:result.name,id:result.id})); await loadWorkflows(); } catch(e) { toast(e.message,true); }
}
async function editWorkflow(name) {
  const workflow=state.workflows.get(name); if (!workflow || workflow.schedule.enabled) return toast(t("请先下线定时"),true);
  try {
    const definition=await api(`/api/workflows/${encodeURIComponent(name)}/yaml`);
    openModal(`${namedId(workflow.description,name)} · ${t("编辑")}`, `<div class="form-stack"><label>${t("工作流 ID")}<input value="${esc(name)}" readonly></label><label>${t("工作流 YAML")}${yamlEditor("workflowDefinition",definition)}</label><span class="form-hint"><i class="yaml-key-sample">${t("高亮字段")}</i>${t("会被运行时使用；顶层 name、migration 和 YAML schedule 不参与后续执行。保存后立即写入数据库。")}</span><button class="button" onclick='saveWorkflow(${jsArg(name)})'>${t("保存")}</button></div>`, false);
  } catch(e) { toast(e.message,true); }
}
async function saveWorkflow(name) {
  try {
    await api(`/api/workflows/${encodeURIComponent(name)}`,{method:"PUT",body:JSON.stringify({definition:document.getElementById("workflowDefinition").value})});
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
  const workflow = state.workflows.get(name); if (!workflow) return;
  const svg = buildDagSvg(workflow.tasks, name);
  openModal(`${namedId(workflow.description, workflow.name)} · ${t("DAG 结构")}`, `<div class="dag-legend"><span><i class="legend-node active-node"></i>${t("启用节点")}</span><span><i class="legend-node inactive-node"></i>${t("禁用节点")}</span><span>${t("从左向右表示依赖方向")}</span></div><div class="dag-scroll">${svg}</div><div id="dagTaskDetail" class="dag-detail"><span class="secondary">${t("点击节点查看任务命令和依赖")}</span></div>`);
}

function buildDagSvg(tasks, workflowName, showStatuses=false) {
  const byName = new Map(tasks.map(task => [task.name, task]));
  const levels = new Map();
  function getLevel(task) {
    if (levels.has(task.name)) return levels.get(task.name);
    const level = task.depends.length ? 1 + Math.max(...task.depends.map(name => getLevel(byName.get(name)))) : 0;
    levels.set(task.name, level); return level;
  }
  tasks.forEach(getLevel);
  const columns = new Map();
  tasks.forEach(task => { const level=levels.get(task.name); if (!columns.has(level)) columns.set(level, []); columns.get(level).push(task); });
  const nodeWidth=190, nodeHeight=68, columnGap=85, rowGap=42, margin=45;
  const maxLevel=Math.max(0, ...levels.values());
  const maxRows=Math.max(1, ...[...columns.values()].map(items => items.length));
  const width=margin*2+(maxLevel+1)*nodeWidth+maxLevel*columnGap;
  const height=margin*2+maxRows*nodeHeight+(maxRows-1)*rowGap;
  const positions=new Map();
  for (const [level, items] of columns) {
    const contentHeight=items.length*nodeHeight+(items.length-1)*rowGap;
    const startY=(height-contentHeight)/2;
    items.forEach((task,index) => positions.set(task.name,{x:margin+level*(nodeWidth+columnGap),y:startY+index*(nodeHeight+rowGap)}));
  }
  const edges=[];
  tasks.forEach(task => task.depends.forEach(parentName => {
    const from=positions.get(parentName), to=positions.get(task.name);
    const x1=from.x+nodeWidth, y1=from.y+nodeHeight/2, x2=to.x, y2=to.y+nodeHeight/2, mid=(x1+x2)/2;
    edges.push(`<path class="dag-edge" d="M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}" marker-end="url(#dagArrow)"/>`);
  }));
  const nodes=tasks.map(task => {
    const p=positions.get(task.name), title=(task.description || task.name).slice(0,22), taskId=task.name.slice(0,27);
    const status=showStatuses ? (task.status || "PENDING") : "";
    const click=showStatuses ? "" : `onclick="selectDagTask('${esc(workflowName)}','${esc(task.name)}')"`;
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
  openModal(`${namedId(workflow.description, workflow.name)} · ${t("定时配置")}`, `<div class="schedule-editor-head"><label>${t("统一时区")}<input id="timezoneInput" value="${esc(s.timezone || "Asia/Shanghai")}" oninput="this.setCustomValidity('')"></label><button class="button ghost" type="button" onclick="addScheduleRow()">＋ ${t("增加定时")}</button></div><div id="scheduleInputs" class="schedule-inputs">${rows}</div><span class="form-hint">${t("Cron 使用 5 个字段。每小时第 10 分钟：10 * * * *　每天 10:00：0 10 * * *")}</span><label class="check"><input id="enabledInput" type="checkbox" ${s.enabled ? "checked" : ""}>${t("启用全部自动调度")}</label><button class="button" onclick='saveSchedule(${jsArg(name)})'>${t("保存配置")}</button>`, false);
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
  const bounds = [[0,59], [0,23], [1,31], [1,12], [0,6]], [minimum, maximum] = bounds[index];
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
  try {
    const data = await api(`/api/runs/${encodeURIComponent(id)}`);
    const rows = data.tasks.map(taskRun => {
      const label = taskRun.task_description || taskRun.task_name;
      const logButton = taskRun.log_file ? `<button class="button ghost" onclick='showLog(${jsArg(id)}, ${jsArg(taskRun.task_name)}, ${jsArg(label)})'>${t("日志")}</button>` : "—";
      const handled = taskRun.handled_by ? `<div class="handled-note">${t("已由 {name} 处理",{name:esc(taskRun.handled_by)})}</div>` : "";
      return `<tr><td><div class="primary">${esc(label)}</div></td><td class="mono">${esc(taskRun.task_name)}</td><td>${statusBadge(taskRun.status)}${handled}</td><td>${fmt(taskRun.start_time)}</td><td>${fmt(taskRun.end_time)}</td><td>${taskRun.exit_code ?? "—"}</td><td><div class="secondary" title="${esc(taskRun.error_message)}">${esc(taskRun.error_message || "")}</div></td><td>${logButton}</td></tr>`;
    }).join("");
    const dag=data.graph_tasks?.length ? `<div class="run-dag"><div class="dag-legend"><span><i class="legend-node status-running"></i>RUNNING</span><span><i class="legend-node status-success"></i>SUCCESS</span><span><i class="legend-node status-failed"></i>FAILED</span><span><i class="legend-node status-skipped"></i>SKIPPED</span><span><i class="legend-node status-pending"></i>PENDING</span></div><div class="dag-scroll">${buildDagSvg(data.graph_tasks,data.run.workflow_name,true)}</div></div>` : "";
    openModal(`${namedId(data.run.workflow_description, data.run.workflow_name)} · ${id}`, `<div class="detail-toolbar"><button class="button ghost" onclick='showRun(${jsArg(id)})'>${t("刷新状态")}</button></div>${dag}<div class="table-wrap"><table><thead><tr><th>${t("任务")}</th><th>ID</th><th>${t("状态")}</th><th>${t("开始")}</th><th>${t("结束")}</th><th>Exit</th><th>${t("错误")}</th><th>${t("日志")}</th></tr></thead><tbody>${rows}</tbody></table></div>`);
  } catch(e) { toast(e.message, true); }
}
async function showLog(runId, task, label) {
  try {
    const text = await api(`/api/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(task)}/log`);
    document.getElementById("logModalTitle").textContent = currentLanguage === "en" ? `${label || task} (${task})` : `${label || task}（${task}）`;
    document.getElementById("logContent").textContent = text;
    document.getElementById("logModal").classList.remove("hidden");
  } catch(e) { toast(e.message, true); }
}

async function copyLog() {
  const content = document.getElementById("logContent");
  if (!content) return;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(content.textContent);
    } else {
      const input = document.createElement("textarea");
      input.value = content.textContent;
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
function openModal(title, body, closeOnBackdrop=true) { modalBackdropClosable=closeOnBackdrop; document.getElementById("modalTitle").textContent = title; document.getElementById("modalBody").innerHTML = body; document.getElementById("modal").classList.remove("hidden"); }
function closeModal() { closeLogModal(); document.getElementById("modal").classList.add("hidden"); }
function closeModalFromBackdrop(event) { if (event.target.id === "modal" && modalBackdropClosable) closeModal(); }
function closeLogModal() { document.getElementById("logModal").classList.add("hidden"); }
function closeLogModalFromBackdrop(event) { if (event.target.id === "logModal") closeLogModal(); }
let toastTimer; function toast(message, error=false) { const box=document.getElementById("toast"); box.textContent=message; box.style.background=error?"#b42318":"#182230"; box.classList.remove("hidden"); clearTimeout(toastTimer); toastTimer=setTimeout(()=>box.classList.add("hidden"),4000); }

window.addEventListener("dagrunner-language-change", () => {
  const previousState = serviceState;
  serviceState = "";
  setServiceStatus(previousState);
  renderWorkflows();
  loadRuns();
});

loadWorkflows(); loadRuns(); setInterval(loadRuns, 3000); setInterval(loadWorkflows, 15000);
