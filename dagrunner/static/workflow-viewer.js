let workflowViewer = null;

function disposeWorkflowViewer() {
  workflowViewer = null;
}

function openWorkflowViewer(kind, id) {
  openModal(kind === 'run' ? '运行详情' : '工作流', `<aside id="viewerDetails" class="viewer-details form-stack"><span id="viewerMessage" role="status">正在加载…</span><div id="viewerNode">正在加载节点内容…</div><div id="viewerTasks"></div></aside><section id="viewerGraphPanel"><div id="viewerLegend" class="dag-legend"></div><div id="viewerGraph" class="dag-scroll"></div></section>`);
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
      : '<span>点击节点查看 YAML 代码</span>';
    if (run) {
      const tableScroll = document.querySelector('#viewerTasks .viewer-table-scroll');
      const scrollTop = tableScroll?.scrollTop || 0, scrollLeft = tableScroll?.scrollLeft || 0;
      const startTime = task => {
        const value = task.start_time ? Date.parse(task.start_time) : NaN;
        return Number.isFinite(value) ? value : Infinity;
      };
      data.tasks = [...data.tasks].sort((left, right) => {
        const a = startTime(left), b = startTime(right);
        return a === b ? left.task_name.localeCompare(right.task_name) : a < b ? -1 : 1;
      });
      document.getElementById('viewerTasks').innerHTML = `<div class="viewer-records-heading"><h4>任务运行记录</h4><button class="button ghost" onclick="refreshWorkflowViewer()">刷新</button></div><div class="viewer-table-scroll" tabindex="0" role="region" aria-label="任务运行记录"><table class="viewer-run-table"><thead><tr><th>任务</th><th>开始</th><th>结束</th><th>Exit</th><th>错误</th><th>日志</th></tr></thead><tbody>${data.tasks.map(task => `<tr data-run-task="${esc(task.task_name)}"><td><div class="viewer-task-heading"><button class="workflow-link" onclick='selectViewerTask(${jsArg(task.task_name)})'>${esc(task.task_description || task.task_name)}</button>${statusBadge(task.status)}</div><div class="viewer-task-id mono">${esc(task.task_name)}</div></td><td>${fmt(task.start_time)}</td><td>${fmt(task.end_time)}</td><td>${esc(task.exit_code ?? '—')}</td><td class="viewer-table-error" title="${esc(task.error_message || '')}">${esc(task.error_message || '—')}</td><td>${task.log_file ? `<button class="button ghost" onclick='showLog(${jsArg(view.id)},${jsArg(task.task_name)},${jsArg(task.task_description || task.task_name)})'>日志</button>` : '—'}</td></tr>`).join('')}</tbody></table></div>`;
      const updatedScroll = document.querySelector('#viewerTasks .viewer-table-scroll');
      updatedScroll.scrollTop = scrollTop;
      updatedScroll.scrollLeft = scrollLeft;
    }
    if (!view.tasks.some(task => task.name === view.selected)) view.selected = view.tasks[0]?.name || null;
    renderViewerGraph();
    if (view.tasks.some(task => task.name === view.selected)) selectViewerTask(view.selected);
    else {
      view.selected = null;
      view.renderedNode = null;
      document.getElementById('viewerNode').textContent = '点击节点查看详情';
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
  document.querySelectorAll('#viewerGraph .dag-node').forEach(node => node.classList.toggle('preview-selected', node.dataset.task === view.selected));
}

function selectViewerTask(name) {
  const view = workflowViewer, task = view?.tasks.find(item => item.name === name);
  if (!task) return;
  view.selected = name;
  document.querySelectorAll('#viewerGraph .dag-node').forEach(node => node.classList.toggle('preview-selected', node.dataset.task === name));
  document.querySelectorAll('[data-run-task]').forEach(row => row.classList.toggle('viewer-row-selected', row.dataset.runTask === name));
  // Status refreshes must not replace the code DOM or reset its selection/scroll.
  if (view.renderedNode === name) return;
  view.renderedNode = name;
  const target = document.getElementById('viewerNode');
  const heading = `<strong title="${esc(task.description || name)}">${esc(task.description || name)}</strong><span class="secondary mono" title="${esc(name)}">${esc(name)}</span>`;
  const caption = view.kind === 'run' ? '节点 YAML（只读 · 当前定义，非运行时快照）' : '节点 YAML（只读）';
  target.innerHTML = `<div class="viewer-node-heading">${heading}<span class="secondary viewer-node-caption">${caption}</span></div><pre class="viewer-code">${esc(task.code ?? '该节点当前定义已不可用，历史记录未保存代码快照。')}</pre>`;
}
