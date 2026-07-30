const state = { workflows: new Map(), runs: [] };
let serviceState = "checking";

async function api(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
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
function fmt(value) { if (!value) return "—"; const d = new Date(value); return Number.isNaN(d.valueOf()) ? esc(value) : d.toLocaleString(); }
function duration(start, end) { if (!start) return "—"; const seconds = Math.max(0, (new Date(end || Date.now()) - new Date(start)) / 1000); return seconds < 60 ? `${Math.round(seconds)} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`; }
function statusBadge(status) { return `<span class="badge ${esc(status)}">${esc(status)}</span>`; }

async function loadWorkflows() {
  try {
    const data = await api("/api/workflows");
    state.workflows = new Map(data.workflows.map(w => [w.name, w]));
    document.getElementById("workflowRows").innerHTML = data.workflows.map(w => {
      const s = w.schedule;
      return `<tr>
        <td><button class="workflow-link" onclick="showDag('${esc(w.name)}')">${esc(w.description || w.name)}</button><div class="workflow-id mono" title="${esc(w.name)}">${esc(w.name)}</div></td>
        <td><span class="primary">${w.task_count}</span> 个节点</td>
        <td><span class="badge ${s.enabled ? "enabled" : "disabled"}">${s.enabled ? "已启用" : "未启用"}</span></td>
        <td><div class="mono">${esc(s.cron || "未配置")}</div><div class="secondary">${esc(s.timezone)}</div></td>
        <td>${fmt(s.next_run_time)}</td>
        <td><div class="actions"><button class="button" onclick="runWorkflow('${esc(w.name)}')">运行</button><button class="button ghost" onclick="showDag('${esc(w.name)}')">DAG</button><button class="button ghost" onclick="showTasks('${esc(w.name)}')">任务</button><button class="button ghost" onclick="editSchedule('${esc(w.name)}')">定时</button><button class="button ghost" onclick="exportYaml('${esc(w.name)}')">导出 YAML</button></div></td>
      </tr>`;
    }).join("") || `<tr><td colspan="6" class="empty">没有找到 YAML 工作流</td></tr>`;
    const errors = Object.entries(data.config_errors || {});
    const box = document.getElementById("configErrors");
    box.classList.toggle("hidden", !errors.length);
    box.textContent = errors.map(([file, error]) => `${file}: ${error}`).join("\n");
  } catch (error) { if (!error.offline) toast(error.message, true); }
}

async function loadRuns() {
  try {
    const data = await api("/api/runs?limit=100"); state.runs = data.runs;
    document.getElementById("runRows").innerHTML = data.runs.map(r => {
      const complete = (r.success_count || 0) + (r.failed_count || 0) + (r.skipped_count || 0);
      const percent = r.task_count ? Math.round(complete * 100 / r.task_count) : 0;
      const actions = [`<button class="button ghost" onclick="showRun('${r.run_id}')">详情</button>`];
      if (r.active) actions.push(`<button class="button danger" onclick="runAction('${r.run_id}','stop')">停止</button>`);
      else {
        actions.push(`<button class="button ghost" onclick="runAction('${r.run_id}','rerun')">重跑</button>`);
        if (r.status === "FAILED" && r.error_message !== "stopped by user") actions.push(`<button class="button" onclick="runAction('${r.run_id}','resume')">失败续跑</button>`);
      }
      return `<tr><td><div>${fmt(r.start_time)}</div><div class="secondary mono">${esc(r.run_id)}</div></td><td class="primary">${esc(r.workflow_name)}</td><td>${esc(r.trigger_type || "manual")}</td><td>${statusBadge(r.status)}</td><td><div class="progress"><span style="width:${percent}%"></span></div><div class="progress-label">${complete}/${r.task_count || 0} · 成功 ${r.success_count || 0}</div></td><td>${duration(r.start_time, r.end_time)}</td><td><div class="actions">${actions.join("")}</div></td></tr>`;
    }).join("") || `<tr><td colspan="7" class="empty">还没有运行记录</td></tr>`;
  } catch (error) { if (!error.offline) toast(error.message, true); }
}

async function runWorkflow(name) { try { const r = await api(`/api/workflows/${encodeURIComponent(name)}/run`, {method:"POST", body:"{}"}); toast(`已提交 ${r.run_id}`); setTimeout(loadRuns, 250); } catch(e) { toast(e.message, true); } }
async function runAction(id, action) { if (action === "stop" && !confirm("确定停止这个运行实例？当前子进程会被终止。")) return; try { const r = await api(`/api/runs/${encodeURIComponent(id)}/${action}`, {method:"POST", body:"{}"}); toast(action === "stop" ? "已发送停止请求" : `已提交新运行 ${r.run_id}`); setTimeout(loadRuns, 300); } catch(e) { toast(e.message, true); } }

function showTasks(name) {
  const w = state.workflows.get(name); if (!w) return;
  openModal(`${name} · DAG 任务`, `<div class="table-wrap"><table><thead><tr><th>任务</th><th>依赖</th><th>命令</th><th>启用</th></tr></thead><tbody>${w.tasks.map(t => `<tr><td><div class="primary">${esc(t.description || t.name)}</div><div class="workflow-id mono">${esc(t.name)}</div></td><td class="mono">${esc(t.depends.join(", ") || "—")}</td><td class="mono">${esc(t.command)}</td><td>${t.enabled ? "是" : "否"}</td></tr>`).join("")}</tbody></table></div>`);
}

function showDag(name) {
  const workflow = state.workflows.get(name); if (!workflow) return;
  const svg = buildDagSvg(workflow.tasks, name);
  openModal(`${name} · DAG 结构`, `<div class="dag-legend"><span><i class="legend-node active-node"></i>启用节点</span><span><i class="legend-node inactive-node"></i>禁用节点</span><span>从左向右表示依赖方向</span></div><div class="dag-scroll">${svg}</div><div id="dagTaskDetail" class="dag-detail"><span class="secondary">点击节点查看任务命令和依赖</span></div>`);
}

function buildDagSvg(tasks, workflowName) {
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
    return `<g class="dag-node ${task.enabled ? "dag-enabled" : "dag-disabled"}" transform="translate(${p.x},${p.y})" onclick="selectDagTask('${esc(workflowName)}','${esc(task.name)}')"><rect width="${nodeWidth}" height="${nodeHeight}" rx="8"></rect><circle cx="17" cy="19" r="5"></circle><text class="dag-node-title" x="29" y="24">${esc(title)}</text><text class="dag-node-id" x="15" y="49">${esc(taskId)}</text><title>${esc(task.description || task.name)} · ${esc(task.name)}</title></g>`;
  }).join("");
  return `<svg class="dag-canvas" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(workflowName)} DAG"><defs><marker id="dagArrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z"></path></marker></defs>${edges.join("")}${nodes}</svg>`;
}

function selectDagTask(workflowName, taskName) {
  const workflow=state.workflows.get(workflowName), task=workflow?.tasks.find(item => item.name===taskName), target=document.getElementById("dagTaskDetail");
  if (!task || !target) return;
  target.innerHTML=`<div><span class="badge ${task.enabled ? "enabled" : "disabled"}">${task.enabled ? "已启用" : "已禁用"}</span> <strong>${esc(task.name)}</strong> <span class="secondary dag-inline">${esc(task.description)}</span></div><div class="dag-meta"><b>依赖：</b><span class="mono">${esc(task.depends.join(", ") || "无")}</span></div><div class="dag-meta"><b>命令：</b><code>${esc(task.command)}</code></div>`;
}

function exportYaml(name) { window.location.assign(`/api/workflows/${encodeURIComponent(name)}/yaml`); }

function editSchedule(name) {
  const s = state.workflows.get(name).schedule;
  openModal(`${name} · 定时配置`, `<div class="form-grid"><label>Cron（5 字段）<input id="cronInput" value="${esc(s.cron || "0 18 * * mon-fri")}"></label><label>时区<input id="timezoneInput" value="${esc(s.timezone || "Asia/Shanghai")}"></label></div><label class="check"><input id="enabledInput" type="checkbox" ${s.enabled ? "checked" : ""}>启用自动调度</label><button class="button" onclick="saveSchedule('${esc(name)}')">保存配置</button>`);
}
async function saveSchedule(name) { try { await api(`/api/workflows/${encodeURIComponent(name)}/schedule`, {method:"PUT", body:JSON.stringify({cron:document.getElementById("cronInput").value, timezone:document.getElementById("timezoneInput").value, enabled:document.getElementById("enabledInput").checked})}); closeModal(); toast("定时配置已保存"); loadWorkflows(); } catch(e) { toast(e.message, true); } }

async function showRun(id) {
  try {
    const data = await api(`/api/runs/${encodeURIComponent(id)}`);
    const rows = data.tasks.map(t => `<tr><td><div class="primary">${esc(t.task_name)}</div></td><td>${statusBadge(t.status)}</td><td>${fmt(t.start_time)}</td><td>${fmt(t.end_time)}</td><td>${t.exit_code ?? "—"}</td><td><div class="secondary" title="${esc(t.error_message)}">${esc(t.error_message || "")}</div></td><td>${t.log_file ? `<button class="button ghost" onclick="showLog('${id}','${esc(t.task_name)}')">日志</button>` : "—"}</td></tr>`).join("");
    openModal(`${data.run.workflow_name} · ${id}`, `<div class="table-wrap"><table><thead><tr><th>任务</th><th>状态</th><th>开始</th><th>结束</th><th>Exit</th><th>错误</th><th>日志</th></tr></thead><tbody>${rows}</tbody></table></div>`);
  } catch(e) { toast(e.message, true); }
}
async function showLog(runId, task) { try { const text = await api(`/api/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(task)}/log`); openModal(`${task} · 日志`, `<pre>${esc(text)}</pre>`); } catch(e) { toast(e.message, true); } }

function openModal(title, body) { document.getElementById("modalTitle").textContent = title; document.getElementById("modalBody").innerHTML = body; document.getElementById("modal").classList.remove("hidden"); }
function closeModal() { document.getElementById("modal").classList.add("hidden"); }
function closeModalFromBackdrop(event) { if (event.target.id === "modal") closeModal(); }
let toastTimer; function toast(message, error=false) { const box=document.getElementById("toast"); box.textContent=message; box.style.background=error?"#b42318":"#182230"; box.classList.remove("hidden"); clearTimeout(toastTimer); toastTimer=setTimeout(()=>box.classList.add("hidden"),4000); }

loadWorkflows(); loadRuns(); setInterval(loadRuns, 3000); setInterval(loadWorkflows, 15000);
