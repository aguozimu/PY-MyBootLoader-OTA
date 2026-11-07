"use strict";

const $ = (id) => document.getElementById(id);
const PROGRAM_SIZE = 15 * 1024;
const MAX_LOG_ENTRIES = 2000;

let appState = null;
let logEntries = [];
let lastLogSeq = 0;
let currentLogFilter = "all";
let selectedFirmwareFile = null;
let selectedFirmwareBase64 = "";
let firmwareMetadata = null;
let connectionError = false;
let eventSource = null;
let sseFallbackTimer = null;
let lastTaskSignature = "";

const STATUS_ZH = {
  EMPTY: "空 / 未初始化",
  FLASHABLE: "可刷入",
  RECEIVING: "接收中",
  PAUSED: "已暂停",
  VERIFYING: "校验中",
  VALID_BACKUP: "有效备用",
  RUNNING: "运行中",
  ERROR: "错误",
  UNKNOWN: "未查询",
  RESERVED: "保留状态",
};

const DECISION_LABEL = {
  pending: "待查询",
  skip: "跳过 · CRC 相同",
  write: "写入",
};

const PAGE_STATUS_LABEL = {
  idle: "待机",
  waiting: "等待",
  sending: "传输中",
  confirmed: "已确认",
  skipped: "已跳过",
  failed: "失败",
};

const PHASE_TONE = {
  success: "ok",
  query_crc: "active",
  query_mid: "active",
  query_status: "active",
  compare: "active",
  control_page: "active",
  program_pages: "active",
  finalize: "active",
  verifying: "active",
  failed: "bad",
  aborted: "warn",
  idle: "",
};

function hex(value, width = 8) {
  if (value === null || value === undefined || value === "") return "—";
  const number = typeof value === "string" ? Number.parseInt(value, 10) : Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toString(16).toUpperCase().padStart(width, "0");
}

function formatId(value) {
  return value === null || value === undefined ? "—" : `0x${hex(value, 4)}`;
}

function formatSize(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(bytes % 1024 === 0 ? 0 : 2)} KB`;
}

function formatCrc(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string" && /^[0-9A-Fa-f]{8}$/.test(value)) return value.toUpperCase();
  return hex(value, 8);
}

function formatTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", { hour12: false });
}

function parseProgramId() {
  const value = $("programIdInput").value.trim();
  if (!/^[0-9a-fA-F]{1,4}$/.test(value)) return null;
  const parsed = Number.parseInt(value, 16);
  return parsed >= 1 && parsed <= 0xFFFF ? parsed : null;
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function api(path, options = {}) {
  const request = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, request);
  } catch (error) {
    throw new Error(`无法连接本地服务：${error.message}`);
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch (_) {
    payload = {};
  }
  if (!response.ok) {
    throw new Error(payload.error || `请求失败 (${response.status})`);
  }
  return payload;
}

function showToast(type, title, message, timeout = 4200) {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  const body = document.createElement("div");
  const heading = document.createElement("strong");
  heading.textContent = title;
  const text = document.createElement("p");
  text.textContent = message;
  body.append(heading, text);
  node.append(body);
  $("toastStack").append(node);
  window.setTimeout(() => {
    node.style.opacity = "0";
    node.style.transform = "translateY(6px)";
    window.setTimeout(() => node.remove(), 180);
  }, timeout);
}

async function refreshPorts() {
  const button = $("refreshPortsBtn");
  button.disabled = true;
  try {
    const result = await api("/api/ports");
    const select = $("portSelect");
    const previous = select.value;
    select.replaceChildren();
    if (!result.serial_available) {
      const option = new Option("未安装 pyserial", "");
      select.add(option);
      select.disabled = true;
      $("connectBtn").disabled = true;
      showToast("warn", "缺少串口组件", "请运行 start.bat 安装依赖，或执行 pip install -r requirements.txt。", 6500);
      return;
    }
    if (!result.ports.length) {
      select.add(new Option("未发现串口", ""));
      select.disabled = false;
      return;
    }
    for (const port of result.ports) {
      const label = port.description ? `${port.device} · ${port.description}` : port.device;
      select.add(new Option(label, port.device));
    }
    if (previous && result.ports.some((port) => port.device === previous)) {
      select.value = previous;
    }
  } catch (error) {
    showToast("error", "串口扫描失败", error.message);
  } finally {
    button.disabled = false;
    render();
  }
}

async function toggleConnection() {
  if (!appState) return;
  if (appState.connection.connected) {
    await runUiAction($("connectBtn"), "断开中…", async () => {
      await api("/api/disconnect", { method: "POST", body: {} });
      showToast("success", "串口已断开", "可以重新选择串口连接。");
    });
    return;
  }
  const port = $("portSelect").value;
  if (!port) {
    showToast("warn", "未选择串口", "请先选择有效的 COM 端口。");
    return;
  }
  connectionError = false;
  await runUiAction($("connectBtn"), "连接中…", async () => {
    await api("/api/connect", { method: "POST", body: { port, baudrate: 115200 } });
    showToast("success", "串口连接成功", `${port} · 115200 · 8N1`);
  }, true);
}

async function runUiAction(button, busyText, action, setError = false) {
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = busyText;
  try {
    await action();
  } catch (error) {
    if (setError) connectionError = true;
    showToast("error", "操作失败", error.message);
  } finally {
    if (button.isConnected) {
      button.textContent = previous;
    }
    render();
  }
}

async function startQuery(which) {
  const names = { crc: "CRC", mid: "程序 ID", status: "设备状态" };
  try {
    await api("/api/query", { method: "POST", body: { which } });
    showToast("info", "查询已启动", `正在查询${names[which]}。`, 2200);
  } catch (error) {
    showToast("error", "查询失败", error.message);
  }
}

function connectEvents() {
  if (eventSource) eventSource.close();
  if (!("EventSource" in window)) {
    startSseFallback();
    return;
  }
  eventSource = new EventSource("/api/events");
  eventSource.onmessage = (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (payload.kind === "state") {
      appState = payload.state;
      render();
    } else if (payload.kind === "log") {
      appendLog(payload.log);
    } else if (payload.kind === "initial_logs") {
      setLogs(payload.logs || []);
    } else if (payload.kind === "clear_logs") {
      setLogs([]);
    }
  };
  eventSource.onerror = () => {
    startSseFallback();
  };
  eventSource.onopen = () => {
    stopSseFallback();
  };
}

function startSseFallback() {
  if (sseFallbackTimer) return;
  sseFallbackTimer = window.setInterval(async () => {
    try {
      const fallback = await api("/api/state");
      appState = fallback;
      setLogs(fallback.logs || []);
      render();
    } catch (_) {
      // The local server may be restarting; keep polling quietly.
    }
  }, 1800);
}

function stopSseFallback() {
  if (!sseFallbackTimer) return;
  window.clearInterval(sseFallbackTimer);
  sseFallbackTimer = null;
}
function render() {
  if (!appState) return;
  renderConnection();
  renderDevice();
  renderFirmware();
  renderTask();
  renderPages();
  renderButtons();
}

function renderConnection() {
  const connection = appState.connection;
  const pill = $("connectionPill");
  pill.classList.toggle("connected", connection.connected);
  pill.classList.toggle("error", !connection.connected && connectionError);
  $("connectionText").textContent = connection.connected
    ? `${connection.port} · 已连接`
    : connectionError ? "连接异常" : "未连接";
  $("connectBtn").textContent = connection.connected ? "断开串口" : "连接串口";
  $("portSelect").disabled = connection.connected || appState.task.active || !connection.serial_available;
  if (connection.connected) {
    const exists = Array.from($("portSelect").options).some((option) => option.value === connection.port);
    if (!exists) $("portSelect").add(new Option(connection.port, connection.port));
    $("portSelect").value = connection.port;
  }
}

function renderDevice() {
  const device = appState.device;
  const statusName = device.run_status_name || "UNKNOWN";
  const statusText = device.run_status_zh || STATUS_ZH[statusName] || "未查询";
  const statusNode = $("runStatus");
  statusNode.textContent = statusText;
  statusNode.className = "";
  if (device.run_status === 0x05 || device.run_status === 0x06) statusNode.classList.add("ok");
  else if (device.run_status === 0x03 || device.run_status === 0x04) statusNode.classList.add("warn");
  else if (device.run_status === 0x07) statusNode.classList.add("bad");
  $("runStatusRaw").textContent = device.run_status === null ? "UNKNOWN" : statusName;
  $("programId").textContent = formatId(device.program_id);
  $("currentPageBlock").textContent =
    `${device.page === null ? "—" : `0x${hex(device.page, 2)}`} / ${device.block === null ? "—" : `0x${hex(device.block, 2)}`}`;
  const errorNode = $("errorCode");
  errorNode.textContent = `0x${hex(device.error_code || 0, 2)}`;
  errorNode.className = `mono${device.error_code ? " bad" : ""}`;
  $("errorDesc").textContent = device.error_code ? (device.error_zh || device.error_name) : "无错误";
  $("lastQuery").textContent = device.last_query_at
    ? `最近查询：${formatTime(device.last_query_at)}${device.last_query_ms === null ? "" : ` · ${device.last_query_ms} ms`}`
    : "尚未进行设备查询";
  const badge = $("deviceQueryBadge");
  badge.textContent = device.last_query_at ? "已同步" : "未查询";
  badge.className = `micro-badge${device.last_query_at ? " ok" : ""}`;
}

function renderFirmware() {
  const firmware = appState.firmware;
  if (selectedFirmwareFile) {
    $("fileName").textContent = selectedFirmwareFile.name;
  } else {
    $("fileName").textContent = "拖放或选择固件 .bin";
  }
  if (firmware.valid) {
    const originalSize = selectedFirmwareFile ? selectedFirmwareFile.size : firmware.size;
    $("fileMeta").textContent =
      `${formatSize(originalSize)} · 15 页 · ${firmware.padded_bytes ? `补齐 ${firmware.padded_bytes} B` : "无需补齐"}`;
    $("overallCrc").textContent = formatCrc(firmware.overall_crc);
    $("paddedBytes").textContent = `${firmware.padded_bytes || 0} B`;
    $("firmwareBadge").textContent = "已校验";
    $("firmwareBadge").className = "micro-badge ok";
    $("firmwareHint").textContent = firmware.message || "固件校验完成";
  } else {
    $("fileMeta").textContent = selectedFirmwareFile ? formatSize(selectedFirmwareFile.size) : "不足 15 KB 时可自动以 0xFF 补齐";
    $("overallCrc").textContent = "—";
    $("paddedBytes").textContent = "0 B";
    $("firmwareBadge").textContent = "15 KB 程序区";
    $("firmwareBadge").className = "micro-badge";
    $("firmwareHint").textContent = selectedFirmwareFile ? "正在校验固件…" : "选择固件后将计算 15 个程序页 CRC";
  }
}

function renderTask() {
  const task = appState.task;
  const phaseNode = $("taskPhase");
  phaseNode.textContent = task.phase_zh || "待机";
  $("taskMessage").textContent = task.message || "等待操作";
  const badge = $("taskBadge");
  const tone = PHASE_TONE[task.phase] || "";
  badge.textContent = task.active ? "执行中" : (task.phase === "success" ? "完成" : task.phase === "failed" ? "失败" : task.phase === "aborted" ? "中止" : "待机");
  badge.className = `micro-badge${tone === "ok" ? " ok" : tone === "bad" ? " bad" : tone === "warn" ? " warn" : ""}`;
  const dot = $("taskPhaseDot");
  dot.className = "task-phase-dot";
  if (tone === "active") dot.classList.add("active");
  if (tone === "bad") dot.classList.add("error");
  if (tone === "warn") dot.classList.add("warn");

  const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
  $("progressText").textContent = `${progress.toFixed(1)}%`;
  $("progressBar").style.width = `${progress}%`;
  $("progressBar").classList.toggle("error", task.phase === "failed");
  $("progressTrack").setAttribute("aria-valuenow", String(Math.round(progress)));
  $("confirmedFrames").textContent = `${task.confirmed_frames || 0} / ${task.total_frames || 0}`;
  $("retryCount").textContent = `${task.retry || 0} / 5`;
  updateTaskTime();

  const signature = `${task.started_at}|${task.finished_at}|${task.result}|${task.message}`;
  if (task.finished_at && signature !== lastTaskSignature) {
    lastTaskSignature = signature;
    if (task.result === "success") showToast("success", "任务完成", task.message, 5200);
    else if (task.result === "error") showToast("error", "任务失败", task.message, 7600);
    else if (task.result === "aborted") showToast("warn", "任务已中止", task.message, 5200);
  }
}

function updateTaskTime() {
  if (!appState) return;
  const task = appState.task;
  let seconds = 0;
  if (task.started_at) {
    const start = new Date(task.started_at).getTime();
    const end = task.finished_at ? new Date(task.finished_at).getTime() : Date.now();
    if (Number.isFinite(start) && Number.isFinite(end)) seconds = Math.max(0, Math.floor((end - start) / 1000));
  }
  $("taskTime").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function cell(text, className = "") {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function renderPages() {
  const tbody = $("pageRows");
  tbody.replaceChildren();
  for (const page of appState.pages || []) {
    const row = document.createElement("tr");
    if (page.status === "sending") row.classList.add("page-active");
    if (page.status === "failed") row.classList.add("page-error");
    if (page.kind === "control") row.classList.add("control-row");

    const labelCell = document.createElement("td");
    const label = document.createElement("div");
    label.className = "page-label";
    const index = document.createElement("span");
    index.className = "page-index";
    index.textContent = page.kind === "control" ? "C" : String(page.index).padStart(2, "0");
    const name = document.createElement("span");
    name.textContent = page.label;
    label.append(index, name);
    labelCell.append(label);

    row.append(labelCell);
    row.append(cell(page.address, "mono"));
    row.append(cell(formatCrc(page.local_crc), `mono${page.local_crc === null ? " crc-missing" : ""}`));
    row.append(cell(formatCrc(page.device_crc), `mono${page.device_crc === null ? " crc-missing" : ""}`));

    const decisionCell = document.createElement("td");
    const decision = document.createElement("span");
    decision.className = `tag ${page.decision || "pending"}`;
    decision.textContent = DECISION_LABEL[page.decision] || "待查询";
    decisionCell.append(decision);

    const statusCell = document.createElement("td");
    const status = document.createElement("span");
    const statusClass = ["confirmed", "sending", "failed"].includes(page.status) ? page.status : page.status === "skipped" ? "skip" : "";
    status.className = `tag ${statusClass}`;
    status.textContent = PAGE_STATUS_LABEL[page.status] || page.status || "待机";
    statusCell.append(status);

    row.append(decisionCell, statusCell);
    tbody.append(row);
  }
}

function renderButtons() {
  if (!appState) return;
  const connected = appState.connection.connected;
  const busy = appState.task.active;
  const selection = parseProgramId();
  $("connectBtn").disabled = busy || (!connected && !$("portSelect").value);
  $("queryCrcBtn").disabled = !connected || busy;
  $("queryMidBtn").disabled = !connected || busy;
  $("queryStatusBtn").disabled = !connected || busy;
  $("stopBtn").disabled = !busy;
  $("startUpgradeBtn").disabled = !connected || busy || !selectedFirmwareFile || !appState.firmware.valid || !selection;
  $("chooseFileBtn").disabled = busy;
  $("dropZone").disabled = busy;
}

function matchesFilter(entry) {
  if (currentLogFilter === "all") return true;
  return entry.category === currentLogFilter;
}

function setLogs(entries) {
  logEntries = entries
    .slice(-MAX_LOG_ENTRIES)
    .sort((a, b) => (a.seq || 0) - (b.seq || 0));
  lastLogSeq = logEntries.length ? logEntries[logEntries.length - 1].seq : 0;
  renderLogs();
}

function appendLog(entry) {
  if (!entry || entry.seq <= lastLogSeq) return;
  lastLogSeq = entry.seq;
  logEntries.push(entry);
  if (logEntries.length > MAX_LOG_ENTRIES) logEntries.shift();
  if (matchesFilter(entry)) {
    removeLogEmpty();
    $("logList").append(createLogLine(entry));
    trimRenderedLogs();
    updateLogCount();
    scrollLogsIfNeeded();
  }
}

function renderLogs() {
  const list = $("logList");
  list.replaceChildren();
  const filtered = logEntries.filter(matchesFilter);
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "log-empty";
    empty.id = "logEmpty";
    empty.textContent = "暂无匹配日志";
    list.append(empty);
  } else {
    const fragment = document.createDocumentFragment();
    for (const entry of filtered) fragment.append(createLogLine(entry));
    list.append(fragment);
  }
  updateLogCount();
  scrollLogsIfNeeded();
}

function createLogLine(entry) {
  const line = document.createElement("div");
  line.className = `log-line ${entry.category || "system"} ${entry.level || ""}`.trim();
  const time = document.createElement("span");
  time.className = "log-time";
  time.textContent = entry.time || "";
  const kind = document.createElement("span");
  kind.className = "log-kind";
  kind.textContent = (entry.category || "SYS").toUpperCase();
  const message = document.createElement("span");
  message.className = "log-message";
  message.textContent = entry.message || "";
  line.append(time, kind, message);
  return line;
}

function removeLogEmpty() {
  const empty = $("logEmpty");
  if (empty) empty.remove();
}

function trimRenderedLogs() {
  const list = $("logList");
  while (list.children.length > MAX_LOG_ENTRIES) list.firstElementChild?.remove();
}

function updateLogCount() {
  $("logCount").textContent = `${logEntries.length} 条`;
}

function scrollLogsIfNeeded() {
  if (!$("autoScrollCheckbox").checked) return;
  requestAnimationFrame(() => {
    const list = $("logList");
    list.scrollTop = list.scrollHeight;
  });
}
async function handleFirmwareFile(file) {
  if (!file || appState?.task.active) return;
  selectedFirmwareFile = file;
  firmwareMetadata = null;
  if (appState) {
    appState.firmware.valid = false;
    appState.firmware.message = "正在校验固件…";
  }
  render();
  if (file.size > PROGRAM_SIZE) {
    selectedFirmwareBase64 = "";
    showToast("error", "固件过大", `文件为 ${formatSize(file.size)}，超过 15 KB 程序区。`);
    $("fileMeta").textContent = `超出 15 KB：${formatSize(file.size)}`;
    renderButtons();
    return;
  }
  try {
    const buffer = await file.arrayBuffer();
    selectedFirmwareBase64 = arrayBufferToBase64(buffer);
    await inspectSelectedFirmware();
    showToast("success", "固件已校验", `${file.name} · ${formatSize(file.size)}`, 2800);
  } catch (error) {
    selectedFirmwareBase64 = "";
    showToast("error", "固件读取失败", error.message);
    if (appState) appState.firmware.valid = false;
    render();
  }
}

async function inspectSelectedFirmware() {
  if (!selectedFirmwareBase64) return;
  $("fileMeta").textContent = "正在计算页 CRC…";
  try {
    firmwareMetadata = await api("/api/firmware/inspect", {
      method: "POST",
      body: {
        filename: selectedFirmwareFile?.name || "firmware.bin",
        firmware_base64: selectedFirmwareBase64,
        pad: $("padCheckbox").checked,
      },
    });
  } catch (error) {
    firmwareMetadata = null;
    if (appState) appState.firmware.valid = false;
    showToast("error", "固件校验失败", error.message);
  } finally {
    render();
  }
}

function openUpgradeDialog() {
  const programId = parseProgramId();
  if (!selectedFirmwareFile || !selectedFirmwareBase64 || !firmwareMetadata || !programId) {
    showToast("warn", "无法开始升级", "请选择有效固件并填写目标程序 ID。");
    return;
  }
  $("dialogFileName").textContent = selectedFirmwareFile.name;
  $("dialogProgramId").textContent = `0x${hex(programId, 4)}`;
  $("dialogFirmwareSize").textContent = formatSize(selectedFirmwareFile.size);
  const dialog = $("upgradeDialog");
  dialog.returnValue = "";
  dialog.showModal();
}

async function performUpgrade() {
  const programId = parseProgramId();
  if (!programId || !selectedFirmwareBase64) return;
  try {
    await api("/api/upgrade", {
      method: "POST",
      body: {
        filename: selectedFirmwareFile?.name || "firmware.bin",
        firmware_base64: selectedFirmwareBase64,
        program_id: `0x${hex(programId, 4)}`,
        pad: $("padCheckbox").checked,
      },
    });
    showToast("info", "升级已启动", "正在查询设备 CRC 并构建控制页。", 3200);
  } catch (error) {
    showToast("error", "无法启动升级", error.message, 6500);
  }
}

async function stopUpgrade() {
  try {
    await api("/api/stop", { method: "POST", body: {} });
    showToast("warn", "中止请求已发送", "上位机将在当前 ACK 窗口结束后停止。", 3500);
  } catch (error) {
    showToast("error", "中止失败", error.message);
  }
}

async function clearLogs() {
  setLogs([]);
  try {
    await api("/api/clear-logs", { method: "POST", body: {} });
  } catch (error) {
    showToast("error", "清空日志失败", error.message);
  }
}

function downloadLogs() {
  if (!logEntries.length) {
    showToast("warn", "没有可导出的日志", "当前日志列表为空。");
    return;
  }
  const lines = logEntries.map((entry) =>
    `${entry.iso || ""} [${(entry.category || "system").toUpperCase()}] ${entry.level || "info"} ${entry.message || ""}`.trim()
  );
  const blob = new Blob([`${lines.join("\n")}\n`], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  link.href = url;
  link.download = `ota-console-${stamp}.log`;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function bindEvents() {
  $("refreshPortsBtn").addEventListener("click", refreshPorts);
  $("connectBtn").addEventListener("click", toggleConnection);
  $("queryCrcBtn").addEventListener("click", () => startQuery("crc"));
  $("queryMidBtn").addEventListener("click", () => startQuery("mid"));
  $("queryStatusBtn").addEventListener("click", () => startQuery("status"));
  $("stopBtn").addEventListener("click", stopUpgrade);

  $("chooseFileBtn").addEventListener("click", () => $("firmwareInput").click());
  $("dropZone").addEventListener("click", () => $("firmwareInput").click());
  $("firmwareInput").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (file) handleFirmwareFile(file);
    event.target.value = "";
  });
  for (const eventName of ["dragenter", "dragover"]) {
    $("dropZone").addEventListener(eventName, (event) => {
      event.preventDefault();
      $("dropZone").classList.add("dragging");
    });
  }
  for (const eventName of ["dragleave", "drop"]) {
    $("dropZone").addEventListener(eventName, (event) => {
      event.preventDefault();
      $("dropZone").classList.remove("dragging");
    });
  }
  $("dropZone").addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) handleFirmwareFile(file);
  });

  $("padCheckbox").addEventListener("change", () => {
    if (selectedFirmwareFile) inspectSelectedFirmware();
  });
  $("programIdInput").addEventListener("input", (event) => {
    event.target.value = event.target.value.replace(/[^0-9a-fA-F]/g, "").slice(0, 4);
    renderButtons();
  });
  $("programIdInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !$("startUpgradeBtn").disabled) openUpgradeDialog();
  });
  $("startUpgradeBtn").addEventListener("click", openUpgradeDialog);
  $("upgradeDialog").addEventListener("close", () => {
    if ($("upgradeDialog").returnValue === "confirm") performUpgrade();
  });

  $("logFilters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (!button) return;
    currentLogFilter = button.dataset.filter;
    for (const item of $("logFilters").querySelectorAll("button")) {
      item.classList.toggle("active", item === button);
    }
    renderLogs();
  });
  $("autoScrollCheckbox").addEventListener("change", scrollLogsIfNeeded);
  $("clearLogsBtn").addEventListener("click", clearLogs);
  $("downloadLogsBtn").addEventListener("click", downloadLogs);
  $("downloadLogsBtn2").addEventListener("click", downloadLogs);

  document.addEventListener("keydown", (event) => {
    const tag = document.activeElement?.tagName;
    if (["INPUT", "SELECT", "TEXTAREA"].includes(tag) || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key.toLowerCase() === "g") startQuery("crc");
    if (event.key.toLowerCase() === "m") startQuery("mid");
    if (event.key.toLowerCase() === "s") startQuery("status");
  });

  window.addEventListener("beforeunload", () => eventSource?.close());
}

async function init() {
  bindEvents();
  try {
    appState = await api("/api/state");
    setLogs(appState.logs || []);
    render();
    await refreshPorts();
    connectEvents();
    window.setInterval(updateTaskTime, 1000);
  } catch (error) {
    showToast("error", "上位机服务不可用", error.message, 10000);
    $("connectionText").textContent = "服务异常";
    $("connectionPill").classList.add("error");
  }
}

init();
