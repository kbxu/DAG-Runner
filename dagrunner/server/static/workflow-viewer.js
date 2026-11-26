let workflowViewer = null;

function disposeWorkflowViewer() {
  workflowViewer = null;
}

function openWorkflowViewer(kind, id) {
  if (kind !== 'run') { openReadOnlyWorkflow(id); return; }
  openModal('运行详情', `<aside id="viewerDetails" class="viewer-details form-stack"><span id="viewerMessage" role="status">正在加载…</span><div id="viewerNode">正在加载节点内容…</div><div id="viewerTasks"></div></aside><section id="viewerGraphPanel"><div id="viewerLegend" class="dag-legend"></div><div id="viewerGraph" class="dag-scroll"></div></section>`);
  workflowViewer = {kind, id, direction: 'TB', selected: null, data: null, tasks: [], request: 0, logRequest: 0};
  mountWorkflowFrame(document.getElementById('viewerDetails'), document.getElementById('viewerGraphPanel'), direction => {
    const view = workflowViewer;
    if (!view) return;
    view.direction = direction;
    renderViewerGraph();
  }).classList.add('workflow-viewer-frame');
  refreshWorkflowViewer();
}

async function refreshWorkflowViewer() {
  const view = workflowViewer;
  if (!view || view.refreshing) return;
  view.refreshing = true;
  const initialLoad = !view.data;
  const request = ++view.request;
  const refreshButton = document.querySelector('#viewerTasks .viewer-records-heading button');
  if (refreshButton) { refreshButton.disabled = true; refreshButton.setAttribute('aria-busy', 'true'); }
  if (initialLoad) {
    document.getElementById('viewerMessage').textContent = '正在加载…';
    document.getElementById('viewerMessage').classList.remove('hidden');
  }
  try {
    const data = await api(view.kind === 'run' ? `/api/runs/${encodeURIComponent(view.id)}` : `/api/workflows/${encodeURIComponent(view.id)}/view`);
    if (workflowViewer !== view || view.request !== request) return;
    view.data = data;
    view.tasks = view.kind === 'run' ? data.graph_tasks : data.tasks;
    const run = data.run;
    document.getElementById('modalTitle').textContent = run ? `${namedId(run.workflow_description, run.workflow_name)} · ${view.id}` : namedId(data.description, data.name);
    document.getElementById('viewerMessage').textContent = '';
    document.getElementById('viewerMessage').classList.add('hidden');
    document.getElementById('viewerLegend').innerHTML = run
      ? ['RUNNING','SUCCESS','FAILED','SKIPPED','PENDING'].map(status => statusBadge(status)).join(' ')
      : `<span>${t("点击节点定位任务卡片 · 右键执行任务")}</span>`;
    if (run) {
      // Scoped runs hide the tasks that were never part of the run.
      if (view.hideOutsideScope === undefined) view.hideOutsideScope = Boolean(run.from_task);
      const tableScroll = document.querySelector('#viewerTasks .viewer-table-scroll');
      const scrollTop = tableScroll?.scrollTop || 0, scrollLeft = tableScroll?.scrollLeft || 0;
      document.getElementById('viewerTasks').innerHTML = renderViewerTasks(view);
      const updatedScroll = document.querySelector('#viewerTasks .viewer-table-scroll');
      updatedScroll.scrollTop = scrollTop;
      updatedScroll.scrollLeft = scrollLeft;
    }
    renderViewerGraph();
    if (view.kind === 'workflow') {
      await loadViewerForm(view);
      if (view.selected && view.tasks.some(task => task.name === view.selected)) selectViewerTask(view.selected);
    } else if (view.tasks.some(task => task.name === view.selected)) {
      selectViewerTask(view.selected);
    } else {
      view.selected = view.tasks[0]?.name || null;
      if (view.selected) selectViewerTask(view.selected);
      else {
        view.renderedNode = null;
        document.getElementById('viewerNode').textContent = '点击节点查看详情';
      }
    }
  } catch (error) {
    if (workflowViewer === view && view.request === request) {
      if (initialLoad) {
        document.getElementById('viewerMessage').classList.remove('hidden');
        document.getElementById('viewerMessage').textContent = `加载失败：${error.message}`;
      } else toast(`刷新失败：${error.message}`, true);
    }
  } finally {
    view.refreshing = false;
    if (workflowViewer === view) {
      const button = document.querySelector('#viewerTasks .viewer-records-heading button');
      if (button) { button.disabled = false; button.removeAttribute('aria-busy'); }
    }
  }
}

async function loadViewerForm(view) {
  const target = document.getElementById('viewerReadForm');
  if (!target) return;
  try {
    const model = await api(`/api/workflows/${encodeURIComponent(view.id)}/model`);
    if (workflowViewer !== view) return;
    model.schedule = model.schedule || {crons: [], timezone: 'Asia/Shanghai', enabled: false};
    renderViewerForm(view, model);
  } catch (error) {
    target.innerHTML = `<span class="tf-note">只读表单加载失败：${esc(error.message)}</span>`;
  }
}

function toggleViewerForm(open) {
  const view = workflowViewer;
  if (!view || view.kind !== 'workflow') return;
  view.openTasks = new Set(open ? view.tasks.map(task => task.name) : []);
  document.querySelectorAll('#viewerReadForm details.tf-task').forEach(card => { card.open = open; });
}

function renderViewerForm(view, model) {
  const options = {readOnly: true, openSections: view.openSections, openTasks: view.openTasks};
  const cards = model.tasks.map((task, index) => taskCardHtml(model, task, index, options)).join('');
  const extraButtons = `<button type="button" class="button ghost small" onclick="toggleViewerForm(true)">全部展开</button>`
    + `<button type="button" class="button ghost small" onclick="toggleViewerForm(false)">全部折叠</button>`;
  document.getElementById('viewerReadForm').innerHTML =
    schedulePanelHtml(model, options)
    + setupPanelHtml(model, options)
    + `<div class="tf-panel tf-section-gap">${taskListToolbarHtml(extraButtons)}<div class="tf-tasks tf-readonly-list">${cards}</div></div>`;
  document.querySelectorAll('#viewerReadForm details.tf-collapse').forEach(section => {
    section.addEventListener('toggle', () => {
      if (section.open) view.openSections.add(section.dataset.section);
      else view.openSections.delete(section.dataset.section);
    });
  });
}

function renderViewerTasks(view) {
  const rows = view.data.tasks;
  const outside = rows.filter(task => task.skip_kind === 'OUTSIDE_SCOPE');
  const visible = view.hideOutsideScope
    ? rows.filter(task => task.skip_kind !== 'OUTSIDE_SCOPE')
    : rows;
  const toggle = outside.length
    ? `<button class="button ghost" onclick="toggleOutsideScopeTasks()">${view.hideOutsideScope ? t("显示范围外任务") : t("隐藏范围外任务")}</button>`
    : '';
  return `<div class="viewer-records-heading"><h4>${t("任务运行记录")}</h4><div class="viewer-records-actions">${toggle}<button class="button ghost" onclick="refreshWorkflowViewer()">${t("刷新")}</button></div></div><div class="viewer-table-scroll" tabindex="0" role="region" aria-label="${t("任务运行记录")}"><table class="viewer-run-table card-table"><thead><tr><th>${t("任务")}</th><th>${t("开始")}</th><th>${t("结束")}</th><th>Exit</th><th>${t("错误")}</th><th>${t("日志")}</th></tr></thead><tbody>${visible.map(task => `<tr data-run-task="${esc(task.task_name)}"><td class="card-cell-name"><div class="viewer-task-heading"><button class="workflow-link" onclick='selectViewerTask(${jsArg(task.task_name)})'>${esc(task.task_description || task.task_name)}</button>${statusBadge(task.status)}</div><div class="viewer-task-id mono">${esc(task.task_name)}</div></td><td data-label="${esc(t("开始"))}">${fmt(task.start_time)}</td><td data-label="${esc(t("结束"))}">${fmt(task.end_time)}</td><td data-label="Exit">${esc(task.exit_code ?? '—')}</td><td data-label="${esc(t("错误"))}" class="viewer-table-error" title="${esc(task.error_message || '')}">${esc(task.error_message || '—')}</td><td data-label="${esc(t("日志"))}">${task.log_file ? `<button class="button ghost" onclick='showLog(${jsArg(view.id)},${jsArg(task.task_name)},${jsArg(task.task_description || task.task_name)})'>${t("日志")}</button>` : '—'}</td></tr>`).join('')}</tbody></table></div>`;
}

function toggleOutsideScopeTasks() {
  const view = workflowViewer;
  if (!view) return;
  view.hideOutsideScope = !view.hideOutsideScope;
  const tableScroll = document.querySelector('#viewerTasks .viewer-table-scroll');
  const scrollTop = tableScroll?.scrollTop || 0, scrollLeft = tableScroll?.scrollLeft || 0;
  document.getElementById('viewerTasks').innerHTML = renderViewerTasks(view);
  const updatedScroll = document.querySelector('#viewerTasks .viewer-table-scroll');
  updatedScroll.scrollTop = scrollTop;
  updatedScroll.scrollLeft = scrollLeft;
}

function renderViewerGraph() {
  const view = workflowViewer;
  if (!view?.data) return;
  const graph = document.getElementById('viewerGraph');
  const markup = view.tasks.length
    ? buildDagSvg(view.tasks, '工作流', view.kind === 'run', false, view.direction, true)
    : '<p class="secondary">暂无任务节点</p>';
  if (view.graphMarkup !== markup) {
    const {scrollTop, scrollLeft} = graph;
    graph.innerHTML = markup;
    view.graphMarkup = markup;
    graph.scrollTop = scrollTop;
    graph.scrollLeft = scrollLeft;
  }
  if (typeof fitMaximizedCardWidth === "function") fitMaximizedCardWidth();
  document.querySelectorAll('#viewerGraph .dag-node').forEach(node => node.classList.toggle('preview-selected', node.dataset.task === view.selected));
  if (view.kind === 'workflow' && !graph.dataset.blankClick) {
    graph.dataset.blankClick = '1';
    graph.addEventListener('click', event => {
      if (event.target.closest('.dag-node')) return;
      clearViewerSelection();
    });
  }
  if (view.kind === 'workflow' && !graph.dataset.runMenu) {
    graph.dataset.runMenu = '1';
    graph.addEventListener('contextmenu', event => {
      const node = event.target.closest('.dag-node');
      if (!node) return;
      // 局部执行（单任务/子树）属于 maintainer 能力；executor 只能整张手动运行。
      if (typeof canWorkflow !== 'function' || !canWorkflow(view.id, 'edit')) return;
      event.preventDefault();
      openTaskRunMenu(view.id, node.dataset.task, event);
    });
  }
}

function renderViewerYaml(view) {
  const definition = view.data?.definition;
  const target = document.getElementById('viewerNode');
  if (definition == null) {
    view.yamlRendered = false;
    view.yamlDefinition = null;
    target.classList.remove('viewer-yaml-node');
    return false;
  }
  target.classList.add('viewer-yaml-node');
  if (view.yamlDefinition !== definition) {
    view.yamlDefinition = definition;
    const run = view.data.run;
    const title = run ? namedId(run.workflow_description, run.workflow_name) : namedId(view.data.description, view.data.name);
    const caption = run ? '工作流 YAML（只读 · 当前定义，非运行时快照）' : '工作流 YAML（只读）';
    target.innerHTML = `<div class="viewer-node-heading"><strong title="${esc(title)}">${esc(title)}</strong><span class="secondary viewer-node-caption">${caption}</span></div><div class="viewer-yaml">${yamlEditor('viewerYaml', definition, false, true)}</div>`;
    syncYamlHighlight('viewerYaml');
    ensureYamlFlash(document.getElementById('viewerYaml'));
  }
  view.yamlRendered = true;
  return true;
}

function focusViewerYamlTask(name) {
  const view = workflowViewer;
  const textarea = document.getElementById('viewerYaml');
  if (!view || !textarea) return;
  const definition = normalizeYamlNewlines(view.data?.definition ?? textarea.value);
  const line = scrollYamlToTask(textarea, definition, name);
  flashYamlLine(textarea, line);
}

function clearViewerSelection() {
  const view = workflowViewer;
  if (!view) return;
  view.selected = null;
  document.querySelectorAll('#viewerGraph .dag-node').forEach(node => node.classList.remove('preview-selected'));
  document.querySelectorAll('[data-run-task]').forEach(row => row.classList.remove('viewer-row-selected'));
  document.querySelectorAll('#viewerReadForm .tf-focus').forEach(card => card.classList.remove('tf-focus'));
  const textarea = document.getElementById('viewerYaml');
  if (textarea) {
    clearYamlFlash(textarea);
    textarea.focus();
    textarea.setSelectionRange(0, 0);
    textarea.scrollTop = 0;
    syncYamlScroll('viewerYaml');
  } else if (document.getElementById('viewerNode')) {
    document.getElementById('viewerNode').textContent = '点击节点查看详情';
  }
}

function selectViewerTask(name) {
  const view = workflowViewer, task = view?.tasks.find(item => item.name === name);
  if (!task) return;
  view.selected = name;
  document.querySelectorAll('#viewerGraph .dag-node').forEach(node => node.classList.toggle('preview-selected', node.dataset.task === name));
  document.querySelectorAll('[data-run-task]').forEach(row => row.classList.toggle('viewer-row-selected', row.dataset.runTask === name));
  if (view.kind === 'workflow') {
    document.querySelectorAll('#viewerReadForm .tf-focus').forEach(card => card.classList.remove('tf-focus'));
    // 与编辑模式一致：选中一个任务时其他任务卡片收起。
    document.querySelectorAll('#viewerReadForm details.tf-task').forEach(item => {
      if (item.dataset.task !== name) {
        item.open = false;
        view.openTasks.delete(item.dataset.task);
      }
    });
    const card = document.querySelector(`#viewerReadForm details.tf-task[data-task="${CSS.escape(name)}"]`);
    if (card) {
      card.classList.add('tf-focus');
      card.open = true;
      view.openTasks.add(name);
      card.scrollIntoView({block: 'nearest'});
    }
    return;
  }
  if (view.yamlRendered) {
    focusViewerYamlTask(name);
    return;
  }
  // Status refreshes must not replace the code DOM or reset its selection/scroll.
  if (view.renderedNode === name) return;
  view.renderedNode = name;
  const target = document.getElementById('viewerNode');
  const heading = `<strong title="${esc(task.description || name)}">${esc(task.description || name)}</strong><span class="secondary mono" title="${esc(name)}">${esc(name)}</span>`;
  const caption = view.kind === 'run' ? '节点 YAML（只读 · 当前定义，非运行时快照）' : '节点 YAML（只读）';
  target.innerHTML = `<div class="viewer-node-heading">${heading}<span class="secondary viewer-node-caption">${caption}</span></div><pre class="viewer-code">${esc(task.code ?? '该节点当前定义已不可用，历史记录未保存代码快照。')}</pre>`;
}
