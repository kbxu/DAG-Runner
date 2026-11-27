let workflowViewer = null;

function disposeWorkflowViewer() {
  if (workflowViewer) {
    clearTimeout(workflowViewer.pollTimer);
    workflowViewer = null;
  }
}

function openWorkflowViewer(kind, id) {
  if (kind !== 'run') { openReadOnlyWorkflow(id); return; }
  openModal('运行详情', `<aside id="viewerDetails" class="viewer-details form-stack"><span id="viewerMessage" class="tf-note" role="status">正在加载…</span><div id="viewerSummaryHost" class="rv-summary-host"></div><div id="viewerSections" class="tf-sections rv-scroll-body"></div></aside><section id="viewerGraphPanel"><div class="preview-heading"><strong>运行 DAG 视图</strong><div id="viewerLegend" class="dag-legend"></div></div><div id="viewerGraph" class="dag-scroll"></div></section>`);
  const details = document.getElementById('viewerDetails');
  const graphPanel = document.getElementById('viewerGraphPanel');
  const layout = mountWorkflowFrame(details, graphPanel, direction => {
    const view = workflowViewer;
    if (!view) return;
    view.direction = direction;
    renderViewerGraph();
  });
  layout.classList.add('workflow-viewer-frame');
  const footer = document.createElement('div');
  footer.id = 'viewerFooter';
  footer.className = 'workflow-editor-footer';
  footer.innerHTML = `<button type="button" class="button ghost" onclick="closeModal()">${esc(t('关闭'))}</button>`;
  layout.after(footer);
  workflowViewer = {
    kind, id, direction: 'TB', selected: null, data: null, tasks: [],
    request: 0, openTasks: new Set(), openLogs: new Set(), openSections: new Set(),
    logCache: new Map(), pollTimer: null, initialized: false,
  };
  document.getElementById('viewerSummaryHost').addEventListener('click', onViewerSectionClick);
  const sections = document.getElementById('viewerSections');
  sections.addEventListener('toggle', onViewerSectionToggle, true);
  sections.addEventListener('click', onViewerSectionClick);
  refreshWorkflowViewer();
}

function scheduleViewerPoll(view) {
  clearTimeout(view.pollTimer);
  view.pollTimer = null;
  if (workflowViewer !== view || view.data?.run?.status !== 'RUNNING') return;
  view.pollTimer = setTimeout(() => {
    if (workflowViewer === view) refreshWorkflowViewer(true);
  }, 2500);
}

async function refreshWorkflowViewer(silent = false) {
  const view = workflowViewer;
  if (!view || view.refreshing) return;
  view.refreshing = true;
  const initialLoad = !view.data;
  const request = ++view.request;
  const refreshButton = document.querySelector('#viewerSummaryHost [data-rv-action="refresh"]');
  if (refreshButton) { refreshButton.disabled = true; refreshButton.setAttribute('aria-busy', 'true'); }
  if (initialLoad) {
    const msg = document.getElementById('viewerMessage');
    if (msg) { msg.textContent = '正在加载…'; msg.classList.remove('hidden'); }
  }
  try {
    const data = await api(`/api/runs/${encodeURIComponent(view.id)}`);
    if (workflowViewer !== view || view.request !== request) return;
    view.data = data;
    view.tasks = data.graph_tasks || [];
    const run = data.run;
    document.getElementById('modalTitle').textContent = `${namedId(run.workflow_description, run.workflow_name)} · ${view.id}`;
    const msg = document.getElementById('viewerMessage');
    if (msg) { msg.textContent = ''; msg.classList.add('hidden'); }
    document.getElementById('viewerLegend').innerHTML = ['RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED', 'PENDING']
      .map(status => statusBadge(status)).join(' ');
    if (!view.initialized) {
      view.initialized = true;
      const focusTask = (data.tasks || []).find(item => item.status === 'FAILED')
        || (data.tasks || []).find(item => item.status === 'RUNNING');
      if (focusTask) {
        view.selected = focusTask.task_name;
        view.openTasks.add(focusTask.task_name);
      }
    }
    renderViewerLeft(view);
    renderViewerFooter(view);
    renderViewerGraph();
    // 仅刷新用户主动展开了内嵌日志预览且正在运行中的任务
    for (const taskName of view.openLogs) {
      const row = (data.tasks || []).find(item => item.task_name === taskName);
      if (viewerTaskHasLog(row) && (!view.logCache.has(taskName) || row.status === 'RUNNING' || !silent)) {
        loadViewerTaskLog(view, taskName, {silent: true});
      }
    }
    scheduleViewerPoll(view);
  } catch (error) {
    if (workflowViewer === view && view.request === request) {
      if (initialLoad) {
        const msg = document.getElementById('viewerMessage');
        if (msg) { msg.classList.remove('hidden'); msg.textContent = `加载失败：${error.message}`; }
      } else if (!silent) {
        toast(`刷新失败：${error.message}`, true);
      }
    }
  } finally {
    view.refreshing = false;
    if (workflowViewer === view) {
      const button = document.querySelector('#viewerSummaryHost [data-rv-action="refresh"]');
      if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
    }
  }
}

function viewerTriggerLabel(run) {
  const map = {manual: '手动运行', schedule: '定时调度', rerun: '重跑', resume: '失败续跑'};
  const base = map[run.trigger_type] || run.trigger_type || '手动运行';
  return run.trigger_user ? `${base} · ${run.trigger_user}` : base;
}

function viewerTaskHasLog(row) {
  return Boolean(row && row.log_file && row.status !== 'SKIPPED' && row.status !== 'PENDING');
}

function viewerShortTime(value) {
  if (!value) return '';
  const match = String(value).match(/T(\d{2}:\d{2}:\d{2})/);
  return match ? match[1] : String(value);
}

function viewerTimeRange(row) {
  if (!row.start_time) return '—';
  const start = viewerShortTime(row.start_time);
  const end = row.end_time ? viewerShortTime(row.end_time) : (row.status === 'RUNNING' ? '运行中' : '');
  return end ? `${start} ~ ${end}` : start;
}

function renderViewerSummaryHtml(view) {
  const run = view.data.run;
  const rows = view.data.tasks || [];
  const fromTaskRow = run.from_task ? rows.find(item => item.task_name === run.from_task) : null;
  const fromLabel = fromTaskRow ? (fromTaskRow.task_description || run.from_task) : run.from_task;
  const errorLine = run.error_message
    ? `<div class="rv-run-error">${esc(run.error_message)}</div>`
    : '';
  return `<div class="tf-panel rv-summary-card">
    <div class="rv-summary-top">
      <div class="rv-summary-main">
        ${statusBadge(run.status)}
        <span class="rv-summary-meta">${esc(viewerTriggerLabel(run))}</span>
        <span class="rv-summary-sep">·</span>
        <span class="rv-summary-meta">耗时 ${esc(duration(run.start_time, run.end_time))}</span>
        ${run.from_task ? `<span class="rv-scope-pill" title="${esc(run.from_task)}">起点：${esc(fromLabel)}</span>` : ''}
      </div>
      <div class="rv-summary-actions">
        <button type="button" class="button ghost small" data-rv-action="refresh">${t("刷新")}</button>
      </div>
    </div>
    <div class="rv-summary-times">
      <span>开始：${fmt(run.start_time)}</span>
      <span>结束：${fmt(run.end_time)}</span>
      <span>默认位置：${esc(run.runner && run.runner !== 'local' ? run.runner : t('本机'))}</span>
    </div>
    ${errorLine}
  </div>`;
}

function renderViewerSetupHtml(view) {
  const run = view.data.run;
  const open = view.openSections.has('setup');
  const runnerLabel = run.runner && run.runner !== 'local' ? run.runner : t('本机');
  const setupText = String(run.setup || '').trim();
  return `<details class="tf-panel tf-collapse" data-section="setup"${open ? ' open' : ''}>
    <summary class="tf-panel-head">
      <span class="tf-section-head">环境配置<span class="tf-subhint">每个任务前执行</span></span>
      <span class="tf-section-right"><span class="tf-meta-pill">${esc(runnerLabel)}${setupText ? ' · 含预跑' : ''}</span><span class="tf-head-caret">▶</span></span>
    </summary>
    <div class="tf-collapse-body">
      <div class="tf-ro-block"><span class="tf-ro-label">运行位置</span><span class="tf-ro-value">${esc(runnerLabel)}</span></div>
      ${setupText
        ? `<div class="tf-ro-block"><span class="tf-ro-label">预跑命令</span><pre class="tf-ro-code">${esc(setupText)}</pre></div>`
        : '<span class="tf-note">未配置预跑命令</span>'}
    </div>
  </details>`;
}

function renderViewerTaskCardHtml(view, row, graphTask, names) {
  const name = row.task_name;
  const open = view.openTasks.has(name);
  const selected = view.selected === name;
  const taskType = graphTask?.type || row.task_type || 'command';
  const isSkipped = row.status === 'SKIPPED';
  const isFailed = row.status === 'FAILED';
  const title = row.task_description || graphTask?.description || name;
  const dur = row.start_time ? duration(row.start_time, row.end_time) : '—';
  const timeRange = viewerTimeRange(row);
  const fullTimeTip = row.start_time ? `开始：${fmt(row.start_time)} / 结束：${fmt(row.end_time)}` : '未开始';
  const deps = graphTask?.depends || [];
  const runnerLabel = row.runner && row.runner !== 'local' ? row.runner : t('本机');
  const showLogBox = viewerTaskHasLog(row);
  const logOpen = showLogBox && view.openLogs.has(name);
  const cachedLog = showLogBox ? view.logCache.get(name) : null;
  let logSectionHtml = '';
  if (showLogBox) {
    const logBodyHtml = cachedLog
      ? (cachedLog.loading && !cachedLog.text
        ? '<div class="rv-log-empty">正在加载日志…</div>'
        : (cachedLog.error
          ? `<div class="rv-log-empty error">日志加载失败：${esc(cachedLog.error)}</div>`
          : `<pre class="rv-log-pre">${esc(cachedLog.text || '（日志内容为空）')}</pre>`))
      : '<div class="rv-log-empty">点击展开预览前 300 行，或点击右侧「全屏」查看完整日志</div>';
    const logMeta = cachedLog && typeof cachedLog.total === 'number'
      ? `<span class="rv-log-meta">${cachedLog.truncated ? `预览前 ${cachedLog.shown} / 共 ${cachedLog.total} 行` : `共 ${cachedLog.total} 行`}</span>`
      : '';
    logSectionHtml = `<details class="rv-log-box" data-log-task="${esc(name)}"${logOpen ? ' open' : ''}>
        <summary class="rv-log-head">
          <span class="rv-log-title"><span class="tf-head-caret">▶</span>运行日志${logMeta}</span>
          <span class="rv-log-actions">
            <button type="button" class="button ghost small" data-rv-action="fullscreen-log" data-task="${esc(name)}" data-label="${esc(title)}">全屏</button>
            <a class="button ghost small" data-rv-stop="1" href="/api/runs/${encodeURIComponent(view.id)}/tasks/${encodeURIComponent(name)}/log?download=1" download>下载</a>
          </span>
        </summary>
        <div class="rv-log-body">${logBodyHtml}</div>
      </details>`;
  }
  const commandSection = taskType === 'condition'
    ? `<div class="tf-ro-block"><span class="tf-ro-label">条件结果</span><span class="tf-ro-value">${row.condition_result ? `<strong>${esc(row.condition_result.toUpperCase())}</strong>` : '—'}</span></div>`
    : (graphTask?.command
      ? `<div class="tf-block"><div class="subhead">执行命令</div><pre class="tf-ro-code">${esc(graphTask.command)}</pre></div>`
      : '');
  const depsSection = deps.length
    ? `<div class="tf-ro-block"><span class="tf-ro-label">前置依赖</span><div class="tf-chips">${deps.map(id => `<span class="tf-chip">${esc(names[id] || id)}</span>`).join('')}</div></div>`
    : '';
  return `<details class="tf-task rv-task${selected ? ' tf-focus' : ''}${isFailed ? ' tf-has-error' : ''}" data-task="${esc(name)}"${open ? ' open' : ''}>
    <summary>
      <span class="tf-tags"><span class="tf-tag ${taskType}${isSkipped ? ' is-disabled' : ''}">${taskType === 'condition' ? '条件' : '任务'}</span></span>
      <span class="tf-caret">▶</span>
      <span class="tf-heading"><span class="tf-title">${esc(title)}</span><span class="tf-id mono">${esc(name)}</span></span>
      <span class="rv-col-time mono" title="${esc(fullTimeTip)}">${esc(timeRange)}</span>
      <span class="rv-col-dur">${esc(dur)}</span>
      <span class="rv-col-status">${statusBadge(row.status)}</span>
    </summary>
    <div class="tf-body rv-task-body">
      <div class="rv-meta-grid">
        <div><span class="rv-meta-k">开始</span><span class="rv-meta-v">${fmt(row.start_time)}</span></div>
        <div><span class="rv-meta-k">结束</span><span class="rv-meta-v">${fmt(row.end_time)}</span></div>
        <div><span class="rv-meta-k">Exit</span><span class="rv-meta-v mono">${esc(row.exit_code ?? '—')}</span></div>
        <div><span class="rv-meta-k">运行位置</span><span class="rv-meta-v">${esc(runnerLabel)}</span></div>
      </div>
      ${row.error_message ? `<div class="rv-task-note${isFailed ? ' is-error' : ''}">${esc(row.error_message)}</div>` : ''}
      ${depsSection}
      ${commandSection}
      ${logSectionHtml}
    </div>
  </details>`;
}

function renderViewerLeft(view) {
  const summaryHost = document.getElementById('viewerSummaryHost');
  const container = document.getElementById('viewerSections');
  if (!container || !view.data) return;
  if (summaryHost) summaryHost.innerHTML = renderViewerSummaryHtml(view);
  const prevScroll = container.scrollTop;
  const rows = view.data.tasks || [];
  const graphByName = new Map((view.tasks || []).map(item => [item.name, item]));
  const names = Object.fromEntries(rows.map(item => [item.task_name, item.task_description || item.task_name]));
  const cards = rows.map(row => renderViewerTaskCardHtml(view, row, graphByName.get(row.task_name), names)).join('');
  const toolbar = `<div class="tf-panel-head tf-list-head"><span class="tf-section-head">任务列表<span class="tf-count-badge">${rows.length}</span></span><div class="tf-toolbar">
    <button type="button" class="button ghost small" data-rv-action="expand">展开</button>
    <button type="button" class="button ghost small" data-rv-action="collapse">折叠</button>
  </div></div>`;
  container.innerHTML = renderViewerSetupHtml(view)
    + `<div class="tf-panel tf-task-list-panel">${toolbar}<div class="tf-tasks">${cards || '<div class="tf-empty-tasks">暂无可展示的任务</div>'}</div></div>`;
  container.scrollTop = prevScroll;
}

function renderViewerFooter(view) {
  const footer = document.getElementById('viewerFooter');
  if (!footer || !view.data) return;
  const run = view.data.run;
  const canEdit = typeof canWorkflow === 'function' ? canWorkflow(run.workflow_name, 'edit') : false;
  const canExec = typeof canWorkflow === 'function' ? canWorkflow(run.workflow_name, 'execute') : false;
  const buttons = [];
  if (run.status === 'RUNNING' && canExec) {
    buttons.push(`<button type="button" class="button danger" onclick="viewerRunAction('stop')">${esc(t('停止'))}</button>`);
  }
  if (run.status === 'FAILED' && canEdit) {
    buttons.push(`<button type="button" class="button" onclick="viewerRunAction('resume')">${esc(t('失败续跑'))}</button>`);
  }
  buttons.push(`<button type="button" class="button ghost" onclick="closeModal()">${esc(t('关闭'))}</button>`);
  footer.innerHTML = buttons.join('');
}

async function viewerRunAction(action) {
  const view = workflowViewer;
  if (!view) return;
  if (action === 'stop' && !confirm(t('确定停止这个运行实例？当前子进程会被终止。'))) return;
  try {
    const r = await api(`/api/runs/${encodeURIComponent(view.id)}/${action}`, {method: 'POST', body: '{}'});
    toast(action === 'stop' ? t('已发送停止请求') : t('已提交新运行 {id}', {id: r.run_id}));
    setTimeout(loadRuns, 300);
    if (action === 'stop') {
      refreshWorkflowViewer();
    } else if (r.run_id) {
      openWorkflowViewer('run', r.run_id);
    }
  } catch (e) {
    toast(e.message, true);
  }
}

async function loadViewerTaskLog(view, taskName, options = {}) {
  if (workflowViewer !== view) return;
  const prev = view.logCache.get(taskName);
  view.logCache.set(taskName, {...(prev || {}), loading: true, error: null});
  updateViewerTaskLogDom(view, taskName);
  try {
    const data = await api(`/api/runs/${encodeURIComponent(view.id)}/tasks/${encodeURIComponent(taskName)}/log?offset=0&limit=300`);
    if (workflowViewer !== view) return;
    const lines = data.lines || [];
    view.logCache.set(taskName, {
      loading: false,
      error: null,
      text: lines.join('\n'),
      total: data.total ?? lines.length,
      shown: lines.length,
      truncated: Boolean(data.has_more),
    });
    updateViewerTaskLogDom(view, taskName);
  } catch (error) {
    if (workflowViewer !== view) return;
    view.logCache.set(taskName, {loading: false, error: error.message, text: ''});
    updateViewerTaskLogDom(view, taskName);
    if (!options.silent) toast(error.message, true);
  }
}

function updateViewerTaskLogDom(view, taskName) {
  const box = document.querySelector(`#viewerSections .rv-log-box[data-log-task="${CSS.escape(taskName)}"]`);
  if (!box) return;
  const entry = view.logCache.get(taskName);
  if (!entry) return;
  const metaHost = box.querySelector('.rv-log-title');
  if (metaHost) {
    const meta = typeof entry.total === 'number'
      ? `<span class="rv-log-meta">${entry.truncated ? `预览前 ${entry.shown} / 共 ${entry.total} 行` : `共 ${entry.total} 行`}</span>`
      : '';
    metaHost.innerHTML = `<span class="tf-head-caret">▶</span>运行日志${meta}`;
  }
  const body = box.querySelector('.rv-log-body');
  if (!body) return;
  if (entry.loading && !entry.text) {
    body.innerHTML = '<div class="rv-log-empty">正在加载日志…</div>';
  } else if (entry.error) {
    body.innerHTML = `<div class="rv-log-empty error">日志加载失败：${esc(entry.error)}</div>`;
  } else {
    body.innerHTML = `<pre class="rv-log-pre">${esc(entry.text || '（日志内容为空）')}</pre>`;
    const row = (view.data?.tasks || []).find(item => item.task_name === taskName);
    if (row?.status === 'RUNNING') {
      const pre = body.querySelector('.rv-log-pre');
      if (pre) pre.scrollTop = pre.scrollHeight;
    }
  }
}

function onViewerSectionToggle(event) {
  const target = event.target;
  const view = workflowViewer;
  if (!view || !target.matches) return;
  if (target.matches('details.tf-collapse')) {
    const section = target.dataset.section;
    if (target.open) view.openSections.add(section);
    else view.openSections.delete(section);
    return;
  }
  if (target.matches('details.rv-log-box')) {
    const taskName = target.dataset.logTask;
    if (target.open) {
      view.openLogs.add(taskName);
      if (!view.logCache.has(taskName)) loadViewerTaskLog(view, taskName, {silent: true});
    } else {
      view.openLogs.delete(taskName);
    }
    return;
  }
  if (!target.matches('details.tf-task')) return;
  const taskName = target.dataset.task;
  if (target.open) {
    view.openTasks.add(taskName);
    view.selected = taskName;
    syncViewerSelectionClasses(view);
  } else {
    view.openTasks.delete(taskName);
    if (view.selected === taskName) {
      view.selected = null;
      syncViewerSelectionClasses(view);
    }
  }
}

function onViewerSectionClick(event) {
  if (event.target.closest('[data-rv-stop]')) {
    event.stopPropagation();
    return;
  }
  const btn = event.target.closest('[data-rv-action]');
  if (!btn || !workflowViewer) return;
  const view = workflowViewer;
  const action = btn.dataset.rvAction;
  if (action === 'fullscreen-log' || action === 'reload-log') {
    event.preventDefault();
    event.stopPropagation();
  }
  if (action === 'refresh') {
    refreshWorkflowViewer(false);
  } else if (action === 'expand') {
    const rows = view.data?.tasks || [];
    rows.forEach(item => view.openTasks.add(item.task_name));
    renderViewerLeft(view);
  } else if (action === 'collapse') {
    view.openTasks.clear();
    view.selected = null;
    renderViewerLeft(view);
    syncViewerSelectionClasses(view);
  } else if (action === 'reload-log') {
    loadViewerTaskLog(view, btn.dataset.task, {silent: false});
  } else if (action === 'fullscreen-log') {
    showLog(view.id, btn.dataset.task, btn.dataset.label || btn.dataset.task);
  }
}

function syncViewerSelectionClasses(view) {
  document.querySelectorAll('#viewerGraph .dag-node').forEach(node => {
    node.classList.toggle('preview-selected', node.dataset.task === view.selected);
  });
  document.querySelectorAll('#viewerSections details.tf-task').forEach(card => {
    card.classList.toggle('tf-focus', card.dataset.task === view.selected);
  });
}

function renderViewerGraph() {
  const view = workflowViewer;
  if (!view?.data) return;
  const graph = document.getElementById('viewerGraph');
  const markup = view.tasks.length
    ? buildDagSvg(view.tasks, '工作流', true, false, view.direction, true)
    : '<div class="dag-empty">暂无任务节点</div>';
  if (view.graphMarkup !== markup) {
    const {scrollTop, scrollLeft} = graph;
    graph.innerHTML = markup;
    view.graphMarkup = markup;
    graph.scrollTop = scrollTop;
    graph.scrollLeft = scrollLeft;
  }
  if (typeof fitMaximizedCardWidth === "function") fitMaximizedCardWidth();
  syncViewerSelectionClasses(view);
  if (!graph.dataset.blankClick) {
    graph.dataset.blankClick = '1';
    graph.addEventListener('click', event => {
      if (event.target.closest('.dag-node')) return;
      clearViewerSelection();
    });
  }
}

function clearViewerSelection() {
  const view = workflowViewer;
  if (!view) return;
  view.selected = null;
  view.openTasks.clear();
  document.querySelectorAll('#viewerSections details.tf-task').forEach(card => { card.open = false; });
  syncViewerSelectionClasses(view);
}

function selectViewerTask(name) {
  const view = workflowViewer, task = view?.tasks.find(item => item.name === name);
  if (!task) return;
  if (view.selected === name) {
    clearViewerSelection();
    return;
  }
  const row = (view.data?.tasks || []).find(item => item.task_name === name);
  view.selected = name;
  view.openTasks.clear();
  view.openTasks.add(name);
  document.querySelectorAll('#viewerSections details.tf-task').forEach(item => {
    item.open = item.dataset.task === name;
  });
  syncViewerSelectionClasses(view);
  const card = document.querySelector(`#viewerSections details.tf-task[data-task="${CSS.escape(name)}"]`);
  if (card) card.scrollIntoView({block: 'nearest'});
}
