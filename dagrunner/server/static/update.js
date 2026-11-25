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

function branchRefText() {
  const configured = updateStatus?.branch?.configured || {};
  return `${configured.remote || "origin"}/${configured.name || "main"}`;
}

function updateBranchHint() {
  const info = updateBranch;
  if (!info || !Object.keys(info).length) {
    return updateStatus?.branch?.available
      ? t("正在检查远端分支…")
      : t("分支更新不可用：{reason}", { reason: t("不是 git 仓库") });
  }
  if (!info.available) return t("分支更新不可用：{reason}", { reason: info.reason || "unknown" });
  if (info.dirty) return t("工作区有未提交改动，无法更新");
  if (Number(info.behind) > 0) {
    return t("落后 {count} 个提交", { count: info.behind })
      + (info.latest_subject ? ` · ${info.latest_subject}` : "")
      + (info.remote_sha ? ` · ${info.remote_sha.slice(0, 8)}` : "");
  }
  return t("本地已是最新");
}

function updateReady() {
  const info = updateBranch || {};
  return Boolean(info.available && !info.dirty && Number(info.behind) > 0);
}

function renderUpdateModal() {
  const data = updateStatus || {};
  const current = data.current_version || "—";
  // 其它更新入口（更新源 / 目标版本 / 发布包安装 / 回滚）已暂时从界面隐藏；
  // 后端接口与旧逻辑仍保留，恢复时把对应 UI 加回来即可。
  openModal(t("版本与更新"), `<div class="update-panel" id="updatePanel">
    <div class="update-current">
      <span>${t("当前版本")}</span><b class="mono">v${esc(current)}</b>
      ${updateStateBadge(data.state)}
      ${data.skipped_schedules ? `<span class="form-hint">${t("更新期间跳过了 {count} 次定时触发", { count: data.skipped_schedules })}</span>` : ""}
    </div>
    <div class="update-branch">
      <span class="update-branch-label">${t("更新方式")}</span>
      <b class="mono">git pull · ${esc(branchRefText())}</b>
    </div>
    <div id="branchBox" class="form-hint">${updateBranchHint()}</div>
    <div class="update-satellites">${updateSatelliteRows(knownSatellites(data))}</div>
    ${data.message ? `<p class="form-hint">${esc(data.message)}</p>` : ""}
    <div class="update-actions">
      <button class="button" type="button" onclick="applyUpdate()"${updateReady() ? "" : " disabled"}>${t("更新")}</button>
      ${data.active ? `<button class="button ghost" type="button" onclick="abortUpdate()">${t("中止")}</button>` : ""}
      <button class="button ghost" type="button" onclick="closeUpdateModal()">${t("关闭")}</button>
    </div>
  </div>`, false, "compact");
}

function openUpdateSettings() {
  renderUpdateModal();
  refreshUpdateStatus();
  checkBranchUpdate();
}

function closeUpdateModal() {
  closeModal();
}

async function checkBranchUpdate() {
  try {
    const data = await api("/api/update/check", {
      method: "POST",
      body: JSON.stringify({ source: "branch", target: "" }),
    });
    updateBranch = data.branch || {};
    updateVersions = [];
    updateSource = "branch";
    updateStatus = { ...updateStatus, target_version: data.target || "" };
    if (updateModalVisible()) renderUpdateModal();
    toast(updateBranch.behind > 0
      ? t("落后 {count} 个提交", { count: updateBranch.behind })
      : t("本地已是最新"));
  } catch (e) {
    updateBranch = { available: false, reason: e.message };
    if (updateModalVisible()) renderUpdateModal();
    toast(e.message, true);
  }
}

async function applyUpdate() {
  const target = updateStatus?.target_version || updateBranch?.remote_sha || "";
  if (!target) {
    toast(t("请先检查更新"), true);
    return;
  }
  const ref = updateBranch?.name || updateStatus?.branch?.configured?.name || "main";
  if (!await confirmDelete(t("同步 {ref} 分支？", { ref }), t("将执行 git pull --ff-only 并重启主程序；工作区必须干净，本地不能有未推送提交。"))) return;
  try {
    const data = await api("/api/update", {
      method: "POST",
      body: JSON.stringify({ target, source: "branch" }),
    });
    if (data.ok) {
      toast(t("正在同步分支并重启，请稍候…"));
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
