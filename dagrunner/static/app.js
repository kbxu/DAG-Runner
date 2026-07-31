const state = {
  workflows: new Map(),
  workflowSort: { key: "", direction: "asc" },
  workflowPage: 1,
  workflowPageSize: 15,
  runs: [],
  runPage: 1,
  runPageSize: 20,
  runPagination: { page: 1, page_size: 20, total: 0, total_pages: 1 },
  runFilters: { workflow: "", status: "", trigger: "" },
};
let serviceState = "checking";
let runLoadSequence = 0;
let runSearchTimer = null;

async function api(url, options = {}) {
  let response;
  try {
    const headers = options.body instanceof FormData ? {} : { "Content-Type": "application/json" };
    response = await fetch(url, { headers, ...options });
    setServiceStatus("online");
  } catch (cause) {
    setServiceStatus("offline");
    const error = new Error("后端服务已断开");
    error.offline = true;
    error.cause = cause;
    throw error;
  }
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : await response.text();
  if (response.status === 401 && body?.login_url) {
    window.location.assign(body.login_url);
    const error = new Error("登录已失效，请重新登录");
    error.loginRequired = true;
    throw error;
  }
  if (!response.ok) throw new Error(body.error || body || `HTTP ${response.status}`);
  return body;
}

function setServiceStatus(status) {
  if (serviceState === status) return;
  serviceState = status;
  const box = document.getElementById("serviceStatus");
  if (!box) return;
  box.classList.remove("online", "offline", "checking");
  box.classList.add(status);
  box.querySelector("b").textContent = status === "online" ? "服务在线" : status === "offline" ? "服务断线" : "正在连接";
}

function esc(value) { return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c])); }
function jsArg(value) { return JSON.stringify(String(value ?? "")).replace(/</g, "\\u003c").replace(/>/g, "\\u003e").replace(/&/g, "\\u0026").replace(/'/g, "\\u0027"); }
function namedId(name, id) { return name && name !== id ? `${name}（${id}）` : id; }
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
      (includeSchedule && topSection==="schedule" && indent===2 && ["cron","timezone","enabled"].includes(key));
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
function duration(start, end) { if (!start) return "—"; const seconds = Math.max(0, (new Date(end || Date.now()) - new Date(start)) / 1000); return seconds < 60 ? `${Math.round(seconds)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`; }
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
        <td><span class="primary">${w.task_count}</span> 个节点</td>
        <td><span class="badge ${s.enabled ? "enabled" : "disabled"}">${s.enabled ? "已启用" : "未启用"}</span></td>
        <td><div class="mono">${esc(s.cron || "未配置")}</div><div class="secondary">${esc(s.timezone)}</div></td>
        <td>${fmt(w.last_run_time)}</td>
        <td>${fmt(s.next_run_time)}</td>
        <td><div class="actions"><button class="button" onclick="runWorkflow('${esc(w.name)}')">运行</button><button class="button ghost" onclick="showDag('${esc(w.name)}')">DAG</button><button class="button ghost" onclick="showTasks('${esc(w.name)}')">任务</button><button class="button ghost" onclick="editSchedule('${esc(w.name)}')">定时</button>${s.enabled ? "" : `<button class="button ghost" onclick="editWorkflow('${esc(w.name)}')">编辑</button>`}<button class="button ghost" onclick="exportYaml('${esc(w.name)}')">导出 YAML</button><button class="button danger" onclick="deleteWorkflow('${esc(w.name)}')">删除</button></div></td>
      </tr>`;
    }).join("") || `<tr><td colspan="7" class="empty">还没有导入工作流</td></tr>`;
  document.getElementById("workflowPagination").innerHTML = `<span>共 ${workflows.length} 条 · 第 ${state.workflowPage} / ${totalPages} 页</span><div><button class="button ghost" ${state.workflowPage <= 1 ? "disabled" : ""} onclick="changeWorkflowPage(${state.workflowPage - 1})">上一页</button><button class="button ghost" ${state.workflowPage >= totalPages ? "disabled" : ""} onclick="changeWorkflowPage(${state.workflowPage + 1})">下一页</button></div>`;
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
      const actions = [`<button class="button ghost" onclick="showRun('${r.run_id}')">详情</button>`];
      if (r.status === "RUNNING") actions.push(`<button class="button danger" onclick="runAction('${r.run_id}','stop')">停止</button>`);
      else {
        actions.push(`<button class="button ghost" onclick="runAction('${r.run_id}','rerun')">重跑</button>`);
        if (r.status === "FAILED" && r.error_message !== "stopped by user") actions.push(`<button class="button" onclick="runAction('${r.run_id}','resume')">失败续跑</button>`);
        actions.push(`<button class="button danger" onclick="deleteRun('${r.run_id}')">删除</button>`);
      }
      const handled = r.handled_count ? ` · 已处理 ${r.handled_count}` : "";
      return `<tr><td><div>${fmt(r.start_time)}</div><div class="secondary mono">${esc(r.run_id)}</div></td><td><div class="primary">${esc(r.workflow_description || r.workflow_name)}</div><div class="workflow-id mono">${esc(r.workflow_name)}</div></td><td>${esc(r.trigger_type || "manual")}</td><td>${statusBadge(r.status)}</td><td><div class="progress"><span style="width:${percent}%"></span></div><div class="progress-label">${complete}/${r.task_count || 0} · 成功 ${r.success_count || 0}${handled}</div></td><td>${duration(r.start_time, r.end_time)}</td><td><div class="actions">${actions.join("")}</div></td></tr>`;
    }).join("") || `<tr><td colspan="7" class="empty">没有符合条件的运行记录</td></tr>`;
    renderRunPagination();
  } catch (error) { if (!error.offline) toast(error.message, true); }
}

function renderRunPagination() {
  const p = state.runPagination;
  document.getElementById("runPagination").innerHTML = `<span>共 ${p.total} 条 · 第 ${p.page} / ${p.total_pages} 页</span><div><button class="button ghost" ${p.page <= 1 ? "disabled" : ""} onclick="changeRunPage(${p.page - 1})">上一页</button><button class="button ghost" ${p.page >= p.total_pages ? "disabled" : ""} onclick="changeRunPage(${p.page + 1})">下一页</button></div>`;
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

async function runWorkflow(name) { try { const r = await api(`/api/workflows/${encodeURIComponent(name)}/run`, {method:"POST", body:"{}"}); toast(`已提交 ${r.run_id}`); setTimeout(loadRuns, 250); } catch(e) { toast(e.message, true); } }
async function logout() { try { const result=await api("/api/auth/logout",{method:"POST",body:"{}"}); window.location.replace(result.redirect || "/login"); } catch(e) { if (!e.loginRequired) toast(e.message,true); } }
let pendingImportFilename = "workflow.yaml";
function openImportWorkflow() {
  const input=document.createElement("input");
  input.type="file"; input.accept=".yaml,.yml,text/yaml";
  input.onchange=async () => {
    const file=input.files[0]; if (!file) return;
    pendingImportFilename=file.name;
    try {
      const [definition,nextId]=await Promise.all([file.text(),api("/api/workflows/next-id")]);
      openModal("导入并编辑工作流", `<div class="form-stack"><label>拟分配 ID<input value="${esc(nextId.id)}" readonly></label><label>工作流 YAML${yamlEditor("importDefinition",definition,true)}</label><span class="form-hint"><i class="yaml-key-sample">高亮字段</i>会在导入或运行时使用；中文名称取自 description。顶层 name 和 migration 仅作来源信息，导入后的定时由数据库单独管理。</span><button class="button" onclick="submitImportWorkflow()">导入</button></div>`, false);
    } catch(e) { toast(`读取文件失败：${e.message}`,true); }
  };
  input.click();
}
async function submitImportWorkflow() {
  const definition=document.getElementById("importDefinition").value;
  const file=new File([definition],pendingImportFilename,{type:"application/yaml"});
  const form=new FormData(); form.append("file",file);
  try { const result=await api("/api/workflows/import",{method:"POST",body:form}); closeModal(); toast(`已导入 ${result.name}（${result.id}）`); await loadWorkflows(); } catch(e) { toast(e.message,true); }
}
async function editWorkflow(name) {
  const workflow=state.workflows.get(name); if (!workflow || workflow.schedule.enabled) return toast("请先下线定时",true);
  try {
    const definition=await api(`/api/workflows/${encodeURIComponent(name)}/yaml`);
    openModal(`${namedId(workflow.description,name)} · 编辑`, `<div class="form-stack"><label>工作流 ID<input value="${esc(name)}" readonly></label><label>工作流 YAML${yamlEditor("workflowDefinition",definition)}</label><span class="form-hint"><i class="yaml-key-sample">高亮字段</i>会被运行时使用；顶层 name、migration 和 YAML schedule 不参与后续执行。保存后立即写入数据库。</span><button class="button" onclick='saveWorkflow(${jsArg(name)})'>保存</button></div>`, false);
  } catch(e) { toast(e.message,true); }
}
async function saveWorkflow(name) {
  try {
    await api(`/api/workflows/${encodeURIComponent(name)}`,{method:"PUT",body:JSON.stringify({definition:document.getElementById("workflowDefinition").value})});
    closeModal(); toast("工作流已更新"); await loadWorkflows();
  } catch(e) { toast(e.message,true); }
}
async function runAction(id, action) { if (action === "stop" && !confirm("确定停止这个运行实例？当前子进程会被终止。")) return; try { const r = await api(`/api/runs/${encodeURIComponent(id)}/${action}`, {method:"POST", body:"{}"}); toast(action === "stop" ? "已发送停止请求" : `已提交新运行 ${r.run_id}`); setTimeout(loadRuns, 300); } catch(e) { toast(e.message, true); } }
async function deleteWorkflow(name) {
  const workflow = state.workflows.get(name), label = namedId(workflow?.description, name);
  if (!await confirmDelete(`删除工作流“${label}”？`, "将同时删除工作流正文、定时配置、全部历史运行记录、任务记录和对应日志，此操作不可恢复。")) return;
  try {
    await api(`/api/workflows/${encodeURIComponent(name)}`, {method:"DELETE"});
    toast(`已删除工作流 ${label}`);
    await loadWorkflows();
  } catch(e) { toast(e.message, true); }
}
async function deleteRun(id) {
  if (!await confirmDelete("删除这条运行记录？", `${id}\n对应的任务日志也会一并删除。`)) return;
  try {
    await api(`/api/runs/${encodeURIComponent(id)}`, {method:"DELETE"});
    toast(`已删除运行记录 ${id}`);
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
  openModal(`${namedId(w.description, w.name)} · DAG 任务`, `<div class="table-wrap"><table><thead><tr><th>任务</th><th>依赖</th><th>命令 / 类型</th><th>启用</th></tr></thead><tbody>${w.tasks.map(t => `<tr><td><div class="primary">${esc(t.description || t.name)}</div><div class="workflow-id mono">${esc(t.name)}</div></td><td>${esc(t.depends.map(id => taskNamedId(w, id)).join(", ") || "—")}</td><td class="mono">${esc(t.type === "condition" ? "条件分支" : t.command)}</td><td>${t.enabled ? "是" : "否"}</td></tr>`).join("")}</tbody></table></div>`);
}

function showDag(name) {
  const workflow = state.workflows.get(name); if (!workflow) return;
  const svg = buildDagSvg(workflow.tasks, name);
  openModal(`${namedId(workflow.description, workflow.name)} · DAG 结构`, `<div class="dag-legend"><span><i class="legend-node active-node"></i>启用节点</span><span><i class="legend-node inactive-node"></i>禁用节点</span><span>从左向右表示依赖方向</span></div><div class="dag-scroll">${svg}</div><div id="dagTaskDetail" class="dag-detail"><span class="secondary">点击节点查看任务命令和依赖</span></div>`);
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
  const detail=task.type === "condition" ? `<div class="dag-meta"><b>类型：</b><code>条件分支</code></div><div class="dag-meta"><b>成功：</b><span>${esc(task.success.map(id => taskNamedId(workflow, id)).join(", ") || "无")}</span></div><div class="dag-meta"><b>失败：</b><span>${esc(task.failure.map(id => taskNamedId(workflow, id)).join(", ") || "无")}</span></div>` : `<div class="dag-meta"><b>命令：</b><code>${esc(task.command)}</code></div>`;
  target.innerHTML=`<div><span class="badge ${task.enabled ? "enabled" : "disabled"}">${task.enabled ? "已启用" : "已禁用"}</span> <strong>${esc(task.description || task.name)}</strong> <span class="secondary dag-inline mono">${esc(task.name)}</span></div><div class="dag-meta"><b>依赖：</b><span>${esc(task.depends.map(id => taskNamedId(workflow, id)).join(", ") || "无")}</span></div>${detail}`;
}

function exportYaml(name) { window.location.assign(`/api/workflows/${encodeURIComponent(name)}/yaml`); }

function editSchedule(name) {
  const workflow = state.workflows.get(name), s = workflow.schedule;
  openModal(`${namedId(workflow.description, workflow.name)} · 定时配置`, `<div class="form-grid"><label>Cron（5 字段）<input id="cronInput" value="${esc(s.cron || "0 18 * * mon-fri")}"><span class="form-hint">每小时第 10 分钟：10 * * * *　每天 10:00：0 10 * * *</span></label><label>时区<input id="timezoneInput" value="${esc(s.timezone || "Asia/Shanghai")}"></label></div><label class="check"><input id="enabledInput" type="checkbox" ${s.enabled ? "checked" : ""}>启用自动调度</label><button class="button" onclick="saveSchedule('${esc(name)}')">保存配置</button>`);
}
async function saveSchedule(name) { try { await api(`/api/workflows/${encodeURIComponent(name)}/schedule`, {method:"PUT", body:JSON.stringify({cron:document.getElementById("cronInput").value, timezone:document.getElementById("timezoneInput").value, enabled:document.getElementById("enabledInput").checked})}); closeModal(); toast("定时配置已保存"); loadWorkflows(); } catch(e) { toast(e.message, true); } }

async function showRun(id) {
  try {
    const data = await api(`/api/runs/${encodeURIComponent(id)}`);
    const rows = data.tasks.map(t => {
      const label = t.task_description || t.task_name;
      const logButton = t.log_file ? `<button class="button ghost" onclick='showLog(${jsArg(id)}, ${jsArg(t.task_name)}, ${jsArg(label)})'>日志</button>` : "—";
      const handled = t.handled_by ? `<div class="handled-note">已由 ${esc(t.handled_by)} 处理</div>` : "";
      return `<tr><td><div class="primary">${esc(label)}</div></td><td class="mono">${esc(t.task_name)}</td><td>${statusBadge(t.status)}${handled}</td><td>${fmt(t.start_time)}</td><td>${fmt(t.end_time)}</td><td>${t.exit_code ?? "—"}</td><td><div class="secondary" title="${esc(t.error_message)}">${esc(t.error_message || "")}</div></td><td>${logButton}</td></tr>`;
    }).join("");
    const dag=data.graph_tasks?.length ? `<div class="run-dag"><div class="dag-legend"><span><i class="legend-node status-running"></i>RUNNING</span><span><i class="legend-node status-success"></i>SUCCESS</span><span><i class="legend-node status-failed"></i>FAILED</span><span><i class="legend-node status-skipped"></i>SKIPPED</span><span><i class="legend-node status-pending"></i>PENDING</span></div><div class="dag-scroll">${buildDagSvg(data.graph_tasks,data.run.workflow_name,true)}</div></div>` : "";
    openModal(`${namedId(data.run.workflow_description, data.run.workflow_name)} · ${id}`, `<div class="detail-toolbar"><button class="button ghost" onclick='showRun(${jsArg(id)})'>刷新状态</button></div>${dag}<div class="table-wrap"><table><thead><tr><th>任务</th><th>ID</th><th>状态</th><th>开始</th><th>结束</th><th>Exit</th><th>错误</th><th>日志</th></tr></thead><tbody>${rows}</tbody></table></div>`);
  } catch(e) { toast(e.message, true); }
}
async function showLog(runId, task, label) {
  try {
    const text = await api(`/api/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(task)}/log`);
    document.getElementById("logModalTitle").textContent = `${label || task}（${task}）`;
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
      if (!copied) throw new Error("浏览器不支持自动复制");
    }
    toast("日志已复制");
  } catch(e) { toast(`复制失败：${e.message}`, true); }
}

let modalBackdropClosable = true;
function openModal(title, body, closeOnBackdrop=true) { modalBackdropClosable=closeOnBackdrop; document.getElementById("modalTitle").textContent = title; document.getElementById("modalBody").innerHTML = body; document.getElementById("modal").classList.remove("hidden"); }
function closeModal() { closeLogModal(); document.getElementById("modal").classList.add("hidden"); }
function closeModalFromBackdrop(event) { if (event.target.id === "modal" && modalBackdropClosable) closeModal(); }
function closeLogModal() { document.getElementById("logModal").classList.add("hidden"); }
function closeLogModalFromBackdrop(event) { if (event.target.id === "logModal") closeLogModal(); }
let toastTimer; function toast(message, error=false) { const box=document.getElementById("toast"); box.textContent=message; box.style.background=error?"#b42318":"#182230"; box.classList.remove("hidden"); clearTimeout(toastTimer); toastTimer=setTimeout(()=>box.classList.add("hidden"),4000); }

loadWorkflows(); loadRuns(); setInterval(loadRuns, 3000); setInterval(loadWorkflows, 15000);
