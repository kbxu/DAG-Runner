/* Version badge and git-pull update panel. Depends on app.js globals. */
let updateStatus = { current_version: "", state: "idle", satellites: {} };
let updateBranch = null;
let updateChecking = false;
let updateApplying = false;

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

function updateStateBadge(stateName) {
  const key = stateName || "idle";
  const labels = {
    idle: "空闲",
    ready: "可更新",
    updating: "更新中",
    restarting: "重启中",
    up_to_date: "已是最新",
    skipped: "已跳过",
    done: "已完成",
    failed: "失败",
  };
  const label = t(labels[key] || key);
  return `<span class="update-state ${esc(key)}">${esc(label)}</span>`;
}

function updateSatelliteRows(satellites) {
  const entries = Object.entries(satellites || {});
  if (!entries.length) {
    return `<div class="update-sat-empty">${esc(t("暂无卫星节点"))}</div>`;
  }
  return entries.map(([alias, info]) => {
    const version = info.current_version || "—";
    const online = info.online ? t("在线") : t("离线");
    const stateName = info.state || "";
    const message = info.message ? `<div class="update-satellite-message">${esc(info.message)}</div>` : "";
    const branchPill = info.branch
      ? `<span class="update-sat-cap ${info.branch.available ? "ok" : "no"}" title="${esc(info.branch.reason || "")}">${info.branch.available ? t("支持分支更新") : t("不支持")}</span>`
      : "";
    return `<div class="update-satellite-row">
      <div class="update-sat-main">
        <span class="satellite-row-alias">${esc(alias)}</span>
        <span class="satellite-status ${info.online ? "online" : "offline"}">${esc(online)}</span>
        <span class="mono update-sat-ver">v${esc(version)}</span>
      </div>
      <div class="update-sat-meta">
        ${branchPill}
        ${stateName ? updateStateBadge(stateName) : ""}
      </div>
      ${message}
    </div>`;
  }).join("");
}

function branchRefText() {
  const configured = updateStatus?.branch?.configured || {};
  return `${configured.remote || "origin"}/${configured.name || "main"}`;
}

function updateBranchSummary() {
  if (updateChecking) {
    return { tone: "checking", title: t("正在检查远端分支…"), detail: branchRefText() };
  }
  const info = updateBranch;
  if (!info || !Object.keys(info).length) {
    return updateStatus?.branch?.available
      ? { tone: "checking", title: t("等待检查远端分支"), detail: branchRefText() }
      : { tone: "error", title: t("不支持在线更新"), detail: t("非 git 仓库部署（发布包安装请手动替换代码）") };
  }
  if (!info.available) {
    const detail = info.reason === "not_git"
      ? t("非 git 仓库部署（发布包安装请手动替换代码）")
      : (info.reason || "unknown");
    return { tone: "error", title: t("不支持在线更新"), detail };
  }
  if (info.dirty) {
    return { tone: "warn", title: t("工作区有未提交改动，无法更新"), detail: t("请先提交或清理本地改动后再试") };
  }
  if (Number(info.ahead) > 0) {
    return { tone: "warn", title: t("本地有未推送提交，无法更新"), detail: t("请先推送或清理本地提交后再试") };
  }
  if (Number(info.behind) > 0) {
    const detail = [info.latest_subject, info.remote_sha ? info.remote_sha.slice(0, 8) : ""].filter(Boolean).join(" · ");
    return { tone: "ready", title: t("发现新版本 · 落后 {count} 个提交", { count: info.behind }), detail };
  }
  return {
    tone: "ok",
    title: t("当前已是最新版本"),
    detail: info.remote_sha ? `${branchRefText()} · ${info.remote_sha.slice(0, 8)}` : branchRefText(),
  };
}

function updateReady() {
  const info = updateBranch || {};
  return Boolean(
    !updateChecking
    && !updateApplying
    && !updateStatus?.active
    && info.available
    && !info.dirty
    && Number(info.ahead || 0) === 0
    && Number(info.behind) > 0
  );
}

function updatePanelMarkup() {
  const data = updateStatus || {};
  const current = data.current_version || "—";
  const summary = updateBranchSummary();
  const satellites = knownSatellites(data);
  const satCount = Object.keys(satellites).length;
  return `<div class="update-panel" id="updatePanel">
    <div class="update-hero">
      <div class="update-hero-left">
        <div class="update-hero-label">${esc(t("当前主控版本"))}</div>
        <div class="update-hero-ver-row">
          <b class="mono update-hero-ver">v${esc(current)}</b>
          ${updateStateBadge(data.state)}
        </div>
        <div class="update-hero-ref mono">git pull · ${esc(branchRefText())}</div>
      </div>
      <div class="update-hero-right">
        <button class="button ghost small" type="button" onclick="checkBranchUpdate(true)"${updateChecking || data.active ? " disabled" : ""}>${updateChecking ? esc(t("检查中…")) : esc(t("重新检查"))}</button>
      </div>
    </div>

    <div class="update-diff-card tone-${esc(summary.tone)}">
      <div class="update-diff-title">${esc(summary.title)}</div>
      ${summary.detail ? `<div class="update-diff-detail">${esc(summary.detail)}</div>` : ""}
    </div>

    ${data.skipped_schedules ? `<div class="update-notice">${esc(t("更新期间跳过了 {count} 次定时触发", { count: data.skipped_schedules }))}</div>` : ""}
    ${data.message ? `<div class="update-notice">${esc(data.message)}</div>` : ""}

    <div class="update-sat-section">
      <div class="update-sat-head">
        <span>${esc(t("卫星节点同步状态"))}</span>
        <span class="tf-count-badge">${satCount}</span>
      </div>
      <div class="update-satellites">${updateSatelliteRows(satellites)}</div>
    </div>

    <div class="update-actions">
      ${data.active ? `<button class="button danger" type="button" onclick="abortUpdate()">${esc(t("中止更新"))}</button>` : ""}
      <button class="button" type="button" onclick="applyUpdate()"${updateReady() ? "" : " disabled"}>${updateApplying ? esc(t("正在更新…")) : esc(t("立即更新"))}</button>
      <button class="button ghost" type="button" onclick="closeUpdateModal()">${esc(t("关闭"))}</button>
    </div>
  </div>`;
}

function renderUpdateModal() {
  const existing = document.getElementById("updatePanel");
  const modal = document.getElementById("modal");
  if (existing && modal && !modal.classList.contains("hidden")) {
    existing.outerHTML = updatePanelMarkup();
    modalBackdropClosable = !Boolean(updateStatus?.active || updateApplying);
    return;
  }
  openModal(t("版本与更新"), updatePanelMarkup(), !Boolean(updateStatus?.active || updateApplying), "compact");
}

function openUpdateSettings() {
  updateBranch = null;
  updateChecking = true;
  renderUpdateModal();
  refreshUpdateStatus();
  checkBranchUpdate(false);
}

function closeUpdateModal() {
  closeModal();
}

async function checkBranchUpdate(manual = false) {
  updateChecking = true;
  if (updateModalVisible()) renderUpdateModal();
  try {
    const data = await api("/api/update/check", {
      method: "POST",
      body: JSON.stringify({ target: "" }),
    });
    updateBranch = data.branch || {};
    updateStatus = { ...updateStatus, target_version: data.target || "" };
    if (manual) {
      toast(updateBranch.behind > 0
        ? t("落后 {count} 个提交", { count: updateBranch.behind })
        : t("本地已是最新"));
    }
  } catch (e) {
    updateBranch = { available: false, reason: e.message };
    if (manual) toast(e.message, true);
  } finally {
    updateChecking = false;
    if (updateModalVisible()) renderUpdateModal();
  }
}

function waitForRestartAndReload() {
  const deadline = Date.now() + 60000;
  async function probe() {
    if (Date.now() >= deadline) {
      window.location.reload();
      return;
    }
    try {
      const res = await fetch("/api/system/overview", { cache: "no-store" });
      if (res.ok) {
        window.location.reload();
        return;
      }
    } catch (_) {
      /* expected while the service is restarting */
    }
    setTimeout(probe, 1000);
  }
  setTimeout(probe, 2200);
}

async function applyUpdate() {
  if (!updateReady()) return;
  const target = updateStatus?.target_version || updateBranch?.remote_sha || "";
  if (!target) {
    toast(t("请先检查更新"), true);
    return;
  }
  const ref = updateBranch?.name || updateStatus?.branch?.configured?.name || "main";
  if (!await confirmDialog(
    t("确认更新 {ref} 分支？", { ref }),
    t("将拉取最新代码并重启主程序，拉取失败会提示报错信息。"),
    { confirmText: t("确认"), danger: false }
  )) return;
  updateApplying = true;
  if (updateModalVisible()) renderUpdateModal();
  try {
    const data = await api("/api/update", {
      method: "POST",
      body: JSON.stringify({ target }),
    });
    if (data.ok) {
      expectedRestarting = true;
      stopPoller("workflows");
      stopPoller("runs");
      stopPoller("system");
      stopPoller("update");
      setServiceStatus("checking");
      toast(t("已拉取最新代码，正在重启服务…"));
      waitForRestartAndReload();
    } else {
      updateApplying = false;
      toast(data.error || t("更新已中止"), true);
      await refreshUpdateStatus();
    }
  } catch (e) {
    updateApplying = false;
    if (updateModalVisible()) renderUpdateModal();
    toast(e.message, true);
  }
}

async function abortUpdate() {
  try {
    await api("/api/update/abort", { method: "POST", body: "{}" });
    updateApplying = false;
    toast(t("更新已中止"));
    await refreshUpdateStatus();
  } catch (e) {
    toast(e.message, true);
  }
}

window.addEventListener("dagrunner-language-change", () => {
  renderVersionBadge(updateStatus);
  if (updateModalVisible()) renderUpdateModal();
});

startPoller("update", refreshUpdateStatus, 10000, 30000);
