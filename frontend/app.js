/* HDF5 measurement import — vanilla JS SPA, no build step. */
"use strict";

const API = "/api";
const SERIES_COLORS = [
  "var(--series-1)", "var(--series-2)", "var(--series-3)",
  "var(--series-4)", "var(--series-5)",
];

const state = {
  files: [],
  currentFile: null,          // file row
  tree: null,                 // raw tree payload
  suggestions: {},            // dataset path -> suggested time config
  currentNode: null,          // selected dataset node (from tree)
  datasetRow: null,           // registered dataset row from DB
  preview: null,              // last preview payload
  previewChannels: [],        // channel indices used in preview
  readings: [],               // current readings-table rows (for the modal)
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(API + path, {
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch (_) {}
    const msg = typeof detail === "object" && detail !== null
      ? JSON.stringify(detail) : String(detail);
    throw new Error(msg);
  }
  return res.json();
}

/* ====================================================================== */
/* theme                                                                   */
/* ====================================================================== */

$("theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const next = root.dataset.theme === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  if (state.preview) renderChart();
});

/* ====================================================================== */
/* files + tree                                                            */
/* ====================================================================== */

async function loadFiles() {
  state.files = await api("/files");
  const box = $("file-list");
  box.innerHTML = "";
  for (const f of state.files) {
    const div = document.createElement("div");
    div.className = "file-item";
    div.dataset.id = f.id;
    div.innerHTML = `<div class="fname">${escapeHtml(f.filename)}</div>
      <div class="fmeta">#${f.id} · ${formatBytes(f.size_bytes)} · ${f.uploaded_at.slice(0, 19).replace("T", " ")}</div>`;
    div.addEventListener("click", () => selectFile(f.id));
    box.appendChild(div);
  }
  if (state.currentFile) {
    const el = box.querySelector(`[data-id="${state.currentFile.id}"]`);
    if (el) el.classList.add("active");
  }
}

async function selectFile(fileId) {
  const data = await api(`/files/${fileId}`);
  state.currentFile = data.file;
  state.tree = data.tree;
  state.suggestions = data.time_suggestions || {};
  state.currentNode = null;
  state.datasetRow = null;
  state.preview = null;
  document.querySelectorAll(".file-item").forEach((el) =>
    el.classList.toggle("active", Number(el.dataset.id) === fileId));
  renderTree();
  $("dataset-detail").hidden = true;
  $("dataset-empty").hidden = false;
}

function renderTree() {
  const root = $("tree");
  root.innerHTML = "";
  root.appendChild(renderNode(state.tree, true));
}

function renderNode(node, expandedDefault = false) {
  const wrap = document.createElement("div");
  if (node.type === "group" || node.type === "broken_link") {
    const row = document.createElement("div");
    row.className = "node";
    const icon = node.type === "broken_link" ? "⚠" : "📁";
    row.innerHTML = `<span class="icon">${icon}</span><span class="nm">${escapeHtml(node.name)}</span>`;
    const ul = document.createElement("ul");
    ul.hidden = !expandedDefault;
    row.addEventListener("click", () => { ul.hidden = !ul.hidden; });
    wrap.append(row, ul);
    for (const child of node.children || []) ul.appendChild(renderNode(child));
    if (node.issue) row.title = node.issue;
  } else {
    const row = document.createElement("div");
    row.className = "node dataset-node";
    row.dataset.path = node.path;
    row.innerHTML = `<span class="icon">📊</span>
      <span><span class="nm">${escapeHtml(node.name)}</span><br/>
      <span class="path">${escapeHtml(node.path)} · [${node.shape.join(" × ")}]</span></span>`;
    row.addEventListener("click", () => selectDataset(node));
    wrap.appendChild(row);
  }
  return wrap;
}

async function selectDataset(node) {
  document.querySelectorAll(".dataset-node").forEach((el) =>
    el.classList.toggle("active", el.dataset.path === node.path));
  state.currentNode = node;
  state.preview = null;
  $("dataset-empty").hidden = true;
  $("dataset-detail").hidden = false;

  const datasets = await api(`/datasets?file_id=${state.currentFile.id}`);
  state.datasetRow = datasets.find((d) => d.dataset_path === node.path) || null;
  renderDatasetDetail(node);
  await refreshJobs();
}

/* ====================================================================== */
/* dataset detail + config form                                            */
/* ====================================================================== */

function renderDatasetDetail(node) {
  const row = state.datasetRow;
  const status = row?.status || "active";
  $("ds-title").textContent = node.path;
  const badge = $("ds-status");
  badge.textContent = status === "paused" ? "已暂停" : "可用";
  badge.className = `badge ${status}`;
  const treeNode = document.querySelector(`.dataset-node[data-path="${cssEscape(node.path)}"]`);
  if (treeNode) treeNode.classList.toggle("paused", status === "paused");

  const problems = row?.issues || [];
  const issues = $("ds-issues");
  const nonMeasurable = Boolean(node.issue);
  if (problems.length || nonMeasurable) {
    issues.hidden = false;
    const items = [
      ...(nonMeasurable ? [node.issue] : []),
      ...problems,
    ];
    issues.innerHTML = `<b>${nonMeasurable ? "该数组不能作为测量数据导入"
      : "阻断问题（该数据集已暂停，其他数据集不受影响）"}</b>
      <ul>${items.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`;
  } else issues.hidden = true;
  $("btn-preview").disabled = nonMeasurable;
  $("btn-import").disabled = nonMeasurable;

  $("ds-meta").innerHTML = [
    ["形状", `[${node.shape.join(" × ")}]`],
    ["数据类型", node.dtype],
    ["分块", node.chunks ? `[${node.chunks.join(" × ")}]` : "连续存储"],
    ["压缩", node.compression || "无"],
    ["填充值", node.fill_value ?? "未声明"],
    ["通道数", node.n_channels ?? "—"],
    ["长度", node.length ?? "—"],
  ].map(([k, v]) => `<div><dt>${k}</dt><dd>${escapeHtml(String(v))}</dd></div>`).join("");

  $("ds-attrs").innerHTML = Object.entries(node.attrs || {}).length
    ? Object.entries(node.attrs).map(([k, v]) =>
        `<span class="attr-chip"><b>${escapeHtml(k)}</b>=${escapeHtml(formatAttr(v))}</span>`).join("")
    : '<span class="hint">无属性</span>';

  // time config — prefer file-derived suggestion, then manual
  const sug = state.suggestions[node.path] || { mode: "array", unit: "s" };
  fillTimeForm(sug, node);
  renderChannelPicker(node);
  $("range-stop").value = node.length;
  $("range-start").max = node.length - 1;
  $("range-stop").max = node.length;
  $("import-msg").textContent = "";
  $("legend").innerHTML = "";
  clearChart();
}

function fillTimeForm(sug, node) {
  const mode = sug.mode || "array";
  $("time-mode").value = mode;
  toggleModeFields(mode);

  const pathSelect = $("time-array-path");
  pathSelect.innerHTML = "";
  const candidates = collectTimeArrayPaths(state.tree);
  for (const p of candidates) {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p;
    pathSelect.appendChild(opt);
  }
  if (sug.array_path) pathSelect.value = sug.array_path;

  $("time-start").value = sug.start ?? "";
  $("time-interval").value = sug.interval ?? "";
  $("time-unit").value = sug.unit || "s";
  $("time-tz").value = sug.timezone || "";
  $("fill-value").value = "";
  $("use-scale").checked = true;
}

function collectTimeArrayPaths(node, acc = []) {
  if (node.type === "dataset" && (node.shape?.length === 1)
      && /time|timestamp|^t$|datetime/i.test(node.name)) {
    acc.push(node.path);
  }
  for (const c of node.children || []) collectTimeArrayPaths(c, acc);
  return acc;
}

function toggleModeFields(mode) {
  const array = mode === "array";
  $("label-array-path").hidden = !array;
  $("label-start").hidden = array;
  $("label-interval").hidden = array;
}
$("time-mode").addEventListener("change", (e) => toggleModeFields(e.target.value));

function renderChannelPicker(node) {
  const picker = $("channel-picker");
  picker.innerHTML = "";
  const names = node.channel_names || [];
  names.forEach((name, i) => {
    const label = document.createElement("label");
    label.className = "channel-chip";
    const color = SERIES_COLORS[i % SERIES_COLORS.length];
    label.innerHTML = `<input type="checkbox" value="${i}" checked />
      <span class="dot" style="background:${color}"></span>
      <span>${escapeHtml(name)}</span>`;
    picker.appendChild(label);
  });
}

function readTimeConfig() {
  const mode = $("time-mode").value;
  const cfg = {
    mode,
    unit: $("time-unit").value,
    timezone: $("time-tz").value.trim() || null,
    use_scale: $("use-scale").checked,
  };
  const fillRaw = $("fill-value").value.trim();
  if (fillRaw !== "") cfg.fill_value = Number(fillRaw);
  if (mode === "array") {
    cfg.array_path = $("time-array-path").value;
  } else {
    cfg.start = Number($("time-start").value);
    cfg.interval = Number($("time-interval").value);
  }
  return cfg;
}

function selectedChannels() {
  return [...document.querySelectorAll("#channel-picker input:checked")]
    .map((el) => Number(el.value));
}

/* ====================================================================== */
/* validate / preview / import                                             */
/* ====================================================================== */

$("btn-validate").addEventListener("click", async () => {
  const msg = $("validate-msg");
  msg.className = "inline-msg";
  try {
    const body = {
      file_id: state.currentFile.id,
      dataset_path: state.currentNode.path,
      time_config: readTimeConfig(),
    };
    const result = await api("/datasets/validate", {
      method: "POST", body: JSON.stringify(body),
    });
    if (result.problems.length) {
      msg.textContent = "已暂停：" + result.problems.join("；");
      msg.className = "inline-msg err";
    } else {
      msg.textContent = "校验通过，可以预览和导入";
      msg.className = "inline-msg ok";
    }
    await selectFile(state.currentFile.id);
    const node = findNode(state.tree, state.currentNode.path);
    await selectDataset(node);
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "inline-msg err";
  }
});

$("btn-preview").addEventListener("click", async () => {
  const msg = $("import-msg");
  msg.className = "inline-msg";
  const channels = selectedChannels();
  if (!channels.length) {
    msg.textContent = "请至少选择一个通道";
    msg.className = "inline-msg err";
    return;
  }
  try {
    const body = {
      file_id: state.currentFile.id,
      dataset_path: state.currentNode.path,
      time_config: readTimeConfig(),
      channels,
      start: Number($("range-start").value),
      stop: Number($("range-stop").value),
      max_points: Number($("max-points").value),
    };
    state.preview = await api("/preview", {
      method: "POST", body: JSON.stringify(body),
    });
    state.previewChannels = channels;
    const p = state.preview;
    $("preview-meta").textContent =
      `原始 ${p.raw_count} 点 → 返回 ${p.returned} 点${p.decimated ? "（已抽稀）" : ""}；null = 缺失/填充值，不参与连线`;
    renderChart();
    renderLegend();
    msg.textContent = "预览成功（分块读取，未加载整个文件）";
    msg.className = "inline-msg ok";
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "inline-msg err";
  }
});

$("btn-import").addEventListener("click", async () => {
  const msg = $("import-msg");
  msg.className = "inline-msg";
  const channels = selectedChannels();
  if (!channels.length) {
    msg.textContent = "请至少选择一个通道";
    msg.className = "inline-msg err";
    return;
  }
  try {
    const body = {
      file_id: state.currentFile.id,
      dataset_path: state.currentNode.path,
      time_config: readTimeConfig(),
      channels,
      start: Number($("range-start").value),
      stop: Number($("range-stop").value),
    };
    const result = await api("/import", {
      method: "POST", body: JSON.stringify(body),
    });
    renderImportResult(result, msg);
    await refreshJobs();
    await loadReadings();
  } catch (err) {
    msg.textContent = "导入失败：" + err.message;
    msg.className = "inline-msg err";
  }
});

function renderImportResult(result, msg) {
  const c = result.counts;
  const labels = {
    duplicate: "重复切片，未新增任何读数",
    imported: `导入完成`,
    updated: `按新配置更新完成`,
  };
  msg.textContent =
    `${labels[result.status] || result.status}（配置 v${result.config_version}）：` +
    `新增 ${c.inserted}，更新 ${c.updated}，去重跳过 ${c.unchanged}，缺失 ${c.missing}`;
  msg.className = "inline-msg " +
    (result.status === "duplicate" ? "warn" : result.status === "updated" ? "warn" : "ok");
  if (result.diff) {
    msg.textContent += `；与 v${result.diff.previous_version} 的差异见下方任务卡`;
  }
}

async function refreshJobs() {
  if (!state.datasetRow) return;
  const jobs = await api(`/datasets/${state.datasetRow.id}/jobs`);
  const box = $("jobs");
  box.innerHTML = jobs.length ? "" : '<span class="hint">尚无导入任务</span>';
  for (const job of jobs) box.appendChild(renderJobCard(job));
}

function renderJobCard(job) {
  const card = document.createElement("div");
  card.className = "job-card";
  const statusText = {
    duplicate: "重复（零新增）", imported: "已导入",
    updated: "配置更新", failed: "失败",
  }[job.status] || job.status;
  let html = `<div class="job-head">
      <span>任务 #${job.id} · 切片 [${job.index_start}, ${job.index_stop}) ·
        通道 ${job.channels.join(",")}</span>
      <span class="job-status ${job.status}">${statusText}</span>
    </div>
    <div>新增 ${job.inserted} · 更新 ${job.updated} · 去重 ${job.skipped_duplicates}
      · ${job.created_at.slice(0, 19).replace("T", " ")}</div>`;
  if (job.diff) {
    html += `<div class="diff-box">
      <b>相对配置 v${job.diff.previous_version} 的差异</b>
      <table><thead><tr><th>字段</th><th>旧值</th><th>新值</th></tr></thead><tbody>
      ${job.diff.changes.map((ch) =>
        `<tr><td>${escapeHtml(ch.field)}</td><td>${escapeHtml(stringify(ch.old))}</td>
         <td>${escapeHtml(stringify(ch.new))}</td></tr>`).join("")}
      </tbody></table></div>`;
  }
  if (job.error) html += `<div class="inline-msg err">${escapeHtml(job.error)}</div>`;
  card.innerHTML = html;
  return card;
}

/* ====================================================================== */
/* canvas line chart with null gaps + crosshair tooltip                    */
/* ====================================================================== */

const chart = $("chart");
const ctx = chart.getContext("2d");
const tooltip = $("tooltip");
let chartGeom = null; // stores scales for hit-testing

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function clearChart() {
  const { width, height } = chart.getBoundingClientRect();
  chart.width = width * devicePixelRatio;
  chart.height = 340 * devicePixelRatio;
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  ctx.clearRect(0, 0, width, 340);
  chartGeom = null;
  tooltip.hidden = true;
}

function renderChart() {
  const p = state.preview;
  if (!p) return;
  const W = chart.parentElement.clientWidth - 20;
  const H = 340;
  chart.width = W * devicePixelRatio;
  chart.height = H * devicePixelRatio;
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);

  const pad = { l: 64, r: 18, t: 16, b: 46 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const n = p.times.length;

  // shared y-domain across selected series (one axis, never dual)
  let yMin = Infinity, yMax = -Infinity;
  for (const arr of Object.values(p.series)) {
    for (const v of arr) {
      if (v !== null) { yMin = Math.min(yMin, v); yMax = Math.max(yMax, v); }
    }
  }
  if (!isFinite(yMin)) { yMin = 0; yMax = 1; }
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const padY = (yMax - yMin) * 0.06;
  yMin -= padY; yMax += padY;

  const xAt = (i) => pad.l + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yAt = (v) => pad.t + (1 - (v - yMin) / (yMax - yMin)) * plotH;

  // grid + axes
  ctx.strokeStyle = cssVar("--gridline");
  ctx.fillStyle = cssVar("--text-muted");
  ctx.lineWidth = 1;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const v = yMin + (i / ticks) * (yMax - yMin);
    const y = yAt(v);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(W - pad.r, y);
    ctx.stroke();
    ctx.fillText(formatTick(v), pad.l - 8, y);
  }
  // x tick labels (up to 6, ISO shortened)
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  const xTicks = Math.min(6, n);
  for (let i = 0; i < xTicks; i++) {
    const idx = Math.round((i / Math.max(1, xTicks - 1)) * (n - 1));
    const x = xAt(idx);
    ctx.strokeStyle = cssVar("--gridline");
    ctx.beginPath();
    ctx.moveTo(x, pad.t);
    ctx.lineTo(x, pad.t + plotH);
    ctx.stroke();
    ctx.fillStyle = cssVar("--text-muted");
    ctx.fillText(shortTime(p.times[idx]), x, pad.t + plotH + 6);
  }
  // axes baseline
  ctx.strokeStyle = cssVar("--axis");
  ctx.strokeRect(pad.l, pad.t, plotW, plotH);

  // series — break the line at null gaps instead of interpolating
  const names = Object.keys(p.series);
  names.forEach((name, si) => {
    const arr = p.series[name];
    const varName = SERIES_COLORS[state.previewChannels[si] % SERIES_COLORS.length]
      .replace("var(", "").replace(")", "");
    ctx.strokeStyle = cssVar(varName);
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    let pen = false;
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const v = arr[i];
      if (v === null || v === undefined) { pen = false; continue; }
      const x = xAt(i), y = yAt(v);
      if (!pen) { ctx.moveTo(x, y); pen = true; }
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
  });

  chartGeom = { pad, plotW, plotH, xAt, yAt, n, names, W, H };
  tooltip.hidden = true;
}

chart.addEventListener("mousemove", (e) => {
  if (!chartGeom || !state.preview) return;
  const g = chartGeom;
  const rect = chart.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  if (mx < g.pad.l || mx > g.W - g.pad.r || my < g.pad.t ||
      my > g.pad.t + g.plotH) {
    tooltip.hidden = true;
    renderChart();
    return;
  }
  const idx = Math.round(((mx - g.pad.l) / g.plotW) * (g.n - 1));
  const p = state.preview;

  // redraw + crosshair
  renderChart();
  ctx.strokeStyle = cssVar("--axis");
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(g.xAt(idx), g.pad.t);
  ctx.lineTo(g.xAt(idx), g.pad.t + g.plotH);
  ctx.stroke();
  ctx.setLineDash([]);

  let html = `<div class="tt-time">${escapeHtml(p.times[idx] ?? "时间缺失")}</div>`;
  g.names.forEach((name, si) => {
    const v = p.series[name][idx];
    const varName = SERIES_COLORS[state.previewChannels[si] % SERIES_COLORS.length]
      .replace("var(", "").replace(")", "");
    const color = cssVar(varName);
    const valHtml = v === null || v === undefined
      ? '<span class="tt-missing">缺失（填充值）</span>'
      : formatTick(v);
    html += `<div class="tt-row"><span class="dot" style="background:${color}"></span>
      <span>${escapeHtml(name)}</span><b>${valHtml}</b></div>`;
  });
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  const wrapW = chart.parentElement.clientWidth;
  tooltip.style.left = Math.min(mx + 14, wrapW - 180) + "px";
  tooltip.style.top = Math.max(8, my - 20) + "px";
});
chart.addEventListener("mouseleave", () => { tooltip.hidden = true; });

function renderLegend() {
  const p = state.preview;
  const box = $("legend");
  box.innerHTML = "";
  Object.keys(p.series).forEach((name, si) => {
    const item = document.createElement("span");
    item.className = "lg-item";
    const color = SERIES_COLORS[state.previewChannels[si] % SERIES_COLORS.length];
    item.innerHTML = `<span class="dot" style="background:${color}"></span>${escapeHtml(name)}`;
    box.appendChild(item);
  });
}

/* ====================================================================== */
/* readings table                                                          */
/* ====================================================================== */

async function loadReadings() {
  const data = await api("/readings?limit=300");
  state.readings = data.items;
  $("readings-count").textContent =
    `共 ${data.total} 条读数（显示前 ${data.items.length} 条）；点击任意一行查看原文件、数据集路径与数组下标`;
  const tbody = $("readings-table").querySelector("tbody");
  tbody.innerHTML = data.items.map((r) => `
    <tr class="${r.is_missing ? "missing-row" : ""}" data-rid="${r.id}">
      <td class="num">${r.id}</td>
      <td>${r.timestamp_utc ? escapeHtml(r.timestamp_utc) : "—"}</td>
      <td><span class="dot-inline" style="color:var(--series-${(r.channel_index % 5) + 1})">●</span>
        ${escapeHtml(r.channel_name)} <small>[${r.channel_index}]</small></td>
      <td class="num">${r.array_index}</td>
      <td class="num">${r.is_missing ? "缺失" : (r.value === null ? "—" : formatTick(r.value))}</td>
      <td>${r.is_missing ? "是" : ""}</td>
      <td class="num">v${r.config_version}</td>
      <td title="来源溯源">${escapeHtml(r.source_dataset_path)}</td>
    </tr>`).join("");
}

/* ---- reading provenance modal ---- */
function openReadingModal(rid) {
  const r = (state.readings || []).find((x) => x.id === rid);
  if (!r) return;
  const srcFile = state.files.find((f) => f.id === r.source_file_id);
  const rows = [
    ["读数 ID", r.id],
    ["原文件", srcFile ? `${srcFile.filename}（#${srcFile.id}）` : `文件 #${r.source_file_id}`],
    ["数据集路径", r.source_dataset_path],
    ["数组下标", r.array_index],
    ["通道", `${r.channel_name}（下标 ${r.channel_index}）`],
    ["时间 (UTC)", r.timestamp_utc || "时间缺失（NaT）"],
    ["测量值", r.is_missing ? "缺失（填充值，未计入曲线）" : formatTick(r.value)],
    ["配置版本", `v${r.config_version}`],
  ];
  $("modal-body").innerHTML = rows.map(([k, v]) =>
    `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`).join("");
  $("reading-modal").hidden = false;
}
$("readings-table").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-rid]");
  if (tr) openReadingModal(Number(tr.dataset.rid));
});
$("modal-close").addEventListener("click", () => { $("reading-modal").hidden = true; });
$("reading-modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("reading-modal").hidden = true;
});
$("btn-refresh-readings").addEventListener("click", loadReadings);

/* ====================================================================== */
/* upload                                                                  */
/* ====================================================================== */

$("file-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await fetch(API + "/files", { method: "POST", body: fd });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail ?? detail; } catch (_) {}
      alert("上传失败：" + (typeof detail === "object" ? JSON.stringify(detail) : detail));
      return;
    }
    const data = await res.json();
    await loadFiles();
    await selectFile(data.file_id);
  } catch (err) {
    alert("上传失败：" + err.message);
  } finally {
    e.target.value = "";
  }
});

/* ====================================================================== */
/* utilities                                                               */
/* ====================================================================== */

function findNode(node, path) {
  if (node.path === path) return node;
  for (const c of node.children || []) {
    const found = findNode(c, path);
    if (found) return found;
  }
  return null;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function cssEscape(s) {
  return s.replace(/(["\\])/g, "\\$1");
}
function formatBytes(n) {
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB"];
  let v = n, i = -1;
  do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
  return v.toFixed(1) + " " + units[i];
}
function formatAttr(v) {
  if (Array.isArray(v)) return "[" + v.map((x) => escapeHtml(stringify(x))).join(", ") + "]";
  return escapeHtml(stringify(v));
}
function stringify(v) {
  if (v === null) return "null";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
function formatTick(v) {
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e5 || a < 1e-3)) return v.toExponential(2);
  return Number(v.toFixed(4)).toString();
}
function shortTime(iso) {
  if (!iso) return "—";
  return iso.includes("T") ? iso.split("T")[1].slice(0, 8) : iso.slice(11, 19);
}

/* boot */
loadFiles().then(loadReadings);
window.addEventListener("resize", () => { if (state.preview) renderChart(); });
