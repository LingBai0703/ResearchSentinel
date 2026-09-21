const state = {
  data: null,
  toastTimer: null,
  lastHealth: null,
  expandedPids: new Set(),
  autoShown: new Set(),
  settingsLoaded: false,
  deleteId: null,
  requestVersion: 0,
  mutations: 0,
  currentView: location.hash.replace("#", "") || "overview"
};

const el = (id) => document.getElementById(id);
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

function formatBytes(bytes) {
  if (bytes == null) return "--";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes), index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return "--";
  let value = Math.max(0, Math.round(Number(seconds)));
  const days = Math.floor(value / 86400); value %= 86400;
  const hours = Math.floor(value / 3600); value %= 3600;
  const minutes = Math.floor(value / 60); const secs = value % 60;
  const clock = [hours, minutes, secs].map((item) => String(item).padStart(2, "0")).join(":");
  return days ? `${days}天 ${clock}` : clock;
}

function shortPath(value) {
  if (!value) return "--";
  const parts = String(value).split(/[\\/]/);
  return parts[parts.length - 1] || value;
}

function artifactUrl(path) {
  return `/api/artifact?path=${encodeURIComponent(path)}`;
}

function showToast(message) {
  const toast = el("toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.remove("show"), 2800);
}

function activateView(name, updateHash = true) {
  const allowed = new Set(["overview", "processes", "settings"]);
  const selected = allowed.has(name) ? name : (name === "results" || name === "activity" ? "processes" : "overview");
  state.currentView = selected;
  document.querySelectorAll(".app-view").forEach((view) => view.classList.toggle("active", view.id === `view-${selected}`));
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === selected));
  if (updateHash) history.replaceState(null, "", `#${selected}`);
  if (selected === "overview" && state.data) requestAnimationFrame(() => drawChart(state.data.history || []));
}

function statusClass(health) {
  if (["CRASHED", "STALLED"].includes(health)) return "danger";
  if (["IDLE", "WAITING", "RECOVERY_PENDING", "RESTARTING"].includes(health)) return "warning";
  return "";
}

function statusName(health) {
  return ({
    RUNNING: "运行中", IDLE: "低活动", STALLED: "疑似卡住", WAITING: "等待进程",
    COMPLETED: "正常完成", CRASHED: "异常退出", RECOVERY_PENDING: "等待恢复",
    RESTARTING: "正在恢复", EXITED: "退出状态未知"
  })[health] || health || "未知";
}

function makeElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function makeResultCard(result) {
  const figure = makeElement("figure", "result-card");
  let preview;
  if (result.previewable) {
    preview = makeElement("button", "result-preview");
    preview.type = "button";
    preview.setAttribute("aria-label", `预览 ${result.name}`);
    const image = document.createElement("img");
    image.src = artifactUrl(result.path);
    image.alt = result.name;
    image.loading = "lazy";
    preview.appendChild(image);
    preview.addEventListener("click", () => openViewer(result));
  } else {
    preview = makeElement("a", "result-preview");
    preview.href = artifactUrl(result.path);
    preview.target = "_blank";
    preview.rel = "noopener";
    preview.appendChild(makeElement("span", "result-file-icon", result.extension.replace(".", "").toUpperCase()));
  }
  const caption = document.createElement("figcaption");
  caption.append(makeElement("strong", "", result.name));
  caption.append(makeElement("small", "", `${formatBytes(result.size)} · ${result.modified ? new Date(result.modified).toLocaleString() : "--"}`));
  figure.append(preview, caption);
  return figure;
}

function fillResults(container, results) {
  container.replaceChildren();
  if (!results.length) {
    container.appendChild(makeElement("p", "empty-state compact", "任务完成后，结果图会自动显示在这里"));
    return;
  }
  results.forEach((result) => container.appendChild(makeResultCard(result)));
}

function fillEvents(container, events) {
  container.replaceChildren();
  if (!events.length) {
    container.appendChild(makeElement("li", "empty", "暂无监控事件"));
    return;
  }
  events.slice(0, 20).forEach((event) => {
    const item = document.createElement("li");
    const time = makeElement("time", "", event.time ? new Date(event.time).toLocaleString() : "--");
    item.append(time, makeElement("span", "", event.message || event.kind));
    container.appendChild(item);
  });
}

function renderProcesses(tasks, data) {
  const grid = el("processGrid");
  if (grid.contains(document.activeElement) && document.activeElement.matches("input:not([type=checkbox])")) return;
  const scrollPositions = new Map([...grid.querySelectorAll(".process-card")].map(card =>
    [card.dataset.id, card.querySelector(".process-log")?.scrollTop || 0]));
  const focusedId = document.activeElement?.closest(".process-card")?.dataset.id;
  const focusedClass = document.activeElement?.className;
  grid.replaceChildren();
  if (!tasks.length) {
    grid.appendChild(makeElement("p", "empty-state", "暂无科研任务"));
    return;
  }
  tasks.forEach((task) => {
    const live = task.processes.length > 0;
    const samples = live ? task.processes : (task.last_processes || []);
    const process = {
      pid: samples.map(p => p.pid).join(", ") || "--",
      cpu_percent: samples.reduce((n, p) => n + p.cpu_percent, 0),
      memory_bytes: samples.reduce((n, p) => n + p.memory_bytes, 0),
      uptime_seconds: (task.ended || data.timestamp_epoch) - task.started,
      executable: task.argv[0] || "--"
    };
    const taskData = { job: task.job, results: task.results, events: task.events };
    const pid = task.id;
    const expanded = state.expandedPids.has(pid);
    const card = makeElement("article", `process-card${expanded ? " expanded" : ""}`);
    card.dataset.id = pid;
    const toggle = makeElement("button", "process-toggle");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.setAttribute("aria-controls", `process-details-${pid}`);
    const identity = makeElement("span", "process-identity");
    identity.appendChild(makeElement("span", "process-avatar", task.kind === "python" ? "Py" : "M"));
    const title = makeElement("span", "process-title");
    title.append(makeElement("strong", "", task.name));
    title.append(makeElement("span", "", `PID ${process.pid} · ${statusName(task.status)}`));
    identity.append(title, makeElement("span", live ? "process-live" : "process-live inactive"), makeElement("span", "process-chevron", "⌄"));

    const stats = makeElement("span", "process-stats");
    [[live ? "CPU" : "最后 CPU", `${Number(process.cpu_percent).toFixed(1)}%`], [live ? "内存" : "最后内存", formatBytes(process.memory_bytes)], ["运行时长", formatDuration(process.uptime_seconds)]].forEach(([label, value]) => {
      const item = makeElement("span", "process-stat");
      item.append(makeElement("span", "", label), makeElement("strong", "", value));
      stats.appendChild(item);
    });
    const path = makeElement("span", "process-path");
    path.append(makeElement("span", "", "可执行文件"), makeElement("code", "", process.executable || process.name));
    toggle.append(identity, stats, path);
    toggle.addEventListener("click", () => {
      if (state.expandedPids.has(pid)) state.expandedPids.delete(pid);
      else state.expandedPids.add(pid);
      renderProcesses(state.data.tasks || [], state.data);
    });
    card.appendChild(toggle);
    const actions = makeElement("div", "task-actions");
    const restartLabel = makeElement("label", "setting-check");
    const restart = makeElement("input", "task-restart");
    restart.type = "checkbox";
    restart.checked = task.auto_restart;
    restart.disabled = Boolean(data.memory_protection?.low);
    restart.addEventListener("change", async () => {
      const enabled = restart.checked;
      task.auto_restart = enabled;
      try {
        await request("/api/task/settings", { method: "POST", body: JSON.stringify({id: pid, auto_restart: enabled}) });
        await refresh();
      } catch (error) { task.auto_restart = !enabled; restart.checked = !enabled; showToast(error.message); }
    });
    restartLabel.append(restart, document.createTextNode("自动续跑"));
    const remove = makeElement("button", "button secondary task-delete", "删除");
    remove.type = "button";
    remove.addEventListener("click", () => {
      state.deleteId = pid;
      el("deleteName").textContent = task.name;
      el("deleteStop").checked = false;
      el("deleteError").textContent = "";
      el("deleteDialog").showModal();
    });
    const count = task.job.progress;
    actions.append(restartLabel, makeElement("span", "task-progress",
      count.total ? `${count.completed} / ${count.total}` : "进度未知"), remove);
    card.append(actions);
    if (task.ended) card.append(makeElement("p", "task-ended",
      `${statusName(task.status)} · ${new Date(task.ended * 1000).toLocaleString()}`));

    const details = makeElement("div", "process-details");
    details.id = `process-details-${pid}`;
    details.hidden = !expanded;

    const resultPanel = makeElement("section", "process-detail-panel result-panel");
    const resultHeading = makeElement("div", "detail-heading");
    const resultCopy = makeElement("div");
    resultCopy.append(makeElement("h3", "", "结果"), makeElement("p", "", taskData.job.directory || "未关联目录"));
    resultHeading.append(resultCopy, makeElement("span", "compact-value", `${taskData.results.length} 个文件`));
    const gallery = makeElement("div", "result-gallery compact-gallery");
    fillResults(gallery, taskData.results || []);
    resultPanel.append(resultHeading, gallery);

    const activityPanel = makeElement("section", "process-detail-panel activity-panel");
    const activityHeading = makeElement("div", "detail-heading");
    const activityCopy = makeElement("div");
    activityCopy.append(makeElement("h3", "", "日志与事件"), makeElement("p", "", task.log_path || "未关联日志"));
    activityHeading.append(activityCopy);
    const logLines = [...(taskData.job.log_tail || [])];
    if ((taskData.job.stderr_tail || []).length) logLines.push("", "[stderr]", ...taskData.job.stderr_tail);
    const log = makeElement("pre", "process-log", logLines.length ? logLines.join("\n") : "等待日志");
    const eventList = makeElement("ol", "event-list");
    fillEvents(eventList, taskData.events || []);
    activityPanel.append(activityHeading, log, eventList);

    details.append(resultPanel, activityPanel);
    card.appendChild(details);
    const paths = makeElement("form", "task-paths");
    [["log_path", "日志文件"], ["output_dir", "结果目录"]].forEach(([name, text]) => {
      const label = makeElement("label", "", text);
      const input = makeElement("input");
      input.name = name;
      input.value = task[name] || "";
      label.append(input);
      paths.append(label);
    });
    const savePaths = makeElement("button", "button secondary", "保存路径");
    savePaths.type = "submit";
    paths.append(savePaths);
    paths.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await request("/api/task/settings", {method: "POST", body: JSON.stringify({
          id: pid, log_path: paths.elements.log_path.value, output_dir: paths.elements.output_dir.value
        })});
        showToast("路径已更新");
        savePaths.focus();
        await refresh();
      } catch (error) { showToast(error.message); }
    });
    paths.hidden = !expanded;
    card.append(paths);
    grid.appendChild(card);
    log.scrollTop = scrollPositions.get(pid) || 0;
    if (focusedId === pid && focusedClass === "process-toggle") toggle.focus({preventScroll: true});
  });
}

function openViewer(result) {
  if (!result.previewable) return;
  el("viewerImage").src = artifactUrl(result.path);
  el("viewerImage").alt = result.name;
  el("viewerCaption").textContent = result.relative_path;
  el("imageViewer").hidden = false;
  document.body.style.overflow = "hidden";
}

function closeViewer() {
  el("imageViewer").hidden = true;
  el("viewerImage").removeAttribute("src");
  document.body.style.overflow = "";
}

function drawChart(history) {
  const canvas = el("activityChart");
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.floor(rect.width * ratio);
  canvas.height = Math.floor(rect.height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const width = rect.width, height = rect.height;
  const pad = { left: 42, right: 12, top: 12, bottom: 24 };
  const plotWidth = width - pad.left - pad.right, plotHeight = height - pad.top - pad.bottom;
  ctx.clearRect(0, 0, width, height);
  ctx.font = '11px "Segoe UI", sans-serif';
  ctx.fillStyle = "#929da6";
  ctx.strokeStyle = "#29323a";
  ctx.lineWidth = 1;
  const maximum = Math.max(100, ...history.map((item) => Number(item.cpu || 0)));
  [0, .5, 1].forEach((fraction) => {
    const y = pad.top + plotHeight * (1 - fraction);
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    ctx.fillText(`${Math.round(maximum * fraction)}%`, 3, y + 4);
  });
  if (history.length < 2) return;
  ctx.strokeStyle = "#38bdf8";
  ctx.lineWidth = 2;
  ctx.beginPath();
  history.forEach((item, index) => {
    const x = pad.left + plotWidth * index / (history.length - 1);
    const y = pad.top + plotHeight * (1 - clamp(Number(item.cpu || 0) / maximum, 0, 1));
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function render(data) {
  state.data = data;
  if (!data.ready) return;
  const progress = data.job.progress;
  renderSettings(data);
  const results = data.results || [];
  const fraction = progress.fraction == null ? 0 : progress.fraction;
  const percent = progress.fraction == null ? "--" : `${(fraction * 100).toFixed(1)}%`;
  const statusBand = el("statusBand");
  statusBand.className = `status-band ${data.memory_protection?.latched ? "warning" : statusClass(data.health)}`.trim();
  el("statusLabel").textContent = statusName(data.health);
  el("statusMessage").textContent = data.memory_protection?.latched ? "内存保护：自动续跑已关闭" : (data.message || "--");
  el("updatedAt").textContent = data.timestamp ? new Date(data.timestamp).toLocaleTimeString() : "--";
  el("projectPath").textContent = data.project_root || "CELL-IRS";
  el("platformLabel").textContent = data.platform?.label || "--";
  el("platformSupport").textContent = data.platform?.support_label || "--";
  el("progressPercent").textContent = percent;
  el("progressCount").textContent = progress.completed == null ? "-- / --" : `${progress.completed} / ${progress.total ?? "--"}`;
  el("cpuValue").textContent = `${Number(data.totals.cpu_percent || 0).toFixed(1)}%`;
  el("memoryValue").textContent = formatBytes(data.totals.memory_bytes);
  el("processCount").textContent = `${data.processes.length} 个进程`;
  el("etaValue").textContent = formatDuration(progress.eta_seconds);
  el("itemTime").textContent = progress.item_seconds == null ? "单点耗时 --" : `单点约 ${Number(progress.item_seconds).toFixed(1)} 秒`;
  el("jobDirectory").textContent = data.job.directory || "尚未发现任务目录";
  el("trialValue").textContent = progress.trial == null ? "轮次 --" : `轮次 ${progress.trial} / ${progress.trials}`;
  el("progressFill").style.width = `${clamp(fraction * 100, 0, 100)}%`;
  document.querySelector(".progress-track").setAttribute("aria-valuenow", String(Math.round(fraction * 100)));
  el("latestLine").textContent = progress.latest_line || "暂无进度输出";
  el("activityAge").textContent = data.job.activity_age_seconds == null ? "最近活动 --" : `最近活动 ${formatDuration(data.job.activity_age_seconds)} 前`;

  el("processBadge").textContent = String((data.tasks || []).length);
  el("processSummary").textContent = `${(data.tasks || []).length} 个任务 · ${data.processes.length} 个运行进程`;
  el("processCpuSummary").textContent = `CPU ${Number(data.totals.cpu_percent || 0).toFixed(1)}%`;
  for (const task of data.tasks || []) {
    if (task.status === "COMPLETED" && task.results.length && !state.autoShown.has(task.id)) {
      state.autoShown.add(task.id);
      state.expandedPids.add(task.id);
      if (state.currentView !== "settings" && !el("deleteDialog").open) activateView("processes");
      showToast(`${task.name} 已完成`);
    }
  }
  renderProcesses(data.tasks || [], data);

  const recovery = data.recovery;
  el("recoveryMessage").textContent = recovery.last_message || "监控已就绪";
  el("recoveryAttempts").textContent = `${recovery.attempts} / ${recovery.max_attempts}`;
  el("commandState").textContent = recovery.command_available ? `已捕获 · ${recovery.captured_at ? new Date(recovery.captured_at).toLocaleTimeString() : "--"}` : "未捕获";
  el("nextRestart").textContent = recovery.next_restart_at ? new Date(recovery.next_restart_at).toLocaleTimeString() : "--";
  el("recoveryCwd").textContent = recovery.cwd || "--";
  el("restartLog").textContent = recovery.last_restart_log ? shortPath(recovery.last_restart_log) : "--";

  if (state.currentView === "overview") drawChart(data.history || []);

  const checkpoint = data.job.checkpoint;
  el("checkpointName").textContent = checkpoint ? shortPath(checkpoint.path) : "--";
  el("checkpointAge").textContent = checkpoint ? `${formatDuration(checkpoint.age_seconds)} 前` : "未发现";
  const stderr = data.job.stderr;
  el("stderrState").textContent = (data.job.stderr_tail || []).length ? "检测到内容" : (stderr ? "空" : "未发现");
  el("stderrPath").textContent = stderr ? shortPath(stderr.path) : "--";

  state.lastHealth = data.health;
}

async function request(url, options = {}) {
  const mutation = options.method === "POST";
  if (mutation) { state.mutations++; state.requestVersion++; }
  try {
    const response = await fetch(url, { cache: "no-store", headers: { "Content-Type": "application/json" }, ...options });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.message || `请求失败 ${response.status}`);
    return payload;
  } finally {
    if (mutation) { state.mutations--; state.requestVersion++; }
  }
}

async function refresh() {
  if (state.mutations) return;
  const version = ++state.requestVersion;
  try {
    const data = await request("/api/status");
    if (version === state.requestVersion && !state.mutations) render(data);
  }
  catch (error) {
    const band = el("statusBand"); band.className = "status-band danger";
    el("statusLabel").textContent = "服务断开"; el("statusMessage").textContent = error.message;
  }
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => activateView(button.dataset.view)));
el("closeViewer").addEventListener("click", closeViewer);
el("imageViewer").addEventListener("click", (event) => { if (event.target === el("imageViewer")) closeViewer(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !el("imageViewer").hidden) closeViewer(); });

function renderSettings(data) {
  const form = el("settingsForm");
  if (!state.settingsLoaded) {
    for (const input of form.querySelectorAll("input")) {
      if (input.type === "checkbox") input.checked = Boolean(data.settings[input.name]);
      else input.value = data.settings[input.name];
    }
    state.settingsLoaded = true;
  }
  el("settingsPlatform").textContent = data.platform.label;
  el("autostartState").textContent = data.autostart?.installed ? "自启动：已安装（用户登录后）" : "自启动：未安装";
  const memory = data.memory_protection || {};
  el("memoryState").textContent = `系统内存 ${memory.percent ?? "--"}% · 可用 ${formatBytes(memory.available)}${memory.latched ? " · 内存保护已触发，自动续跑保持关闭" : ""}`;
  el("startupCommand").textContent = data.startup_command || "--";
  el("networkPort").textContent = `服务端口：${data.port} · 当前${data.settings.lan_enabled ? "允许网络访问" : "仅限本机"}`;
}

el("settingsForm").addEventListener("submit", async event => {
  event.preventDefault();
  const payload = {};
  for (const input of event.target.querySelectorAll("input")) {
    payload[input.name] = input.type === "checkbox" ? input.checked : Number(input.value);
  }
  if (payload.lan_enabled !== state.data.settings.lan_enabled &&
      !confirm(payload.lan_enabled ? "允许可信网络设备访问此服务及任务操作？" : "关闭网络访问后，远程页面将断开，需在服务器本机访问。继续？")) return;
  try {
    const result = await request("/api/settings", {method: "POST", body: JSON.stringify(payload)});
    state.settingsLoaded = false;
    el("settingsFeedback").textContent = result.network_changed ? "已保存，监听地址正在切换" : "已保存";
    await refresh();
  } catch (error) { el("settingsFeedback").textContent = error.message; }
});
el("deleteCancel").addEventListener("click", () => el("deleteDialog").close());
el("deleteForm").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await request("/api/task/delete", {method: "POST", body: JSON.stringify({id: state.deleteId, stop: el("deleteStop").checked})});
    el("deleteDialog").close();
    await refresh();
  } catch (error) { el("deleteError").textContent = error.message; }
});

el("rescanButton").addEventListener("click", async () => {
  try { await request("/api/action/rescan", { method: "POST", body: "{}" }); showToast("已重新扫描"); setTimeout(refresh, 250); }
  catch (error) { showToast(error.message); }
});

window.addEventListener("resize", () => state.currentView === "overview" && state.data && drawChart(state.data.history || []));
activateView(state.currentView, false);
async function poll() {
  await refresh();
  setTimeout(poll, (state.data?.settings?.refresh_seconds || 2) * 1000);
}
poll();
