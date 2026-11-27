let workflowEditor = null;

// Shared shell for editing, workflow inspection and run inspection.
function mountWorkflowFrame(left, graph, onDirection) {
  const layout = document.createElement('div');
  layout.className = 'workflow-editor-layout workflow-editor-vertical';
  left.before(layout);
  graph.classList.add('workflow-preview');
  layout.append(left, graph);
  const card = document.querySelector('#modal .modal-card');
  card.classList.add('workflow-editor-card');
  const toggle = document.createElement('button');
  toggle.id = 'workflowViewToggle';
  toggle.type = 'button';
  toggle.className = 'button ghost';
  let direction = 'TB';
  const update = () => {
    const vertical = direction === 'TB';
    layout.classList.toggle('workflow-editor-vertical', vertical);
    toggle.textContent = vertical ? '视图：上 ↓ 下 · 切换为左 → 右' : '视图：左 → 右 · 切换为上 ↓ 下';
    toggle.setAttribute('aria-pressed', String(vertical));
  };
  toggle.onclick = () => { direction = direction === 'TB' ? 'LR' : 'TB'; update(); onDirection(direction); };
  update();
  card.querySelector('.modal-head .icon-button').before(toggle);
  return layout;
}

function disposeWorkflowEditor() {
  if (workflowEditor) {
    clearTimeout(workflowEditor.timer);
    clearTimeout(workflowEditor.formSyncTimer);
    clearTimeout(workflowEditor.scheduleSyncTimer);
    workflowEditor.controller?.abort();
    workflowEditor.syncController?.abort();
    workflowEditor = null;
  }
  document.getElementById('workflowViewToggle')?.remove();
  document.querySelector('#modal .modal-card')?.classList.remove('workflow-editor-card', 'form-mode');
}

function initWorkflowEditor() {
  const input = document.querySelector('#modalBody #importDefinition, #modalBody #workflowDefinition');
  if (!input) return;
  const isReadOnlyForm = Boolean(document.querySelector('#modalBody #workflowDisplayName')?.disabled);
  const panel = document.createElement('section');
  panel.className = 'workflow-preview';
  panel.innerHTML = `<div class="preview-heading"><strong>实时工作流预览</strong><span id="previewStatus" class="preview-status-pill" aria-live="polite"></span></div><div id="previewGraph" class="dag-scroll"><div class="dag-empty">正在加载预览…</div></div>`;
  panel.addEventListener('mousedown', event => event.preventDefault());
  panel.querySelector('#previewGraph')?.addEventListener('click', event => {
    if (event.target.closest('.dag-node')) return;
    clearPreviewSelection();
  });
  panel.querySelector('#previewGraph')?.addEventListener('contextmenu', event => {
    const editor = workflowEditor;
    if (!editor || !editor.isEdit || !editor.isReadOnly) return;
    const node = event.target.closest('.dag-node');
    if (!node) return;
    if (typeof canWorkflow !== 'function' || !canWorkflow(editor.name, 'edit')) return;
    event.preventDefault();
    if (typeof openTaskRunMenu === 'function') {
      openTaskRunMenu(editor.name, node.dataset.task, event);
    }
  });
  const save = document.querySelector('#modalBody button[onclick="submitImportWorkflow()"], #modalBody button[onclick^="saveWorkflow("]');
  const form = input.closest('.form-stack');
  const layout = mountWorkflowFrame(form, panel, toggleWorkflowView);
  const footer = document.createElement('div');
  footer.className = 'workflow-editor-footer';
  if (save) footer.append(save);
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'button ghost';
  cancel.textContent = isReadOnlyForm ? t('关闭') : t('取消');
  cancel.onclick = closeModal;
  footer.append(cancel);
  const formError = document.createElement("div");
  formError.id = "formError";
  formError.className = "form-error";
  formError.setAttribute("role", "alert");
  formError.style.display = "none";
  footer.prepend(formError);
  layout.after(footer);
  workflowEditor = {
    input, panel, controls: null, save, layout, form, direction: 'TB', valid: false, source: null, tasks: [], selected: null,
    name: input.id === 'workflowDefinition' ? document.querySelector('#modalBody input[readonly]').value : pendingWorkflowId,
    timer: null, controller: null, revision: 0, undo: null, busy: false,
    mode: 'form', isEdit: input.id === 'workflowDefinition', formModel: null, formSyncTimer: null, formOpen: new Set(),
    scheduleSyncTimer: null, scheduleDirty: false, dirty: false, syncRevision: 0, syncController: null,
    runnerFieldHtml: '', openSections: new Set(), isReadOnly: isReadOnlyForm, collapseGuardUntil: 0,
    selectedTask: null, formError, errorTasks: new Set(),
  };
  document.querySelector('#modal .modal-card')?.classList.add('form-mode');
  const runnerSelect = document.querySelector('#modalBody #workflowRunner, #modalBody #importRunner');
  if (runnerSelect) {
    const label = runnerSelect.closest('label');
    workflowEditor.runnerFieldHtml = label ? label.outerHTML : '';
    if (label) label.remove();
  }
  // 表单模式下隐藏旧 YAML 说明文字，减少无效占位。
  const yamlHint = form.querySelector('.yaml-key-sample')?.closest('.form-hint');
  if (yamlHint) yamlHint.classList.add('yaml-only-hint');
  const yamlLabel = input.closest('label');
  const formPanel = document.createElement('div');
  formPanel.id = 'taskFormPanel';
  formPanel.className = 'task-form-panel';
  yamlLabel.before(formPanel);
  formPanel.addEventListener('input', onTaskFormInput);
  formPanel.addEventListener('change', onTaskFormChange);
  formPanel.addEventListener('click', onTaskFormClick);
  formPanel.addEventListener('toggle', onTaskFormToggle, true);
  formPanel.addEventListener('scroll', onTaskFormScroll, true);
  // 删除/新增定时后列表会整体上移，防止同一位置的连击误触到下方的任务摘要。
  formPanel.addEventListener('click', event => {
    const editor = workflowEditor;
    if (!editor || Date.now() > (editor.collapseGuardUntil || 0)) return;
    if (event.target.closest('details.tf-task > summary')) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);
  // 所有工作流编辑都使用表单模式；旧 YAML 编辑器保留在代码中但不再展示。
  yamlLabel.style.display = 'none';
  loadFormModel();
  queueWorkflowPreview(input.id, 0);
}

function toggleWorkflowView(direction) {
  const editor = workflowEditor;
  if (!editor) return;
  editor.direction = direction;
  renderPreviewGraph(editor);
  if (typeof fitMaximizedCardWidth === "function") fitMaximizedCardWidth();
}

function setPreviewStatus(text, state = 'ok') {
  const normalized = state === true ? 'error' : state === false ? 'ok' : state;
  const status = document.getElementById('previewStatus');
  if (!status) {
    if (normalized === 'error') toast(text, true);
    return;
  }
  status.textContent = text;
  status.classList.toggle('preview-error', normalized === 'error');
  status.classList.toggle('preview-warn', normalized === 'warn');
  status.classList.toggle('preview-syncing', normalized === 'syncing');
}

function updateEditorControls() {
  const editor = workflowEditor;
  if (!editor) return;
  const ready = editor.mode === 'form'
    ? !editor.busy
    : (!editor.busy && editor.source === editor.input.value);
  if (editor.save) editor.save.disabled = !ready;
  editor.controls?.querySelectorAll('[data-add-node]').forEach(button => { button.disabled = !ready; });
  const undo = document.getElementById('previewUndo');
  if (undo) undo.disabled = editor.busy || !editor.undo || editor.input.value !== editor.undo.after;
}

function renderPreviewGraph(editor, statusOverride = null) {
  if (!editor) return;
  const graph = document.getElementById('previewGraph');
  if (!graph) return;
  const validation = editor.formModel
    ? validateWorkflowFormModel(editor.formModel)
    : {errors: [], taskIssues: new Map()};
  const tasks = editor.tasks || [];
  const scrollTop = graph.scrollTop;
  const scrollLeft = graph.scrollLeft;
  if (!tasks.length) {
    graph.innerHTML = `<div class="dag-empty">${esc(t("暂无任务节点，请在左侧新增任务"))}</div>`;
  } else {
    graph.innerHTML = buildDagSvg(tasks, '工作流预览', false, true, editor.direction, false, validation.taskIssues);
    editor.panel.querySelectorAll('.dag-node').forEach(node => {
      node.classList.toggle('preview-selected', node.dataset.task === editor.selected);
    });
  }
  graph.scrollTop = scrollTop;
  graph.scrollLeft = scrollLeft;
  if (typeof fitMaximizedCardWidth === "function") fitMaximizedCardWidth();
  if (statusOverride) {
    setPreviewStatus(statusOverride.text, statusOverride.state);
  } else if (validation.errors.length) {
    setPreviewStatus(`${tasks.length} 个节点 · ${validation.errors.length} 项待完善`, 'warn');
  } else {
    setPreviewStatus(`${tasks.length} 个节点 · 校验通过`, 'ok');
  }
}

function renderDraftPreviewFromModel(editor) {
  if (!editor || !editor.formModel) return;
  const fallbackRunner = editor.formModel.runner || 'local';
  editor.tasks = editor.formModel.tasks.map(task => ({
    name: task.name,
    description: task.description,
    depends: [...(task.depends || [])],
    enabled: Boolean(task.enabled),
    type: task.type || 'command',
    command: task.command || '',
    success: [...(task.success || [])],
    failure: [...(task.failure || [])],
    runner: task.runner || fallbackRunner,
  }));
  renderPreviewGraph(editor);
}

function queueWorkflowPreview(id, delay = 450) {
  const editor = workflowEditor;
  if (!editor || editor.input.id !== id) return;
  clearTimeout(editor.timer);
  editor.controller?.abort();
  editor.revision++;
  editor.valid = false;
  updateEditorControls();
  setPreviewStatus('正在加载预览…', 'syncing');
  editor.timer = setTimeout(() => refreshWorkflowPreview(editor), delay);
}

async function refreshWorkflowPreview(editor, addition = null) {
  if (workflowEditor !== editor) return;
  const source = editor.input.value, revision = editor.revision;
  const controller = new AbortController();
  editor.controller = controller;
  try {
    // Use a dedicated request so an aborted preview does not mark the service offline.
    const response = await fetch('/api/workflows/editor-preview', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-DAGRunner-Language': currentLanguage}, signal: controller.signal,
      body: JSON.stringify({definition: source, workflow_name: editor.name, ...(addition ? {add: addition} : {})})
    });
    const data = await response.json();
    if (workflowEditor !== editor || revision !== editor.revision || source !== editor.input.value) return;
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    if (addition) {
      editor.undo = {before: source, after: data.definition};
      editor.input.value = data.definition;
      syncYamlHighlight(editor.input.id);
      clearTimeout(editor.timer);
      editor.selected = data.added;
      if (editor.mode === 'form') await loadFormModel();
    }
    editor.tasks = data.tasks;
    editor.source = editor.input.value;
    editor.valid = true;
    renderPreviewGraph(editor);
    if (editor.selected && data.tasks.some(task => task.name === editor.selected)) selectPreviewTask(editor.selected, Boolean(addition), false);
    else editor.selected = null;
  } catch (error) {
    if (error.name === 'AbortError' || workflowEditor !== editor || revision !== editor.revision || source !== editor.input.value) return;
    editor.valid = false;
    if (editor.mode === 'form' && editor.formModel) {
      renderDraftPreviewFromModel(editor);
    } else {
      setPreviewStatus(`无法更新预览：${error.message}`, 'error');
    }
  } finally {
    if (workflowEditor === editor) updateEditorControls();
  }
}

function clearPreviewSelection() {
  const editor = workflowEditor;
  if (!editor) return;
  editor.selected = null;
  editor.selectedTask = null;
  editor.formOpen.clear();
  editor.panel.querySelectorAll('.dag-node').forEach(node => node.classList.remove('preview-selected'));
  document.querySelectorAll('#taskFormPanel details.tf-task').forEach(item => { item.open = false; });
  refreshSelectionHighlight();
  refreshInsertButtons();
}

function selectPreviewTask(name, scrollToTask = true, allowToggle = true) {
  const editor = workflowEditor, task = editor?.tasks.find(item => item.name === name);
  if (!task) return;
  if (allowToggle && editor.selected === name) {
    clearPreviewSelection();
    return;
  }
  editor.selected = name;
  editor.selectedTask = name;
  editor.panel.querySelectorAll('.dag-node').forEach(node => node.classList.toggle('preview-selected', node.dataset.task === name));
  const card = document.querySelector(`#taskFormPanel details.tf-task[data-task="${CSS.escape(name)}"]`);
  if (card) {
    // 手风琴：选中一个任务时，其他任务卡片收起。
    document.querySelectorAll('#taskFormPanel details.tf-task').forEach(item => {
      if (item.dataset.task !== name) {
        item.open = false;
        editor.formOpen.delete(item.dataset.task);
      }
    });
    card.open = true;
    editor.formOpen.add(name);
    refreshSelectionHighlight();
    card.scrollIntoView({block: 'nearest'});
  }
  refreshInsertButtons();
  const selection = document.getElementById('previewSelection');
  if (!selection) return;
  selection.innerHTML = `<strong>${esc(task.description || name)}</strong><div class="secondary mono">${esc(name)}</div><div class="preview-add-actions">${task.type === 'condition' ? '<label>连接分支<select id="previewBranch"><option value="success">条件成立（success）</option><option value="failure">条件不成立（failure）</option></select></label>' : ''}<button type="button" class="button ghost" data-add-node onclick="addPreviewTask('command')">＋ 普通模块</button><button type="button" class="button ghost" data-add-node onclick="addPreviewTask('condition')">＋ 条件分支</button></div><span class="form-hint">${task.type === 'condition' ? '新节点仅在所选分支被选中时运行。' : '新增模块依赖当前节点，已有后续模块继续保留。'} 条件分支默认判断当前节点状态为 SUCCESS，可在 YAML 中修改。</span>`;
  updateEditorControls();
  if (scrollToTask && editor.mode !== 'form') {
    const line = scrollYamlToTask(editor.input, editor.input.value, name);
    flashYamlLine(editor.input, line);
  } else if (!scrollToTask) {
    editor.input.focus();
  }
}

async function addPreviewTask(kind) {
  const editor = workflowEditor;
  if (!editor || !editor.valid || editor.busy || editor.source !== editor.input.value || !editor.selected) return;
  clearTimeout(editor.timer);
  editor.busy = true;
  editor.input.readOnly = true;
  setPreviewStatus('正在生成节点…', 'syncing');
  updateEditorControls();
  try {
    await refreshWorkflowPreview(editor, {parent: editor.selected, kind, branch: document.getElementById('previewBranch')?.value});
  } finally {
    editor.input.readOnly = false;
    editor.busy = false;
    if (workflowEditor === editor) updateEditorControls();
  }
}

function undoWorkflowGeneration() {
  const editor = workflowEditor;
  if (!editor || editor.busy || !editor.undo || editor.input.value !== editor.undo.after) return;
  editor.input.value = editor.undo.before;
  editor.undo = null;
  syncYamlHighlight(editor.input.id);
  if (editor.mode === 'form') loadFormModel();
  queueWorkflowPreview(editor.input.id, 200);
}

/* ---------------- 表单模式（紧凑任务卡片） ---------------- */

async function setEditorMode(mode) {
  const editor = workflowEditor;
  if (!editor || !editor.isEdit || editor.mode === mode) return;
  editor.mode = mode;
  document.querySelector('#modal .modal-card').classList.toggle('form-mode', mode === 'form');
  editor.input.closest('label').style.display = mode === 'form' ? 'none' : '';
  document.getElementById('taskFormPanel').classList.toggle('hidden', mode !== 'form');
  const button = document.getElementById('editorModeToggle');
  if (button) button.textContent = mode === 'form' ? t('YAML 模式') : t('表单模式');
  if (mode === 'form') await loadFormModel();
  else queueWorkflowPreview(editor.input.id, 200);
}

async function loadFormModel() {
  const editor = workflowEditor;
  if (!editor) return;
  const panel = document.getElementById('taskFormPanel');
  panel.innerHTML = '<span class="tf-note">正在加载表单…</span>';
  try {
    const model = editor.isEdit
      ? await api(`/api/workflows/${encodeURIComponent(editor.name)}/model`)
      : await api('/api/workflows/import-model', {
          method: 'POST',
          body: JSON.stringify({definition: editor.input.value}),
        });
    if (workflowEditor !== editor || editor.mode !== 'form') return;
    model.schedule = model.schedule || {crons: [], timezone: 'Asia/Shanghai', enabled: false};
    editor.formModel = model;
    editor.scheduleDirty = false;
    editor.dirty = false;
    renderTaskForm();
    renderDraftPreviewFromModel(editor);
  } catch (error) {
    panel.innerHTML = `<span class="tf-note">表单加载失败：${esc(error.message)}</span>`;
  }
}

function refreshScheduleEnabledState() {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  const schedule = editor.formModel.schedule;
  const hasCron = schedule.crons.some(item => item.trim());
  const box = document.querySelector('[data-field="schedule-enabled"]');
  if (box) {
    box.disabled = !hasCron;
    if (!hasCron && schedule.enabled) {
      schedule.enabled = false;
      box.checked = false;
    }
  }
}

function findDuplicateCrons() {
  const crons = workflowEditor?.formModel?.schedule?.crons || [];
  const seen = new Set();
  const duplicates = new Set();
  for (const raw of crons) {
    const value = raw.trim();
    if (!value) continue;
    if (seen.has(value)) duplicates.add(value);
    else seen.add(value);
  }
  return [...duplicates];
}

function addFormCron(index) {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  const schedule = editor.formModel.schedule;
  const defaultCron = '0 10 * * *';
  if (index == null) schedule.crons.push(defaultCron);
  else schedule.crons.splice(Number(index) + 1, 0, defaultCron);
  editor.scheduleDirty = true;
  renderTaskForm();
  refreshScheduleEnabledState();
  editor.collapseGuardUntil = Date.now() + 350;
  scheduleFormSync();
}

function removeFormCron(index) {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  editor.formModel.schedule.crons.splice(Number(index), 1);
  editor.scheduleDirty = true;
  renderTaskForm();
  refreshScheduleEnabledState();
  editor.collapseGuardUntil = Date.now() + 350;
  scheduleFormSync();
}

// 定时配置仅在点击保存时统一提交，避免编辑过程中提前落库或取消无法回滚。
async function flushScheduleServerSync() {
  const editor = workflowEditor;
  if (!editor || !editor.isEdit || editor.isReadOnly || !editor.formModel || !editor.scheduleDirty) return;
  const schedule = editor.formModel.schedule;
  const crons = (schedule.crons || []).map(item => item.trim()).filter(Boolean);
  if (!crons.length) {
    editor.scheduleDirty = false;
    return;
  }
  await api(`/api/workflows/${encodeURIComponent(editor.name)}/schedule`, {
    method: 'PUT',
    body: JSON.stringify({crons, timezone: (schedule.timezone || 'Asia/Shanghai').trim(), enabled: Boolean(schedule.enabled)}),
  });
  editor.scheduleDirty = false;
}

function countTextLines(text) {
  const s = String(text || '');
  let count = 1;
  for (let i = 0; i < s.length; i += 1) {
    if (s.charCodeAt(i) === 10) count += 1;
  }
  return count;
}

function codeLineNumbers(countOrText) {
  const count = typeof countOrText === 'number' ? Math.max(1, countOrText) : countTextLines(countOrText);
  let out = '1';
  for (let i = 2; i <= count; i += 1) out += `\n${i}`;
  return out;
}

function codeBoxHtml(textareaHtml, text) {
  return `<div class="tf-code-box"><div class="tf-code-gutter" aria-hidden="true">${codeLineNumbers(text)}</div>${textareaHtml}</div>`;
}

function onTaskFormScroll(event) {
  const target = event.target;
  if (!target?.matches?.('.tf-command, .tf-setup')) return;
  const gutter = target.closest('.tf-code-box')?.querySelector('.tf-code-gutter');
  if (gutter) gutter.scrollTop = target.scrollTop;
}

function autoGrowSetup(textarea, force = false) {
  if (!textarea) return;
  const lineCount = countTextLines(textarea.value);
  const prevCount = Number(textarea.dataset.lineCount || 0);
  if (!force && prevCount === lineCount) return;
  textarea.dataset.lineCount = String(lineCount);
  const lineHeight = 18;
  const padY = 10;
  const maxLines = 8;
  const visibleLines = Math.min(Math.max(1, lineCount), maxLines);
  const heightPx = `${visibleLines * lineHeight + padY}px`;
  if (textarea.style.height !== heightPx) textarea.style.height = heightPx;
  const nextOverflowY = lineCount > maxLines ? 'auto' : 'hidden';
  if (textarea.style.overflowY !== nextOverflowY) textarea.style.overflowY = nextOverflowY;
  const gutter = textarea.closest('.tf-code-box')?.querySelector('.tf-code-gutter');
  if (gutter) {
    gutter.textContent = codeLineNumbers(lineCount);
    if (gutter.style.height !== heightPx) gutter.style.height = heightPx;
    gutter.scrollTop = textarea.scrollTop;
  }
}

function scheduleSummaryBadge(schedule) {
  const crons = (schedule?.crons || []).map(item => item.trim()).filter(Boolean);
  if (!crons.length) return '<span class="tf-meta-pill muted">未配置</span>';
  const stateLabel = schedule.enabled ? '已启用' : '未启用';
  const cls = schedule.enabled ? 'active' : 'muted';
  return `<span class="tf-meta-pill ${cls}">${crons.length} 条 · ${stateLabel}</span>`;
}

function setupSummaryBadge(model) {
  const runner = model.runner || 'local';
  const runnerLabel = runner === 'local' ? t('本机') : runner;
  const hasSetup = Boolean(String(model.setup || '').trim());
  return `<span class="tf-meta-pill">${esc(runnerLabel)}${hasSetup ? ' · 含预跑' : ''}</span>`;
}

function updateSectionSummaries() {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  const schedSummary = document.querySelector('#taskFormPanel details[data-section="schedule"] .tf-section-summary');
  if (schedSummary) schedSummary.innerHTML = scheduleSummaryBadge(editor.formModel.schedule);
  const setupSummary = document.querySelector('#taskFormPanel details[data-section="setup"] .tf-section-summary');
  if (setupSummary) setupSummary.innerHTML = setupSummaryBadge(editor.formModel);
}

function schedulePanelHtml(model, options = {}) {
  const schedule = model.schedule || {crons: [], timezone: 'Asia/Shanghai', enabled: false};
  const readOnly = Boolean(options.readOnly);
  const disabled = readOnly ? ' disabled' : '';
  const hasCron = schedule.crons.some(item => item.trim());
  const open = options.openSections ? options.openSections.has('schedule') : false;
  const rows = schedule.crons.length
    ? schedule.crons.map((cron, index) => `
      <div class="schedule-row">
        <span class="schedule-label">${index === 0 ? '定时' : ''}</span>
        <input type="text" data-field="schedule-cron" data-index="${index}" value="${esc(cron)}" placeholder="分 时 日 月 周"${disabled}>
        ${readOnly ? '' : `<span class="schedule-actions">
          <button type="button" class="icon-button" data-action="add-cron" data-index="${index}" title="在下方新增">＋</button>
          <button type="button" class="icon-button" data-action="remove-cron" data-index="${index}" title="删除该定时"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></button>
        </span>`}
      </div>`).join('')
    : `<div class="schedule-row schedule-empty-row">
        <span class="schedule-label">定时</span>
        ${readOnly ? '<span class="tf-note">未配置定时</span>' : '<button type="button" class="button ghost small" data-action="add-cron">＋ 添加定时</button>'}
      </div>`;
  return `<details class="tf-panel tf-collapse" data-section="schedule"${open ? ' open' : ''}>
    <summary class="tf-panel-head">
      <span class="tf-section-head">定时配置<span class="tf-subhint">保存时一并生效</span></span>
      <span class="tf-section-right"><span class="tf-section-summary">${scheduleSummaryBadge(schedule)}</span><span class="tf-head-caret">▶</span></span>
    </summary>
    <div class="tf-collapse-body">
      <div class="schedule-inline-top">
        <div class="schedule-row schedule-tz">
          <span class="schedule-label">时区</span>
          <input type="text" data-field="schedule-tz" value="${esc(schedule.timezone)}"${disabled}>
        </div>
        <label class="schedule-check"><input type="checkbox" data-field="schedule-enabled"${schedule.enabled ? ' checked' : ''}${hasCron && !readOnly ? '' : ' disabled'}> 启用定时<span class="schedule-note">（${t("需至少一条有效定时")}）</span></label>
      </div>
      <div id="tfScheduleRows">${rows}</div>
      <p class="form-hint">5 字段 Cron，如每小时第 10 分钟 <code>10 * * * *</code>，每天 10:00 <code>0 10 * * *</code></p>
    </div>
  </details>`;
}

function setupPanelHtml(model, options = {}) {
  const readOnly = Boolean(options.readOnly);
  const disabled = readOnly ? ' disabled' : '';
  const open = options.openSections ? options.openSections.has('setup') : false;
  const runnerBlock = options.runnerFieldHtml || `<label>${t("运行位置")}<select class="ui-select"${disabled}><option>${esc(model.runner || 'local')}</option></select></label>`;
  return `<details class="tf-panel tf-collapse" data-section="setup"${open ? ' open' : ''}>
    <summary class="tf-panel-head">
      <span class="tf-section-head">环境配置<span class="tf-subhint">仅对与工作流相同运行位置的任务生效</span></span>
      <span class="tf-section-right"><span class="tf-section-summary">${setupSummaryBadge(model)}</span><span class="tf-head-caret">▶</span></span>
    </summary>
    <div class="tf-collapse-body">
      <div class="tf-setup-grid">
        <label class="f">预跑命令${codeBoxHtml(`<textarea id="tfSetup" class="tf-setup" rows="1" wrap="off" data-field="setup" placeholder="如：激活虚拟环境、切换目录或设置环境变量"${disabled}>${esc(model.setup)}</textarea>`, model.setup)}</label>
        <div class="tf-runner-slot">${runnerBlock}</div>
      </div>
      <p class="form-hint">预跑命令仅对与工作流「运行位置」相同的任务生效；单独指定了其他运行位置的任务不会执行该预跑命令。</p>
    </div>
  </details>`;
}

function taskListToolbarHtml(extraButtons = '', count = null) {
  const countBadge = count == null ? '' : `<span class="tf-count-badge">${count}</span>`;
  return `<div class="tf-panel-head tf-list-head"><span class="tf-section-head">任务列表${countBadge}</span><div class="tf-toolbar">
    <span id="tfInsertHint" class="tf-subhint">${t("未选择任务")}</span>
    <button type="button" class="button ghost small" data-action="expand">展开</button>
    <button type="button" class="button ghost small" data-action="collapse">折叠</button>
    ${extraButtons}
  </div></div>`;
}

function taskRunnerOptionsHtml(selected, workflowRunner) {
  const defaultAlias = workflowRunner && workflowRunner !== 'local' ? workflowRunner : '';
  const defaultLabel = defaultAlias || t('本机');
  const options = [
    `<option value=""${!selected ? ' selected' : ''}>${esc(t('跟随工作流'))}（${esc(defaultLabel)}）</option>`,
    `<option value="local"${selected === 'local' ? ' selected' : ''}>${esc(t('本机'))}</option>`,
  ];
  const known = new Set(['', 'local']);
  const satellites = typeof satelliteConfigs !== 'undefined' && Array.isArray(satelliteConfigs) ? satelliteConfigs : [];
  for (const satellite of satellites) {
    known.add(satellite.alias);
    options.push(`<option value="${esc(satellite.alias)}"${selected === satellite.alias ? ' selected' : ''}>${esc(satellite.alias)}</option>`);
  }
  if (selected && !known.has(selected)) {
    options.push(`<option value="${esc(selected)}" selected>${esc(selected)}（${esc(t('未配置'))}）</option>`);
  }
  return options.join('');
}

function taskCardHtml(model, task, index, options = {}) {
  const names = Object.fromEntries(model.tasks.map(item => [item.name, item.description || item.name]));
  const byName = new Map(model.tasks.map(item => [item.name, item]));
  const readOnly = Boolean(options.readOnly);
  const disabled = readOnly ? ' disabled' : '';
  const open = options.openTasks ? options.openTasks.has(task.name) : false;
  const hasError = options.errorTasks ? options.errorTasks.has(task.name) : false;
  // 依赖候选项：默认展示前面的任务，同时保留乱序导入场景下已存在的其他依赖防止静默丢失。
  const preceding = model.tasks.slice(0, index);
  const precedingSet = new Set(preceding.map(item => item.name));
  const extraDeps = (task.depends || [])
    .filter(id => id !== task.name && !precedingSet.has(id) && byName.has(id))
    .map(id => byName.get(id));
  const depCandidates = [...preceding, ...extraDeps];
  const depsText = task.depends.length ? task.depends.map(id => names[id] || id).join('、') : '无依赖';
  const depBoxes = depCandidates.map(item => `
      <label><input type="checkbox" data-field="depends" data-task="${esc(task.name)}" value="${esc(item.name)}"${task.depends.includes(item.name) ? ' checked' : ''}${disabled}>
        <span class="tf-opt-name">${esc(item.description || item.name)}</span><span class="tf-opt-id mono">${esc(item.name)}</span></label>`).join('');
  // 条件分支候选项：默认展示后面的任务，同时保留已存在的乱序分支引用。
  const branchBoxes = field => {
    const following = model.tasks.slice(index + 1);
    const followingSet = new Set(following.map(item => item.name));
    const extraBranch = (task[field] || [])
      .filter(id => id !== task.name && !followingSet.has(id) && byName.has(id))
      .map(id => byName.get(id));
    const candidates = [...following, ...extraBranch];
    const boxes = candidates.map(item => `
      <label><input type="checkbox" data-field="${field}" data-task="${esc(task.name)}" value="${esc(item.name)}"${task[field].includes(item.name) ? ' checked' : ''}${disabled}>
        <span class="tf-opt-name">${esc(item.description || item.name)}</span><span class="tf-opt-id mono">${esc(item.name)}</span></label>`).join('');
    return boxes || '<span class="tf-note">后面没有可选任务</span>';
  };
  const body = task.type === 'condition'
    ? `<div class="tf-cond-bar">
        <div class="tf-cond-basis">
          <span class="subhead-inline">判断依据</span>
          <div class="tf-chips">${task.depends.length ? task.depends.map(id => `<span class="tf-chip">${esc(names[id] || id)}</span>`).join('') : '<span class="tf-note warn">需至少选择 1 个依赖</span>'}</div>
        </div>
        <label class="tf-cond-relation"><span>关系</span>
          <select data-field="relation" data-task="${esc(task.name)}"${disabled}>
            <option value="AND"${task.conditionRelation === 'AND' ? ' selected' : ''}>AND（全成功）</option>
            <option value="OR"${task.conditionRelation === 'OR' ? ' selected' : ''}>OR（任一成功）</option>
          </select>
        </label>
      </div>`
    : `<label class="f tf-command-field">命令${codeBoxHtml(`<textarea class="tf-command" rows="1" wrap="off" data-field="command" data-task="${esc(task.name)}" placeholder="输入要执行的命令"${disabled}>${esc(task.command)}</textarea>`, task.command)}</label>`;
  const branches = task.type === 'condition'
    ? `<div class="tf-branch-grid">
         <div class="tf-block"><div class="subhead"><span class="branch-dot success"></span>成立分支（success）<span class="note">${task.success.length}</span></div><div class="tf-checklist">${branchBoxes('success')}</div></div>
         <div class="tf-block"><div class="subhead"><span class="branch-dot failure"></span>不成立分支（failure）<span class="note">${task.failure.length}</span></div><div class="tf-checklist">${branchBoxes('failure')}</div></div>
       </div>`
    : '';
  const statusMenu = readOnly ? '' : `<div class="tf-task-menu" data-task-menu="${esc(task.name)}">
            <button type="button" class="button ghost small tf-menu-trigger ${task.enabled ? 'is-enabled' : 'is-disabled'}" data-action="toggle-task-menu" data-task="${esc(task.name)}" aria-haspopup="menu" aria-expanded="false">${task.enabled ? '启用' : '停用'} ▾</button>
            <div class="tf-menu-pop" role="menu">
              <button type="button" class="tf-menu-item${task.enabled ? ' is-current' : ''}" role="menuitem" data-action="set-enabled" data-enabled="true" data-task="${esc(task.name)}"><span>启用</span>${task.enabled ? '<span class="tf-menu-check">✓</span>' : ''}</button>
              <button type="button" class="tf-menu-item${!task.enabled ? ' is-current' : ''}" role="menuitem" data-action="set-enabled" data-enabled="false" data-task="${esc(task.name)}"><span>停用</span>${!task.enabled ? '<span class="tf-menu-check">✓</span>' : ''}</button>
              <div class="tf-menu-sep"></div>
              <button type="button" class="tf-menu-item danger" role="menuitem" data-action="delete" data-task="${esc(task.name)}"><span>删除</span></button>
            </div>
          </div>`;
  const runnerCol = task.type === 'command'
    ? `<div class="tf-task-runner-col">
        <div class="tf-label-row">
          <label class="tf-field-label" for="tfRunner_${esc(task.name)}">${t("运行位置")}</label>
          ${statusMenu}
        </div>
        <select id="tfRunner_${esc(task.name)}" class="ui-select tf-task-runner" data-field="task-runner" data-task="${esc(task.name)}"${disabled}>${taskRunnerOptionsHtml(task.runner || '', model.runner || '')}</select>
      </div>`
    : `<div class="tf-task-runner-col tf-task-runner-empty">
        <div class="tf-label-row tf-label-row-end">${statusMenu}</div>
      </div>`;
  return `<details class="tf-task${hasError ? ' tf-has-error' : ''}" data-task="${esc(task.name)}"${open ? ' open' : ''}>
    <summary>
      <span class="tf-tags"><span class="tf-tag ${task.type}${task.enabled ? '' : ' is-disabled'}">${task.type === 'condition' ? '条件' : '任务'}</span></span>
      <span class="tf-caret">▶</span>
      <span class="tf-heading"><span class="tf-title">${esc(task.description || '（未命名任务）')}</span><span class="tf-id mono">${esc(task.name)}</span></span>
      <span class="tf-deps"><span class="tf-deps-text" title="依赖：${esc(depsText)}">${task.depends.length ? `← ${esc(depsText)}` : '无依赖'}</span></span>
    </summary>
    <div class="tf-body">
      <div class="tf-title-runner-grid">
        <div class="tf-title-col">
          <div class="tf-label-row">
            <label class="tf-field-label" for="tfTitle_${esc(task.name)}">任务标题</label>
          </div>
          <input id="tfTitle_${esc(task.name)}" class="tf-title-input" type="text" data-field="title" data-task="${esc(task.name)}" value="${esc(task.description)}" placeholder="输入任务标题"${disabled}>
        </div>
        ${runnerCol}
      </div>
      <div class="tf-block tf-dep-block">
        <div class="subhead">前置依赖<span class="note">${task.depends.length} 项</span></div>
        <div class="tf-checklist tf-depends">${depBoxes || '<span class="tf-note">没有可选的前置任务</span>'}</div>
      </div>
      ${body}
      ${branches}
    </div>
  </details>`;
}

function renderTaskForm() {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  const prevScroll = document.getElementById('tfSections')?.scrollTop || 0;
  const model = editor.formModel;
  const options = {
    readOnly: editor.isReadOnly,
    openSections: editor.openSections,
    openTasks: editor.formOpen,
    errorTasks: editor.errorTasks,
  };
  const addButtons = editor.isReadOnly ? '' : `<button type="button" id="tfAddCommand" class="button small" data-action="add-command">＋ 普通任务</button>`
    + `<button type="button" id="tfAddCondition" class="button small" data-action="add-condition">＋ 条件任务</button>`;
  const cards = model.tasks.map((task, index) =>
    taskCardHtml(model, task, index, options)
  ).join('');
  document.getElementById('taskFormPanel').innerHTML = `<div id="tfSections" class="tf-sections">`
    + schedulePanelHtml(model, options)
    + setupPanelHtml(model, {...options, runnerFieldHtml: editor.runnerFieldHtml})
    + `<div class="tf-panel tf-task-list-panel">${taskListToolbarHtml(addButtons, model.tasks.length)}<div class="tf-tasks">${cards || '<div class="tf-empty-tasks">暂无任务，点击上方「＋ 普通任务」开始创建</div>'}</div></div>`
    + `</div>`;
  const runnerSelect = document.querySelector('#taskFormPanel #workflowRunner, #taskFormPanel #importRunner');
  if (runnerSelect && model.runner) runnerSelect.value = model.runner;
  autoGrowSetup(document.getElementById('tfSetup'));
  document.querySelectorAll('#taskFormPanel .tf-command').forEach(autoGrowSetup);
  refreshSelectionHighlight();
  refreshInsertButtons();
  const nextSections = document.getElementById('tfSections');
  if (nextSections) nextSections.scrollTop = prevScroll;
}

function refreshSelectionHighlight() {
  const editor = workflowEditor;
  if (!editor) return;
  document.querySelectorAll('#taskFormPanel details.tf-task').forEach(card => {
    card.classList.toggle('tf-focus', card.dataset.task === editor.selectedTask);
    card.classList.toggle('tf-has-error', editor.errorTasks?.has(card.dataset.task));
  });
}

function refreshInsertButtons() {
  const editor = workflowEditor;
  if (!editor) return;
  const tasks = editor.formModel?.tasks || [];
  const selected = editor.selectedTask;
  const label = selected
    ? (tasks.find(task => task.name === selected)?.description || selected)
    : '';
  const hint = document.getElementById('tfInsertHint');
  if (hint) {
    if (!tasks.length) {
      hint.textContent = t("可直接新增首个任务");
      hint.classList.add('active');
    } else {
      hint.textContent = selected ? t("插入位置：{name}", {name: label}) : t("点击卡片选择插入位置");
      hint.classList.toggle('active', Boolean(selected));
    }
  }
  const addCommandBtn = document.querySelector('#taskFormPanel [data-action="add-command"]');
  if (addCommandBtn) {
    const canAddCommand = !tasks.length || Boolean(selected);
    addCommandBtn.disabled = !canAddCommand;
    addCommandBtn.title = !tasks.length
      ? t("新增第一个普通任务")
      : (selected ? t("在任务 {name} 后新增", {name: label}) : t("请先选择要插入位置的任务"));
  }
  const addConditionBtn = document.querySelector('#taskFormPanel [data-action="add-condition"]');
  if (addConditionBtn) {
    const canAddCondition = Boolean(tasks.length && selected);
    addConditionBtn.disabled = !canAddCondition;
    addConditionBtn.title = !tasks.length
      ? t("请先新增普通任务作为前置依赖")
      : (selected ? t("在任务 {name} 后新增条件分支", {name: label}) : t("请先选择要插入位置的任务"));
  }
}

function scheduleFormSync() {
  const editor = workflowEditor;
  if (!editor || editor.mode !== 'form' || editor.isReadOnly) return;
  editor.dirty = true;
  if (editor.errorTasks?.size) {
    editor.errorTasks.clear();
    refreshSelectionHighlight();
  }
  clearFormError();
  clearTimeout(editor.formSyncTimer);
  editor.formSyncTimer = setTimeout(() => syncFormToServer(false), 350);
}

async function flushWorkflowModelSync() {
  const editor = workflowEditor;
  if (editor && editor.mode === 'form' && (editor.formSyncTimer || editor.dirty)) {
    await syncFormToServer(true);
  }
}

async function syncFormToServer(throwOnError = false) {
  const editor = workflowEditor;
  if (!editor || editor.mode !== 'form' || editor.isReadOnly || !editor.formModel) return;
  clearTimeout(editor.formSyncTimer);
  editor.formSyncTimer = null;
  editor.syncController?.abort();
  const controller = new AbortController();
  editor.syncController = controller;
  const revision = ++editor.syncRevision;
  editor.busy = true;
  updateEditorControls();
  setPreviewStatus('同步中…', 'syncing');
  try {
    const payload = {
      setup: editor.formModel.setup,
      runner: editor.formModel.runner || '',
      tasks: editor.formModel.tasks,
      schedule: editor.formModel.schedule,
    };
    const url = editor.isEdit
      ? `/api/workflows/${encodeURIComponent(editor.name)}/model-preview`
      : '/api/workflows/import-model-preview';
    const body = editor.isEdit
      ? JSON.stringify(payload)
      : JSON.stringify({definition: editor.input.value, model: payload});
    const response = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-DAGRunner-Language': currentLanguage},
      signal: controller.signal,
      body,
    });
    const data = await response.json();
    if (workflowEditor !== editor || revision !== editor.syncRevision) return;
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    editor.input.value = data.definition;
    syncYamlHighlight(editor.input.id, true);
    editor.source = editor.input.value;
    editor.tasks = Array.isArray(data.tasks) ? data.tasks : [];
    editor.dirty = false;
    editor.valid = !data.warning;
    renderPreviewGraph(editor);
  } catch (error) {
    if (error.name === 'AbortError' || workflowEditor !== editor || revision !== editor.syncRevision) return;
    editor.valid = false;
    editor.dirty = true;
    renderDraftPreviewFromModel(editor);
    setPreviewStatus(`同步失败：${error.message}`, 'error');
    if (throwOnError) throw error;
  } finally {
    if (workflowEditor === editor && revision === editor.syncRevision) {
      editor.busy = false;
      updateEditorControls();
    }
  }
}

function onTaskFormInput(event) {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  const field = event.target.dataset.field;
  const task = editor.formModel.tasks.find(item => item.name === event.target.dataset.task);
  if (field === 'setup') {
    editor.formModel.setup = event.target.value;
    autoGrowSetup(event.target);
    updateSectionSummaries();
  } else if (field === 'schedule-tz') {
    editor.formModel.schedule.timezone = event.target.value;
    editor.scheduleDirty = true;
  } else if (field === 'schedule-cron') {
    editor.formModel.schedule.crons[Number(event.target.dataset.index)] = event.target.value;
    editor.scheduleDirty = true;
    refreshScheduleEnabledState();
    updateSectionSummaries();
  } else if (task) {
    if (field === 'title') {
      task.description = event.target.value;
      const title = document.querySelector(`details.tf-task[data-task="${CSS.escape(task.name)}"] .tf-title`);
      if (title) title.textContent = task.description || '（未命名任务）';
      renderDraftPreviewFromModel(editor);
    } else if (field === 'command') {
      const wasEmpty = !String(task.command || '').trim();
      task.command = event.target.value;
      const isEmpty = !String(task.command || '').trim();
      autoGrowSetup(event.target);
      if (wasEmpty !== isEmpty) renderDraftPreviewFromModel(editor);
    }
  }
  scheduleFormSync();
}

function onTaskFormChange(event) {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  const field = event.target.dataset.field;
  if (field === 'schedule-enabled') {
    editor.formModel.schedule.enabled = event.target.checked;
    editor.scheduleDirty = true;
    updateSectionSummaries();
    scheduleFormSync();
    return;
  }
  const task = editor.formModel.tasks.find(item => item.name === event.target.dataset.task);
  if (!field || !task) return;
  const byName = new Map(editor.formModel.tasks.map(item => [item.name, item]));
  if (field === 'enabled') {
    task.enabled = event.target.checked;
    renderTaskForm();
    renderDraftPreviewFromModel(editor);
  } else if (field === 'task-runner') {
    task.runner = event.target.value;
    renderDraftPreviewFromModel(editor);
  } else if (field === 'relation') {
    task.conditionRelation = event.target.value;
  } else if (field === 'depends') {
    const root = event.target.closest('details.tf-task');
    const prevDeps = new Set(task.depends || []);
    const nextDeps = [...root.querySelectorAll('[data-field="depends"]:checked')].map(box => box.value);
    const nextDepSet = new Set(nextDeps);
    task.depends = nextDeps;
    // 反向联动：取消对某条件任务的依赖时，自动把当前任务从该条件任务的 success/failure 中移除。
    for (const removedId of prevDeps) {
      if (nextDepSet.has(removedId)) continue;
      const parent = byName.get(removedId);
      if (parent && parent.type === 'condition') {
        parent.success = (parent.success || []).filter(id => id !== task.name);
        parent.failure = (parent.failure || []).filter(id => id !== task.name);
      }
    }
    renderTaskForm();
    renderDraftPreviewFromModel(editor);
  } else if (field === 'success' || field === 'failure') {
    const otherField = field === 'success' ? 'failure' : 'success';
    const root = event.target.closest('details.tf-task');
    const prevTargets = new Set(task[field] || []);
    const nextTargets = [...root.querySelectorAll(`[data-field="${field}"]:checked`)].map(box => box.value);
    const nextTargetSet = new Set(nextTargets);
    task[field] = nextTargets;
    // 分支互斥：勾选进一侧分支时，自动从另一侧分支移除。
    task[otherField] = (task[otherField] || []).filter(id => !nextTargetSet.has(id));
    // 正向联动：勾选分支目标 -> 自动添加对该条件任务的依赖。
    for (const addedId of nextTargets) {
      if (prevTargets.has(addedId)) continue;
      const target = byName.get(addedId);
      if (target && !target.depends.includes(task.name)) {
        target.depends.push(task.name);
      }
    }
    // 取消分支目标 -> 同步移除目标对该条件任务的依赖（若未留在另一分支中）。
    for (const removedId of prevTargets) {
      if (nextTargetSet.has(removedId)) continue;
      if ((task[otherField] || []).includes(removedId)) continue;
      const target = byName.get(removedId);
      if (target) {
        target.depends = (target.depends || []).filter(id => id !== task.name);
      }
    }
    renderTaskForm();
    renderDraftPreviewFromModel(editor);
  }
  scheduleFormSync();
}

function closeAllTaskMenus(exceptMenu = null) {
  document.querySelectorAll('#taskFormPanel .tf-task-menu.open').forEach(menu => {
    if (menu === exceptMenu) return;
    menu.classList.remove('open');
    menu.querySelector('.tf-menu-trigger')?.setAttribute('aria-expanded', 'false');
  });
}

if (!window._tfTaskMenuOutsideBound) {
  window._tfTaskMenuOutsideBound = true;
  document.addEventListener('click', event => {
    if (!event.target.closest('.tf-task-menu')) closeAllTaskMenus();
  });
}

function setFormTaskEnabled(name, enabled) {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  const task = editor.formModel.tasks.find(item => item.name === name);
  if (!task || task.enabled === enabled) {
    closeAllTaskMenus();
    return;
  }
  task.enabled = enabled;
  renderTaskForm();
  renderDraftPreviewFromModel(editor);
  scheduleFormSync();
}

function onTaskFormClick(event) {
  const actionButton = event.target.closest('[data-action]');
  if (!actionButton) return;
  const action = actionButton.dataset.action;
  if (action === 'toggle-task-menu') {
    const menu = actionButton.closest('.tf-task-menu');
    if (!menu) return;
    const willOpen = !menu.classList.contains('open');
    closeAllTaskMenus(willOpen ? menu : null);
    menu.classList.toggle('open', willOpen);
    actionButton.setAttribute('aria-expanded', String(willOpen));
    return;
  }
  if (action === 'set-enabled') {
    setFormTaskEnabled(actionButton.dataset.task, actionButton.dataset.enabled === 'true');
    return;
  }
  closeAllTaskMenus();
  if (action === 'expand') toggleTaskForm(true);
  else if (action === 'collapse') toggleTaskForm(false);
  else if (action === 'add-command') addFormTask('command');
  else if (action === 'add-condition') addFormTask('condition');
  else if (action === 'add-cron') addFormCron(actionButton.dataset.index);
  else if (action === 'remove-cron') removeFormCron(actionButton.dataset.index);
  else if (action === 'delete') deleteFormTask(actionButton.dataset.task);
}

function onTaskFormToggle(event) {
  const target = event.target;
  const editor = workflowEditor;
  if (!editor || !target.matches) return;
  if (target.matches('details.tf-collapse')) {
    const section = target.dataset.section;
    if (target.open) {
      editor.openSections.add(section);
      if (section === 'setup') autoGrowSetup(document.getElementById('tfSetup'));
    } else {
      editor.openSections.delete(section);
    }
    return;
  }
  if (!target.matches('details.tf-task')) return;
  if (target.open) {
    editor.formOpen.add(target.dataset.task);
    autoGrowSetup(target.querySelector('.tf-command'));
    // 展开任务卡片即选中它，作为新增任务的插入锚点。
    editor.selectedTask = target.dataset.task;
    editor.selected = target.dataset.task;
    refreshSelectionHighlight();
    refreshInsertButtons();
    editor.panel.querySelectorAll('.dag-node').forEach(node => {
      node.classList.toggle('preview-selected', node.dataset.task === editor.selected);
    });
  } else {
    editor.formOpen.delete(target.dataset.task);
    if (editor.selectedTask === target.dataset.task) {
      editor.selectedTask = null;
      editor.selected = null;
      refreshSelectionHighlight();
      refreshInsertButtons();
      editor.panel.querySelectorAll('.dag-node').forEach(node => node.classList.remove('preview-selected'));
    }
  }
}

function toggleTaskForm(open) {
  const editor = workflowEditor;
  if (!editor) return;
  editor.formOpen.clear();
  if (open) editor.formModel.tasks.forEach(task => editor.formOpen.add(task.name));
  renderTaskForm();
}

function addFormTask(type) {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  const makeId = () => {
    const alphabet = '0123456789abcdef';
    for (;;) {
      let hex = '';
      for (let index = 0; index < 8; index += 1) {
        hex += alphabet[Math.floor(Math.random() * alphabet.length)];
      }
      const id = `task_${hex}`;
      if (!editor.formModel.tasks.some(task => task.name === id)) return id;
    }
  };
  const tasks = editor.formModel.tasks;
  // 空任务列表时允许直接新增首个普通任务。
  if (!tasks.length) {
    if (type !== 'command') {
      toast(t("请先新增普通任务作为前置依赖"), true);
      return;
    }
    const id = makeId();
    tasks.push({
      name: id,
      description: '新普通任务',
      type: 'command',
      enabled: true,
      depends: [],
      command: 'echo TODO',
      conditionRelation: 'AND',
      success: [],
      failure: [],
      runner: '',
    });
    editor.selectedTask = id;
    editor.selected = id;
    editor.formOpen.clear();
    editor.formOpen.add(id);
    renderTaskForm();
    renderDraftPreviewFromModel(editor);
    scheduleFormSync();
    return;
  }
  const selected = editor.selectedTask;
  const anchorIndex = selected ? tasks.findIndex(item => item.name === selected) : -1;
  if (anchorIndex < 0) {
    toast(t("请先选择要插入位置的任务"), true);
    return;
  }
  const anchor = tasks[anchorIndex];
  const id = makeId();
  const task = {
    name: id,
    description: type === 'condition' ? '新条件任务' : '新普通任务',
    type,
    enabled: true,
    depends: [anchor.name],
    command: type === 'command' ? 'echo TODO' : '',
    conditionRelation: 'AND',
    success: [],
    failure: [],
    runner: '',
  };
  let insertAt = anchorIndex + 1;
  tasks.splice(insertAt, 0, task);
  insertAt += 1;
  if (type === 'condition') {
    // 与原有生成逻辑一致：条件任务自动带「成立后 / 不成立后」两个子任务。
    const children = [
      {description: '条件成立后执行', branch: 'success'},
      {description: '条件不成立后执行', branch: 'failure'},
    ];
    for (const child of children) {
      const childId = makeId();
      tasks.splice(insertAt, 0, {
        name: childId,
        description: child.description,
        type: 'command',
        enabled: true,
        depends: [id],
        command: 'echo TODO',
        conditionRelation: 'AND',
        success: [],
        failure: [],
      });
      insertAt += 1;
      task[child.branch].push(childId);
    }
  }
  editor.selectedTask = id;
  editor.selected = id;
  // 新任务默认选中并展开，其余任务卡片折叠。
  editor.formOpen.clear();
  editor.formOpen.add(id);
  renderTaskForm();
  renderDraftPreviewFromModel(editor);
  scheduleFormSync();
}

function deleteFormTask(name) {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  if (editor.selectedTask === name) editor.selectedTask = null;
  if (editor.selected === name) editor.selected = null;
  editor.formModel.tasks = editor.formModel.tasks.filter(task => task.name !== name);
  editor.formModel.tasks.forEach(task => {
    task.depends = (task.depends || []).filter(item => item !== name);
    task.success = (task.success || []).filter(item => item !== name);
    task.failure = (task.failure || []).filter(item => item !== name);
  });
  editor.formOpen.delete(name);
  editor.errorTasks?.delete(name);
  renderTaskForm();
  renderDraftPreviewFromModel(editor);
  scheduleFormSync();
}

function validateWorkflowFormModel(model) {
  const errors = [];
  const taskIssues = new Map();
  const addError = (message, taskName = null) => {
    errors.push({message, taskName});
    if (taskName) {
      if (!taskIssues.has(taskName)) taskIssues.set(taskName, []);
      taskIssues.get(taskName).push(message);
    }
  };
  if (!model) return {errors, taskIssues};
  const tasks = model.tasks || [];
  if (!tasks.length) {
    addError(t("工作流至少需要一个任务"));
  }
  const idPattern = /^[A-Za-z0-9_.-]+$/;
  const byName = new Map();
  const labelOf = task => (String(task?.description || '').trim() || task?.name || '未命名任务');

  for (let index = 0; index < tasks.length; index += 1) {
    const task = tasks[index];
    const name = String(task.name || '').trim();
    if (!name) {
      addError(`第 ${index + 1} 个任务缺少任务 ID`);
      continue;
    }
    if (!idPattern.test(name)) {
      addError(`任务「${labelOf(task)}」的 ID 仅支持字母、数字、_、.、-`, name);
    }
    if (byName.has(name)) {
      addError(t("存在重复的任务 ID：{name}", {name}), name);
    } else {
      byName.set(name, task);
    }
    if (!String(task.description || '').trim()) {
      addError(t("第 {n} 个任务的标题不能为空", {n: index + 1}), name);
    }
  }

  for (const task of tasks) {
    const label = labelOf(task);
    const deps = task.depends || [];
    for (const dep of deps) {
      if (dep === task.name) {
        addError(`任务「${label}」不能依赖自身`, task.name);
      } else if (!byName.has(dep)) {
        addError(`任务「${label}」依赖了不存在的任务（${dep}）`, task.name);
      }
    }
    if (task.type === 'condition') {
      if (!deps.length) {
        addError(`条件任务「${label}」至少需要依赖 1 个前置任务作为判断依据`, task.name);
      }
      const success = task.success || [];
      const failure = task.failure || [];
      if (!success.length && !failure.length) {
        addError(`条件任务「${label}」至少需要配置 1 个成立或不成立分支`, task.name);
      }
      const successSet = new Set(success);
      for (const targetId of failure) {
        if (successSet.has(targetId)) {
          const targetLabel = byName.has(targetId) ? labelOf(byName.get(targetId)) : targetId;
          addError(`条件任务「${label}」的成立与不成立分支不能同时包含「${targetLabel}」`, task.name);
        }
      }
      for (const targetId of [...success, ...failure]) {
        const target = byName.get(targetId);
        if (!target) {
          addError(`条件任务「${label}」指向了不存在的分支任务（${targetId}）`, task.name);
        } else if (!(target.depends || []).includes(task.name)) {
          addError(`条件任务「${label}」的分支目标「${labelOf(target)}」未依赖该条件任务`, task.name);
        }
      }
    } else if (!String(task.command || '').trim()) {
      addError(`任务「${label}」的命令不能为空`, task.name);
    }
  }

  // 环依赖检测（Kahn 拓扑排序）
  if (tasks.length) {
    const indegree = new Map();
    const children = new Map();
    for (const task of tasks) {
      indegree.set(task.name, 0);
      children.set(task.name, []);
    }
    for (const task of tasks) {
      for (const dep of task.depends || []) {
        if (dep !== task.name && byName.has(dep)) {
          indegree.set(task.name, (indegree.get(task.name) || 0) + 1);
          children.get(dep).push(task.name);
        }
      }
    }
    const queue = [...indegree.entries()].filter(([, deg]) => deg === 0).map(([name]) => name);
    let visited = 0;
    while (queue.length) {
      const current = queue.shift();
      visited += 1;
      for (const child of children.get(current) || []) {
        const nextDeg = indegree.get(child) - 1;
        indegree.set(child, nextDeg);
        if (nextDeg === 0) queue.push(child);
      }
    }
    if (visited < tasks.length) {
      const cyclic = tasks.filter(task => (indegree.get(task.name) || 0) > 0);
      const namesText = cyclic.map(labelOf).join('、');
      for (const task of cyclic) {
        if (!taskIssues.has(task.name)) taskIssues.set(task.name, []);
        taskIssues.get(task.name).push(`存在循环依赖`);
      }
      errors.push({message: `任务依赖存在循环：${namesText}`, taskName: cyclic[0]?.name || null});
    }
  }

  // 定时配置校验
  const schedule = model.schedule;
  if (schedule) {
    const rawCrons = schedule.crons || [];
    const seenCrons = new Set();
    const validCrons = [];
    rawCrons.forEach((raw, index) => {
      const trimmed = String(raw || '').trim();
      if (!trimmed) return;
      const fields = trimmed.split(/\s+/);
      const normalized = fields.join(' ');
      if (fields.length !== 5 || (typeof cronFieldIsValid === 'function' && !fields.every((f, idx) => cronFieldIsValid(f, idx)))) {
        addError(`定时配置第 ${index + 1} 条 Cron 格式无效（需为合法 5 字段表达式）`);
        return;
      }
      if (seenCrons.has(normalized)) {
        addError(`定时配置存在重复的 Cron：${normalized}`);
        return;
      }
      seenCrons.add(normalized);
      validCrons.push(normalized);
    });
    if (schedule.enabled && !validCrons.length) {
      addError('启用定时任务时至少需要配置 1 条有效 Cron');
    }
    if (validCrons.length) {
      const tz = String(schedule.timezone || '').trim();
      if (!tz) {
        addError('定时配置的时区不能为空');
      } else {
        try {
          new Intl.DateTimeFormat('zh-CN', {timeZone: tz}).format();
        } catch {
          addError(`定时配置的时区无效：${tz}`);
        }
      }
    }
  }

  return {errors, taskIssues};
}

function focusErrorTask(taskName) {
  const editor = workflowEditor;
  if (!editor || !taskName) return;
  editor.selectedTask = taskName;
  editor.selected = taskName;
  editor.formOpen.clear();
  editor.formOpen.add(taskName);
  const card = document.querySelector(`#taskFormPanel details.tf-task[data-task="${CSS.escape(taskName)}"]`);
  if (card) {
    document.querySelectorAll('#taskFormPanel details.tf-task').forEach(item => {
      item.open = item.dataset.task === taskName;
    });
    refreshSelectionHighlight();
    refreshInsertButtons();
    card.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }
  editor.panel.querySelectorAll('.dag-node').forEach(node => {
    node.classList.toggle('preview-selected', node.dataset.task === taskName);
  });
}

function workflowEditorSaveError(afterSync = false) {
  const editor = workflowEditor;
  if (!editor) return null;
  if (afterSync && (editor.busy || editor.dirty || editor.source !== editor.input.value)) {
    return t("表单仍在同步，请稍后重试");
  }
  if (!editor.formModel) return null;
  const {errors, taskIssues} = validateWorkflowFormModel(editor.formModel);
  editor.errorTasks = new Set(taskIssues.keys());
  refreshSelectionHighlight();
  if (!errors.length) return null;
  const firstTask = errors.find(item => item.taskName)?.taskName;
  if (firstTask) focusErrorTask(firstTask);
  if (errors.length === 1) return errors[0].message;
  const lines = errors.slice(0, 5).map((item, idx) => `${idx + 1}. ${item.message}`);
  if (errors.length > 5) lines.push(`…共 ${errors.length} 项问题，请修正后再保存`);
  return lines.join('\n');
}

function workflowEditorCanSave() {
  return !workflowEditorSaveError();
}

function showFormError(message) {
  const editor = workflowEditor;
  if (!editor || !editor.formError || !message) return;
  editor.formError.textContent = message;
  editor.formError.style.display = "block";
}

function clearFormError() {
  const editor = workflowEditor;
  if (editor && editor.formError) editor.formError.style.display = "none";
}
