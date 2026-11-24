/* Version badge and manual update panel. Depends on app.js globals. */
let updateStatus = { current_version: "", state: "idle", satellites: {} };
let updateVersions = [];
let updateSource = "";
let updateBranch = null;

function knownSatellites(status) {
  const merged = {};
  for (const config of (typeof satelliteConfigs !== "undefined" ? satelliteConfigs : [])) {
    merged[config.alias] = {
      current_version: config.status?.agent_version || "",
      online: config.status?.status === "online",
      state: "",
      message: "",
      branch: config.status?.branch || null,
    };
  }
  for (const [alias, info] of Object.entries(status?.satellites || {})) {
    merged[alias] = { ...(merged[alias] || {}), ...info };
  }
  return merged;
}

function renderVersionBadge(status) {
  const badge = document.getElementById("versionBadge");
  if (!badge) return;
  const version = status?.current_version || "";
  badge.textContent = version ? `v${version}` : "v—";
  badge.classList.toggle("updating", Boolean(status?.active));
  const mismatch = Object.entries(knownSatellites(status)).filter(
    ([, info]) => info?.online && info?.current_version && version && info.current_version !== version
  );
  if (status?.active) badge.title = t("更新进行中：{state}", { state: status.state });
  else if (mismatch.length) badge.title = t("版本不一致：") + mismatch.map(([alias, info]) => `${alias} v${info.current_version}`).join(", ");
  else badge.title = "";
}

function updateModalVisible() {
  const modal = document.getElementById("modal");
  return Boolean(document.getElementById("updatePanel"))
    && modal !== null
    && !modal.classList.contains("hidden");
}

async function refreshUpdateStatus() {
  try {
    let data;
    if (state.auth?.is_admin) {
      data = await api("/api/update/status");
    } else {
      const overview = await api("/api/system/overview");
      data = { current_version: overview.version, state: "idle", satellites: {} };
    }
    updateStatus = data;
    renderVersionBadge(data);
    if (updateModalVisible()) renderUpdateModal();
  } catch (_) {
    /* the console is offline; the service indicator already shows it */
  }
}

function updateStateBadge(state) {
  const label = t(state || "idle");
  return `<span class="update-state ${esc(state || "")}">${esc(label)}</span>`;
}

function updateSatelliteRows(satellites) {
  const entries = Object.entries(satellites || {});
  if (!entries.length) return `<p class="form-hint">${t("暂无卫星节点")}</p>`;  return entries.map(([alias, info]) => {
    const version = info.current_version || "—";
    const online = info.online ? t("在线") : t("离线");
    const state = info.state || "";
    const message = info.message ? ` · ${esc(info.message)}` : "";
    return `<div class="update-satellite-row">
      <span class="satellite-row-alias">${esc(alias)}</span>
      <span class="satellite-status ${info.online ? "online" : "offline"}">${online}</span>
      <span class="mono">v${esc(version)}</span>
      ${info.branch ? `<span class="satellite-branch ${info.branch.available ? "online" : ""}" title="${esc(info.branch.reason || "")}">${info.branch.available ? t("支持") : t("不支持")}</span>` : ""}
      ${updateStateBadge(state)}
      <span class="update-satellite-message">${message}</span>
    </div>`;
  }).join("");
}

function renderUpdateModal() {
  const data = updateStatus || {};
  const current = data.current_version || "—";
  const target = data.target_version || "";
  const versionOptions = updateVersions
    .map((version) => `<option value="${esc(version)}"${version === target ? " selected" : ""}>${esc(version)}</option>`)
    .join("");
  const sourceSelect = `
    <label>${t("更新源")}<select id="updateSource" class="ui-select">
      <option value=""${updateSource === "" ? " selected" : ""}>${t("自动")}</option>
      <option value="release"${updateSource === "release" ? " selected" : ""}>GitHub Release</option>
      <option value="git"${updateSource === "git" ? " selected" : ""}>git</option>
      <option value="branch"${updateSource === "branch" ? " selected" : ""}>${t("git 分支")}</option>
    </select></label>`;
  openModal(t("版本与更新"), `<div class="update-panel" id="updatePanel">
    <div class="update-current">
      <span>${t("当前版本")}</span><b class="mono">v${esc(current)}</b>
      ${updateStateBadge(data.state)}
      ${data.skipped_schedules ? `<span class="form-hint">${t("更新期间跳过了 {count} 次定时触发", { count: data.skipped_schedules })}</span>` : ""}
    </div>
    <div class="update-form">
      ${sourceSelect}
      <label id="updateTargetLabel">${t("目标版本")}<select id="updateTarget" class="ui-select"><option value="">${t("最新版本")}</option>${versionOptions}</select></label>
      <div id="branchBox" class="form-hint" style="display:none"></div>
      <button class="button ghost" type="button" onclick="checkUpdate()">${t("检查更新")}</button>
    </div>
    <div class="update-satellites">${updateSatelliteRows(knownSatellites(data))}</div>
    ${data.message ? `<p class="form-hint">${esc(data.message)}</p>` : ""}
    <div class="update-actions">
      <button class="button" type="button" onclick="applyUpdate()">${t("更新")}</button>
      <button class="button ghost" type="button" onclick="abortUpdate()">${t("中止")}</button>
      <button class="button ghost" type="button" onclick="rollbackUpdate()">${t("回滚")}</button>
      <button class="button ghost" type="button" onclick="closeUpdateModal()">${t("关闭")}</button>
    </div>
  </div>`, false, "compact");
  syncBranchUi();
}

function syncBranchUi() {
  const isBranch = updateSource === "branch";
  const box = document.getElementById("branchBox");
  const label = document.getElementById("updateTargetLabel");
  const select = document.getElementById("updateTarget");
  if (label) label.style.display = isBranch ? "none" : "";
  if (select) select.disabled = isBranch;
  if (!box) return;
  if (!isBranch) {
    box.style.display = "none";
    return;
  }
  const info = updateBranch || {};
  if (!info.available) {
    box.textContent = t("分支更新不可用：{reason}", { reason: info.reason || "unknown" });
  } else if (info.dirty) {
    box.textContent = t("工作区有未提交改动，无法更新");
  } else if (info.behind > 0) {
    box.textContent = t("落后 {count} 个提交", { count: info.behind }) +
      (info.latest_subject ? ` · ${info.latest_subject}` : "") +
      (info.remote_sha ? ` · ${info.remote_sha.slice(0, 8)}` : "");
  } else {
    box.textContent = t("本地已是最新");
  }
  box.style.display = "";
}

function openUpdateSettings() {
  renderUpdateModal();
  refreshUpdateStatus();
}

function closeUpdateModal() {
  closeModal();
}

async function checkUpdate() {
  const source = document.getElementById("updateSource")?.value || "";
  const target = document.getElementById("updateTarget")?.value || "";
  try {
    const data = await api("/api/update/check", {
      method: "POST",
      body: JSON.stringify({ source, target }),
    });
    if (data.source === "branch") {
      updateBranch = data.branch || {};
      updateVersions = [];
      updateSource = "branch";
      updateStatus = { ...updateStatus, target_version: data.target || "" };
      renderUpdateModal();
      toast(updateBranch.behind > 0
        ? t("落后 {count} 个提交", { count: updateBranch.behind })
        : t("本地已是最新"));
      return;
    }
    updateBranch = null;
    updateVersions = data.versions || [];
    updateSource = data.source || "";
    updateStatus = { ...updateStatus, target_version: data.target || data.latest || "" };
    renderUpdateModal();
    toast(t("发现 {count} 个可用版本", { count: updateVersions.length }));
  } catch (e) {
    toast(e.message, true);
  }
}

async function applyUpdate() {
  const source = document.getElementById("updateSource")?.value || "";
  let target = document.getElementById("updateTarget")?.value || "";
  let title;
  let body;
  if (source === "branch") {
    target = updateStatus?.target_version || updateBranch?.remote_sha || "";
    if (!target) {
      toast(t("请先检查更新"), true);
      return;
    }
    const ref = updateBranch?.name || "main";
    title = t("同步 {ref} 分支？", { ref });
    body = t("将执行 git pull --ff-only 并重启主程序；工作区必须干净，本地不能有未推送提交。");
  } else {
    if (!target) {
      toast(t("请先检查更新并选择目标版本"), true);
      return;
    }
    title = t("更新到 v{version}？", { version: target });
    body = t("更新期间定时任务会跳过，已运行的任务必须已结束；主程序与在线卫星会同时重启。");
  }
  if (!await confirmDelete(title, body)) return;
  try {
    const data = await api("/api/update", {
      method: "POST",
      body: JSON.stringify({ target, source }),
    });
    if (data.ok) {
      toast(source === "branch"
        ? t("正在同步分支并重启，请稍候…")
        : t("正在重启，请稍候…"));
      setTimeout(() => window.location.reload(), 4000);
    } else {
      toast(data.error || t("更新已中止"), true);
      await refreshUpdateStatus();
    }
  } catch (e) {
    toast(e.message, true);
  }
}

async function abortUpdate() {
  try {
    await api("/api/update/abort", { method: "POST", body: "{}" });
    toast(t("更新已中止"));
    await refreshUpdateStatus();
  } catch (e) {
    toast(e.message, true);
  }
}

async function rollbackUpdate() {
  const version = updateStatus?.previous_version || "";
  if (!version) {
    toast(t("没有可回滚的版本"), true);
    return;
  }
  if (!await confirmDelete(t("回滚到 v{version}？", { version }), t("服务将重启。"))) return;
  try {
    await api("/api/update/rollback", { method: "POST", body: JSON.stringify({ version }) });
    toast(t("正在回滚，请稍候…"));
    setTimeout(() => window.location.reload(), 4000);
  } catch (e) {
    toast(e.message, true);
  }
}

window.addEventListener("dagrunner-language-change", () => {
  renderVersionBadge(updateStatus);
  if (updateModalVisible()) renderUpdateModal();
});

startPoller("update", refreshUpdateStatus, 10000, 30000);
