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
    workflowEditor.controller?.abort();
    workflowEditor = null;
  }
  document.getElementById('workflowViewToggle')?.remove();
  document.querySelector('#modal .modal-card')?.classList.remove('workflow-editor-card');
}

function initWorkflowEditor() {
  const input = document.querySelector('#modalBody #importDefinition, #modalBody #workflowDefinition');
  if (!input) return;
  const isReadOnlyForm = Boolean(document.querySelector('#modalBody #workflowDisplayName')?.disabled);
  const panel = document.createElement('section');
  panel.className = 'workflow-preview';
  panel.innerHTML = `<div class="preview-heading"><strong>实时工作流预览</strong></div><div id="previewGraph" class="dag-scroll"><span class="secondary">有效的工作流将在这里显示</span></div>`;
  panel.addEventListener('mousedown', event => event.preventDefault());
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
  layout.after(footer);
  workflowEditor = {input, panel, controls: null, save, layout, form, direction: 'TB', valid: false, source: null, tasks: [], selected: null,
    name: input.id === 'workflowDefinition' ? document.querySelector('#modalBody input[readonly]').value : pendingWorkflowId,
    timer: null, controller: null, revision: 0, undo: null, busy: false,
    mode: 'form', isEdit: input.id === 'workflowDefinition', formModel: null, formSyncTimer: null, formOpen: new Set(), scheduleSyncTimer: null, runnerFieldHtml: '', openSections: new Set(), isReadOnly: false, collapseGuardUntil: 0};
  workflowEditor.isReadOnly = isReadOnlyForm;
  const runnerSelect = document.querySelector('#modalBody #workflowRunner, #modalBody #importRunner');
  if (runnerSelect) {
    const label = runnerSelect.closest('label');
    workflowEditor.runnerFieldHtml = label ? label.outerHTML : '';
    if (label) label.remove();
  }
  const yamlLabel = input.closest('label');
  const formPanel = document.createElement('div');
  formPanel.id = 'taskFormPanel';
  formPanel.className = 'task-form-panel';
  yamlLabel.before(formPanel);
  formPanel.addEventListener('input', onTaskFormInput);
  formPanel.addEventListener('change', onTaskFormChange);
  formPanel.addEventListener('click', onTaskFormClick);
  formPanel.addEventListener('toggle', onTaskFormToggle, true);
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
  // Reuse the last valid graph even when the current YAML is invalid.
  if (editor.tasks.length) {
    document.getElementById('previewGraph').innerHTML = buildDagSvg(editor.tasks, '工作流预览', false, true, editor.direction);
    editor.panel.querySelectorAll('.dag-node').forEach(node => node.classList.toggle('preview-selected', node.dataset.task === editor.selected));
  }
  if (typeof fitMaximizedCardWidth === "function") fitMaximizedCardWidth();
}

function setPreviewStatus(text, error=false) {
  const status = document.getElementById('previewStatus');
  if (!status) {
    if (error) toast(text, true);
    return;
  }
  status.textContent = text;
  status.classList.toggle('preview-error', Boolean(error));
}

function updateEditorControls() {
  const editor = workflowEditor;
  if (!editor) return;
  const ready = editor.valid && editor.source === editor.input.value && !editor.busy;
  if (editor.save) editor.save.disabled = !ready;
  editor.controls?.querySelectorAll('[data-add-node]').forEach(button => { button.disabled = !ready; });
  const undo = document.getElementById('previewUndo');
  if (undo) undo.disabled = editor.busy || !editor.undo || editor.input.value !== editor.undo.after;
}

function queueWorkflowPreview(id, delay=450) {
  const editor = workflowEditor;
  if (!editor || editor.input.id !== id) return;
  clearTimeout(editor.timer);
  editor.controller?.abort();
  editor.revision++;
  editor.valid = false;
  updateEditorControls();
  setPreviewStatus('正在校验当前 YAML…（保留上次有效预览）');
  editor.timer = setTimeout(() => refreshWorkflowPreview(editor), delay);
}

async function refreshWorkflowPreview(editor, addition=null) {
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
    // Build before touching the previous diagram: a rendering failure must preserve it too.
    const svg = buildDagSvg(data.tasks, '工作流预览', false, true, editor.direction);
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
    document.getElementById('previewGraph').innerHTML = svg;
    if (typeof fitMaximizedCardWidth === "function") fitMaximizedCardWidth();
    setPreviewStatus(`校验通过 · ${data.tasks.length} 个节点 · 尚未保存`);
    if (editor.selected && data.tasks.some(task => task.name === editor.selected)) selectPreviewTask(editor.selected, Boolean(addition));
    else editor.selected = null;
  } catch (error) {
    if (error.name === 'AbortError' || workflowEditor !== editor || revision !== editor.revision || source !== editor.input.value) return;
    editor.valid = false;
    setPreviewStatus(`无法更新预览：${error.message}。已保留上次有效预览，请修正后继续。`, true);
  } finally {
    if (workflowEditor === editor) updateEditorControls();
  }
}

function selectPreviewTask(name, scrollToTask=true) {
  const editor = workflowEditor, task = editor?.tasks.find(item => item.name === name);
  if (!task) return;
  editor.selected = name;
  editor.panel.querySelectorAll('.dag-node').forEach(node => node.classList.toggle('preview-selected', node.dataset.task === name));
  const card = document.querySelector(`#taskFormPanel details.tf-task[data-task="${CSS.escape(name)}"]`);
  if (card) {
    document.querySelectorAll('#taskFormPanel .tf-focus').forEach(item => item.classList.remove('tf-focus'));
    // 手风琴：选中一个任务时，其他任务卡片收起。
    document.querySelectorAll('#taskFormPanel details.tf-task').forEach(item => {
      if (item.dataset.task !== name) {
        item.open = false;
        editor.formOpen.delete(item.dataset.task);
      }
    });
    card.classList.add('tf-focus');
    card.open = true;
    editor.formOpen.add(name);
    card.scrollIntoView({block: 'nearest'});
  }
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
  setPreviewStatus('正在生成节点并校验连接…');
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
    renderTaskForm();
  } catch (error) {
    panel.innerHTML = `<span class="tf-note">表单加载失败：${esc(error.message)}</span>`;
  }
}

function tfScheduleRows(schedule) {
  if (!schedule.crons.length) {
    return '<div class="schedule-empty"><button type="button" class="button ghost small" data-action="add-cron">＋ 添加定时</button></div>';
  }
  return schedule.crons.map((cron, index) => `
    <div class="schedule-row">
      <span class="schedule-label">定时${index + 1}</span>
      <input type="text" data-field="schedule-cron" data-index="${index}" value="${esc(cron)}" placeholder="分 时 日 月 周">
      <span class="schedule-actions">
        <button type="button" class="icon-button" data-action="add-cron" data-index="${index}" title="在下方新增">＋</button>
        <button type="button" class="icon-button" data-action="remove-cron" data-index="${index}" title="删除该定时"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></button>
      </span>
    </div>`).join('');
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
  renderTaskForm();
  refreshScheduleEnabledState();
  editor.collapseGuardUntil = Date.now() + 350;
  scheduleFormSync();
  scheduleServerSync();
}

function removeFormCron(index) {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  editor.formModel.schedule.crons.splice(Number(index), 1);
  renderTaskForm();
  refreshScheduleEnabledState();
  editor.collapseGuardUntil = Date.now() + 350;
  scheduleFormSync();
  scheduleServerSync();
}

function scheduleServerSync() {
  const editor = workflowEditor;
  if (!editor || !editor.isEdit || editor.isReadOnly) return;
  clearTimeout(editor.scheduleSyncTimer);
  editor.scheduleSyncTimer = setTimeout(pushScheduleToServer, 700);
}

async function flushScheduleServerSync() {
  const editor = workflowEditor;
  if (editor && editor.isEdit && editor.scheduleSyncTimer) await pushScheduleToServer();
}

async function pushScheduleToServer() {
  const editor = workflowEditor;
  if (!editor || !editor.isEdit || editor.isReadOnly || !editor.formModel) return;
  clearTimeout(editor.scheduleSyncTimer);
  editor.scheduleSyncTimer = null;
  const schedule = editor.formModel.schedule;
  const crons = schedule.crons.map(item => item.trim()).filter(Boolean);
  if (!crons.length) return;
  try {
    await api(`/api/workflows/${encodeURIComponent(editor.name)}/schedule`, {
      method: 'PUT',
      body: JSON.stringify({crons, timezone: schedule.timezone || 'Asia/Shanghai', enabled: schedule.enabled}),
    });
  } catch (error) {
    setPreviewStatus(`定时保存失败：${error.message}`, true);
  }
}

function autoGrowSetup(textarea) {
  if (!textarea) return;
  const lineHeight = parseFloat(getComputedStyle(textarea).lineHeight) || 18;
  textarea.style.height = 'auto';
  const max = lineHeight * 8 + 4;
  const next = Math.min(textarea.scrollHeight + 2, max);
  textarea.style.height = `${Math.max(next, lineHeight + 8)}px`;
  textarea.style.overflowY = textarea.scrollHeight > max ? 'auto' : 'hidden';
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
        <span class="schedule-label">定时${index + 1}</span>
        <input type="text" data-field="schedule-cron" data-index="${index}" value="${esc(cron)}" placeholder="分 时 日 月 周"${disabled}>
        ${readOnly ? '' : `<span class="schedule-actions">
          <button type="button" class="icon-button" data-action="add-cron" data-index="${index}" title="在下方新增">＋</button>
          <button type="button" class="icon-button" data-action="remove-cron" data-index="${index}" title="删除该定时"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg></button>
        </span>`}
      </div>`).join('')
    : (readOnly ? '<span class="tf-note">未配置定时</span>'
       : '<div class="schedule-empty"><button type="button" class="button ghost small" data-action="add-cron">＋ 添加定时</button></div>');
  return `<details class="tf-panel tf-collapse" data-section="schedule"${open ? ' open' : ''}>
    <summary class="tf-panel-head">
      <span class="tf-section-head">定时配置<span class="tf-subhint">仅 Maintainer（edit）可修改</span></span>
      <span class="tf-head-caret">▶</span>
    </summary>
    <div class="tf-collapse-body">
      <div class="schedule-row schedule-enabled">
        <span class="schedule-label">定时任务</span>
        <label class="schedule-check"><input type="checkbox" data-field="schedule-enabled"${schedule.enabled ? ' checked' : ''}${hasCron && !readOnly ? '' : ' disabled'}> 启用<span class="schedule-note">${t("需至少一条有效定时")}</span></label>
      </div>
      <div class="schedule-row schedule-tz">
        <span class="schedule-label">时区</span>
        <input type="text" data-field="schedule-tz" value="${esc(schedule.timezone)}"${disabled}>
      </div>
      <div id="tfScheduleRows">${rows}</div>
      <p class="form-hint">Cron 使用 5 个字段。每小时第 10 分钟：10 * * * *；每天 10:00：0 10 * * *。</p>
    </div>
  </details>`;
}

function setupPanelHtml(model, options = {}) {
  const readOnly = Boolean(options.readOnly);
  const disabled = readOnly ? ' disabled' : '';
  const open = options.openSections ? options.openSections.has('setup') : false;
  const runnerBlock = options.runnerFieldHtml || `<label>${t("运行位置")}<select class="ui-select"${disabled}><option>${esc(model.runner || 'local')}</option></select></label>`;
  return `<details class="tf-panel tf-collapse tf-section-gap" data-section="setup"${open ? ' open' : ''}>
    <summary class="tf-panel-head">
      <span class="tf-section-head">Setup 脚本<span class="tf-subhint">运行单个任务前，会先运行此脚本</span></span>
      <span class="tf-head-caret">▶</span>
    </summary>
    <div class="tf-collapse-body">
      <label class="f">脚本内容<textarea id="tfSetup" class="tf-setup" rows="1" data-field="setup"${disabled}>${esc(model.setup)}</textarea></label>
      ${runnerBlock}
    </div>
  </details>`;
}

function taskListToolbarHtml(extraButtons = '') {
  return `<div class="tf-panel-head"><span class="tf-section-head">任务列表</span><div class="tf-toolbar">
    <button type="button" class="button ghost small" data-action="expand">全部展开</button>
    <button type="button" class="button ghost small" data-action="collapse">全部折叠</button>
    ${extraButtons}
  </div></div>`;
}

function taskCardHtml(model, task, index, options = {}) {
  const names = Object.fromEntries(model.tasks.map(item => [item.name, item.description]));
  const readOnly = Boolean(options.readOnly);
  const disabled = readOnly ? ' disabled' : '';
  const open = options.openTasks ? options.openTasks.has(task.name) : false;
  const preceding = model.tasks.slice(0, index);
  const depsText = task.depends.length ? task.depends.map(id => names[id] || id).join('、') : '无依赖';
  const depBoxes = preceding.map(item => `
      <label><input type="checkbox" data-field="depends" data-task="${esc(task.name)}" value="${esc(item.name)}"${task.depends.includes(item.name) ? ' checked' : ''}${disabled}>
        <span>${esc(item.description)}</span> <span class="mono">${esc(item.name)}</span></label>`).join('');
  const branchBoxes = field => model.tasks.filter(item => item.name !== task.name).map(item => `
      <label><input type="checkbox" data-field="${field}" data-task="${esc(task.name)}" value="${esc(item.name)}"${task[field].includes(item.name) ? ' checked' : ''}${disabled}>
        <span>${esc(item.description)}</span> <span class="mono">${esc(item.name)}</span></label>`).join('');
  const body = task.type === 'condition'
    ? `<div class="tf-block">
        <div class="subhead">判断依据（来自「依赖」列表）</div>
        <div class="tf-chips">${task.depends.length ? task.depends.map(id => `<span class="tf-chip">${esc(names[id] || id)}</span>`).join('') : '<span class="tf-note">未设置依赖，条件不会触发</span>'}</div>
        <div class="tf-grid2" style="margin-top:8px">
          <label class="f">依赖任务关系
            <select data-field="relation" data-task="${esc(task.name)}"${disabled}>
              <option value="AND"${task.conditionRelation === 'AND' ? ' selected' : ''}>AND（全部成功才成立）</option>
              <option value="OR"${task.conditionRelation === 'OR' ? ' selected' : ''}>OR（任一成功即成立）</option>
            </select>
          </label>
        </div>
        <p class="form-hint">条件依据其「依赖」列表中的任务判断。</p>
      </div>`
    : `<label class="f">命令<textarea class="tf-command" data-field="command" data-task="${esc(task.name)}"${disabled}>${esc(task.command)}</textarea></label>`;
  const branches = task.type === 'condition'
    ? `<div class="tf-block"><div class="subhead">成立分支（success）<span class="note">${task.success.length} 个</span></div><div class="tf-checklist">${branchBoxes('success')}</div></div>
       <div class="tf-block"><div class="subhead">不成立分支（failure）<span class="note">${task.failure.length} 个</span></div><div class="tf-checklist">${branchBoxes('failure')}</div></div>`
    : '';
  return `<details class="tf-task" data-task="${esc(task.name)}"${open ? ' open' : ''}>
    <summary>
      <span class="tf-tags">${task.enabled ? '' : '<span class="tf-tag disabled">未启用</span>'}<span class="tf-tag ${task.type}">${task.type === 'condition' ? '条件' : '任务'}</span></span>
      <span class="tf-caret">▶</span>
      <span class="tf-heading"><span class="tf-title">${esc(task.description)}</span><span class="tf-id mono">${esc(task.name)}</span></span>
      <span class="tf-deps"><span class="tf-deps-text" title="依赖：${esc(depsText)}">依赖：${esc(depsText)}</span></span>
    </summary>
    <div class="tf-body">
      <div class="tf-grid2">
        <label class="f">任务标题<input type="text" data-field="title" data-task="${esc(task.name)}" value="${esc(task.description)}"${disabled}></label>
        <label class="switch"><input type="checkbox" data-field="enabled" data-task="${esc(task.name)}"${task.enabled ? ' checked' : ''}${disabled}> 启用该任务</label>
      </div>
      <div class="tf-block">
        <div class="subhead">依赖（仅显示前面的任务）<span class="note">${task.depends.length} 个</span></div>
        <div class="tf-checklist tf-depends">${depBoxes || '<span class="tf-note">没有可选任务</span>'}</div>
      </div>
      ${body}
      ${branches}
      ${readOnly ? '' : `<div class="tf-actions"><button type="button" class="button danger small" data-action="delete" data-task="${esc(task.name)}">删除任务</button></div>`}
    </div>
  </details>`;
}

function renderTaskForm() {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  const model = editor.formModel;
  const options = {readOnly: editor.isReadOnly, openSections: editor.openSections, openTasks: editor.formOpen};
  const addButtons = editor.isReadOnly ? '' : `<button type="button" class="button small" data-action="add-command">＋ 新增普通任务</button>`
    + `<button type="button" class="button small" data-action="add-condition">＋ 新增条件任务</button>`;
  const cards = model.tasks.map((task, index) =>
    taskCardHtml(model, task, index, options)
  ).join('');
  document.getElementById('taskFormPanel').innerHTML =
    schedulePanelHtml(model, options)
    + setupPanelHtml(model, {...options, runnerFieldHtml: editor.runnerFieldHtml})
    + `<div class="tf-panel tf-section-gap">${taskListToolbarHtml(addButtons)}<div class="tf-tasks">${cards || '<span class="tf-note">暂无任务</span>'}</div></div>`;
  autoGrowSetup(document.getElementById('tfSetup'));
}

function scheduleFormSync() {
  const editor = workflowEditor;
  if (!editor || editor.mode !== 'form' || editor.isReadOnly) return;
  clearTimeout(editor.formSyncTimer);
  editor.formSyncTimer = setTimeout(syncFormToServer, 450);
}

async function flushWorkflowModelSync() {
  const editor = workflowEditor;
  if (editor && editor.mode === 'form' && editor.formSyncTimer) await syncFormToServer();
}

async function syncFormToServer() {
  const editor = workflowEditor;
  if (!editor || editor.mode !== 'form' || editor.isReadOnly || !editor.formModel) return;
  clearTimeout(editor.formSyncTimer);
  editor.formSyncTimer = null;
  editor.busy = true;
  updateEditorControls();
  setPreviewStatus('正在校验表单…');
  try {
    const payload = {
      setup: editor.formModel.setup,
      tasks: editor.formModel.tasks,
      schedule: editor.formModel.schedule,
    };
    const data = editor.isEdit
      ? await api(`/api/workflows/${encodeURIComponent(editor.name)}/model-preview`, {
          method: 'POST',
          body: JSON.stringify(payload),
        })
      : await api('/api/workflows/import-model-preview', {
          method: 'POST',
          body: JSON.stringify({definition: editor.input.value, model: payload}),
        });
    if (workflowEditor !== editor) return;
    editor.input.value = data.definition;
    syncYamlHighlight(editor.input.id);
    editor.source = editor.input.value;
    editor.tasks = data.tasks;
    editor.valid = true;
    document.getElementById('previewGraph').innerHTML = buildDagSvg(data.tasks, '工作流预览', false, true, editor.direction);
    if (typeof fitMaximizedCardWidth === 'function') fitMaximizedCardWidth();
    setPreviewStatus(`校验通过 · ${data.tasks.length} 个节点 · 尚未保存`);
  } catch (error) {
    if (workflowEditor !== editor) return;
    editor.valid = false;
    setPreviewStatus(`表单校验失败：${error.message}`, true);
  } finally {
    editor.busy = false;
    if (workflowEditor === editor) updateEditorControls();
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
  } else if (field === 'schedule-tz') {
    editor.formModel.schedule.timezone = event.target.value;
  } else if (field === 'schedule-cron') {
    editor.formModel.schedule.crons[Number(event.target.dataset.index)] = event.target.value;
    refreshScheduleEnabledState();
    scheduleServerSync();
  } else if (task) {
    if (field === 'title') {
      task.description = event.target.value;
      const title = document.querySelector(`details.tf-task[data-task="${CSS.escape(task.name)}"] .tf-title`);
      if (title) title.textContent = task.description;
    } else if (field === 'command') {
      task.command = event.target.value;
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
    scheduleServerSync();
    return;
  }
  const task = editor.formModel.tasks.find(item => item.name === event.target.dataset.task);
  if (!field || !task) return;
  if (field === 'enabled') {
    task.enabled = event.target.checked;
    renderTaskForm();
  } else if (field === 'relation') {
    task.conditionRelation = event.target.value;
  } else if (field === 'schedule-enabled') {
    editor.formModel.schedule.enabled = event.target.checked;
    scheduleServerSync();
  } else if (['depends', 'success', 'failure'].includes(field)) {
    const root = event.target.closest('details.tf-task');
    task[field] = [...root.querySelectorAll(`[data-field="${field}"]:checked`)].map(box => box.value);
    if (field === 'depends') renderTaskForm();
  }
  scheduleFormSync();
}

function onTaskFormClick(event) {
  const actionButton = event.target.closest('[data-action]');
  if (!actionButton) return;
  const action = actionButton.dataset.action;
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
  if (target.open) editor.formOpen.add(target.dataset.task);
  else editor.formOpen.delete(target.dataset.task);
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
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    for (;;) {
      let id = '';
      for (let index = 0; index < 8; index += 1) {
        id += alphabet[Math.floor(Math.random() * alphabet.length)];
      }
      if (!editor.formModel.tasks.some(task => task.name === id)) return id;
    }
  };
  const id = makeId();
  const previousTask = editor.formModel.tasks[editor.formModel.tasks.length - 1];
  const task = {
    name: id,
    description: type === 'condition' ? '新条件任务' : '新普通任务',
    type,
    enabled: true,
    depends: type === 'condition' && previousTask ? [previousTask.name] : [],
    command: type === 'command' ? 'echo TODO' : '',
    conditionRelation: 'AND',
    success: [],
    failure: [],
  };
  editor.formModel.tasks.push(task);
  if (type === 'condition') {
    // 与原有生成逻辑一致：条件任务自动带「成立后 / 不成立后」两个子任务。
    const children = [
      {description: '条件成立后执行', branch: 'success'},
      {description: '条件不成立后执行', branch: 'failure'},
    ];
    for (const child of children) {
      const childId = makeId();
      editor.formModel.tasks.push({
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
      task[child.branch].push(childId);
    }
  }
  editor.formOpen.add(id);
  renderTaskForm();
  scheduleFormSync();
}

function deleteFormTask(name) {
  const editor = workflowEditor;
  if (!editor || !editor.formModel) return;
  editor.formModel.tasks = editor.formModel.tasks.filter(task => task.name !== name);
  editor.formModel.tasks.forEach(task => {
    task.depends = task.depends.filter(item => item !== name);
    task.success = task.success.filter(item => item !== name);
    task.failure = task.failure.filter(item => item !== name);
  });
  editor.formOpen.delete(name);
  renderTaskForm();
  scheduleFormSync();
}

function workflowEditorCanSave() {
  const editor = workflowEditor;
  return !editor || (editor.valid && !editor.busy && editor.source === editor.input.value);
}
