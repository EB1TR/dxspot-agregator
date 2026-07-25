const commonSourceRows = document.querySelector("#common-source-rows");
const clientRows = document.querySelector("#client-rows");
const connectionStatus = document.querySelector("#connection-status");
const liveStatus = document.querySelector(".live-status");
const activitySelect = document.querySelector("#activity-select");
const activityList = document.querySelector("#activity-list");

const RATE_HISTORY_SECONDS = 600;
const rateHistories = new Map();
const chartRegistry = new Map();
let fallbackTimer = null;
let latestState = null;
let activePanelKey = "activity";
let streamRequestIdentifier = 0;
let panelOptionsSignature = "";

function formatAge(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) {
    return `${minutes}m${String(remainder).padStart(2, "0")}s`;
  }
  const hours = Math.floor(minutes / 60);
  return `${hours}h${String(minutes % 60).padStart(2, "0")}m`;
}

function formatNumber(value) {
  return new Intl.NumberFormat("es-ES").format(value ?? 0);
}

function formatActivityTimestamp(value) {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return "[--:--:--.---]";
  const twoDigits = (part) => String(part).padStart(2, "0");
  return (
    `[${twoDigits(timestamp.getHours())}:`
    + `${twoDigits(timestamp.getMinutes())}:`
    + `${twoDigits(timestamp.getSeconds())}.`
    + `${String(timestamp.getMilliseconds()).padStart(3, "0")}]`
  );
}

function stateClass(state) {
  const normalized = (state || "").toUpperCase();
  if (normalized === "CONECTADO" || normalized === "ACTIVO") {
    return "connected";
  }
  if (normalized === "DESCONECTADO") return "disconnected";
  if (normalized === "DESACTIVADO") return "disabled";
  return "pending";
}

function chartColor(name) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
}

function niceCeiling(value) {
  if (value <= 1) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = value / magnitude;
  const step =
    normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return step * magnitude;
}

function formatAxisValue(value) {
  if (value >= 1000) {
    const scaled = value / 1000;
    return `${scaled >= 10 ? scaled.toFixed(0) : scaled.toFixed(1)}k`;
  }
  return String(Math.round(value));
}

function updateHistory(key, rate, providedHistory, generatedAt) {
  if (!rateHistories.has(key)) rateHistories.set(key, []);
  const history = rateHistories.get(key);
  if (Array.isArray(providedHistory)) {
    history.splice(
      0,
      history.length,
      ...providedHistory.map(([timestamp, value]) => ({ timestamp, value })),
    );
  }
  const timestamp = Date.parse(generatedAt) / 1000;
  const lastSample = history.at(-1);
  if (
    lastSample
    && Math.floor(lastSample.timestamp) === Math.floor(timestamp)
  ) {
    lastSample.timestamp = timestamp;
    lastSample.value = rate;
  } else {
    history.push({ timestamp, value: rate });
  }
  const oldestTimestamp = timestamp - RATE_HISTORY_SECONDS;
  while (history.length && history[0].timestamp <= oldestTimestamp) {
    history.shift();
  }
  return history;
}

function drawSeries(context, samples, currentTime, area, color, maximum) {
  const oldestTimestamp = currentTime - RATE_HISTORY_SECONDS;
  const segments = [];
  let segment = [];
  samples.forEach((sample, index) => {
    const x =
      area.x
      + ((sample.timestamp - oldestTimestamp) / RATE_HISTORY_SECONDS)
      * area.width;
    const y =
      area.y + area.height - (sample.value / maximum) * area.height;
    const previous = samples[index - 1];
    if (index === 0 || sample.timestamp - previous.timestamp > 5) {
      if (segment.length) segments.push(segment);
      segment = [];
    }
    segment.push({ x, y });
  });
  if (segment.length) segments.push(segment);

  context.save();
  context.fillStyle = color;
  context.globalAlpha = 0.25;
  for (const points of segments) {
    context.beginPath();
    context.moveTo(points[0].x, area.y + area.height);
    context.lineTo(points[0].x, points[0].y);
    for (const point of points.slice(1)) {
      context.lineTo(point.x, point.y);
    }
    context.lineTo(points.at(-1).x, area.y + area.height);
    context.closePath();
    context.fill();
  }
  context.restore();

  context.beginPath();
  for (const points of segments) {
    context.moveTo(points[0].x, points[0].y);
    for (const point of points.slice(1)) {
      context.lineTo(point.x, point.y);
    }
  }
  context.save();
  context.strokeStyle = color;
  context.lineWidth = 2.25;
  context.lineJoin = "round";
  context.lineCap = "round";
  context.shadowColor = color;
  context.shadowBlur = 4;
  context.stroke();
  context.restore();
  const lastPoint = segments.at(-1)?.at(-1);
  if (lastPoint) {
    context.beginPath();
    context.arc(lastPoint.x, lastPoint.y, 2.4, 0, Math.PI * 2);
    context.fillStyle = color;
    context.fill();
  }
}

function drawChart(canvas, descriptor) {
  const bounds = canvas.getBoundingClientRect();
  if (!bounds.width || !bounds.height) return;
  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(bounds.width * pixelRatio);
  canvas.height = Math.round(bounds.height * pixelRatio);
  const context = canvas.getContext("2d");
  context.scale(pixelRatio, pixelRatio);
  context.clearRect(0, 0, bounds.width, bounds.height);

  const allValues = descriptor.series.flatMap((serie) =>
    serie.samples.map((sample) => sample.value)
  );
  const maximum = niceCeiling(Math.max(0, ...allValues));
  const plot = {
    x: 32,
    y: 11,
    width: Math.max(1, bounds.width - 38),
    height: Math.max(1, bounds.height - 28),
  };
  const gridColor = chartColor("--line-soft");
  const labelColor = chartColor("--muted");

  context.fillStyle = labelColor;
  context.font = '12px "Avenir Next", Avenir, "Segoe UI", sans-serif';
  context.textBaseline = "middle";
  context.textAlign = "right";
  context.strokeStyle = gridColor;
  context.lineWidth = 1;
  for (const ratio of [0, 0.5, 1]) {
    const y = plot.y + ratio * plot.height;
    context.fillText(
      formatAxisValue(maximum * (1 - ratio)),
      plot.x - 5,
      y,
    );
    context.beginPath();
    context.moveTo(plot.x, y);
    context.lineTo(plot.x + plot.width, y);
    context.stroke();
  }
  for (const ratio of [0, 0.5, 1]) {
    const x = plot.x + ratio * plot.width;
    context.beginPath();
    context.moveTo(x, plot.y);
    context.lineTo(x, plot.y + plot.height);
    context.stroke();
  }

  descriptor.series.forEach((serie) => {
    drawSeries(
      context,
      serie.samples,
      descriptor.currentTime,
      plot,
      chartColor(serie.color),
      maximum,
    );
  });

  context.fillStyle = labelColor;
  context.textBaseline = "bottom";
  context.textAlign = "left";
  context.fillText("−10m", plot.x, bounds.height);
  context.textAlign = "center";
  context.fillText("−5m", plot.x + plot.width / 2, bounds.height);
  context.textAlign = "right";
  context.fillText("ahora", plot.x + plot.width, bounds.height);
  canvas.setAttribute("aria-label", descriptor.label);
}

function registerChart(canvas, descriptor) {
  chartRegistry.set(canvas, descriptor);
  drawChart(canvas, descriptor);
}

function createStateBadge(state, error = "") {
  const badge = document.createElement("span");
  badge.className = `state-badge ${stateClass(state)}`;
  badge.textContent = state;
  if (error) badge.title = error;
  return badge;
}

function createMetricList(metrics) {
  const list = document.createElement("dl");
  list.className = "metric-list";
  for (const metric of metrics) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = metric.label;
    const value = document.createElement("dd");
    value.textContent = metric.value;
    if (metric.className) value.className = metric.className;
    row.append(term, value);
    list.append(row);
  }
  return list;
}

function createCardChart(series, generatedAt, label, legend = null) {
  const wrapper = document.createElement("div");
  wrapper.className = "card-chart";
  if (legend) {
    const legendNode = document.createElement("div");
    legendNode.className = "chart-legend";
    for (const item of legend) {
      const marker = document.createElement("span");
      marker.className = item.className;
      marker.textContent = item.label;
      legendNode.append(marker);
    }
    wrapper.append(legendNode);
  }
  const canvas = document.createElement("canvas");
  canvas.setAttribute("role", "img");
  wrapper.append(canvas);
  registerChart(canvas, {
    currentTime: Date.parse(generatedAt) / 1000,
    series,
    label,
  });
  return wrapper;
}

function createMetricCard({
  title,
  subtitle,
  state,
  error,
  metrics,
  series,
  generatedAt,
  chartLabel,
  legend = null,
}) {
  const card = document.createElement("article");
  card.className = "metric-card";
  const header = document.createElement("header");
  header.className = "metric-card-header";
  const identity = document.createElement("div");
  const heading = document.createElement("h3");
  heading.textContent = title;
  const secondary = document.createElement("p");
  secondary.textContent = subtitle;
  identity.append(heading, secondary);
  header.append(identity, createStateBadge(state, error));
  card.append(
    header,
    createMetricList(metrics),
    createCardChart(series, generatedAt, chartLabel, legend),
  );
  return card;
}

function renderCommonSources(items, generatedAt) {
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const history = updateHistory(
      `common:${item.key}`,
      item.line_rate,
      item.rate_history,
      generatedAt,
    );
    fragment.append(
      createMetricCard({
        title: item.label,
        subtitle: item.address,
        state: item.state,
        error: item.last_error,
        metrics: [
          {
            label: "Líneas/min",
            value: formatNumber(item.line_rate),
            className: "metric-cyan",
          },
          {
            label: "Última",
            value: formatAge(item.last_activity_seconds),
          },
          {
            label: "Reconexiones",
            value: formatNumber(item.reconnects),
          },
        ],
        series: [{ samples: history, color: "--cyan" }],
        generatedAt,
        chartLabel:
          `${item.label}: líneas por minuto durante los últimos diez minutos`,
      }),
    );
  }
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-card-grid";
    empty.textContent = "No hay fuentes comunes configuradas";
    fragment.append(empty);
  }
  commonSourceRows.replaceChildren(fragment);
}

function renderClients(clients, generatedAt) {
  const fragment = document.createDocumentFragment();
  for (const client of clients) {
    const deliveryHistory = updateHistory(
      `delivery:${client.key}`,
      client.delivery.line_rate,
      client.delivery.rate_history,
      generatedAt,
    );
    fragment.append(
      createMetricCard({
        title: client.callsign,
        subtitle: client.dxspot_login,
        state: client.connection.state,
        error: client.connection.last_error,
        metrics: [
          {
            label: "Entrega/min",
            value: formatNumber(client.delivery.line_rate),
            className: "metric-green",
          },
          {
            label: "Última",
            value: formatAge(client.connection.last_activity_seconds),
          },
          {
            label: "Reconexiones",
            value: formatNumber(client.connection.reconnects),
          },
          {
            label: "Cola",
            value:
              `${formatNumber(client.queue_size)}/`
              + `${formatNumber(client.queue_capacity)}`,
            className: client.queue_size ? "metric-warning" : "",
          },
        ],
        series: [{ samples: deliveryHistory, color: "--green" }],
        generatedAt,
        chartLabel:
          `${client.callsign}: entrega general final `
          + "durante los últimos diez minutos",
      }),
    );
  }
  if (!clients.length) {
    const empty = document.createElement("p");
    empty.className = "empty-card-grid";
    empty.textContent = "Sin clientes conectados";
    fragment.append(empty);
  }
  clientRows.replaceChildren(fragment);
}

function eventClass(message) {
  const normalized = message.toLocaleLowerCase("es-ES");
  if (
    normalized.includes("error")
    || normalized.includes("fall")
    || normalized.includes("rechaz")
    || normalized.includes("descart")
    || normalized.includes("desconect")
    || normalized.includes("cerró")
  ) {
    return "event-error";
  }
  if (
    normalized.includes("conectado")
    || normalized.includes("escuchando")
    || normalized.includes("entidades")
  ) {
    return "event-success";
  }
  return "event-info";
}

function renderStructuredEntries(events, emptyText, classify = true) {
  const fragment = document.createDocumentFragment();
  for (const event of events) {
    const entry = document.createElement("li");
    entry.className = classify ? eventClass(event.message) : "stream-line";
    const timestamp = document.createElement("time");
    timestamp.dateTime = event.timestamp;
    timestamp.textContent = formatActivityTimestamp(event.timestamp);
    const message = document.createElement("span");
    message.textContent = event.message;
    entry.append(timestamp, message);
    fragment.append(entry);
  }
  if (!events.length) {
    const entry = document.createElement("li");
    entry.className = "event-info";
    entry.append(
      Object.assign(document.createElement("time"), {
        textContent: "[--:--:--.---]",
      }),
      Object.assign(document.createElement("span"), { textContent: emptyText }),
    );
    fragment.append(entry);
  }
  activityList.replaceChildren(fragment);
}

function panelItems(state) {
  return [
    { key: "activity", label: "ACTIVIDAD" },
    ...(state.streams || []),
  ];
}

function renderPanelSelect(state) {
  const items = panelItems(state);
  if (!items.some((item) => item.key === activePanelKey)) {
    activePanelKey = "activity";
  }
  const signature = items.map(
    (item) => `${item.key}:${item.label}`,
  ).join("|");
  if (signature === panelOptionsSignature) {
    activitySelect.value = activePanelKey;
    return;
  }
  panelOptionsSignature = signature;
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.key;
    option.textContent = item.label;
    fragment.append(option);
  }
  activitySelect.replaceChildren(fragment);
  activitySelect.value = activePanelKey;
}

function preserveActivityScroll(render) {
  const previousScrollTop = activityList.scrollTop;
  const atBottom =
    activityList.scrollHeight
    - activityList.scrollTop
    - activityList.clientHeight
    < 24;
  render();
  activityList.scrollTop = atBottom
    ? activityList.scrollHeight
    : previousScrollTop;
}

async function renderSelectedPanel(state) {
  if (activePanelKey === "activity") {
    streamRequestIdentifier += 1;
    preserveActivityScroll(() => {
      renderStructuredEntries(
        state.events,
        "Esperando eventos del sistema…",
      );
    });
    return;
  }
  const option = (state.streams || []).find(
    (item) => item.key === activePanelKey,
  );
  if (!option) return;
  const requestIdentifier = ++streamRequestIdentifier;
  try {
    const response = await fetch(
      `/api/stream?key=${encodeURIComponent(activePanelKey)}`,
      { cache: "no-store" },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const stream = await response.json();
    if (
      requestIdentifier !== streamRequestIdentifier
      || stream.key !== activePanelKey
    ) return;
    preserveActivityScroll(() => {
      renderStructuredEntries(
        stream.entries,
        "Esperando datos de este stream…",
        false,
      );
    });
  } catch {
    if (requestIdentifier !== streamRequestIdentifier) return;
    renderStructuredEntries(
      [],
      "Stream temporalmente no disponible",
      false,
    );
  }
}

function selectPanel(key) {
  activePanelKey = key;
  if (!latestState) return;
  renderPanelSelect(latestState);
  renderSelectedPanel(latestState);
}

activitySelect.addEventListener("change", () => {
  selectPanel(activitySelect.value);
});

function renderActivity(state) {
  renderPanelSelect(state);
  renderSelectedPanel(state);
}

function renderHeader(application) {
  document.querySelector("#version").textContent = `v${application.version}`;
  document.querySelector("#country-version").textContent =
    application.country_version;
  document.querySelector("#country-version").title = application.country_status;
  document.querySelector("#updated-at").textContent =
    new Date(application.generated_at).toLocaleTimeString("es-ES");
  document.querySelector("#uptime").textContent =
    `UP ${formatAge(application.uptime_seconds)}`;
  document.querySelector("#client-count").textContent =
    `${application.clients} ${application.clients === 1 ? "cliente" : "clientes"}`;
}

function renderState(state) {
  latestState = state;
  chartRegistry.clear();
  renderHeader(state.application);
  renderCommonSources(
    state.common_sources || [],
    state.application.generated_at,
  );
  renderClients(state.clients || [], state.application.generated_at);
  renderActivity(state);
  window.requestAnimationFrame(() => {
    for (const [canvas, descriptor] of chartRegistry) {
      drawChart(canvas, descriptor);
    }
  });
  connectionStatus.textContent = "En directo";
  liveStatus.classList.remove("offline");
}

async function refresh() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderState(await response.json());
  } catch {
    connectionStatus.textContent = "Sin conexión";
    liveStatus.classList.add("offline");
  }
}

function startFallback() {
  if (fallbackTimer !== null) return;
  refresh();
  fallbackTimer = window.setInterval(refresh, 3000);
}

function stopFallback() {
  if (fallbackTimer === null) return;
  window.clearInterval(fallbackTimer);
  fallbackTimer = null;
}

function connectEvents() {
  if (!("EventSource" in window)) {
    startFallback();
    return;
  }
  const events = new EventSource("/api/events");
  events.addEventListener("state", (event) => {
    try {
      renderState(JSON.parse(event.data));
    } catch {
      startFallback();
    }
  });
  events.onopen = stopFallback;
  events.onerror = () => {
    connectionStatus.textContent = "Reconectando";
    liveStatus.classList.add("offline");
    startFallback();
  };
}

refresh().finally(connectEvents);
window.addEventListener("resize", () => {
  for (const [canvas, descriptor] of chartRegistry) {
    drawChart(canvas, descriptor);
  }
});
