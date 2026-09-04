"use strict";

const STORAGE_KEY = "moonli.api-lab.preferences.v1";
const VIEW_TITLES = {
  overview: "overview",
  stages: "test calls",
  configuration: "configuration",
  production: "production",
  devices: "devices",
  activity: "activity",
  documentation: "documentation",
};

const PRODUCTION_REQUEST_FIELDS = {
  "client-text": "productionRequestClientText",
  "client-audio": "productionRequestClientAudio",
  "google-transcription": "productionRequestTranscription",
  "google-normalization": "productionRequestNormalization",
  "google-image-pipeline-1": "productionRequestImageP1",
  "google-image-pipeline-2": "productionRequestImageP2",
};

const state = {
  activeView: "overview",
  csrfToken: sessionStorage.getItem("moonli.csrf") || "",
  googleKey: "",
  config: null,
  production: null,
  devices: [],
  overviewStats: null,
  settings: null,
  templates: {},
  defaultTemplates: {},
  activity: [],
  lastUpdateJob: null,
  objectUrls: new Map(),
  lastFocused: null,
  stageResults: {
    transcription: "",
    normalization: "",
    prompt: "",
    paletteReport: "",
    paletteFile: null,
    palettePipeline: "pipeline-1",
  },
};

const $ = (id) => document.getElementById(id);

class ApiError extends Error {
  constructor(message, status = 0, code = "REQUEST_FAILED") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-US").format(Number(value) || 0);
}

function formatBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = bytes;
  let unit = -1;
  do {
    amount /= 1024;
    unit += 1;
  } while (amount >= 1024 && unit < units.length - 1);
  return `${amount >= 100 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`;
}

function formatDuration(value) {
  let seconds = Math.max(0, Math.floor(Number(value) || 0));
  const days = Math.floor(seconds / 86400);
  seconds %= 86400;
  const hours = Math.floor(seconds / 3600);
  seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  if (days) return `${days}d ${hours}h ${minutes}m`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m ${seconds % 60}s`;
}

function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function deviceField(label, value, className = "") {
  const field = document.createElement("div");
  field.className = "device-field";
  const title = document.createElement("span");
  title.textContent = label;
  const content = document.createElement("strong");
  content.textContent = value;
  if (className) content.className = className;
  field.append(title, content);
  return field;
}

function renderDevices() {
  const root = $("deviceList");
  const openDevices = new Set(
    Array.from(root.querySelectorAll("details[open]"), (item) => item.dataset.deviceId),
  );
  root.replaceChildren();
  $("deviceCount").textContent = `${formatNumber(state.devices.length)} devices`;
  if (!state.devices.length) {
    const empty = document.createElement("p");
    empty.className = "device-empty";
    empty.textContent = "No devices have been registered.";
    root.append(empty);
    return;
  }

  for (const device of state.devices) {
    const card = document.createElement("details");
    card.className = `device-card${device.blocked ? " is-blocked" : ""}`;
    card.dataset.deviceId = device.device_id;
    card.open = openDevices.has(device.device_id);

    const summary = document.createElement("summary");
    const identity = document.createElement("strong");
    identity.textContent = device.device_id;
    const type = document.createElement("span");
    type.textContent = device.type_label;
    const status = document.createElement("span");
    status.className = device.blocked ? "device-status is-blocked" : "device-status";
    status.textContent = device.blocked ? "Blocked" : "Active";
    summary.append(identity, type, status);

    const body = document.createElement("div");
    body.className = "device-card-body";
    const fields = document.createElement("div");
    fields.className = "device-fields";
    fields.append(
      deviceField("Identifier", device.device_id),
      deviceField("Type", device.type_label),
      deviceField("Lifetime requests", formatNumber(device.request_count)),
      deviceField("Registered at", formatDateTime(device.registered_at)),
    );
    const actions = document.createElement("div");
    actions.className = "action-row device-actions";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = device.blocked ? "primary-action" : "danger-action";
    toggle.textContent = device.blocked ? "Unblock" : "Block";
    toggle.addEventListener("click", () => handleDeviceToggle(device, toggle));
    actions.append(toggle);
    body.append(fields, actions);
    card.append(summary, body);
    root.append(card);
  }
}

async function refreshDevices() {
  try {
    const { data } = await apiCall("/internal/devices?limit=500");
    state.devices = Array.isArray(data.devices) ? data.devices : [];
    renderDevices();
  } catch (error) {
    $("deviceList").textContent = errorText(error);
    $("deviceCount").textContent = "Unavailable";
  }
}

async function handleDeviceToggle(device, button) {
  const blocked = !device.blocked;
  setPending(button, true, blocked ? "Blocking..." : "Unblocking...");
  try {
    await apiCall(`/internal/devices/${encodeURIComponent(device.device_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ blocked }),
    });
    notice(blocked ? `Device ${device.device_id} was blocked.` : `Device ${device.device_id} was unblocked.`);
    await refreshDevices();
  } catch (error) {
    notice(errorText(error), "error");
    setPending(button, false, "");
  }
}

function svgElement(name, attributes = {}, textValue = "") {
  const element = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  if (textValue) element.textContent = textValue;
  return element;
}

function renderUsageChart(series) {
  const chart = $("productionUsageChart");
  chart.replaceChildren();
  const values = Array.isArray(series) ? series : [];
  const hasData = values.some((item) => item.requests || item.tokens);
  $("productionUsageEmpty").classList.toggle("hidden", hasData);

  const left = 42;
  const right = 958;
  const top = 30;
  const bottom = 252;
  const width = right - left;
  const height = bottom - top;
  for (let index = 0; index <= 4; index += 1) {
    const y = top + (height / 4) * index;
    chart.append(svgElement("line", {
      x1: left, y1: y, x2: right, y2: y, class: "chart-grid-line",
    }));
  }
  if (!values.length) return;

  const maxRequests = Math.max(1, ...values.map((item) => Number(item.requests) || 0));
  const maxTokens = Math.max(1, ...values.map((item) => Number(item.tokens) || 0));
  const step = width / values.length;
  const barWidth = Math.max(5, Math.min(22, step * .52));
  const points = [];

  values.forEach((item, index) => {
    const x = left + step * index + step / 2;
    const requestHeight = (Number(item.requests) || 0) / maxRequests * height;
    const bar = svgElement("rect", {
      x: x - barWidth / 2,
      y: bottom - requestHeight,
      width: barWidth,
      height: requestHeight,
      class: "chart-request-bar",
    });
    bar.append(svgElement("title", {}, `${formatNumber(item.requests)} requests`));
    chart.append(bar);

    const tokenY = bottom - (Number(item.tokens) || 0) / maxTokens * height;
    points.push(`${x},${tokenY}`);
    if (index % 4 === 0 || index === values.length - 1) {
      const at = new Date(item.at);
      const label = Number.isNaN(at.getTime())
        ? "—"
        : at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      chart.append(svgElement("text", {
        x, y: 280, "text-anchor": "middle", class: "chart-axis-label",
      }, label));
    }
  });

  chart.append(svgElement("polyline", { points: points.join(" "), class: "chart-token-line" }));
  values.forEach((item, index) => {
    if (!item.tokens) return;
    const [x, y] = points[index].split(",");
    const point = svgElement("circle", { cx: x, cy: y, r: 4, class: "chart-token-point" });
    point.append(svgElement("title", {}, `${formatNumber(item.tokens)} tokens`));
    chart.append(point);
  });
  chart.append(svgElement("text", {
    x: left, y: 18, "text-anchor": "start", class: "chart-axis-label",
  }, `max ${formatNumber(maxRequests)} req`));
  chart.append(svgElement("text", {
    x: right, y: 18, "text-anchor": "end", class: "chart-axis-label",
  }, `max ${formatNumber(maxTokens)} tok`));
}

function renderOverviewStats(payload) {
  const system = payload?.system || {};
  const ram = system.ram || {};
  const disk = system.disk || {};
  const usage = payload?.production_api || {};
  const cpuPercent = Math.max(0, Math.min(100, Number(system.cpu_percent) || 0));
  const ramPercent = Math.max(0, Math.min(100, Number(ram.percent) || 0));
  const diskPercent = Math.max(0, Math.min(100, Number(disk.percent) || 0));
  $("overviewCpu").textContent = `${cpuPercent.toFixed(1)}%`;
  $("overviewCpuBar").style.width = `${cpuPercent}%`;
  $("overviewRam").textContent = `${ramPercent.toFixed(1)}%`;
  $("overviewRamDetail").textContent = `${formatBytes(ram.used_bytes)} / ${formatBytes(ram.total_bytes)}`;
  $("overviewRamBar").style.width = `${ramPercent}%`;
  $("overviewDisk").textContent = `${diskPercent.toFixed(1)}%`;
  $("overviewDiskDetail").textContent = `${formatBytes(disk.used_bytes)} / ${formatBytes(disk.total_bytes)}`;
  $("overviewDiskBar").style.width = `${diskPercent}%`;
  $("overviewUptime").textContent = formatDuration(system.uptime_seconds);
  $("overviewRequestCount").textContent = formatNumber(usage.requests);
  $("overviewTokenCount").textContent = formatNumber(usage.tokens);
  $("overviewWindowRequests").textContent = `${formatNumber(usage.window_requests)} in the last 24 hours`;
  $("overviewWindowTokens").textContent = `${formatNumber(usage.window_tokens)} in the last 24 hours`;
  const generatedAt = new Date(payload.generated_at);
  $("overviewStatsUpdated").textContent = Number.isNaN(generatedAt.getTime())
    ? "24H"
    : `24H · ${generatedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  renderUsageChart(usage.series);
}

async function refreshOverviewStats() {
  if ($("appShell").classList.contains("hidden")) return;
  try {
    const response = await fetch("/internal/production/stats", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.overviewStats = await response.json();
    renderOverviewStats(state.overviewStats);
  } catch (_error) {
    $("overviewStatsUpdated").textContent = "24H · unavailable";
  }
}

function uuid() {
  if (crypto.randomUUID) return `lab-${crypto.randomUUID()}`;
  const random = crypto.getRandomValues(new Uint32Array(4));
  return `lab-${Array.from(random, (item) => item.toString(16).padStart(8, "0")).join("")}`;
}

function masked(value) {
  if (!value) return "not set";
  return `••••${value.slice(-4)}`;
}

function readPreferences() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") || {};
  } catch (_error) {
    return {};
  }
}

function applyStoredAccent() {
  const savedAccent = readPreferences().accent || "";
  const accent = /^#[0-9A-Fa-f]{6}$/.test(savedAccent)
    ? savedAccent.toUpperCase()
    : "#00A8FF";
  document.documentElement.style.setProperty("--accent", accent);
}

function savePreferences() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    accent: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim(),
  }));
}

function notice(message, type = "success") {
  const item = document.createElement("div");
  item.className = `notice ${type}`;
  item.textContent = message;
  $("noticeStack").append(item);
  window.setTimeout(() => item.remove(), 5000);
}

async function copyTextResult(value, label) {
  if (!value) return notice("Run this stage first.", "error");
  try {
    await navigator.clipboard.writeText(value);
    notice(`${label} copied.`);
  } catch (_error) {
    notice("The browser denied clipboard access.", "error");
  }
}

function toggleButtons(ids, enabled) {
  for (const id of ids) $(id).disabled = !enabled;
}

function setPending(button, pending, pendingLabel) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = pending;
  button.textContent = pending ? pendingLabel : button.dataset.label;
}

function revokeResultUrl(containerId) {
  const previous = state.objectUrls.get(containerId);
  if (previous) URL.revokeObjectURL(previous);
  state.objectUrls.delete(containerId);
}

function clearSecrets() {
  state.csrfToken = "";
  sessionStorage.removeItem("moonli.csrf");
  state.googleKey = "";
  $("accessKey").value = "";
  $("googleKeyInput").value = "";
  $("productionGoogleKey").value = "";
  updateGoogleKeyStatus();
}

function recordActivity(type, body, ok = true) {
  state.activity.unshift({
    type,
    body: String(body).replaceAll(state.googleKey || "\u0000", "[REDACTED]"),
    ok,
    time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
  });
  state.activity = state.activity.slice(0, 100);
  renderActivity();
}

function renderActivity() {
  const root = $("activityRows");
  root.replaceChildren();
  if (!state.activity.length) {
    const row = document.createElement("div");
    row.className = "log-row";
    const type = document.createElement("span");
    type.textContent = "INFO";
    type.className = "success";
    const body = document.createElement("span");
    body.textContent = "No test calls have been made in this tab.";
    const time = document.createElement("span");
    time.textContent = "—";
    row.append(type, body, time);
    root.append(row);
    return;
  }
  for (const entry of state.activity) {
    const row = document.createElement("div");
    row.className = "log-row";
    row.setAttribute("role", "row");
    const type = document.createElement("span");
    type.textContent = entry.type;
    type.className = entry.ok ? "success" : "error";
    const body = document.createElement("span");
    body.textContent = entry.body;
    const time = document.createElement("span");
    time.textContent = entry.time;
    row.append(type, body, time);
    root.append(row);
  }
}

async function apiCall(path, options = {}) {
  const method = options.method || "GET";
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && state.csrfToken) {
    headers.set("X-CSRF-Token", state.csrfToken);
  }
  if (state.googleKey) headers.set("X-Google-API-Key", state.googleKey);
  const started = performance.now();
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: options.body,
      credentials: "same-origin",
    });
  } catch (error) {
    recordActivity("ERROR", `${method} ${path} · network failure`, false);
    throw new ApiError(error.message || "Network request failed");
  }
  const elapsed = Math.round(performance.now() - started);
  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch (_error) {
      payload = null;
    }
    const code = payload?.error?.code || `HTTP_${response.status}`;
    const message = payload?.error?.message || response.statusText || "Request failed";
    recordActivity("ERROR", `${method} ${path} · ${response.status} ${code} · ${elapsed}ms`, false);
    throw new ApiError(message, response.status, code);
  }
  recordActivity("SUCCESS", `${method} ${path} · ${response.status} · ${elapsed}ms`, true);
  if (options.expect === "blob") {
    return { response, data: await response.blob(), elapsed };
  }
  return { response, data: await response.json(), elapsed };
}

function errorText(error) {
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  return error?.message || String(error);
}

function googleOptions() {
  return {
    google_base_url: state.settings.baseUrl,
    google_image_model: state.settings.imageModel,
    google_transcription_model: state.settings.transcriptionModel,
    google_normalization_model: state.settings.normalizationModel,
    timeout_seconds: state.settings.timeout,
    aspect_ratio: state.settings.aspectRatio,
    image_size: state.settings.imageSize,
  };
}

function appendGoogleOptions(form) {
  const options = googleOptions();
  for (const [key, value] of Object.entries(options)) form.append(key, String(value));
}

function requireGoogle(provider, kind) {
  if (provider !== "google") return;
  const serverHasKey = Boolean(state.config?.providers?.google_key_configured_on_server);
  if (!state.googleKey && !serverHasKey) {
    navigate("configuration");
    throw new ApiError("Enter a Google API Key in Configuration first.", 422, "GOOGLE_KEY_REQUIRED");
  }
  if (kind === "image" && !state.settings.imageModel) {
    navigate("configuration");
    throw new ApiError("Enter the Google image model.", 422, "GOOGLE_MODEL_REQUIRED");
  }
  if (kind === "transcription" && !state.settings.transcriptionModel) {
    navigate("configuration");
    throw new ApiError("Enter the Google transcription model.", 422, "GOOGLE_MODEL_REQUIRED");
  }
  if (kind === "normalization" && !state.settings.normalizationModel) {
    navigate("configuration");
    throw new ApiError("Enter the Google normalization model.", 422, "GOOGLE_MODEL_REQUIRED");
  }
}

function requestPreview(path, body) {
  return {
    method: "POST",
    url: `${location.origin}${path}`,
    headers: {
      Cookie: "HttpOnly operator session (browser-managed)",
      "X-CSRF-Token": masked(state.csrfToken),
      "X-Google-API-Key": state.googleKey ? masked(state.googleKey) : "omitted / server fallback",
      "Idempotency-Key": body.idempotency_key,
      "Content-Type": "multipart/form-data; boundary=<browser-generated>",
    },
    multipart: body.multipart,
  };
}

function headerMetadata(response, blob) {
  const names = [
    "x-moonli-run-id",
    "x-moonli-result-sha256",
    "x-moonli-final-output-sha256",
    "x-moonli-archive-contract",
    "x-moonli-pipeline",
    "x-moonli-input-type",
    "x-idempotent-replay",
    "x-moonli-provider",
    "x-moonli-image-provider",
    "x-moonli-transcription-provider",
    "x-moonli-normalization-provider",
    "x-moonli-palette-valid",
    "x-moonli-palette-quantized",
    "x-moonli-quantized",
    "x-moonli-invalid-pixels",
    "x-moonli-snapped-pixels",
    "x-moonli-changed-pixels",
    "x-moonli-cleanup-changed-pixels",
    "x-moonli-cleanup-removed-components",
    "x-moonli-unique-colors-before",
    "x-moonli-unique-colors-after",
    "x-moonli-opaque-pixels",
    "x-moonli-vectorized",
    "x-moonli-vector-runs",
    "x-moonli-used-colors",
    "x-moonli-segmented",
    "x-moonli-used-layers",
    "x-moonli-total-layers",
    "x-moonli-duration-ms",
  ];
  const result = {
    status: `${response.status} ${response.statusText}`.trim(),
    content_type: blob.type || response.headers.get("content-type") || "application/octet-stream",
    bytes: blob.size,
  };
  for (const name of names) {
    const value = response.headers.get(name);
    if (value !== null && value !== "") result[name] = value;
  }
  return result;
}

function responseFilename(response, fallback) {
  const disposition = response.headers.get("content-disposition") || "";
  const match = /filename="?([^";]+)"?/i.exec(disposition);
  return match ? match[1] : fallback;
}

function renderBlobResult(containerId, result, fallbackName, extraActions = []) {
  const container = $(containerId);
  revokeResultUrl(containerId);
  container.replaceChildren();
  container.classList.remove("empty-state");
  const url = URL.createObjectURL(result.data);
  state.objectUrls.set(containerId, url);

  const wrapper = document.createElement("div");
  wrapper.className = "result-media";
  if ((result.data.type || "").startsWith("image/")) {
    const image = document.createElement("img");
    image.src = url;
    image.alt = "Test generation result";
    wrapper.append(image);
  }

  const metadata = headerMetadata(result.response, result.data);
  const list = document.createElement("dl");
  list.className = "result-meta";
  for (const [key, value] of Object.entries(metadata)) {
    const term = document.createElement("dt");
    term.textContent = key;
    const definition = document.createElement("dd");
    definition.textContent = String(value);
    list.append(term, definition);
  }
  wrapper.append(list);

  const actions = document.createElement("div");
  actions.className = "action-row";
  const download = document.createElement("a");
  download.className = "download-link";
  download.href = url;
  download.download = responseFilename(result.response, fallbackName);
  download.textContent = "Download Result";
  actions.append(download);
  for (const action of extraActions) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = action.label;
    button.addEventListener("click", action.handler);
    actions.append(button);
  }
  wrapper.append(actions);
  container.append(wrapper);
}

function populateFileInput(inputId, blob, filename) {
  try {
    const transfer = new DataTransfer();
    transfer.items.add(new File([blob], filename, { type: blob.type || "image/png" }));
    $(inputId).files = transfer.files;
    const destinations = {
      quantizationStageImage: "Palette quantization",
      paletteStageImage: "Palette validation",
      vectorizationStageImage: "Vectorization",
      segmentationStageVector: "Segmentation",
    };
    notice(`File passed to ${destinations[inputId] || "the next stage"}.`);
  } catch (_error) {
    notice("The browser could not pass the file automatically. Download it and select it manually.", "error");
  }
}

async function updateReachability() {
  const box = $("reachabilityBox");
  try {
    const response = await fetch("/health", { cache: "no-store" });
    if (!response.ok) throw new Error("unreachable");
    box.className = "reachability is-reachable";
    $("reachabilityText").textContent = "Service available";
    $("headerStatus").innerHTML = "<i></i> Connected";
    $("headerStatus").style.color = "";
  } catch (_error) {
    box.className = "reachability is-unreachable";
    $("reachabilityText").textContent = "Service unavailable";
    $("headerStatus").innerHTML = "<i></i> Disconnected";
    $("headerStatus").style.color = "var(--danger)";
  }
}

function hydrateConfiguration(config) {
  const saved = readPreferences();
  state.config = config;
  state.production = config.production || null;
  state.defaultTemplates = { ...config.prompt_templates };
  state.templates = { ...config.prompt_templates };
  state.settings = {
    imageProvider: config.providers.image,
    transcriptionProvider: config.providers.transcription,
    normalizationProvider: config.providers.normalization,
    cleanupPasses: config.palette_processing.cleanup_passes ?? 1,
    generationAttempts: config.palette_processing.generation_attempts ?? 3,
    baseUrl: config.google.base_url,
    imageModel: config.google.image_model || "",
    transcriptionModel: config.google.transcription_model || "",
    normalizationModel: config.google.normalization_model || config.google.transcription_model || "",
    timeout: config.google.timeout_seconds || 180,
    aspectRatio: config.google.aspect_ratio || "1:1",
    imageSize: config.google.image_size || "1K",
  };
  const accent = /^#[0-9A-Fa-f]{6}$/.test(saved.accent || "") ? saved.accent.toUpperCase() : "#00A8FF";
  document.documentElement.style.setProperty("--accent", accent);
  $("accentColor").value = accent;

  $("headerEnvironment").textContent = config.environment.toUpperCase();
  $("templateFields").textContent = config.prompt_template_fields.map((field) => `{${field}}`).join(" · ");
  syncConfigurationControls();
  updateGoogleKeyStatus();
  renderProduction();
}

function renderProduction() {
  const production = state.production;
  if (!production) return;
  const key = production.google_key || {};
  const row = $("productionKeyStatusRow");
  if (key.configured) {
    const source = key.source === "volume" ? "Docker volume" : "environment fallback";
    $("productionKeyStatus").textContent = `Key configured · value hidden · ${source}`;
    row.classList.add("is-ready");
  } else {
    $("productionKeyStatus").textContent = "Production key is not configured";
    row.classList.remove("is-ready");
  }
  $("productionKeyStorage").textContent = production.storage === "docker-volume"
    ? "moonli_secrets volume"
    : production.storage || "—";
  for (const request of production.requests || []) {
    const fieldId = PRODUCTION_REQUEST_FIELDS[request.id];
    if (fieldId && $(fieldId)) $(fieldId).value = pretty(request.request);
  }
}

function syncConfigurationControls() {
  $("settingsImageProvider").value = state.settings.imageProvider;
  $("settingsTranscriptionProvider").value = state.settings.transcriptionProvider;
  $("settingsNormalizationProvider").value = state.settings.normalizationProvider;
  $("settingsCleanupPasses").value = state.settings.cleanupPasses;
  $("settingsGenerationAttempts").value = state.settings.generationAttempts;
  $("googleBaseUrl").value = state.settings.baseUrl;
  $("googleTimeout").value = state.settings.timeout;
  $("googleImageModel").value = state.settings.imageModel;
  $("googleTranscriptionModel").value = state.settings.transcriptionModel;
  $("googleNormalizationModel").value = state.settings.normalizationModel;
  $("googleAspectRatio").value = state.settings.aspectRatio;
  $("googleImageSize").value = state.settings.imageSize;
  $("templatePipeline1").value = state.templates["pipeline-1"] || "";
  $("templatePipeline2").value = state.templates["pipeline-2"] || "";
}

function updateGoogleKeyStatus() {
  const row = $("googleKeyStatus")?.parentElement;
  if (!row) return;
  const serverKey = Boolean(state.config?.providers?.google_key_configured_on_server);
  if (state.googleKey) {
    $("googleKeyStatus").textContent = `Tab key configured · ${masked(state.googleKey)}`;
    row.classList.add("is-ready");
  } else if (serverKey) {
    $("googleKeyStatus").textContent = "The server Google API Key will be used";
    row.classList.add("is-ready");
  } else {
    $("googleKeyStatus").textContent = "No key set for this tab";
    row.classList.remove("is-ready");
  }
}

function navigate(view) {
  if (!VIEW_TITLES[view]) return;
  state.activeView = view;
  document.querySelectorAll(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  $("pageTitle").textContent = VIEW_TITLES[view];
  $("sidebar").classList.remove("open");
  $("mobileMenuButton").setAttribute("aria-expanded", "false");
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (view === "overview") refreshOverviewStats();
  if (view === "devices") refreshDevices();
  if (view === "activity") refreshAudit();
}

function togglePipelineInput() {
  const isAudio = $("pipelineInputType").value === "audio";
  $("pipelineTextField").classList.toggle("hidden", isAudio);
  $("pipelineAudioField").classList.toggle("hidden", !isAudio);
}

function openGoogleKeyDialog() {
  state.lastFocused = document.activeElement;
  $("googleKeyInput").value = "";
  $("googleKeyDialog").classList.remove("hidden");
  window.setTimeout(() => $("googleKeyInput").focus(), 0);
}

function closeGoogleKeyDialog() {
  $("googleKeyInput").value = "";
  $("googleKeyDialog").classList.add("hidden");
  state.lastFocused?.focus?.();
}

function trapDialogFocus(event) {
  if (event.key === "Escape") {
    if (!$("googleKeyInput").value) closeGoogleKeyDialog();
    else notice("Select Cancel or apply the entered key.", "error");
    return;
  }
  if (event.key !== "Tab") return;
  const controls = Array.from($("googleKeyDialog").querySelectorAll("button, input")).filter((item) => !item.disabled);
  const first = controls[0];
  const last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

async function handleLogin(event) {
  event.preventDefault();
  const button = $("loginButton");
  const value = $("accessKey").value.trim();
  $("loginError").textContent = "";
  if (!value) {
    $("loginError").textContent = "Enter the Access Key.";
    $("accessKey").focus();
    return;
  }
  setPending(button, true, "Checking...");
  try {
    const response = await fetch("/internal/auth/session", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ access_key: value }),
    });
    const loginData = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiError(
        loginData?.error?.message || response.statusText,
        response.status,
        loginData?.error?.code || `HTTP_${response.status}`,
      );
    }
    state.csrfToken = loginData.csrf_token;
    sessionStorage.setItem("moonli.csrf", state.csrfToken);
    const { data } = await apiCall("/internal/production/config");
    hydrateConfiguration(data);
    $("accessKey").value = "";
    $("loginView").classList.add("hidden");
    $("appShell").classList.remove("hidden");
    navigate("overview");
    notice("Moonli is ready.");
  } catch (error) {
    state.csrfToken = "";
    sessionStorage.removeItem("moonli.csrf");
    $("loginError").textContent = errorText(error);
    $("accessKey").focus();
  } finally {
    setPending(button, false, "Checking...");
  }
}

async function restoreSession() {
  if (!state.csrfToken) return;
  try {
    await apiCall("/internal/auth/session");
    const { data } = await apiCall("/internal/production/config");
    hydrateConfiguration(data);
    $("loginView").classList.add("hidden");
    $("appShell").classList.remove("hidden");
    navigate("overview");
  } catch (_error) {
    clearSecrets();
  }
}

async function handleProductionGoogleKey(event) {
  event.preventDefault();
  const button = $("saveProductionGoogleKey");
  const value = $("productionGoogleKey").value.trim();
  if (!value) return notice("Enter a Google API Key.", "error");
  setPending(button, true, "Saving...");
  try {
    const { data } = await apiCall("/internal/production/google-key", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ google_api_key: value }),
    });
    state.production.google_key = data.google_key;
    state.config.providers.google_key_configured_on_server = Boolean(data.google_key?.configured);
    $("productionGoogleKey").value = "";
    renderProduction();
    updateGoogleKeyStatus();
    notice("Production Google API Key saved to the Docker volume.");
  } catch (error) {
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Saving...");
  }
}

async function handleClearProductionGoogleKey() {
  if (!window.confirm("Delete the production Google API Key? Google generation will stop until a new key is added.")) return;
  try {
    const { data } = await apiCall("/internal/production/google-key", { method: "DELETE" });
    state.production.google_key = data.google_key;
    state.config.providers.google_key_configured_on_server = false;
    renderProduction();
    updateGoogleKeyStatus();
    notice("Production Google API Key deleted.");
  } catch (error) {
    notice(errorText(error), "error");
  }
}

function downloadResponse(result, fallbackName) {
  const url = URL.createObjectURL(result.data);
  const link = document.createElement("a");
  link.href = url;
  link.download = responseFilename(result.response, fallbackName);
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function handleCreateBackup() {
  const button = $("createBackup");
  setPending(button, true, "Creating...");
  try {
    const result = await apiCall("/internal/backups", { method: "POST", expect: "blob" });
    downloadResponse(result, "moonli-backup.zip");
    $("backupResult").textContent = "Snapshot created, verified by the server, and sent to the browser.";
    notice("Snapshot downloaded.");
  } catch (error) {
    $("backupResult").textContent = errorText(error);
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Creating...");
  }
}

function selectedBackupForm() {
  const file = $("restoreBackupFile").files[0];
  if (!file) throw new ApiError("Select a ZIP snapshot.", 422, "BACKUP_REQUIRED");
  const form = new FormData();
  form.append("file", file);
  return form;
}

async function handleInspectBackup() {
  try {
    const { data } = await apiCall("/internal/backups/inspect", {
      method: "POST",
      body: selectedBackupForm(),
    });
    $("backupResult").textContent = pretty(data);
    notice("Snapshot passed inspection.");
  } catch (error) {
    $("backupResult").textContent = errorText(error);
    notice(errorText(error), "error");
  }
}

async function handleRestoreBackup() {
  if (!window.confirm("Restore will replace current run, statistics, device, and audit data and revoke every browser session. Continue?")) return;
  try {
    const { data } = await apiCall("/internal/backups/restore?confirmation=RESTORE", {
      method: "POST",
      body: selectedBackupForm(),
    });
    $("backupResult").textContent = pretty(data);
    notice("Snapshot restored. Sign in again.");
    clearSecrets();
    $("appShell").classList.add("hidden");
    $("loginView").classList.remove("hidden");
  } catch (error) {
    $("backupResult").textContent = errorText(error);
    notice(errorText(error), "error");
  }
}

async function handleRotateAccessKey(event) {
  event.preventDefault();
  const current = $("currentOperatorKey").value;
  const replacement = $("newOperatorKey").value;
  try {
    await apiCall("/internal/auth/rotate-access-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_access_key: current, new_access_key: replacement }),
    });
    $("currentOperatorKey").value = "";
    $("newOperatorKey").value = "";
    clearSecrets();
    $("appShell").classList.add("hidden");
    $("loginView").classList.remove("hidden");
    notice("Access Key changed and all sessions were revoked. Sign in again.");
  } catch (error) {
    notice(errorText(error), "error");
  }
}

async function handleCheckUpdates() {
  try {
    const status = await apiCall("/internal/updates/status");
    let releases = null;
    try {
      releases = (await apiCall("/internal/updates/releases")).data;
    } catch (_error) {
      releases = { unavailable: true };
    }
    const latestJob = status.data.jobs?.[0] || null;
    state.lastUpdateJob = latestJob;
    $("rollbackUpdate").disabled = !latestJob?.rollback_available;
    $("updateResult").textContent = pretty({ updater: status.data, release_catalog: releases });
  } catch (error) {
    $("updateResult").textContent = errorText(error);
    notice(errorText(error), "error");
  }
}

const UPDATE_TERMINAL_STATES = new Set(["COMPLETED", "ROLLED_BACK", "FAILED", "ROLLBACK_FAILED"]);

async function pollUpdateJob(jobId) {
  let connectionFailures = 0;
  for (let attempt = 0; attempt < 180; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
    try {
      const { data } = await apiCall(`/internal/updates/jobs/${encodeURIComponent(jobId)}`);
      connectionFailures = 0;
      state.lastUpdateJob = data;
      $("updateResult").textContent = pretty(data);
      $("rollbackUpdate").disabled = !data.rollback_available;
      if (UPDATE_TERMINAL_STATES.has(data.state)) return data;
    } catch (error) {
      connectionFailures += 1;
      $("updateResult").textContent = `Container temporarily unavailable; waiting for job ${jobId}… (${connectionFailures})`;
      if (connectionFailures >= 30) throw error;
    }
  }
  throw new ApiError("Updater job did not finish within 6 minutes.", 504, "UPDATE_TIMEOUT");
}

async function handleInstallUpdate() {
  if (!window.confirm("Create a backup and ask the local updater to install the latest stable release?")) return;
  const button = $("installUpdate");
  setPending(button, true, "Updating...");
  try {
    const { data } = await apiCall("/internal/updates/install", { method: "POST" });
    state.lastUpdateJob = data;
    $("updateResult").textContent = pretty(data);
    notice("Update job created.");
    if (data.id) await pollUpdateJob(data.id);
  } catch (error) {
    $("updateResult").textContent = errorText(error);
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Updating...");
  }
}

async function handleRollbackUpdate() {
  const job = state.lastUpdateJob;
  if (!job?.id || !job.rollback_available) return;
  if (!window.confirm(`Roll back update job ${job.id} and restore its backup?`)) return;
  const button = $("rollbackUpdate");
  setPending(button, true, "Rollback...");
  try {
    const { data } = await apiCall(
      `/internal/updates/jobs/${encodeURIComponent(job.id)}/rollback`,
      { method: "POST" },
    );
    state.lastUpdateJob = data;
    $("updateResult").textContent = pretty(data);
    await pollUpdateJob(job.id);
  } catch (error) {
    $("updateResult").textContent = errorText(error);
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Rollback...");
  }
}

async function refreshAudit() {
  if (!state.csrfToken) return;
  try {
    const { data } = await apiCall("/internal/operations/audit?limit=200");
    $("auditRows").textContent = data.events.length ? pretty(data) : "Audit is empty.";
  } catch (error) {
    $("auditRows").textContent = errorText(error);
  }
}

async function handleDownloadAudit() {
  try {
    const result = await apiCall("/internal/operations/logs/export", { expect: "blob" });
    downloadResponse(result, "moonli-logs.zip");
  } catch (error) {
    notice(errorText(error), "error");
  }
}

async function handlePipeline(event) {
  event.preventDefault();
  const button = $("runPipeline");
  const inputType = $("pipelineInputType").value;
  const pipeline = $("pipelineTag").value;
  const imageProvider = state.settings.imageProvider;
  const transcriptionProvider = state.settings.transcriptionProvider;
  const normalizationProvider = state.settings.normalizationProvider;
  try {
    requireGoogle(imageProvider, "image");
    if (inputType === "audio") requireGoogle(transcriptionProvider, "transcription");
    requireGoogle(normalizationProvider, "normalization");
    const form = new FormData();
    form.append("type", inputType);
    form.append("pipeline", pipeline);
    form.append("image_provider", imageProvider);
    form.append("transcription_provider", transcriptionProvider);
    form.append("normalization_provider", normalizationProvider);
    form.append("quantization_cleanup_passes", String(state.settings.cleanupPasses));
    form.append("generation_attempts", String(state.settings.generationAttempts));
    if (inputType === "text") {
      const text = $("pipelineText").value.trim();
      if (!text) throw new ApiError("Enter request text.", 422, "INVALID_INPUT");
      form.append("text", text);
    } else {
      const file = $("pipelineAudio").files[0];
      if (!file) throw new ApiError("Select an audio file.", 422, "INVALID_INPUT");
      form.append("audio", file);
    }
    form.append("prompt_template", state.templates[pipeline]);
    appendGoogleOptions(form);
    const idempotency = $("pipelineIdempotency").value;
    const multipart = {
      type: inputType,
      pipeline,
      text_or_audio: inputType === "text" ? $("pipelineText").value : $("pipelineAudio").files[0]?.name,
      image_provider: imageProvider,
      transcription_provider: transcriptionProvider,
      normalization_provider: normalizationProvider,
      prompt_template: `[${state.templates[pipeline].length} chars]`,
      quantization_cleanup_passes: state.settings.cleanupPasses,
      generation_attempts: state.settings.generationAttempts,
      ...googleOptions(),
    };
    $("pipelineRequestPreview").textContent = pretty(requestPreview("/internal/test/pipeline", {
      idempotency_key: idempotency,
      multipart,
    }));
    setPending(button, true, "Running...");
    const result = await apiCall("/internal/test/pipeline", {
      method: "POST",
      headers: { "Idempotency-Key": idempotency },
      body: form,
      expect: "blob",
    });
    renderBlobResult(
      "pipelineResult",
      result,
      `moonli-run-${pipeline}-${inputType}.zip`,
    );
    notice(`Pipeline completed: ${pipeline}.`);
  } catch (error) {
    $("pipelineResult").textContent = errorText(error);
    $("pipelineResult").classList.add("empty-state");
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Running...");
  }
}

async function handleNormalizationStage(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const provider = $("normalizationStageProvider").value;
  const text = $("normalizationStageText").value.trim();
  if (!text) return notice("Enter a request to normalize.", "error");
  state.stageResults.normalization = "";
  toggleButtons(["copyNormalizationResult", "normalizationToPrompt"], false);
  try {
    requireGoogle(provider, "normalization");
    const payload = { provider, text, ...googleOptions() };
    setPending(button, true, "Normalizing...");
    const { data } = await apiCall("/internal/test/normalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("normalizationStageResult").textContent = pretty(data);
    state.stageResults.normalization = data.normalized_text;
    toggleButtons(["copyNormalizationResult", "normalizationToPrompt"], true);
    notice("Normalization completed. Copy the result or pass it to the next stage.");
  } catch (error) {
    $("normalizationStageResult").textContent = errorText(error);
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Normalizing...");
  }
}

async function handlePromptStage(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const pipeline = $("promptStagePipeline").value;
  const payload = {
    pipeline,
    text: $("promptStageText").value.trim(),
  };
  if ($("promptStageTemplate").value === "custom") payload.prompt_template = state.templates[pipeline];
  state.stageResults.prompt = "";
  toggleButtons(["copyPromptResult", "promptToImage"], false);
  setPending(button, true, "Building...");
  try {
    const { data } = await apiCall("/internal/test/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("promptStageResult").textContent = pretty(data);
    state.stageResults.prompt = data.prompt;
    toggleButtons(["copyPromptResult", "promptToImage"], true);
    notice("Prompt built. Copy the result or pass it to the next stage.");
  } catch (error) {
    $("promptStageResult").textContent = errorText(error);
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Building...");
  }
}

async function handleTranscriptionStage(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const provider = $("transcriptionStageProvider").value;
  const file = $("transcriptionStageAudio").files[0];
  if (!file) return notice("Select an audio file.", "error");
  state.stageResults.transcription = "";
  toggleButtons(["copyTranscriptionResult", "transcriptionToNormalization"], false);
  try {
    requireGoogle(provider, "transcription");
    const form = new FormData();
    form.append("provider", provider);
    form.append("audio", file);
    appendGoogleOptions(form);
    setPending(button, true, "Transcribing...");
    const { data } = await apiCall("/internal/test/transcribe", { method: "POST", body: form });
    $("transcriptionStageResult").textContent = pretty(data);
    state.stageResults.transcription = data.transcription;
    toggleButtons(["copyTranscriptionResult", "transcriptionToNormalization"], true);
    notice("Transcription completed. Copy the result or pass it to the next stage.");
  } catch (error) {
    $("transcriptionStageResult").textContent = errorText(error);
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Transcribing...");
  }
}

async function handleImageStage(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const provider = $("imageStageProvider").value;
  try {
    requireGoogle(provider, "image");
    const payload = {
      pipeline: $("imageStagePipeline").value,
      provider,
      prompt: $("imageStagePrompt").value.trim(),
      validate_palette: $("imageStageValidate").checked,
      snap_distance: 0,
      ...googleOptions(),
    };
    setPending(button, true, "Generating...");
    const result = await apiCall("/internal/test/image", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      expect: "blob",
    });
    renderBlobResult("imageStageResult", result, "moonli-stage.png", [
      {
        label: "Pass to Palette Quantization →",
        handler: () => {
          $("quantizationStagePipeline").value = payload.pipeline;
          populateFileInput("quantizationStageImage", result.data, "generated.png");
        },
      },
    ]);
    notice("Image generated.");
  } catch (error) {
    $("imageStageResult").textContent = errorText(error);
    $("imageStageResult").classList.add("empty-state");
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Generating...");
  }
}

async function handleQuantizationStage(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const file = $("quantizationStageImage").files[0];
  if (!file) return notice("Select a source PNG image.", "error");
  const pipeline = $("quantizationStagePipeline").value;
  const form = new FormData();
  form.append("pipeline", pipeline);
  form.append("cleanup_passes", $("quantizationStageCleanup").value);
  form.append("image", file);
  setPending(button, true, "Quantizing...");
  try {
    const result = await apiCall("/internal/test/quantize", { method: "POST", body: form, expect: "blob" });
    const actions = [
      {
        label: "Pass to Strict Validation →",
        handler: () => {
          $("paletteStagePipeline").value = pipeline;
          $("paletteStageSnap").value = "0";
          populateFileInput("paletteStageImage", result.data, "quantized.png");
        },
      },
    ];
    renderBlobResult("quantizationStageResult", result, "moonli-quantized.png", actions);
    notice("PNG quantized to the exact selected-pipeline palette.");
  } catch (error) {
    $("quantizationStageResult").textContent = errorText(error);
    $("quantizationStageResult").classList.add("empty-state");
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Quantizing...");
  }
}

async function handlePaletteStage(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const file = $("paletteStageImage").files[0];
  if (!file) return notice("Select a PNG image.", "error");
  const pipeline = $("paletteStagePipeline").value;
  const form = new FormData();
  form.append("pipeline", pipeline);
  form.append("snap_distance", $("paletteStageSnap").value);
  form.append("image", file);
  state.stageResults.paletteReport = "";
  state.stageResults.paletteFile = null;
  toggleButtons(["copyPaletteResult", "paletteToVectorization"], false);
  setPending(button, true, "Validating...");
  try {
    const { data } = await apiCall("/internal/test/palette", { method: "POST", body: form });
    $("paletteStageResult").textContent = pretty(data);
    state.stageResults.paletteReport = pretty(data);
    state.stageResults.paletteFile = data.valid ? file : null;
    state.stageResults.palettePipeline = pipeline;
    toggleButtons(["copyPaletteResult"], true);
    toggleButtons(["paletteToVectorization"], data.valid);
    notice(data.valid ? "The palette matches the contract." : "The image failed palette validation.", data.valid ? "success" : "error");
  } catch (error) {
    $("paletteStageResult").textContent = errorText(error);
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Validating...");
  }
}

async function handleVectorizationStage(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const file = $("vectorizationStageImage").files[0];
  if (!file) return notice("Select a strict-palette PNG.", "error");
  const pipeline = $("vectorizationStagePipeline").value;
  const form = new FormData();
  form.append("pipeline", pipeline);
  form.append("image", file);
  setPending(button, true, "Vectorizing...");
  try {
    const result = await apiCall("/internal/test/vectorize", { method: "POST", body: form, expect: "blob" });
    renderBlobResult("vectorizationStageResult", result, "moonli-vector.svg", [
      {
        label: "Pass to Segmentation →",
        handler: () => {
          $("segmentationStagePipeline").value = pipeline;
          populateFileInput("segmentationStageVector", result.data, "moonli-vector.svg");
        },
      },
    ]);
    notice("SVG created and ready for segmentation.");
  } catch (error) {
    $("vectorizationStageResult").textContent = errorText(error);
    $("vectorizationStageResult").classList.add("empty-state");
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Vectorizing...");
  }
}

async function handleSegmentationStage(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const file = $("segmentationStageVector").files[0];
  if (!file) return notice("Select a Moonli vector SVG.", "error");
  const form = new FormData();
  form.append("pipeline", $("segmentationStagePipeline").value);
  form.append("vector", file);
  setPending(button, true, "Segmenting...");
  try {
    const result = await apiCall("/internal/test/segment", { method: "POST", body: form, expect: "blob" });
    renderBlobResult("segmentationStageResult", result, "moonli-vector-layers.zip");
    notice("Vector split into palette layers.");
  } catch (error) {
    $("segmentationStageResult").textContent = errorText(error);
    $("segmentationStageResult").classList.add("empty-state");
    notice(errorText(error), "error");
  } finally {
    setPending(button, false, "Segmenting...");
  }
}

function serverSettingsPayload() {
  return {
    image_provider: state.settings.imageProvider,
    transcription_provider: state.settings.transcriptionProvider,
    normalization_provider: state.settings.normalizationProvider,
    google_api_base_url: state.settings.baseUrl,
    google_image_model: state.settings.imageModel,
    google_transcription_model: state.settings.transcriptionModel,
    google_normalization_model: state.settings.normalizationModel,
    google_timeout_seconds: state.settings.timeout,
    google_image_aspect_ratio: state.settings.aspectRatio,
    google_image_size: state.settings.imageSize,
    palette_cleanup_passes: state.settings.cleanupPasses,
    palette_generation_attempts: state.settings.generationAttempts,
    prompt_templates: state.templates,
  };
}

async function persistServerSettings() {
  const { data } = await apiCall("/internal/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(serverSettingsPayload()),
  });
  return data;
}

async function handleGoogleSettings(event) {
  event.preventDefault();
  const baseUrl = $("googleBaseUrl").value.trim();
  let parsed;
  try {
    parsed = new URL(baseUrl);
  } catch (_error) {
    return notice("The Google Base URL is invalid.", "error");
  }
  if (parsed.protocol !== "https:" || !/(^|\.)googleapis\.com$/i.test(parsed.hostname)) {
    return notice("Only an HTTPS endpoint on googleapis.com is allowed.", "error");
  }
  state.settings = {
    imageProvider: $("settingsImageProvider").value,
    transcriptionProvider: $("settingsTranscriptionProvider").value,
    normalizationProvider: $("settingsNormalizationProvider").value,
    cleanupPasses: Number($("settingsCleanupPasses").value),
    generationAttempts: Number($("settingsGenerationAttempts").value),
    baseUrl: baseUrl.replace(/\/$/, ""),
    imageModel: $("googleImageModel").value.trim(),
    transcriptionModel: $("googleTranscriptionModel").value.trim(),
    normalizationModel: $("googleNormalizationModel").value.trim(),
    timeout: Number($("googleTimeout").value),
    aspectRatio: $("googleAspectRatio").value,
    imageSize: $("googleImageSize").value,
  };
  try {
    await persistServerSettings();
    notice("Full-run and Google settings saved to the server and applied.");
  } catch (error) {
    notice(errorText(error), "error");
  }
}

async function handleTemplates(event) {
  event.preventDefault();
  const first = $("templatePipeline1").value.trim();
  const second = $("templatePipeline2").value.trim();
  if (!first || !second) return notice("Both prompt templates must be filled in.", "error");
  state.templates = { "pipeline-1": first, "pipeline-2": second };
  try {
    await persistServerSettings();
    notice("Prompt templates saved to the server and applied.");
  } catch (error) {
    notice(errorText(error), "error");
  }
}

function filterStages() {
  const query = $("stageSearch").value.trim().toLocaleLowerCase();
  let visible = 0;
  document.querySelectorAll(".stage-card").forEach((card) => {
    const match = !query || card.dataset.search.includes(query) || card.textContent.toLocaleLowerCase().includes(query);
    card.classList.toggle("is-filtered", !match);
    if (match) visible += 1;
  });
  $("stageCount").textContent = `${visible} of 8 stages`;
}

function bindEvents() {
  $("loginForm").addEventListener("submit", handleLogin);
  $("pipelineForm").addEventListener("submit", handlePipeline);
  $("normalizationStageForm").addEventListener("submit", handleNormalizationStage);
  $("promptStageForm").addEventListener("submit", handlePromptStage);
  $("transcriptionStageForm").addEventListener("submit", handleTranscriptionStage);
  $("imageStageForm").addEventListener("submit", handleImageStage);
  $("quantizationStageForm").addEventListener("submit", handleQuantizationStage);
  $("paletteStageForm").addEventListener("submit", handlePaletteStage);
  $("vectorizationStageForm").addEventListener("submit", handleVectorizationStage);
  $("segmentationStageForm").addEventListener("submit", handleSegmentationStage);
  $("copyTranscriptionResult").addEventListener("click", () => {
    copyTextResult(state.stageResults.transcription, "Transcription text");
  });
  $("transcriptionToNormalization").addEventListener("click", () => {
    $("normalizationStageText").value = state.stageResults.transcription;
    $("normalizationStageText").focus();
    notice("Transcription passed to normalization.");
  });
  $("copyNormalizationResult").addEventListener("click", () => {
    copyTextResult(state.stageResults.normalization, "Normalized request");
  });
  $("normalizationToPrompt").addEventListener("click", () => {
    $("promptStageText").value = state.stageResults.normalization;
    $("promptStageText").focus();
    notice("Normalized request passed to Prompt Builder.");
  });
  $("copyPromptResult").addEventListener("click", () => {
    copyTextResult(state.stageResults.prompt, "Prompt");
  });
  $("promptToImage").addEventListener("click", () => {
    $("imageStagePrompt").value = state.stageResults.prompt;
    $("imageStagePipeline").value = $("promptStagePipeline").value;
    $("imageStagePrompt").focus();
    notice("Prompt passed to image generation.");
  });
  $("copyPaletteResult").addEventListener("click", () => {
    copyTextResult(state.stageResults.paletteReport, "Validation report");
  });
  $("paletteToVectorization").addEventListener("click", () => {
    if (!state.stageResults.paletteFile) return notice("Palette validation did not pass.", "error");
    $("vectorizationStagePipeline").value = state.stageResults.palettePipeline;
    populateFileInput(
      "vectorizationStageImage",
      state.stageResults.paletteFile,
      "palette-valid.png",
    );
  });
  $("googleSettingsForm").addEventListener("submit", handleGoogleSettings);
  $("productionGoogleKeyForm").addEventListener("submit", handleProductionGoogleKey);
  $("clearProductionGoogleKey").addEventListener("click", handleClearProductionGoogleKey);
  $("rotateAccessKeyForm").addEventListener("submit", handleRotateAccessKey);
  $("createBackup").addEventListener("click", handleCreateBackup);
  $("inspectBackup").addEventListener("click", handleInspectBackup);
  $("restoreBackup").addEventListener("click", handleRestoreBackup);
  $("checkUpdates").addEventListener("click", handleCheckUpdates);
  $("installUpdate").addEventListener("click", handleInstallUpdate);
  $("rollbackUpdate").addEventListener("click", handleRollbackUpdate);
  $("refreshAudit").addEventListener("click", refreshAudit);
  $("refreshDevices").addEventListener("click", refreshDevices);
  $("downloadAudit").addEventListener("click", handleDownloadAudit);
  $("templatesForm").addEventListener("submit", handleTemplates);
  $("pipelineInputType").addEventListener("change", togglePipelineInput);
  $("newIdempotency").addEventListener("click", () => { $("pipelineIdempotency").value = uuid(); });
  $("stageSearch").addEventListener("input", filterStages);
  $("clearStageSearch").addEventListener("click", () => { $("stageSearch").value = ""; filterStages(); });
  $("clearActivity").addEventListener("click", () => { state.activity = []; renderActivity(); });
  $("openGoogleKeyDialog").addEventListener("click", openGoogleKeyDialog);
  $("closeGoogleKeyDialog").addEventListener("click", closeGoogleKeyDialog);
  $("cancelGoogleKeyDialog").addEventListener("click", closeGoogleKeyDialog);
  $("googleKeyDialog").addEventListener("keydown", trapDialogFocus);
  $("googleKeyForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = $("googleKeyInput").value.trim();
    if (!value) return;
    state.googleKey = value;
    closeGoogleKeyDialog();
    updateGoogleKeyStatus();
    notice("Google API Key added to the current tab's memory.");
  });
  $("clearGoogleKey").addEventListener("click", () => {
    state.googleKey = "";
    $("googleKeyInput").value = "";
    updateGoogleKeyStatus();
    notice("Google API Key removed from the tab's memory.");
  });
  $("resetTemplates").addEventListener("click", () => {
    state.templates = { ...state.defaultTemplates };
    syncConfigurationControls();
    notice("Default templates restored in the form. Select Apply Templates to save them.");
  });
  $("accentColor").addEventListener("input", (event) => {
    const value = event.target.value.trim();
    if (/^#[0-9A-Fa-f]{6}$/.test(value)) document.documentElement.style.setProperty("--accent", value.toUpperCase());
  });
  $("resetAccent").addEventListener("click", () => {
    $("accentColor").value = "#00A8FF";
    document.documentElement.style.setProperty("--accent", "#00A8FF");
  });
  $("applyAccent").addEventListener("click", () => {
    const value = $("accentColor").value.trim().toUpperCase();
    if (!/^#[0-9A-F]{6}$/.test(value)) return notice("Use the #RRGGBB format.", "error");
    document.documentElement.style.setProperty("--accent", value);
    savePreferences();
    notice("Accent color applied.");
  });
  $("mobileMenuButton").addEventListener("click", () => {
    const open = $("sidebar").classList.toggle("open");
    $("mobileMenuButton").setAttribute("aria-expanded", String(open));
  });
  $("logoutButton").addEventListener("click", () => {
    apiCall("/internal/auth/session", { method: "DELETE" }).catch(() => {}).finally(() => {
      clearSecrets();
    });
    for (const id of state.objectUrls.keys()) revokeResultUrl(id);
    state.activity = [];
    renderActivity();
    $("appShell").classList.add("hidden");
    $("loginView").classList.remove("hidden");
    $("loginError").textContent = "";
    $("accessKey").focus();
  });
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
  document.querySelectorAll("[data-go]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.go)));
}

applyStoredAccent();
bindEvents();
togglePipelineInput();
filterStages();
renderActivity();
$("pipelineIdempotency").value = uuid();
updateReachability();
restoreSession();
window.setInterval(updateReachability, 30000);
window.setInterval(refreshOverviewStats, 15000);
window.setInterval(() => {
  if (document.visibilityState === "visible" && state.activeView === "activity") refreshAudit();
}, 5000);
