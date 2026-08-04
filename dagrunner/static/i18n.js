const I18N_EN = {
  "工作流、定时计划和运行状态集中管理": "Manage workflows, schedules, and run status in one place",
  "正在连接": "Connecting",
  "退出": "Sign out",
  "工作流与定时": "Workflows & schedules",
  "查看任务 DAG，配置 cron，或立即发起运行": "Inspect DAGs, configure cron schedules, or start a run",
  "新增工作流": "New workflow",
  "导入工作流": "Import workflow",
  "刷新": "Refresh",
  "工作流": "Workflow",
  "任务": "Tasks",
  "定时状态": "Schedule status",
  "Cron / 时区": "Cron / timezone",
  "上次运行": "Last run",
  "下次运行": "Next run",
  "操作": "Actions",
  "正在加载…": "Loading…",
  "运行记录": "Run history",
  "按开始时间倒序；运行中的记录每 3 秒刷新": "Newest first; active runs refresh every 3 seconds",
  "立即刷新": "Refresh now",
  "开始时间 / Run ID": "Start time / Run ID",
  "触发方式": "Trigger",
  "状态": "Status",
  "任务进度": "Progress",
  "耗时": "Duration",
  "搜索名称或 ID": "Search name or ID",
  "搜索工作流": "Search workflows",
  "筛选触发方式": "Filter trigger",
  "筛选状态": "Filter status",
  "全部": "All",
  "手动": "Manual",
  "定时": "Schedule",
  "重跑": "Rerun",
  "失败续跑": "Resume failed",
  "运行中": "Running",
  "成功": "Success",
  "失败": "Failed",
  "复制日志": "Copy log",
  "取消": "Cancel",
  "确认删除": "Confirm delete",
  "登录控制台": "Sign in to the console",
  "输入管理员账号继续管理工作流。": "Enter your administrator credentials to manage workflows.",
  "账号": "Username",
  "密码": "Password",
  "登录": "Sign in",
  "已允许通过 HTTP 从局域网登录。登录状态默认保持 36 小时，请勿将服务暴露到公网。": "HTTP login from the local network is enabled. Sessions last 36 hours by default. Do not expose this service to the public internet.",
  "登录状态默认保持 36 小时。请通过 HTTPS 访问非本机部署。": "Sessions last 36 hours by default. Use HTTPS for non-local deployments.",
  "服务在线": "Service online",
  "服务断线": "Service offline",
  "后端服务已断开": "The backend service is unavailable",
  "登录已失效，请重新登录": "Your session has expired. Please sign in again",
  "秒": "sec",
  "分": "min",
  "个节点": "nodes",
  "已启用": "Enabled",
  "未启用": "Disabled",
  "运行": "Run",
  "编辑": "Edit",
  "导出 YAML": "Export YAML",
  "删除": "Delete",
  "还没有导入工作流": "No workflows have been imported",
  "共 {total} 条 · 第 {page} / {pages} 页": "{total} total · Page {page} of {pages}",
  "上一页": "Previous",
  "下一页": "Next",
  "未配置": "Not configured",
  "未启用时间": "Disabled",
  "{count} 个定时": "{count} schedules",
  "全部定时（统一时区：{timezone}）": "All schedules (timezone: {timezone})",
  "详情": "Details",
  "停止": "Stop",
  "已处理 {count}": "Handled {count}",
  "没有符合条件的运行记录": "No runs match the current filters",
  "已提交 {id}": "Submitted {id}",
  "识别来源": "Detected source",
  "转换提示": "Conversion notes",
  "拟分配 ID": "Proposed ID",
  "工作流 YAML": "Workflow YAML",
  "高亮字段": "Highlighted fields",
  "会在导入或运行时使用；中文名称取自 description。顶层 name 和 migration 仅作来源信息，导入后的定时由数据库单独管理。": " are used during import or execution. The display name comes from description. Top-level name and migration are source metadata; schedules are managed separately after import.",
  "导入": "Import",
  "新增": "Create",
  "导入并编辑工作流": "Import and edit workflow",
  "加载示例工作流失败：{error}": "Could not load the example workflow: {error}",
  "识别或转换文件失败：{error}": "Could not detect or convert the file: {error}",
  "已新增 {name}（{id}）": "Created {name} ({id})",
  "已导入 {name}（{id}）": "Imported {name} ({id})",
  "请先下线定时": "Disable the schedule first",
  "工作流 ID": "Workflow ID",
  "保存": "Save",
  "会被运行时使用；顶层 name、migration 和 YAML schedule 不参与后续执行。保存后立即写入数据库。": " are used at runtime. Top-level name, migration, and YAML schedule do not affect later runs. Saving writes the definition to the database immediately.",
  "工作流已更新": "Workflow updated",
  "确定停止这个运行实例？当前子进程会被终止。": "Stop this run? Its active child process will be terminated.",
  "已发送停止请求": "Stop request sent",
  "已提交新运行 {id}": "Submitted new run {id}",
  "删除工作流“{name}”？": "Delete workflow “{name}”?",
  "将同时删除工作流正文、定时配置、全部历史运行记录、任务记录和对应日志，此操作不可恢复。": "This permanently deletes the workflow definition, schedule, run history, task records, and logs.",
  "已删除工作流 {name}": "Deleted workflow {name}",
  "删除这条运行记录？": "Delete this run record?",
  "对应的任务日志也会一并删除。": "Its task logs will also be deleted.",
  "已删除运行记录 {id}": "Deleted run record {id}",
  "DAG 任务": "DAG tasks",
  "依赖": "Dependencies",
  "命令 / 类型": "Command / type",
  "启用": "Enabled",
  "条件分支": "Condition",
  "是": "Yes",
  "否": "No",
  "DAG 结构": "DAG structure",
  "启用节点": "Enabled node",
  "禁用节点": "Disabled node",
  "从左向右表示依赖方向": "Dependencies flow from left to right",
  "点击节点查看任务命令和依赖": "Select a node to inspect its command and dependencies",
  "类型：": "Type:",
  "成功：": "Success:",
  "失败：": "Failure:",
  "命令：": "Command:",
  "依赖：": "Depends:",
  "无": "None",
  "已禁用": "Disabled",
  "定时配置": "Schedule settings",
  "统一时区": "Shared timezone",
  "增加定时": "Add schedule",
  "Cron（5 字段）": "Cron (5 fields)",
  "Cron 使用 5 个字段。每小时第 10 分钟：10 * * * *　每天 10:00：0 10 * * *": "Cron uses five fields. Hourly at minute 10: 10 * * * *; daily at 10:00: 0 10 * * *",
  "启用全部自动调度": "Enable all schedules",
  "删除此定时": "Delete this schedule",
  "至少保留一个定时任务": "Keep at least one schedule",
  "时区不能为空": "Timezone is required",
  "请输入有效的 IANA 时区，例如 Asia/Shanghai": "Enter a valid IANA timezone, such as Asia/Shanghai",
  "Cron 不能为空": "Cron is required",
  "Cron 必须包含 5 个字段": "Cron must contain five fields",
  "Cron 含有不支持的字符": "Cron contains unsupported characters",
  "Cron 字段的范围或写法不正确": "A cron field has an invalid range or syntax",
  "请删除重复的 Cron": "Remove the duplicate cron expression",
  "定时配置已保存": "Schedule settings saved",
  "保存配置": "Save settings",
  "日志": "Log",
  "已由 {name} 处理": "Handled by {name}",
  "刷新状态": "Refresh status",
  "开始": "Start",
  "结束": "End",
  "错误": "Error",
  "浏览器不支持自动复制": "This browser does not support automatic copying",
  "日志已复制": "Log copied",
  "复制失败：{error}": "Copy failed: {error}",
  "当前页面无法安全计算密码摘要，请使用 HTTPS 或在本机访问": "This page cannot securely hash the password. Use HTTPS or access it locally.",
  "此 IP 已暂时禁止登录，请在 {time} 后重试": "This IP is temporarily blocked. Try again in {time}.",
  "可以重新尝试登录": "You can try signing in again",
  "正在验证…": "Verifying…",
  "，还可尝试 {count} 次": "; {count} attempts remaining",
  "登录失败": "Sign-in failed",
  "登录失败次数过多，请在 10 分钟后重试": "Too many failed sign-in attempts. Try again in 10 minutes.",
  "账号或密码错误": "Incorrect username or password"
};

function storedLanguage() {
  try { return localStorage.getItem("dagrunner.language"); }
  catch (_) { return null; }
}

let currentLanguage = storedLanguage() || document.body.dataset.defaultLanguage || "zh-CN";
if (!['zh-CN', 'en'].includes(currentLanguage)) currentLanguage = "zh-CN";

function t(source, values={}) {
  let result = currentLanguage === "en" ? (I18N_EN[source] || source) : source;
  for (const [key, value] of Object.entries(values)) result = result.replaceAll(`{${key}}`, value);
  return result;
}

function applyTranslations(root=document) {
  document.documentElement.lang = currentLanguage;
  document.title = document.getElementById("loginTitle") ? t("登录控制台") + " · DAG Runner" : "DAG Runner Console";
  root.querySelectorAll("[data-i18n]").forEach(element => {
    const key = element.dataset.i18nKey || element.textContent.trim();
    element.dataset.i18nKey = key;
    element.textContent = t(key);
  });
  root.querySelectorAll("[data-i18n-placeholder]").forEach(element => {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  });
  root.querySelectorAll("[data-i18n-aria-label]").forEach(element => {
    element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel));
  });
  const toggle = document.getElementById("languageToggle");
  if (toggle) toggle.textContent = currentLanguage === "zh-CN" ? "EN" : "中文";
}

function setLanguage(language) {
  if (!['zh-CN', 'en'].includes(language)) return;
  currentLanguage = language;
  try { localStorage.setItem("dagrunner.language", language); } catch (_) {}
  applyTranslations();
  window.dispatchEvent(new CustomEvent("dagrunner-language-change", {detail:{language}}));
}

function toggleLanguage() {
  setLanguage(currentLanguage === "zh-CN" ? "en" : "zh-CN");
}

document.getElementById("languageToggle")?.addEventListener("click", toggleLanguage);
applyTranslations();
