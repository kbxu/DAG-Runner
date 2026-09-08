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
  const panel = document.createElement('section');
  panel.className = 'workflow-preview';
  panel.innerHTML = `<div class="preview-heading"><strong>实时工作流预览</strong></div><div id="previewGraph" class="dag-scroll"><span class="secondary">有效的工作流将在这里显示</span></div>`;
  const controls = document.createElement('section');
  controls.className = 'workflow-node-controls';
  controls.innerHTML = `<div class="preview-heading"><strong>节点操作</strong><button type="button" class="button ghost" id="previewUndo" disabled onclick="undoWorkflowGeneration()">撤销最近一次生成</button></div><p id="previewStatus" role="status" aria-live="polite">正在校验…</p><div id="previewSelection" class="dag-detail">点击预览节点，添加后续普通模块或条件分支。</div><p class="form-hint">预览不会保存或执行任务。添加节点只追加连接；条件分支自动附带两个后续任务。生成代码会重新排版 YAML（不保留注释），请检查占位命令和判断条件后再保存。</p>`;
  input.closest('label').insertAdjacentElement('afterend', controls);
  const save = document.querySelector('#modalBody button[onclick="submitImportWorkflow()"], #modalBody button[onclick^="saveWorkflow("]');
  const form = input.closest('.form-stack');
  const layout = mountWorkflowFrame(form, panel, toggleWorkflowView);
  const footer = document.createElement('div');
  footer.className = 'workflow-editor-footer';
  if (save) footer.append(save);
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'button ghost';
  cancel.textContent = t('取消');
  cancel.onclick = closeModal;
  footer.append(cancel);
  layout.after(footer);
  workflowEditor = {input, panel, controls, save, layout, form, direction: 'TB', valid: false, source: null, tasks: [], selected: null,
    name: input.id === 'workflowDefinition' ? document.querySelector('#modalBody input[readonly]').value : pendingWorkflowId,
    timer: null, controller: null, revision: 0, undo: null, busy: false};
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
}

function updateEditorControls() {
  const editor = workflowEditor;
  if (!editor) return;
  const ready = editor.valid && editor.source === editor.input.value && !editor.busy;
  if (editor.save) editor.save.disabled = !ready;
  editor.controls.querySelectorAll('[data-add-node]').forEach(button => { button.disabled = !ready; });
  document.getElementById('previewUndo').disabled = editor.busy || !editor.undo || editor.input.value !== editor.undo.after;
}

function queueWorkflowPreview(id, delay=450) {
  const editor = workflowEditor;
  if (!editor || editor.input.id !== id) return;
  clearTimeout(editor.timer);
  editor.controller?.abort();
  editor.revision++;
  editor.valid = false;
  updateEditorControls();
  document.getElementById('previewStatus').textContent = '正在校验当前 YAML…（保留上次有效预览）';
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
    }
    editor.tasks = data.tasks;
    editor.source = editor.input.value;
    editor.valid = true;
    document.getElementById('previewGraph').innerHTML = svg;
    const status = document.getElementById('previewStatus');
    status.classList.remove('preview-error');
    status.textContent = `校验通过 · ${data.tasks.length} 个节点 · 尚未保存`;
    if (editor.selected && data.tasks.some(task => task.name === editor.selected)) selectPreviewTask(editor.selected);
    else {
      editor.selected = null;
      document.getElementById('previewSelection').textContent = '点击节点，添加后续普通模块或条件分支。';
    }
  } catch (error) {
    if (error.name === 'AbortError' || workflowEditor !== editor || revision !== editor.revision || source !== editor.input.value) return;
    editor.valid = false;
    const status = document.getElementById('previewStatus');
    status.classList.add('preview-error');
    status.textContent = `无法更新预览：${error.message}。已保留上次有效预览，请修正 YAML 后继续。`;
  } finally {
    if (workflowEditor === editor) updateEditorControls();
  }
}

function selectPreviewTask(name) {
  const editor = workflowEditor, task = editor?.tasks.find(item => item.name === name);
  if (!task) return;
  editor.selected = name;
  const selection = document.getElementById('previewSelection');
  selection.innerHTML = `<strong>${esc(task.description || name)}</strong><div class="secondary mono">${esc(name)}</div><div class="preview-add-actions">${task.type === 'condition' ? '<label>连接分支<select id="previewBranch"><option value="success">条件成立（success）</option><option value="failure">条件不成立（failure）</option></select></label>' : ''}<button type="button" class="button ghost" data-add-node onclick="addPreviewTask('command')">＋ 普通模块</button><button type="button" class="button ghost" data-add-node onclick="addPreviewTask('condition')">＋ 条件分支</button></div><span class="form-hint">${task.type === 'condition' ? '新节点仅在所选分支被选中时运行。' : '新增模块依赖当前节点，已有后续模块继续保留。'} 条件分支默认判断当前节点状态为 SUCCESS，可在 YAML 中修改。</span>`;
  editor.panel.querySelectorAll('.dag-node').forEach(node => node.classList.toggle('preview-selected', node.dataset.task === name));
  updateEditorControls();
}

async function addPreviewTask(kind) {
  const editor = workflowEditor;
  if (!editor || !editor.valid || editor.busy || editor.source !== editor.input.value || !editor.selected) return;
  clearTimeout(editor.timer);
  editor.busy = true;
  editor.input.readOnly = true;
  document.getElementById('previewStatus').textContent = '正在生成节点并校验连接…';
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
}

function workflowEditorCanSave() {
  const editor = workflowEditor;
  return !editor || (editor.valid && !editor.busy && editor.source === editor.input.value);
}
