/* WorkBuddy 统一管理器 — 统一面板前端
 * 无外部依赖、无构建：由 wb_gateway.py 的 /panel/static/app.js 直接送出。
 * 数据全走 /panel/api/*；鉴权用 localStorage 里的 API Key（也可 URL #key= 传入）。
 */
(function () {
'use strict';

/* ================= 常量 ================= */

var KEY_STORE = 'wb.panel.key';
var THEME_STORE = 'wb.panel.theme';
var REFRESH_MS = 15000;
var LIVE_STORE = 'wb.panel.live';        /* 实时刷新的开关（关掉后只手动刷新） */
var LIVE_SEC_DEFAULT = 15;               /* 实时刷新间隔（秒） */

var PAGE_META = {
  overview: { title: '概览', sub: '网关运行状态 · 账号池 · 实时积分' },
  pool:     { title: '账号池', sub: '四策略分配：下一个轮到谁、为什么' },
  models:   { title: '模型', sub: '模型目录 · 区域 · 开关 · 上下文与倍率' },
  tasks:    { title: '任务中心', sub: '成长任务 · 保活 · 批量执行' },
  accounts: { title: '账号管理', sub: '扫码登录 · Token 刷新 · 账号启停' },
  tokens:   { title: 'Token 统计', sub: '按区域 / 客户端 / 模型 / 账号拆分用量' },
  credits:  { title: '积分统计', sub: '实时余额 · 资源包 · 到期时间' },
  checkin:  { title: '签到日志', sub: '每日任务执行记录与收益' },
  collect:  { title: '实时日志', sub: '积分采集进程 · 实时输出（原老控制台的「实时面板」）' },
  diagnose: { title: '诊断', sub: 'Python · 网关 · 依赖 · 文件 四项自检' },
  settings: { title: '设置', sub: 'API Key · 网关配置 · 外观' }
};

/* 趋势图堆叠顺序与配色（与 CSS 变量保持一致的品牌色） */
var STACKS = [
  { key: 'cached_read', color: '#34d399', label: '缓存读取' },
  { key: 'cache_miss',  color: '#60a5fa', label: '缓存未命中' },
  { key: 'output',      color: '#fbbf24', label: '输出' },
  { key: 'cache_write', color: '#a78bfa', label: '缓存写入' }
];

var REGION_LABELS = { merged: '合并', cn: '国内版', global: '国际版' };

var POLICY_FALLBACK = [
  { key: 'priority',    label: '先用完一个', note: '固定一个号，用尽或被熔断才换' },
  { key: 'round-robin', label: '顺序轮换',   note: '按顺序轮流，负载平均摊开' },
  { key: 'balanced',    label: '均衡分配',   note: '闲置越久越容易被选中' },
  { key: 'sticky',      label: '对话粘性',   note: '一个对话固定一个账号，保住上游缓存' }
];

/* ================= DOM 小工具 ================= */

function $(sel, root) { return (root || document).querySelector(sel); }
function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

function appendKids(node, kids) {
  if (kids === null || kids === undefined || kids === false || kids === '') return;
  if (Array.isArray(kids)) {
    for (var i = 0; i < kids.length; i++) appendKids(node, kids[i]);
    return;
  }
  if (typeof Node !== 'undefined' && kids instanceof Node) { node.appendChild(kids); return; }
  node.appendChild(document.createTextNode(String(kids)));
}

/* h('div', {class:'card', onclick:fn}, [子节点...]) */
function h(tag, attrs, kids) {
  var node = document.createElement(tag);
  if (attrs) {
    for (var k in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
      var v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.className = String(v);
      else if (k === 'text') node.textContent = String(v);
      else if (k === 'style') node.setAttribute('style', String(v));
      else if (k.indexOf('on') === 0 && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : String(v));
    }
  }
  if (kids !== undefined) appendKids(node, kids);
  return node;
}

/* ================= 格式化 ================= */

function toInt(v) {
  var n = Number(v);
  if (!isFinite(n)) return 0;
  return n < 0 ? Math.ceil(n) : Math.floor(n);
}
function toNum(v) {
  var n = Number(v);
  return isFinite(n) ? n : 0;
}

/* 千分位：不用正则，避免任何转义歧义 */
function fmtInt(v) {
  var n = toInt(v);
  var neg = n < 0;
  if (neg) n = -n;
  var s = String(n), out = '', c = 0;
  for (var i = s.length - 1; i >= 0; i--) {
    out = s.charAt(i) + out;
    c++;
    if (c % 3 === 0 && i > 0) out = ',' + out;
  }
  return (neg ? '-' : '') + out;
}

function fmtCompact(v) {
  var n = toInt(v);
  var neg = n < 0; if (neg) n = -n;
  var out;
  if (n >= 1000000000) out = (n / 1000000000).toFixed(2) + 'B';
  else if (n >= 1000000) out = (n / 1000000).toFixed(2) + 'M';
  else if (n >= 10000) out = (n / 1000).toFixed(1) + 'K';
  else out = String(n);
  return (neg ? '-' : '') + out;
}

/* Token 数：小值给精确值，大值给紧凑值 */
function fmtTokens(v) {
  var n = toInt(v);
  if (n < 10000) return fmtInt(n);
  return fmtCompact(n) + ' (' + fmtInt(n) + ')';
}
function fmtShortTokens(v) {
  var n = toInt(v);
  if (n < 10000) return fmtInt(n);
  return fmtCompact(n);
}

function fmtPct(ratio, digits) {
  var v = toNum(ratio);
  var d = digits === undefined ? 1 : digits;
  return (v * 100).toFixed(d) + '%';
}

function pad2(n) { return (n < 10 ? '0' : '') + n; }

function fmtTs(ts) {
  var s = toInt(ts);
  if (s <= 0) return '—';
  var d = new Date(s * 1000);
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
         ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}
function fmtDay(ts) {
  var s = toInt(ts);
  if (s <= 0) return '—';
  var d = new Date(s * 1000);
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}

function fmtElapsed(seconds) {
  var s = toInt(seconds);
  if (s <= 0) return '0 秒';
  var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return d + ' 天 ' + h + ' 小时';
  if (h > 0) return h + ' 小时 ' + m + ' 分';
  if (m > 0) return m + ' 分 ' + (s % 60) + ' 秒';
  return s + ' 秒';
}

function fmtMs(ms) {
  var n = toInt(ms);
  if (n <= 0) return '—';
  if (n < 1000) return n + ' ms';
  if (n < 60000) return (n / 1000).toFixed(2) + ' s';
  return (n / 60000).toFixed(1) + ' min';
}

function relTime(ts) {
  var s = toInt(ts);
  if (s <= 0) return '从未';
  var diff = Math.floor(Date.now() / 1000) - s;
  if (diff < 0) return '刚刚';
  if (diff < 60) return diff + ' 秒前';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  return Math.floor(diff / 86400) + ' 天前';
}

function shortId(id) {
  var s = String(id || '');
  if (s.length <= 12) return s || '—';
  return s.slice(0, 8) + '…' + s.slice(-3);
}

function textOf(v, fallback) {
  if (v === null || v === undefined || v === '') return fallback === undefined ? '—' : fallback;
  return String(v);
}

/* ================= 状态 ================= */

var state = {
  page: 'overview',
  key: '',
  loading: false,
  error: '',
  stats: null,
  status: null,
  pool: null,
  models: null,
  tasks: null,
  checkin: null,
  snapshots: null,
  config: null,
  authFiles: null,
  credits: null,
  usageMeta: null,
  usage: null,
  uRange: '7d',
  uRegion: 'merged',
  uClient: '',
  uModel: '',
  mRegion: 'merged',
  mQuery: '',
  login: null,
  loginTimer: null,
  busy: '',
  collect: null,
  diagnose: null,
  live: true,
  liveSec: LIVE_SEC_DEFAULT
};

/* ================= Key 解析 ================= */

function readKey() {
  var raw = String(location.hash || '') + '&' + String(location.search || '');
  var i = raw.indexOf('key=');
  while (i >= 0) {
    var before = i === 0 ? '' : raw.charAt(i - 1);
    if (before === '#' || before === '&' || before === '?') {
      var rest = raw.slice(i + 4);
      var j = rest.indexOf('&');
      if (j >= 0) rest = rest.slice(0, j);
      try { rest = decodeURIComponent(rest); } catch (e0) { /* 原样使用 */ }
      if (rest) {
        try { localStorage.setItem(KEY_STORE, rest); } catch (e1) { /* 存不下也不影响本次 */ }
        return rest;
      }
    }
    i = raw.indexOf('key=', i + 4);
  }
  try { return localStorage.getItem(KEY_STORE) || ''; } catch (e2) { return ''; }
}

function saveKey(k) {
  state.key = String(k || '').trim();
  try {
    if (state.key) localStorage.setItem(KEY_STORE, state.key);
    else localStorage.removeItem(KEY_STORE);
  } catch (e) { /* 忽略 */ }
}

/* ================= HTTP ================= */

function errMessage(data, status) {
  if (data && data.error) {
    if (typeof data.error === 'string') return data.error;
    if (data.error.message) return String(data.error.message);
    if (data.error.code) return String(data.error.code);
  }
  if (data && data.message) return String(data.message);
  if (data && data._raw) return String(data._raw).slice(0, 200);
  return 'HTTP ' + String(status);
}

function api(url, opts) {
  opts = opts || {};
  var headers = { 'Accept': 'application/json' };
  if (state.key) headers['Authorization'] = 'Bearer ' + state.key;
  var init = { method: opts.method || 'GET', headers: headers };
  if (opts.body !== undefined && opts.body !== null) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  return fetch(url, init).then(function (res) {
    return res.text().then(function (text) {
      var data = null;
      if (text) {
        try { data = JSON.parse(text); } catch (e) { data = { _raw: text }; }
      }
      if (!res.ok) {
        var err = new Error(errMessage(data, res.status));
        err.status = res.status;
        err.data = data;
        throw err;
      }
      return data === null ? {} : data;
    });
  });
}
function apiGet(url) { return api(url); }
function apiPost(url, body) { return api(url, { method: 'POST', body: body || {} }); }

/* ================= 提示条 ================= */

var toastTimer = null;
function toast(msg, isErr) {
  var box = $('#toast');
  if (!box) return;
  box.textContent = String(msg);
  box.className = 'toast show' + (isErr ? ' err' : '');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(function () {
    box.className = 'toast' + (isErr ? ' err' : '');
  }, isErr ? 5200 : 2800);
}

/* 统一的异步按钮：期间禁用并显示 spinner 类文案 */
function run(btn, label, fn) {
  if (state.busy) { toast('上一步还没结束', true); return Promise.resolve(); }
  state.busy = label || 'busy';
  var old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = label || '处理中…'; }
  function done() {
    state.busy = '';
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
  return Promise.resolve()
    .then(fn)
    .then(function (r) { done(); return r; })
    .catch(function (e) {
      done();
      toast((e && e.message) ? e.message : '操作失败', true);
      if (e && e.status === 401) state.error = 'API Key 无效或缺失';
      throw e;
    });
}

/* ================= 主题 ================= */

function currentTheme() {
  var t = document.documentElement.getAttribute('data-theme');
  return t === 'light' ? 'light' : 'dark';
}
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t === 'light' ? 'light' : 'dark');
  try { localStorage.setItem(THEME_STORE, t); } catch (e) { /* 忽略 */ }
}
function initTheme() {
  var t = null;
  try { t = localStorage.getItem(THEME_STORE); } catch (e) { t = null; }
  if (!t) {
    t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
  }
  applyTheme(t);
}

/* ================= 侧栏 / 页面切换 ================= */

function openSidebar() { var s = $('#sidebar'), c = $('#scrim'); if (s) s.className = 'sidebar open'; if (c) c.className = 'scrim show'; }
function closeSidebar() { var s = $('#sidebar'), c = $('#scrim'); if (s) s.className = 'sidebar'; if (c) c.className = 'scrim'; }

function setPage(p) {
  if (!PAGE_META[p]) p = 'overview';
  if (state.page === p) { load(p); return; }
  state.page = p;
  state.error = '';
  closeSidebar();
  var view = $('#view');
  if (view) view.scrollTop = 0;
  load(p);
}

/* ================= 小构件 ================= */

function card(title, right, body, extra) {
  var head = null;
  if (title || right) {
    head = h('div', { class: 'card-head' }, [
      title ? h('h2', null, title) : null,
      h('div', { class: 'spacer' }),
      right || null
    ]);
  }
  return h('div', { class: 'card' + (extra ? ' ' + extra : '') }, [head, body]);
}

function statCard(key, value, detail, accent) {
  return h('div', { class: 'stat' + (accent ? ' accent' : '') }, [
    h('div', { class: 'k' }, key),
    h('div', { class: 'v' }, value),
    detail ? h('div', { class: 'd' }, detail) : null
  ]);
}

function badge(text, kind) {
  return h('span', { class: 'badge badge-' + (kind || 'mute') }, text);
}

function tabs(items, activeKey, onPick) {
  return h('div', { class: 'tabs' }, items.map(function (it) {
    return h('button', {
      class: 'tab' + (String(it.key) === String(activeKey) ? ' active' : ''),
      onclick: function () { onPick(it.key); }
    }, it.label);
  }));
}

function bar(ratio, cls) {
  var pct = Math.max(0, Math.min(1, toNum(ratio))) * 100;
  return h('div', { class: 'bar' + (cls ? ' ' + cls : '') }, [
    h('i', { style: 'width:' + pct.toFixed(1) + '%' })
  ]);
}

function stackBar(parts) {
  /* parts: [{value, color}]，按总量归一 */
  var total = 0, i;
  for (i = 0; i < parts.length; i++) total += toNum(parts[i].value);
  var spans = [];
  for (i = 0; i < parts.length; i++) {
    var w = total > 0 ? (toNum(parts[i].value) / total) * 100 : 0;
    if (w <= 0) continue;
    spans.push(h('span', { style: 'width:' + w.toFixed(2) + '%;background:' + parts[i].color }));
  }
  return h('div', { class: 'stackbar' }, spans);
}

function legend(parts) {
  return h('div', { class: 'legend' }, parts.map(function (p) {
    return h('span', null, [
      h('i', { class: 'sw', style: 'background:' + p.color }),
      p.label + (p.value !== undefined ? ' ' + p.value : '')
    ]);
  }));
}

function emptyBox(text) { return h('div', { class: 'empty' }, text); }

function loadingBox(text) {
  return h('div', { class: 'empty' }, [
    h('span', { class: 'spinner' }),
    ' ' + (text || '正在读取…')
  ]);
}

function kvList(pairs) {
  var kids = [];
  for (var i = 0; i < pairs.length; i++) {
    var p = pairs[i];
    if (!p) continue;
    kids.push(h('dt', null, p[0]));
    kids.push(h('dd', null, p[1]));
  }
  return h('dl', { class: 'kv' }, kids);
}

function table(heads, rows, opts) {
  opts = opts || {};
  var thead = h('thead', null, [h('tr', null, heads.map(function (x) {
    if (x && typeof x === 'object') return h('th', { class: x.cls || null }, x.label);
    return h('th', null, x);
  }))]);
  var tbody = h('tbody', null, rows);
  return h('div', { class: 'table-wrap' }, [h('table', null, [thead, tbody])]);
}

/* ================= 趋势图（纯 canvas） ================= */

function cssVar(name) {
  try {
    var v = getComputedStyle(document.body).getPropertyValue(name);
    return (v || '').trim();
  } catch (e) { return ''; }
}

function drawTrend(canvas, series, opts) {
  if (!canvas) return;
  opts = opts || {};
  var data = series || [];
  var dpr = window.devicePixelRatio || 1;
  var cssW = canvas.clientWidth || (canvas.parentNode ? canvas.parentNode.clientWidth : 0) || 640;
  var cssH = opts.height || 260;
  canvas.style.height = cssH + 'px';
  canvas.width = Math.max(1, Math.round(cssW * dpr));
  canvas.height = Math.max(1, Math.round(cssH * dpr));
  var ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  var gridColor = cssVar('--border-soft') || 'rgba(128,128,128,.25)';
  var mutedColor = cssVar('--muted') || '#8b9dc3';
  var textColor = cssVar('--text') || '#e6ebf5';

  var padL = 56, padR = 48, padT = 16, padB = 30;
  var w = Math.max(20, cssW - padL - padR);
  var hh = Math.max(20, cssH - padT - padB);

  if (!data.length) {
    ctx.fillStyle = mutedColor;
    ctx.font = '13px -apple-system, "Microsoft YaHei", sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('该时间范围内没有请求记录', cssW / 2, cssH / 2);
    return;
  }

  var maxTok = 0, maxReq = 1, i, j;
  for (i = 0; i < data.length; i++) {
    var total = 0;
    for (j = 0; j < STACKS.length; j++) total += toNum(data[i][STACKS[j].key]);
    if (total > maxTok) maxTok = total;
    if (toNum(data[i].requests) > maxReq) maxReq = toNum(data[i].requests);
  }
  if (maxTok <= 0) maxTok = 1;
  maxTok = maxTok * 1.12;
  maxReq = maxReq * 1.35;

  function xOf(idx) {
    if (data.length === 1) return padL + w / 2;
    return padL + (w * idx) / (data.length - 1);
  }
  function yTok(v) { return padT + hh - (toNum(v) / maxTok) * hh; }
  function yReq(v) { return padT + hh - (toNum(v) / maxReq) * hh; }

  /* 横向网格 + 左轴 token 刻度 */
  ctx.strokeStyle = gridColor;
  ctx.lineWidth = 1;
  ctx.font = '11px -apple-system, "Microsoft YaHei", sans-serif';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  var lines = 4;
  for (i = 0; i <= lines; i++) {
    var gy = padT + (hh * i) / lines;
    ctx.beginPath();
    ctx.moveTo(padL, Math.round(gy) + 0.5);
    ctx.lineTo(padL + w, Math.round(gy) + 0.5);
    ctx.stroke();
    ctx.fillStyle = mutedColor;
    ctx.fillText(fmtShortTokens((maxTok * (lines - i)) / lines), padL - 8, gy);
  }

  /* 右轴请求数刻度 */
  ctx.textAlign = 'left';
  for (i = 0; i <= lines; i++) {
    var ry = padT + (hh * i) / lines;
    ctx.fillStyle = mutedColor;
    ctx.fillText(String(Math.round((maxReq * (lines - i)) / lines)), padL + w + 8, ry);
  }

  /* 堆叠柱 */
  var slot = data.length > 1 ? (w / (data.length - 1)) : w;
  var bw = Math.max(2, Math.min(slot * 0.66, 46));
  for (i = 0; i < data.length; i++) {
    var cx = xOf(i);
    var base = padT + hh;
    for (j = 0; j < STACKS.length; j++) {
      var val = toNum(data[i][STACKS[j].key]);
      if (val <= 0) continue;
      var barH = (val / maxTok) * hh;
      ctx.fillStyle = STACKS[j].color;
      ctx.globalAlpha = 0.92;
      ctx.fillRect(cx - bw / 2, base - barH, bw, Math.max(0.8, barH));
      ctx.globalAlpha = 1;
      base -= barH;
    }
  }

  /* 请求数虚线（双轴） */
  ctx.strokeStyle = textColor;
  ctx.globalAlpha = 0.55;
  ctx.lineWidth = 1.6;
  ctx.setLineDash([5, 4]);
  ctx.beginPath();
  for (i = 0; i < data.length; i++) {
    var px = xOf(i), py = yReq(data[i].requests);
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;

  /* 数据点 */
  ctx.fillStyle = textColor;
  ctx.globalAlpha = 0.7;
  for (i = 0; i < data.length; i++) {
    ctx.beginPath();
    ctx.arc(xOf(i), yReq(data[i].requests), 2.2, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;

  /* x 轴标签：最多 8 个 */
  ctx.fillStyle = mutedColor;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  var step = Math.max(1, Math.ceil(data.length / 8));
  for (i = 0; i < data.length; i++) {
    if (i % step !== 0 && i !== data.length - 1) continue;
    ctx.fillText(String(data[i].label || data[i].slot || ''), xOf(i), padT + hh + 8);
  }
}

/* ================= 数据加载 ================= */

function softEmpty(err) {
  if (err && err.status === 401) throw err;
  return null;
}

function usageQuery() {
  var q = 'range=' + encodeURIComponent(state.uRange) + '&region=' + encodeURIComponent(state.uRegion);
  if (state.uClient) q += '&client=' + encodeURIComponent(state.uClient);
  if (state.uModel) q += '&model=' + encodeURIComponent(state.uModel);
  return q;
}

function loadUsageMeta() {
  if (state.usageMeta) return Promise.resolve();
  return apiGet('/panel/api/usage/meta').then(function (d) {
    state.usageMeta = d;
  }).catch(function (e) {
    state.usageMeta = { _error: (e && e.message) ? e.message : String(e) };
  });
}

function loadUsage() {
  return Promise.all([
    loadUsageMeta(),
    apiGet('/panel/api/usage?' + usageQuery()).then(function (d) { state.usage = d; })
  ]);
}

var LOADERS = {
  overview: function () {
    return Promise.all([
      apiGet('/panel/api/stats').catch(softEmpty),
      apiGet('/status').catch(softEmpty)
    ]).then(function (r) {
      state.stats = r[0];
      state.status = r[1];
    });
  },
  pool: function () {
    return apiGet('/panel/api/pool?limit=12').then(function (d) { state.pool = d; });
  },
  accounts: function () {
    return Promise.all([
      apiGet('/panel/api/accounts'),
      apiGet('/api/auth-files').catch(softEmpty),
      apiGet('/panel/api/snapshots').catch(softEmpty)
    ]).then(function (r) {
      state.pool = r[0];
      state.authFiles = r[1];
      state.snapshots = r[2];
    });
  },
  models: function () {
    return apiGet('/panel/api/models?region=' + encodeURIComponent(state.mRegion)).then(function (d) {
      state.models = d;
    });
  },
  tokens: function () { return loadUsage(); },
  credits: function () {
    return apiGet('/api/credits/live').catch(softEmpty).then(function (d) {
      state.credits = d;
      return apiGet('/api/auth-files').catch(softEmpty).then(function (a) { state.authFiles = a; });
    });
  },
  tasks: function () {
    return apiGet('/tasks/list').then(function (d) { state.tasks = d; });
  },
  checkin: function () {
    return apiGet('/panel/api/checkin').then(function (d) {
      state.checkin = d;
    }).catch(function (e) {
      if (e && e.status === 401) throw e;
      state.checkin = { _unavailable: (e && e.message) ? e.message : '尚未启用' };
    });
  },
  collect: function () {
    return apiGet('/panel/api/logs?n=400').then(function (d) {
      state.collect = d || {};
    }).catch(function (e) {
      if (e && e.status === 401) throw e;
      state.collect = { _error: (e && e.message) ? e.message : '采集日志接口不可用' };
    });
  },
  diagnose: function () {
    return apiGet('/panel/api/diagnose').then(function (d) {
      state.diagnose = d || {};
    }).catch(function (e) {
      if (e && e.status === 401) throw e;
      state.diagnose = { _error: (e && e.message) ? e.message : '自检接口不可用' };
    });
  },
  settings: function () {
    return apiGet('/panel/api/config').then(function (d) { state.config = d; });
  }
};

var seq = 0;

function load(page) {
  var token = ++seq;
  state.error = '';
  state.loading = true;
  render();
  var job = LOADERS[page] ? LOADERS[page]() : Promise.resolve();
  return job.then(function () {
    if (token !== seq) return;
    state.loading = false;
    render();
  }).catch(function (e) {
    if (token !== seq) return;
    state.loading = false;
    state.error = (e && e.message) ? e.message : String(e);
    if (e && e.status === 401) {
      state.error = '鉴权失败：请在下方的「API Key」里填入正确的 key（' + state.error + '）';
    }
    render();
  });
}

/* ================= 页面：概览 ================= */

function liveCredits() {
  var lc = state.status && state.status.live_credits;
  if (lc && !lc._error && lc.accounts) return lc;
  return null;
}

function accountNameMap() {
  var map = {};
  var list = (state.status && state.status.accounts) || (state.pool && state.pool.accounts) || [];
  for (var i = 0; i < list.length; i++) {
    map[String(list[i].id)] = list[i].name || String(list[i].id);
  }
  return map;
}

/* 账号对象查询 / 展示：面板上「这是哪个号」必须用人能认的名字，而不是 uuid */
function accountById(id) {
  var key = String(id || '');
  if (!key) return null;
  /* 三个页面模块把账号放在不同地方：账号页 state.pool.accounts、
     概览页 state.status.accounts、账号池页 state.pool.status.accounts。都要找。 */
  var lists = [
    state.pool ? state.pool.accounts : null,
    state.pool && state.pool.status ? state.pool.status.accounts : null,
    state.status ? state.status.accounts : null
  ];
  for (var i = 0; i < lists.length; i++) {
    var list = lists[i];
    if (!list || !list.length) continue;
    for (var j = 0; j < list.length; j++) {
      if (String(list[j].id) === key) return list[j];
    }
  }
  /* 实在找不到对象，用实时余额/鉴权文件里的名字兜底，至少不显示 uuid */
  var lc = state.status && state.status.live_credits;
  var arr = (lc && lc.accounts) || state.credits && state.credits.accounts;
  if (Array.isArray(arr)) {
    for (var k = 0; k < arr.length; k++) {
      if (String(arr[k].uid || arr[k].id) === key) return { name: arr[k].name, region: arr[k].region };
    }
  }
  var files = (state.authFiles && state.authFiles.accounts) || [];
  for (var m = 0; m < files.length; m++) {
    if (String(files[m].uid || files[m].id) === key) return { name: files[m].name, region: files[m].realm };
  }
  return null;
}

function accountBadges(acc, id) {
  if (!acc) return [h('span', { class: 'mono small muted' }, shortId(id))];
  var out = [h('b', { style: 'font-size:15px' }, textOf(acc.name, shortId(id)))];
  out.push(badge(REGION_LABELS[acc.region] || acc.region || '未知区域', 'info'));
  if (acc.active === false) out.push(badge('已停用', 'mute'));
  else if (acc.available) out.push(badge('可派发', 'ok'));
  else out.push(badge('暂不可用', 'bad'));
  return out;
}

/* kvList 里显示「当前派发账号」 */
function accountCell(id) {
  if (!id) return h('span', { class: 'muted' }, '未指定（按策略自动挑）');
  var acc = accountById(id);
  if (!acc) return h('span', { class: 'mono small' }, shortId(id));
  return h('div', null, [
    h('div', null, [textOf(acc.name, shortId(id)), ' ', badge('当前派发', 'ok')]),
    h('div', { class: 'small muted mono' },
      shortId(id) + (acc.region ? ' · ' + (REGION_LABELS[acc.region] || acc.region) : ''))
  ]);
}

function pageOverview() {
  var wrap = h('div', null);
  var stats = state.stats || {};
  var status = state.status;

  if (!stats && !status) {
    return state.loading ? loadingBox('正在读取网关状态…') : emptyBox('读不到 /status —— 确认网关在运行、API Key 正确');
  }

  var total = toInt(status && status.total);
  var avail = toInt(status && status.available);
  var requests = toInt(stats.requests);
  var success = toInt(stats.success);
  var failures = toInt(stats.failures);
  var rate = requests > 0 ? success / requests : 0;

  var lc = liveCredits();
  var remain = lc ? toInt(lc.total_remain) : 0;
  var packSize = lc ? toInt(lc.total_size) : 0;
  var expiring = lc ? toInt(lc.expiring_7d) : 0;

  wrap.appendChild(h('div', { class: 'grid' }, [
    statCard('已连接账号', String(avail) + ' / ' + String(total), avail === total ? '全部可用' : (total - avail) + ' 个不可用'),
    statCard('累计请求', fmtInt(requests), '成功 ' + fmtInt(success) + ' · 失败 ' + fmtInt(failures), true),
    statCard('成功率', requests > 0 ? fmtPct(rate) : '—', requests > 0 ? (failures > 0 ? '有 ' + fmtInt(failures) + ' 次失败' : '零失败') : '还没有请求'),
    statCard('实时积分余额', lc ? fmtInt(remain) : '—', lc ? ('资源包总量 ' + fmtInt(packSize) + ' · 7 天内到期 ' + fmtInt(expiring)) : '实时账单接口未返回')
  ]));

  wrap.appendChild(h('div', { class: 'section-gap' }));

  /* 当前派发账号 —— 「DSH 现在扣的是谁的积分」必须一眼可见 */
  var curId = status && status.current;
  var curAcc = curId ? accountById(curId) : null;
  var curLive = (lc && lc.accounts && curId && lc.accounts[curId]) || null;
  wrap.appendChild(card('当前派发账号', h('button', {
    class: 'btn btn-sm',
    onclick: function () { setPage('accounts'); }
  }, '账号管理'), h('div', null, [
    h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px' },
      accountBadges(curAcc, curId)),
    h('p', { class: 'small muted', style: 'margin:0 0 10px' },
      curAcc ? 'UID ' + shortId(curId) + ' —— 网关和 DSH 的请求都由这个号承担，扣的是它的积分。'
             : '还没有指定当前账号，网关会按下面的策略自动挑号。'),
    kvList([
      ['它的积分余额', curLive
        ? (fmtInt(toInt(curLive.remain)) + ' / ' + fmtInt(toInt(curLive.total)) + '（剩余 ' +
           fmtPct(toInt(curLive.total) > 0 ? toInt(curLive.remain) / toInt(curLive.total) : 0) + '）')
        : (curAcc && toInt(curAcc.credits) ? fmtInt(toInt(curAcc.credits)) : '—')],
      ['上次使用', curAcc ? relTime(curAcc.last_used) : '—'],
      ['上次派发原因', textOf(status && status.policy && status.policy.last_pick_reason, '—')]
    ])
  ])));

  wrap.appendChild(h('div', { class: 'section-gap' }));

  /* 分配策略 */
  var policy = (status && status.policy) || (state.pool && state.pool.policy) || {};
  var policyLabel = '';
  for (var i = 0; i < POLICY_FALLBACK.length; i++) {
    if (POLICY_FALLBACK[i].key === policy.distribution) policyLabel = POLICY_FALLBACK[i].label;
  }
  wrap.appendChild(card('分配策略', h('button', {
    class: 'btn btn-sm',
    onclick: function () { setPage('pool'); }
  }, '调整'), h('div', null, [
    h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px' }, [
      badge(policyLabel || policy.distribution || '未知', 'info'),
      badge('区域 ' + (REGION_LABELS[policy.region] || policy.region || '合并'), 'mute'),
      badge('粘性会话 ' + String(toInt(policy.sticky_sessions)), 'mute'),
      badge('积分下限 ' + String(toInt(policy.credit_floor)), 'mute')
    ]),
    kvList([
      ['上次派活策略', textOf(policy.last_pick_policy)],
      ['上次派活原因', textOf(policy.last_pick_reason)],
      ['当前派发账号', accountCell(status && status.current)]
    ])
  ])));

  /* 账号实时余额 */
  var lcRows = [];
  if (lc) {
    var names = accountNameMap();
    var keys = [];
    for (var uid in lc.accounts) {
      if (Object.prototype.hasOwnProperty.call(lc.accounts, uid)) keys.push(uid);
    }
    keys.sort(function (a, b) { return toInt(lc.accounts[b].remain) - toInt(lc.accounts[a].remain); });
    for (var k = 0; k < keys.length && k < 8; k++) {
      var uid2 = keys[k];
      var info = lc.accounts[uid2] || {};
      var r = toInt(info.remain), t = toInt(info.total);
      lcRows.push(h('tr', null, [
        h('td', null, [h('div', null, names[uid2] || shortId(uid2)), h('div', { class: 'small muted mono' }, shortId(uid2))]),
        h('td', { class: 'nowrap' }, fmtInt(r) + ' / ' + fmtInt(t)),
        h('td', { style: 'min-width:140px' }, [bar(t > 0 ? r / t : 0), h('div', { class: 'small muted' }, t > 0 ? fmtPct(r / t) : '—')]),
        h('td', { class: 'nowrap small muted' }, toInt(info.total_count) + ' 个资源包')
      ]));
    }
  }
  wrap.appendChild(card('账号实时余额', lc ? h('span', { class: 'small muted' }, '查询于 ' + fmtTs(lc.queried_at)) : null,
    lcRows.length ? table(['账号', '剩余 / 总量', '使用比例', '资源包'], lcRows) : emptyBox('实时余额接口没有返回账号数据')));

  /* 系统信息 */
  var endpoints = [];
  if (configListen()) endpoints.push(['API 端点', 'http://' + configListen() + '/v1/chat/completions']);
  endpoints.push(['运行时长', status ? fmtElapsed(status.uptime) : '—']);
  endpoints.push(['面板版本', status ? ('v' + textOf(status.version, '1.0.0')) : '—']);
  endpoints.push(['账号池', total + ' 个（可用 ' + avail + '）']);
  endpoints.push(['鉴权文件', state.authFiles && state.authFiles.accounts ? (state.authFiles.accounts.length + ' 个') : '—']);
  if (status && status.auto_reloaded) endpoints.push(['自动重载', '本次请求触发了账号重载']);

  wrap.appendChild(card('系统信息', null, kvList(endpoints)));

  return wrap;
}

function configListen() {
  var s = state.status && state.status.server;
  if (s && s.host && s.port) return String(s.host) + ':' + String(s.port);
  return '127.0.0.1:7863';
}

/* ================= 页面：账号池 ================= */

function pagePool() {
  var view = state.pool;
  var wrap = h('div', null);
  if (!view) {
    return state.loading ? loadingBox('正在读取账号池…') : emptyBox('账号池接口没有返回数据');
  }
  var policy = view.policy || {};
  var status = view.status || {};

  var policies = (view.policies && view.policies.length) ? view.policies : POLICY_FALLBACK;

  wrap.appendChild(card('分配策略', h('span', { class: 'small muted' }, '切换后立即生效并写入 config.json'),
    h('div', null, [
      h('div', { class: 'toolbar' }, [
        tabs(policies, policy.distribution, function (key) {
          if (key === policy.distribution) return;
          run(null, '切换策略', function () {
            return apiPost('/panel/api/pool', { distribution: key }).then(function (d) {
              state.pool = d;
              toast('已切换到「' + (labelOf(policies, key)) + '」');
              return load('pool');
            });
          });
        }),
        h('div', { class: 'spacer' }),
        h('span', { class: 'small muted' }, '区域 ' + (REGION_LABELS[policy.region] || policy.region || '合并'))
      ]),
      view.note ? h('div', { class: 'small muted' }, view.note) : policyNote(policies, policy.distribution)
    ])));

  /* 下一个轮到谁 */
  var next = view.preview && view.preview.next;
  wrap.appendChild(card('下一个轮到谁', next ? badge('rank #1', 'ok') : null,
    next ? h('div', null, [
      h('div', { style: 'font-size:17px;font-weight:650;margin-bottom:8px' }, next.name || shortId(next.id)),
      kvList([
        ['账号 ID', h('span', { class: 'mono' }, next.id)],
        ['区域', REGION_LABELS[next.region] || next.region || '—'],
        ['积分 / 能量', fmtInt(next.credits) + ' / ' + fmtInt(next.energy)],
        ['空闲', textOf(next.idle_text)],
        ['历史请求', fmtInt(next.requests)]
      ])
    ]) : emptyBox('池里没有可用账号')));

  /* 排序预览 */
  var order = (view.preview && view.preview.order) || [];
  wrap.appendChild(card('派活顺序预览', h('span', { class: 'small muted' },
      '候选 ' + String(toInt(view.preview && view.preview.candidates)) + ' 个 · 按当前策略排序'),
    order.length ? table(['#', '账号', '区域', '积分', '能量', '空闲', '历史请求'], order.map(function (row) {
      return h('tr', null, [
        h('td', { class: 'nowrap' }, ['#' + String(toInt(row.rank)), row.rank === 1 ? ' ' : null, row.rank === 1 ? badge('下一个', 'ok') : null]),
        h('td', null, [h('div', null, row.name || shortId(row.id)), h('div', { class: 'small muted mono' }, shortId(row.id))]),
        h('td', { class: 'nowrap' }, REGION_LABELS[row.region] || row.region || '—'),
        h('td', { class: 'nowrap' }, fmtInt(row.credits)),
        h('td', { class: 'nowrap' }, fmtInt(row.energy)),
        h('td', { class: 'nowrap small muted' }, textOf(row.idle_text)),
        h('td', { class: 'nowrap' }, fmtInt(row.requests))
      ]);
    })) : emptyBox(view.preview ? '没有可用候选账号' : '分配策略模块不可用')));

  /* 不可用原因 */
  var bad = view.unavailable || [];
  var badRows = bad.map(function (b) {
    return h('tr', null, [
      h('td', null, [h('div', null, b.name || shortId(b.id)), h('div', { class: 'small muted mono' }, shortId(b.id))]),
      h('td', { class: 'small' }, textOf(b.reason, '不可用'))
    ]);
  });
  wrap.appendChild(card('不可用账号', h('span', { class: 'badge ' + (bad.length ? 'badge-warn' : 'badge-ok') }, String(bad.length) + ' 个'),
    badRows.length ? table(['账号', '原因'], badRows) : emptyBox('全部账号都可用')));

  wrap.appendChild(card('池状态', null, kvList([
    ['账号总数', String(toInt(status.total))],
    ['参与派发的账号', String(toInt(status.active))],
    ['可用', String(toInt(status.available))],
    ['粘性会话数', String(toInt(policy.sticky_sessions))],
    ['积分下限', String(toInt(policy.credit_floor))],
    ['上次派活', textOf(policy.last_pick_policy) + ' / ' + textOf(policy.last_pick_reason)],
    ['游标', JSON.stringify(policy.cursors || {})]
  ])));

  return wrap;
}

function labelOf(list, key) {
  for (var i = 0; i < list.length; i++) {
    if (String(list[i].key) === String(key)) return list[i].label || key;
  }
  return key;
}

function policyNote(list, key) {
  for (var i = 0; i < list.length; i++) {
    if (String(list[i].key) === String(key)) return h('div', { class: 'small muted' }, textOf(list[i].note, ''));
  }
  return null;
}

/* ================= 页面：账号管理 ================= */

function realmBadge(realm) {
  var r = String(realm || 'cn');
  return badge(REGION_LABELS[r] || r, r === 'global' ? 'info' : 'ok');
}

function snapshotBadge(status) {
  var st = String(status || '');
  if (st.indexOf('可导入') === 0) return badge(st, 'ok');
  if (st.indexOf('已是用网关账号') === 0) return badge(st, 'info');
  if (st.indexOf('只有网页') === 0) return badge(st, 'warn');
  return badge(st || '未知', 'mute');
}

function importSnapshot(snap, btn) {
  var label = textOf(snap.file || snap.path, '');
  if (!window.confirm('把快照导入成 auths/<uid>.json ？' + String.fromCharCode(10) + label)) return;
  return fire(btn, '导入中…', function () {
    return apiPost('/panel/api/snapshots', {
      action: 'import',
      path: snap.path,
      uid: snap.uid,
      name: snap.name,
      realm: snap.realm,
      kind: snap.kind
    }).then(function (d) {
      toast('已导入：' + textOf(d && d.uid, snap.uid || ''), false);
      return load('accounts');
    }).catch(function (e) {
      if (e && e.status === 401) throw e;
      toast(errMessage(e && e.data, e && e.status) + '（导入没有完成）', true);
    });
  });
}

function pageAccounts() {
  var wrap = h('div', null);
  var poolStatus = state.pool;
  if (!poolStatus && !state.authFiles) {
    return state.loading ? loadingBox('正在读取账号…') : emptyBox('读不到账号信息');
  }

  /* 当前派发账号 —— 网关/DSH 正在用哪个号，必须先说清楚 */
  var curId = poolStatus && poolStatus.current;
  var curAcc = curId ? accountById(curId) : null;
  var lcA = liveCredits();
  var curLive = (lcA && lcA.accounts && curId && lcA.accounts[curId]) || null;
  wrap.appendChild(card('当前派发账号', h('button', {
    class: 'btn btn-sm',
    onclick: function () { setPage('tokens'); }
  }, '看用量'), h('div', null, [
    h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px' },
      accountBadges(curAcc, curId).concat([badge('网关与 DSH 共用', 'mute')])),
    h('p', { class: 'small muted', style: 'margin:0 0 10px' },
      curId ? 'UID ' + shortId(curId) + ' —— 点下面列表里的「切换」可以把派发换到别的号。'
            : '还没有指定当前账号，网关会按账号池策略自动挑号。'),
    kvList([
      ['它的积分余额', curLive
        ? (fmtInt(toInt(curLive.remain)) + ' / ' + fmtInt(toInt(curLive.total)))
        : (curAcc && toInt(curAcc.credits) ? fmtInt(toInt(curAcc.credits)) : '—')],
      ['上次使用', curAcc ? relTime(curAcc.last_used) : '—'],
      ['累计请求', curAcc ? (fmtInt(curAcc.requests) + ' / 成功 ' + fmtInt(curAcc.success) + ' / 失败 ' + fmtInt(curAcc.failures)) : '—']
    ])
  ])));
  wrap.appendChild(h('div', { class: 'section-gap' }));

  /* 登录区 */
  var login = state.login;
  var loginBody;
  if (login && !login.done) {
    loginBody = h('div', null, [
      h('p', { class: 'small muted', style: 'margin-bottom:10px' }, '用手机上的 WorkBuddy 扫描下面的链接完成设备授权；页面会自动轮询登录结果。'),
      h('div', { class: 'kv' }, [
        h('dt', null, '登录地址'),
        h('dd', null, h('a', { href: login.url || '#', target: '_blank', rel: 'noreferrer', class: 'mono small' }, login.url || '—')),
        h('dt', null, '状态'),
        h('dd', null, [h('span', { class: 'spinner' }), ' ' + textOf(login.message, '等待授权…')]),
        h('dt', null, '区域'),
        h('dd', null, realmBadge(login.realm)),
        h('dt', null, 'state'),
        h('dd', null, h('span', { class: 'mono small' }, login.state || '—'))
      ])
    ]);
  } else if (login && login.done) {
    loginBody = h('div', null, [
      h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center' }, [
        badge(login.ok ? '登录成功' : '登录失败', login.ok ? 'ok' : 'bad'),
        login.ok && login.nickname ? h('b', null, login.nickname) : null,
        login.ok ? realmBadge(login.realm) : null
      ]),
      h('div', { class: 'small muted', style: 'margin-top:8px' }, textOf(login.message, '')),
      login.ok && login.uid ? h('div', { class: 'small muted mono', style: 'margin-top:4px' },
        'UID ' + shortId(login.uid) + (login.file ? ' · ' + login.file : '')) : null
    ]);
  } else {
    loginBody = emptyBox('还没有进行中的登录。点上面的「扫码登录」开始。');
  }

  wrap.appendChild(card('设备授权登录', h('div', { class: 'toolbar' }, [
    h('button', { class: 'btn btn-sm btn-primary', onclick: function () { run(this, '发起中…', function () { return startLogin('cn'); }); } }, '扫码登录（国内版）'),
    h('button', { class: 'btn btn-sm', onclick: function () { run(this, '发起中…', function () { return startLogin('global'); }); } }, '扫码登录（国际版）'),
    login && !login.done ? h('button', { class: 'btn btn-sm', onclick: function () { state.login = null; if (state.loginTimer) clearTimeout(state.loginTimer); render(); } }, '取消') : null,
    login && login.done ? h('button', { class: 'btn btn-sm', onclick: function () { state.login = null; render(); } }, '关闭') : null
  ]), loginBody));

  /* 账号池表格 */
  var accounts = (poolStatus && poolStatus.accounts) || [];
  var rows = accounts.map(function (acc) {
    var r = toInt(acc.credits), t = toInt(acc.energy);
    var isCurrent = !!(poolStatus && poolStatus.current === acc.id);
    var ops = h('div', { class: 'toolbar', style: 'margin:0;gap:6px' }, [
      h('button', {
        class: 'btn btn-sm' + (isCurrent ? '' : ' btn-primary'),
        disabled: !acc.available || isCurrent,
        title: isCurrent ? '这个号正在派发' : '把网关和 DSH 的派发切到它',
        onclick: function () {
          run(this, '切换中…', function () {
            return apiPost('/panel/api/accounts/switch', { account_id: acc.id }).then(function () {
              if (state.pool) state.pool.current = acc.id;
              if (state.status) state.status.current = acc.id;
              toast('已把派发账号切成 ' + (acc.name || shortId(acc.id)) +
                '：网关和 DSH 后续请求都用它，扣它的积分', false);
              return load('accounts');
            });
          });
        }
      }, isCurrent ? '正在派发' : '设为当前'),
      h('button', {
        class: 'btn btn-sm',
        onclick: function () {
          if (!window.confirm('确认移除账号 ' + (acc.name || acc.id) + ' ？')) return;
          run(this, '移除中…', function () {
            return apiPost('/panel/api/accounts/remove', { account_id: acc.id }).then(function () {
              toast('已移除');
              return load('accounts');
            });
          });
        }
      }, '移除')
    ]);
    return h('tr', null, [
      h('td', null, [
        h('div', null, [textOf(acc.name, shortId(acc.id)), isCurrent ? ' ' : null,
          isCurrent ? badge('当前派发', 'ok') : null]),
        h('div', { class: 'small muted mono' },
          shortId(acc.id) + (acc.region ? ' · ' + (REGION_LABELS[acc.region] || acc.region) : ''))
      ]),
      h('td', { class: 'nowrap' }, textOf(acc.phone, '—')),
      h('td', { class: 'nowrap' }, REGION_LABELS[acc.region] || textOf(acc.region, '—')),
      h('td', { class: 'nowrap' }, [
        h('div', null, r > 0 ? fmtInt(r) : '0'),
        t > 0 ? h('div', { class: 'small muted' }, '能量 ' + fmtInt(t)) : null
      ]),
      h('td', { class: 'nowrap' }, [
        isCurrent ? badge('正在派发', 'ok')
          : (acc.active === false ? badge('已停用（不派发）', 'mute')
          : (acc.available ? badge('备用可派发', 'info') : badge('暂不可用', 'bad')))
      ]),
      h('td', { class: 'nowrap small muted' }, relTime(acc.last_used)),
      h('td', { class: 'nowrap small' }, fmtInt(acc.requests) + ' / ' + fmtInt(acc.success) + ' / ' + fmtInt(acc.failures)),
      h('td', { class: 'small muted' }, textOf(acc.reason, '')),
      h('td', null, ops)
    ]);
  });

  wrap.appendChild(card('账号池', h('div', { class: 'toolbar', style: 'margin:0' }, [
    h('button', { class: 'btn btn-sm', onclick: function () { run(this, '刷新中…', function () { return refreshTokens(); }); } }, '刷新 Token'),
    h('button', { class: 'btn btn-sm', onclick: function () { run(this, '重载中…', function () { return reloadAuths(); }); } }, '重载账号'),
    h('button', { class: 'btn btn-sm', onclick: function () { return load('accounts'); } }, '重新读取')
  ]), rows.length ? table(['账号', '手机', '区域', '积分', '状态', '最后使用', '请求/成功/失败', '不可用原因', '操作'], rows)
    : emptyBox('账号池是空的 —— 先用上面的扫码登录，或把 auths/*.json 放进 auth_dir')));

  /* 鉴权文件 */
  var files = (state.authFiles && state.authFiles.accounts) || [];
  var fileRows = files.map(function (f) {
    var kind = f.status === 'active' ? 'ok' : (f.status === 'expired' ? 'warn' : 'bad');
    var label = f.status === 'active' ? '有效' : (f.status === 'expired' ? '已过期' : '无效');
    return h('tr', null, [
      h('td', null, [h('div', null, textOf(f.name, f.file)), h('div', { class: 'small muted mono' }, f.file || '')]),
      h('td', { class: 'nowrap' }, textOf(f.phone, '—')),
      h('td', { class: 'nowrap' }, realmBadge(f.realm)),
      h('td', { class: 'nowrap' }, [badge(label, kind), f.error ? ' ' : null, f.error ? h('div', { class: 'small', style: 'color:var(--red)' }, f.error) : null]),
      h('td', { class: 'nowrap small muted' }, textOf(f.expires_at_human, '—')),
      h('td', { class: 'nowrap small muted' }, f.expires_in_seconds !== null && f.expires_in_seconds !== undefined ? fmtElapsed(f.expires_in_seconds) : '—')
    ]);
  });
  wrap.appendChild(card('鉴权文件（auths/）', h('span', { class: 'small muted' }, String(files.length) + ' 个'),
    fileRows.length ? table(['文件 / 账号', '手机', '区域', '状态', '到期时间', '剩余'], fileRows) : emptyBox('auth_dir 下没有 *.json')));

  /* 桌面快照发现 */
  var snaps = state.snapshots;
  if (snaps && snaps.snapshots) {
    var srows = snaps.snapshots.map(function (sn) {
      var act = (sn.importable && sn.path)
        ? h('button', { class: 'btn btn-sm', onclick: function () { return importSnapshot(sn, this); } }, '导入')
        : h('span', { class: 'small muted' }, '—');
      return h('tr', null, [
        h('td', null, [
          h('div', null, textOf(sn.name, sn.file || sn.path || '')),
          h('div', { class: 'small muted mono' }, textOf(sn.file || sn.path, ''))
        ]),
        h('td', { class: 'nowrap small' }, textOf(sn.kind, '—')),
        h('td', { class: 'nowrap' }, [snapshotBadge(sn.status), sn.name_suspect ? h('span', { class: 'small muted' }, ' ') : null, sn.name_suspect ? badge('名称可疑', 'warn') : null]),
        h('td', { class: 'nowrap small mono' }, textOf(sn.uid, '—')),
        h('td', { class: 'small muted' }, textOf(sn.action || sn.note, '')),
        h('td', { class: 'nowrap' }, act)
      ]);
    });
    wrap.appendChild(card('桌面快照发现', h('div', { class: 'toolbar', style: 'margin:0' }, [
      h('span', { class: 'small muted' }, String(snaps.snapshots.length) + ' 个候选'),
      h('button', { class: 'btn btn-sm', onclick: function () { return load('accounts'); } }, '重新扫描')
    ]), h('div', null, [
      srows.length ? table(['快照 / 身份', '类型', '状态', 'uid', '说明', '操作'], srows) : emptyBox('没有发现 login_state_*.json / identity_*.json'),
      h('p', { class: 'small muted' }, textOf(snaps.note, '')),
      h('p', { class: 'small muted' }, '导入要求快照里含明文 API accessToken（JWT）；旧版只存了网页会话 Cookie 的快照无法变成网关账号 —— 那些请用上面的「设备授权登录」扫码。')
    ])));
  }

  return wrap;
}

function startLogin(realm) {
  return apiGet('/panel/api/login/start?realm=' + encodeURIComponent(realm)).then(function (d) {
    if (!d || d.ok === false) throw new Error(textOf(d && d.error, '发起登录失败'));
    if (state.loginTimer) { clearTimeout(state.loginTimer); state.loginTimer = null; }
    state.login = { realm: d.realm || realm, state: d.state, url: d.url, message: '等待扫码授权…', done: false, ok: false };
    render();
    pollLogin();
    return d;
  });
}

function pollLogin() {
  if (state.loginTimer) { clearTimeout(state.loginTimer); state.loginTimer = null; }
  if (!state.login || state.login.done) return;
  state.loginTimer = setTimeout(function () {
    if (!state.login || state.login.done) return;
    apiGet('/panel/api/login/poll?state=' + encodeURIComponent(state.login.state)).then(function (d) {
      if (!state.login) return;
      if (d && d.message && !d.done) state.login.message = d.message;
      if (d && d.done) {
        var lg = state.login;
        lg.done = true;
        lg.ok = d.ok !== false;
        lg.uid = d.uid || '';
        lg.nickname = d.nickname || d.name || '';
        lg.realm = d.realm || lg.realm;
        lg.file = d.file || d.filename || '';
        lg.expiresAt = toInt(d.expires_at);
        if (lg.ok) {
          lg.message = '登录成功：' + (lg.nickname || shortId(lg.uid)) +
            (lg.realm ? ' · ' + (REGION_LABELS[lg.realm] || lg.realm) : '') +
            (lg.expiresAt > 0 ? ' · 凭据到期 ' + fmtDay(lg.expiresAt) : '') +
            '。凭据已写进 auths/，账号池已刷新。';
          toast('登录成功：' + (lg.nickname || shortId(lg.uid)) + ' 已载入账号池', false);
        } else {
          lg.message = '登录结束：' + textOf(d.message || d.error, '没有完成');
          toast(lg.message, true);
        }
        load('accounts');
        return;
      }
      if (d && d.ok === false) {
        state.login.done = true;
        state.login.ok = false;
        toast('登录失败：' + textOf(d.message || d.error, ''), true);
        render();
        return;
      }
      render();
      pollLogin();
    }).catch(function (e) {
      if (!state.login) return;
      state.login.message = '轮询失败：' + ((e && e.message) ? e.message : String(e));
      render();
      pollLogin();
    });
  }, 2000);
}

function refreshTokens() {
  return apiGet('/api/refresh-tokens').then(function (d) {
    var ok = toInt(d && d.success), bad = toInt(d && (d.failed || d.failures));
    toast('Token 刷新完成：成功 ' + ok + ' · 失败 ' + bad);
    return load('accounts');
  });
}

function reloadAuths() {
  return apiGet('/panel/api/reload-auths').then(function (d) {
    toast('账号已重载：' + String(toInt(d && (d.pool_accounts !== undefined ? d.pool_accounts : d.accounts))) + ' 个');
    return load('accounts');
  });
}

/* ================= 页面：模型 ================= */

function sourceSummary(item) {
  var src = item.sources || {};
  var hasOverride = false, hasUpstream = false;
  for (var k in src) {
    if (!Object.prototype.hasOwnProperty.call(src, k)) continue;
    if (src[k] === 'override') hasOverride = true;
    else if (src[k] === 'upstream') hasUpstream = true;
  }
  if (hasOverride) return { label: '手改', kind: 'warn' };
  if (hasUpstream) return { label: '上游', kind: 'info' };
  return { label: '内置', kind: 'mute' };
}

function imgCell(v) {
  if (v === true) return badge('支持', 'ok');
  if (v === false) return badge('不支持', 'mute');
  return badge('未知', 'mute');
}

function promoText(p) {
  if (!p) return '';
  var label = p.label || p.model || p.kind || '';
  if (p.window_text) return label ? (label + ' · ' + p.window_text) : p.window_text;
  return String(label);
}

function pageModels() {
  var view = state.models;
  var wrap = h('div', null);
  if (!view) {
    return state.loading ? loadingBox('正在读取模型目录…') : emptyBox('模型目录接口没有返回数据');
  }
  var items = view.items || [];
  var regions = view.regions || [
    { key: 'merged', label: '合并' }, { key: 'cn', label: '国内版' }, { key: 'global', label: '国际版' }
  ];

  var q = state.mQuery.toLowerCase();
  var shown = items.filter(function (it) {
    if (!q) return true;
    var hay = String(it.id || '') + ' ' + String(it.name || '') + ' ' + String(it.family || '');
    return hay.toLowerCase().indexOf(q) >= 0;
  });

  var enabledCount = (view.enabled || []).length;
  var disabledCount = (view.disabled || []).length;

  wrap.appendChild(card('模型目录', h('div', { class: 'toolbar', style: 'margin:0' }, [
    h('button', { class: 'btn btn-sm', title: '所有模型都出现在 /v1/models 并被派发', onclick: function () { run(this, '处理中…', function () { return bulkModels(true); }); } }, '全部启用'),
    h('button', { class: 'btn btn-sm', title: '所有模型都不再出现、也不被派发', onclick: function () { run(this, '处理中…', function () { return bulkModels(false); }); } }, '全部隐藏'),
    h('button', { class: 'btn btn-sm', onclick: function () { return load('models'); } }, '重新读取')
  ]), h('div', null, [
    h('div', { class: 'toolbar' }, [
      tabs(regions, state.mRegion, function (key) {
        state.mRegion = key;
        return load('models');
      }),
      h('input', {
        type: 'search',
        placeholder: '搜索模型 id / 名称 / 家族',
        value: state.mQuery,
        style: 'min-width:220px',
        oninput: function (e) { state.mQuery = e.target.value || ''; render(); }
      }),
      h('div', { class: 'spacer' }),
      h('span', { class: 'small muted' }, '共 ' + String(items.length) + ' 个 · 会派发 ' + String(enabledCount) + ' · 不派发 ' + String(disabledCount))
    ]),
    h('p', { class: 'small muted' }, '开关按区域生效：隐藏的模型不会再出现在 /v1/models，也不会被网关派发；跨区调用仍显式报错而不是静默替换。')
  ])));

  var rows = shown.map(function (it) {
    if (it.known === false) {
      return h('tr', null, [
        h('td', { class: 'mono' }, it.id),
        h('td', { colspan: '8', class: 'small muted' }, '目录里没有这个模型的元数据')
      ]);
    }
    var src = sourceSummary(it);
    var badges = (it.badges || []).map(function (b) { return badge(promoText(b), 'ok'); });
    var efforts = (it.reasoning_efforts && it.reasoning_efforts.length) ? it.reasoning_efforts.join(' / ') : '—';
    return h('tr', null, [
      h('td', null, [
        h('div', null, [textOf(it.name, it.id), badges.length ? ' ' : null].concat(badges)),
        h('div', { class: 'small muted mono' }, it.id)
      ]),
      h('td', { class: 'nowrap small' }, textOf(it.family, '—')),
      h('td', { class: 'nowrap small' }, (it.region_labels && it.region_labels.length) ? it.region_labels.join(' / ') : '—'),
      h('td', { class: 'nowrap' }, it.context_window ? fmtCompact(it.context_window) : '未知'),
      h('td', { class: 'nowrap' }, it.max_tokens ? fmtCompact(it.max_tokens) : '未知'),
      h('td', { class: 'nowrap' }, imgCell(it.supports_image)),
      h('td', { class: 'nowrap small' }, efforts),
      h('td', { class: 'nowrap' }, it.rate === null || it.rate === undefined ? '—' : String(it.rate)),
      h('td', { class: 'nowrap' }, badge(src.label, src.kind)),
      h('td', { class: 'right nowrap' }, h('button', {
        class: 'btn btn-sm',
        onclick: function () {
          var target = !it.enabled;
          run(this, '…', function () { return toggleModel(it, target); });
        }
      }, it.enabled ? '已启用 · 会派发' : '已隐藏 · 不派发'))
    ]);
  });

  wrap.appendChild(card('模型（' + (REGION_LABELS[state.mRegion] || state.mRegion) + '）',
    h('span', { class: 'small muted' }, '共 ' + String(shown.length) + ' 行'),
    rows.length ? table(['模型', '家族', '区域', '上下文', '最大输出', '图片', '推理档位', '倍率', '来源', '开关'], rows)
      : emptyBox('没有匹配的模型')));

  var stats = view.stats || {};
  wrap.appendChild(card('目录信息', null, kvList([
    ['区域', REGION_LABELS[view.region] || textOf(view.region, '合并')],
    ['目录版本', textOf(stats.version, '—')],
    ['内置模型', String(toInt(stats.builtin))],
    ['上游发现', String(toInt(stats.discovered))],
    ['手工覆盖', String(toInt(stats.overrides))],
    ['活动促销', String(toInt(stats.promotions))],
    ['最后更新', fmtTs(view.updated)],
    ['目录文件', h('span', { class: 'mono small' }, textOf(stats.path, '—'))]
  ])));

  return wrap;
}

function modelsRegionParam() {
  return (state.mRegion && state.mRegion !== 'merged') ? state.mRegion : undefined;
}

function toggleModel(item, wantEnabled) {
  return apiPost('/panel/api/models', {
    action: 'toggle',
    model: item.id,
    region: modelsRegionParam(),
    enabled: !!wantEnabled
  }).then(function (d) {
    state.models = d;
    toast(item.id + (wantEnabled ? ' 已启用（会出现在 /v1/models 并被派发）' : ' 已隐藏（不再出现在 /v1/models，也不会被派发）'));
    render();
  });
}

function bulkModels(wantEnabled) {
  var view = state.models;
  if (!view) return Promise.resolve();
  var ids = view.items.filter(function (it) { return it.known !== false; }).map(function (it) { return it.id; });
  var hasCustomFilter = !!state.mQuery;
  if (!window.confirm((wantEnabled ? '启用' : '隐藏') + '当前区域的全部 ' + String(ids.length) + ' 个模型？' + (hasCustomFilter ? '（注意：搜索过滤不会限制批量范围）' : ''))) {
    return Promise.resolve();
  }
  return apiPost('/panel/api/models', {
    action: 'bulk',
    models: ids,
    region: modelsRegionParam(),
    enabled: !!wantEnabled
  }).then(function (d) {
    state.models = d;
    toast('已批量' + (wantEnabled ? '启用' : '隐藏'));
    render();
  });
}

/* ================= 页面：Token 统计 ================= */

function usageTotals() {
  return (state.usage && state.usage.totals) || {
    requests: 0, success: 0, failures: 0, input: 0, output: 0, total: 0,
    cached_read: 0, cache_write: 0, cache_miss: 0, cache_hit_rate: 0, duration_ms: 0,
    share: {}
  };
}

function updateModelOptions() {
  if (!state.usage || state.uModel) return;
  var list = state.usage.by_model || [];
  state.modelOptions = list.map(function (m) { return m.key; });
}

function shareParts(t) {
  return [
    { value: t.cached_read, color: '#34d399', label: '缓存读取', text: fmtShortTokens(t.cached_read) },
    { value: t.cache_miss, color: '#60a5fa', label: '缓存未命中', text: fmtShortTokens(t.cache_miss) },
    { value: t.output, color: '#fbbf24', label: '输出', text: fmtShortTokens(t.output) },
    { value: t.cache_write, color: '#a78bfa', label: '缓存写入', text: fmtShortTokens(t.cache_write) }
  ];
}

function usageTable(rows, firstHead) {
  if (!rows || !rows.length) return emptyBox('这个范围内没有数据');
  var body = rows.map(function (r) {
    var t = r;
    return h('tr', null, [
      h('td', null, [h('div', null, textOf(r.label && r.label !== r.key ? r.label : r.key, '—')),
        r.label && r.label !== r.key ? h('div', { class: 'small muted mono' }, textOf(r.key, '')) : null]),
      h('td', { class: 'nowrap' }, fmtInt(t.requests)),
      h('td', { class: 'nowrap small muted' }, fmtPct(t.requests > 0 ? t.success / t.requests : 0, 0)),
      h('td', { class: 'nowrap' }, fmtShortTokens(t.input)),
      h('td', { class: 'nowrap' }, fmtShortTokens(t.output)),
      h('td', { class: 'nowrap' }, fmtShortTokens(t.total)),
      h('td', { class: 'nowrap' }, fmtPct(t.cache_hit_rate, 0)),
      h('td', { class: 'nowrap small' }, fmtMs(t.duration_ms))
    ]);
  });
  return table([firstHead || '维度', '请求', '成功率', '输入', '输出', '合计', '缓存命中', '耗时'], body);
}

function pageTokens() {
  var wrap = h('div', null);
  var meta = state.usageMeta || {};
  var usage = state.usage;
  if (meta._error) {
    wrap.appendChild(card('Token 统计不可用', null, emptyBox(meta._error)));
    return wrap;
  }
  if (!usage) {
    return state.loading ? loadingBox('正在聚合用量…') : emptyBox('用量接口没有返回数据');
  }

  var ranges = meta.ranges || [
    { key: 'today', label: '今天' }, { key: '7d', label: '近 7 天' },
    { key: '30d', label: '近 30 天' }, { key: 'all', label: '全部' }
  ];
  var regionTabs = [
    { key: 'merged', label: '合并' }, { key: 'cn', label: '国内版' }, { key: 'global', label: '国际版' }
  ];
  var clients = [{ key: '', label: '全部客户端' }].concat(meta.clients || []);
  updateModelOptions();
  var modelOptions = [{ key: '', label: '全部模型' }].concat((state.modelOptions || []).map(function (m) {
    return { key: m, label: m };
  }));

  var t = usageTotals();

  wrap.appendChild(h('div', { class: 'toolbar' }, [
    tabs(regionTabs, state.uRegion, function (key) { state.uRegion = key; return loadUsage().then(render); }),
    tabs(ranges, state.uRange, function (key) { state.uRange = key; return loadUsage().then(render); }),
    h('div', { class: 'spacer' }),
    h('button', { class: 'btn btn-sm', onclick: function () { return load('tokens'); } }, '刷新')
  ]));

  wrap.appendChild(h('div', { class: 'toolbar' }, [
    tabs(clients, state.uClient, function (key) { state.uClient = key; return loadUsage().then(render); }),
    h('select', {
      onchange: function (e) { state.uModel = e.target.value || ''; return loadUsage().then(render); }
    }, modelOptions.map(function (m) {
      return h('option', { value: m.key, selected: state.uModel === m.key }, m.label);
    }))
  ]));

  wrap.appendChild(h('div', { class: 'grid' }, [
    statCard('总 Token', fmtShortTokens(t.total), '输入 ' + fmtShortTokens(t.input) + ' · 输出 ' + fmtShortTokens(t.output), true),
    statCard('请求数', fmtInt(t.requests), '成功 ' + fmtInt(t.success) + ' · 失败 ' + fmtInt(t.failures)),
    statCard('缓存命中率', t.requests > 0 ? fmtPct(t.cache_hit_rate) : '—',
      '命中读取 ' + fmtShortTokens(t.cached_read) + ' · 未命中 ' + fmtShortTokens(t.cache_miss)),
    statCard('上游耗时', t.requests > 0 ? fmtMs(t.requests > 0 ? t.duration_ms / t.requests : 0) : '—',
      '总额 ' + fmtMs(t.duration_ms))
  ]));

  wrap.appendChild(h('div', { class: 'section-gap' }));

  /* 占比 + 趋势 */
  var canvas = h('canvas', { id: 'trendCanvas' });
  wrap.appendChild(card('用量趋势', h('span', { class: 'small muted' },
      (usage.grain === 'hour' ? '按小时' : '按天') + ' · ' + fmtDay(usage.since) + ' ~ ' + fmtDay(usage.until)), h('div', null, [
    t.total > 0 ? stackBar(shareParts(t)) : null,
    t.total > 0 ? h('div', { style: 'margin-top:8px' }, legend(shareParts(t).map(function (p) {
      return { color: p.color, label: p.label, value: p.text };
    }).concat([{ color: 'var(--text)', label: '请求数（虚线，右轴）' }]))) : null,
    h('div', { class: 'chart-box', style: 'margin-top:12px' }, canvas),
    t.requests === 0 ? emptyBox('这个范围内还没有通过网关的请求。用 /v1/chat/completions 打一次就会出现在这里。') : null
  ])));

  var list = usage.series || [];
  var byModel = usage.by_model || [];
  var byClient = usage.by_client || [];
  var byAccount = usage.by_account || [];
  var byRegion = usage.by_region || [];

  wrap.appendChild(card('按模型', h('span', { class: 'small muted' }, String(byModel.length) + ' 个模型'),
    usageTable(byModel, '模型')));
  wrap.appendChild(card('按客户端', null, usageTable(byClient, '客户端')));
  wrap.appendChild(card('按区域', null, usageTable(byRegion.map(function (r) {
    return Object.assign({}, r, { label: REGION_LABELS[r.key] || r.label || r.key });
  }), '区域')));
  wrap.appendChild(card('按账号（花的是谁的积分）', h('span', { class: 'small muted' }, '账号 ID 维度'), usageTable(byAccount, '账号')));

  var uas = meta.top_user_agents || [];
  wrap.appendChild(card('最近见过的 User-Agent', h('span', { class: 'small muted' }, '排查「未知来源」用'),
    uas.length ? table(['User-Agent', '次数'], uas.map(function (u) {
      return h('tr', null, [
        h('td', { class: 'small mono' }, textOf(u.user_agent, '—')),
        h('td', { class: 'nowrap' }, fmtInt(u.count))
      ]);
    })) : emptyBox('还没有记录到 UA')));

  var store = meta.store || {};
  wrap.appendChild(card('存储信息', null, kvList([
    ['范围', textOf(usage.range) + ' · ' + textOf(usage.grain)],
    ['区域', REGION_LABELS[usage.region] || textOf(usage.region)],
    ['客户端', textOf(usage.client, '全部')],
    ['模型', textOf(usage.model, '全部')],
    ['数据版本', textOf(store.version, '—')],
    ['桶 / 账号', String(toInt(store.buckets)) + ' / ' + String(toInt(store.accounts))],
    ['保留天数', String(toInt(store.retention_days))],
    ['文件', h('span', { class: 'mono small' }, textOf(store.path, '—'))]
  ])));
  wrap._series = list;
  return wrap;
}

/* ================= 页面：积分统计 ================= */

function resetCredits() {
  return apiGet('/api/credits/live?refresh=1').catch(softEmpty).then(function (d) {
    state.credits = d;
    toast('已强制刷新余额');
    render();
  });
}

function pageCredits() {
  var wrap = h('div', null);
  var data = state.credits;
  if (!data) {
    return state.loading ? loadingBox('正在查询实时余额…') : emptyBox('余额接口没有返回数据');
  }
  if (!data.accounts || !Object.keys(data.accounts).length) {
    wrap.appendChild(card('实时余额', null, emptyBox('没有账号，或上游账单接口暂时不可达')));
    return wrap;
  }

  var names = {};
  var files = (state.authFiles && state.authFiles.accounts) || [];
  for (var i = 0; i < files.length; i++) names[String(files[i].uid)] = files[i].name || files[i].phone || files[i].file;

  wrap.appendChild(h('div', { class: 'grid' }, [
    statCard('总剩余积分', fmtInt(data.total_remain), '资源包总量 ' + fmtInt(data.total_size), true),
    statCard('7 天内到期', fmtInt(data.expiring_7d), data.expiring_7d > 0 ? '记得优先用掉' : '近期没有到期积分'),
    statCard('账号数', String(Object.keys(data.accounts).length), '按 uid 聚合'),
    statCard('查询时间', fmtTs(data.queried_at), data.forced ? '本次为强制刷新' : '来自 30 秒缓存')
  ]));

  wrap.appendChild(h('div', { class: 'section-gap' }));

  var uids = Object.keys(data.accounts);
  uids.sort(function (a, b) { return toInt(data.accounts[b].remain) - toInt(data.accounts[a].remain); });

  for (var k = 0; k < uids.length; k++) {
    var uid = uids[k];
    var info = data.accounts[uid] || {};
    var remain = toInt(info.remain), size = toInt(info.total);
    var packs = info.packages || [];
    var packRows = packs.map(function (p) {
      var pr = toInt(p.remain), ps = toInt(p.size);
      return h('tr', null, [
        h('td', null, textOf(p.name || p.package_name || p.title, '资源包')),
        h('td', { class: 'nowrap' }, fmtInt(pr) + ' / ' + fmtInt(ps)),
        h('td', { style: 'min-width:160px' }, bar(ps > 0 ? pr / ps : 0, 'sm')),
        h('td', { class: 'nowrap small muted' }, textOf(p.end_time || p.expire_time, '—'))
      ]);
    });
    wrap.appendChild(card(names[uid] || shortId(uid), h('div', { class: 'toolbar', style: 'margin:0' }, [
      badge('剩余 ' + fmtInt(remain), remain > 0 ? 'ok' : 'bad'),
      size > 0 ? badge('共 ' + fmtInt(size), 'mute') : null,
      toInt(info.expiring) > 0 ? badge('7 天到期 ' + fmtInt(info.expiring), 'warn') : null
    ]), h('div', null, [
      h('div', { style: 'margin-bottom:10px' }, [bar(size > 0 ? remain / size : 0),
        h('div', { class: 'small muted', style: 'margin-top:4px' }, size > 0 ? fmtPct(remain / size) : '资源包总量未知')]),
      packRows.length ? table(['资源包', '剩余 / 总量', '使用比例', '到期时间'], packRows)
        : emptyBox('这个账号没有资源包明细'),
      h('div', { class: 'small muted mono', style: 'margin-top:8px' }, 'uid ' + uid)
    ])));
  }

  wrap.appendChild(card('操作', h('button', { class: 'btn btn-sm btn-primary', onclick: function () { run(this, '刷新中…', resetCredits); } }, '强制刷新余额'), kvList([
    ['接口', h('span', { class: 'mono small' }, 'GET /api/credits/live?refresh=1')],
    ['说明', '实时余额来自上游 billing meter；默认 30 秒缓存，强制刷新会逐个账号打上游。']
  ])));

  return wrap;
}

/* ================= 页面：任务中心 ================= */

function fire(btn, label, fn) {
  var p = run(btn, label, fn);
  if (p && p.catch) p.catch(function () { /* 已由 run 弹提示 */ });
  return p;
}

function taskMeta() {
  var t = state.tasks || {};
  var list = t.tasks || [];
  var auto = t.auto_codes || [];
  var manual = 0;
  for (var i = 0; i < list.length; i++) {
    if (String(list[i].kind) === 'manual') manual++;
  }
  return { list: list, auto: auto, manual: manual, accounts: toInt(t.accounts) };
}

function runTaskAction(action, btn, label) {
  var body = { dry_run: !!state.dryRun };
  if (action === 'run') body.claim = true;
  if (state.taskCodes && state.taskCodes.length) body.codes = state.taskCodes;
  return fire(btn, label, function () {
    return apiPost('/tasks/' + action, body).then(function (d) {
      state.taskResult = { action: action, data: d, at: Date.now() };
      var n = (d && d.accounts) ? d.accounts.length : 0;
      toast((action === 'scan' ? '扫描完成' : (action === 'keepalive' ? '保活完成' : '执行完成')) + '：' + String(n) + ' 个账号');
      render();
    });
  });
}

function progressCell(s) {
  var after = s.progress_after || s.progress_before || {};
  if (!after.target) return '—';
  return fmtInt(after.current || 0) + ' / ' + fmtInt(after.target);
}

function taskResultCards() {
  var r = state.taskResult;
  if (!r) return [];
  var d = r.data || {};
  var out = [];
  var title = r.action === 'scan' ? '最近一次扫描' : (r.action === 'keepalive' ? '最近一次保活' : '最近一次执行');
  var kids = [h('div', { class: 'small muted', style: 'margin-bottom:8px' },
    title + ' · ' + fmtTs(Math.round(r.at / 1000)) + ' · dry_run=' + String(!!state.dryRun))];

  if (d.error) kids.push(emptyBox(String(d.error)));
  if (d.errors && d.errors.length) {
    kids.push(table(['账号', '错误'], d.errors.map(function (e) {
      return h('tr', null, [
        h('td', { class: 'nowrap small' }, textOf(e.name || e.uid, '—')),
        h('td', { class: 'small' }, textOf(e.error, ''))
      ]);
    })));
  }

  var items = d.accounts || [];
  if (r.action === 'scan' && d.tasks && d.tasks.length) {
    kids.push(table(['账号', '任务码', '进度', '领取状态'], d.tasks.map(function (t) {
      var pr = t.progress || {};
      return h('tr', null, [
        h('td', { class: 'nowrap small' }, textOf(t.name, shortId(t.uid))),
        h('td', { class: 'mono small' }, textOf(t.task_code || t.code, '—')),
        h('td', { class: 'nowrap' }, fmtInt(pr.current || 0) + ' / ' + fmtInt(pr.target || 0)),
        h('td', { class: 'nowrap small' }, textOf(t.accept_status, '—'))
      ]);
    })));
  }

  if (!items.length && !d.error) kids.push(emptyBox('这次没有返回账号结果'));
  for (var i = 0; i < items.length; i++) {
    var a = items[i] || {};
    var name = textOf(a.name, shortId(a.uid));
    var right = null;
    var body = null;
    if (r.action === 'scan') {
      right = h('div', { class: 'toolbar', style: 'margin:0' }, [
        badge('任务 ' + fmtInt(a.task_count), 'info'),
        badge('命中 ' + fmtInt(a.matched), 'mute')
      ]);
      body = emptyBox('该账号有 ' + String(toInt(a.matched)) + ' 个可自动任务（目录共 ' + String(toInt(a.task_count)) + '）');
    } else if (r.action === 'keepalive') {
      right = badge(a.ok ? '保活成功' : '保活失败', a.ok ? 'ok' : 'bad');
      body = kvList([['状态', textOf(a.status, '—')], ['说明', textOf(a.msg, '—')]]);
    } else {
      right = h('div', { class: 'toolbar', style: 'margin:0' }, [
        badge('失败 ' + fmtInt(a.errors), toInt(a.errors) > 0 ? 'bad' : 'mute'),
        badge('跳过 ' + fmtInt(a.skipped), 'mute')
      ]);
      var steps = a.steps || [];
      body = steps.length ? table(['任务', '结果', '进度', '说明'], steps.map(function (s) {
        var tag = s.skipped ? badge('已完成', 'mute') : (s.ok ? badge(textOf(s.effect, '完成'), 'ok') : badge(textOf(s.effect, '未达标'), 'warn'));
        return h('tr', null, [
          h('td', { class: 'mono small' }, textOf(s.code, '—')),
          h('td', { class: 'nowrap' }, tag),
          h('td', { class: 'nowrap small' }, progressCell(s)),
          h('td', { class: 'small' }, textOf(s.msg, ''))
        ]);
      })) : emptyBox('没有步骤结果');
    }
    out.push(card(name, right, body));
  }
  return [card('执行结果', null, h('div', null, kids))].concat(out);
}

function pageTasks() {
  var wrap = h('div', null);
  var t = state.tasks;
  if (!t) {
    return state.loading ? loadingBox('正在读取任务目录…') : emptyBox('任务接口没有返回数据');
  }
  if (t.error) {
    wrap.appendChild(card('任务引擎不可用', null, emptyBox(String(t.error))));
    return wrap;
  }
  var meta = taskMeta();

  wrap.appendChild(h('div', { class: 'grid' }, [
    statCard('任务目录', String(meta.list.length), '可自动 ' + String(meta.auto.length) + ' · 需手工 ' + String(meta.manual), true),
    statCard('可用账号', String(meta.accounts), meta.accounts > 0 ? '来自 auths/ 与网关池' : '先扫码登录账号'),
    statCard('执行模式', state.dryRun ? '演练（不扣分）' : '真实执行', state.dryRun ? '只打印将要做的事' : '会真的上报与领奖'),
    statCard('结果复查', '执行后回查', 'HTTP 200 不代表进度已涨，界面按回查结果判定')
  ]));

  wrap.appendChild(h('div', { class: 'section-gap' }));

  wrap.appendChild(card('批量操作', null, h('div', null, [
    h('div', { class: 'toolbar' }, [
      h('button', { class: 'btn btn-sm', onclick: function () { runTaskAction('scan', this, '扫描中…'); } }, '扫描任务进度'),
      h('button', { class: 'btn btn-sm btn-primary', onclick: function () { runTaskAction('run', this, '执行中…'); } }, '一键完成全部'),
      h('button', { class: 'btn btn-sm', onclick: function () { runTaskAction('keepalive', this, '保活中…'); } }, '账号保活'),
      h('label', { class: 'small muted', style: 'display:flex;align-items:center;gap:6px' }, [
        h('input', {
          type: 'checkbox',
          checked: !!state.dryRun,
          onchange: function (e) { state.dryRun = !!e.target.checked; render(); }
        }),
        '演练模式（dry_run）'
      ])
    ]),
    h('p', { class: 'small muted' }, '「一键完成全部」会逐个账号跑完可自动任务并在达标后领奖，耗时较长（每个账号几十秒）；建议先用演练模式看一遍。Expert_Philanthropy 需要真实捐款，无法用 API 完成，已排除在自动范围外。')
  ])));

  wrap.appendChild(card('任务目录', h('span', { class: 'small muted' }, '共 ' + String(meta.list.length) + ' 条'),
    table(['任务码', '类型', '奖励', '说明', '自动'], meta.list.map(function (x) {
      var isManual = String(x.kind) === 'manual';
      return h('tr', null, [
        h('td', { class: 'mono small' }, textOf(x.code, '—')),
        h('td', { class: 'nowrap small' }, textOf(x.kind, '—')),
        h('td', { class: 'nowrap small' }, textOf(x.reward, '—')),
        h('td', { class: 'small' }, textOf(x.desc, '')),
        h('td', { class: 'nowrap' }, isManual ? badge('需手工', 'warn') : badge('可自动', 'ok'))
      ]);
    }))));

  var cards = taskResultCards();
  for (var i = 0; i < cards.length; i++) wrap.appendChild(cards[i]);
  if (!cards.length) {
    wrap.appendChild(card('执行结果', null, emptyBox('还没有执行记录。点上面的「扫描任务进度」先看每个账号还差什么。')));
  }
  return wrap;
}

/* ================= 页面：签到日志 ================= */

/* ================= 页面：签到日志 ================= */

var CHECKIN_STATUS = {
  claimed: { text: '已签到', kind: 'ok' },
  'already-claimed': { text: '今天已签到', kind: 'mute' },
  'no-campaign': { text: '本期无活动', kind: 'warn' },
  'dry-run': { text: '演练', kind: 'info' },
  error: { text: '失败', kind: 'bad' },
  unknown: { text: '未签到', kind: 'mute' }
};

function checkinBadge(status) {
  var s = CHECKIN_STATUS[String(status || 'unknown')];
  if (s) return badge(s.text, s.kind);
  return badge(String(status || '未知'), 'warn');
}

function runCheckin(opts, btn, label) {
  var body = { action: 'run' };
  if (opts && opts.catch_up !== undefined) body.catch_up = !!opts.catch_up;
  if (opts && opts.dry_run) body.dry_run = true;
  if (opts && opts.force) body.force = true;
  return fire(btn, label, function () {
    return apiPost('/panel/api/checkin', body).then(function (d) {
      if (d && d.report) state.checkin = d.report;
      var claimed = toInt(d && d.claimed);
      var already = toInt(d && d.already);
      var failed = toInt(d && d.failed);
      var amount = toInt(d && d.amount);
      var msg = '';
      if (body.dry_run) {
        msg = '演练完成（没有真的发请求）';
      } else {
        msg = '签到完成：成功 ' + String(claimed) + ' 个';
        if (amount) msg = msg + '，获得 ' + fmtInt(amount) + ' 积分';
        if (already) msg = msg + '，' + String(already) + ' 个今天已签到';
        if (failed) msg = msg + '，失败 ' + String(failed) + ' 个';
      }
      toast(msg, failed > 0 && claimed === 0);
      render();
    });
  });
}

function checkinLogRows(rows) {
  return rows.slice(0, 400).map(function (r) {
    var kind = String(r.kind || 'task');
    var okTag;
    if (r.skipped) okTag = badge('已跳过', 'mute');
    else if (kind === 'checkin') okTag = checkinBadge(r.status);
    else if (r.ok) okTag = badge(textOf(r.effect, '完成'), 'ok');
    else okTag = badge(textOf(r.effect, '未达标'), 'warn');
    var amount = (r.amount === undefined || r.amount === null || r.amount === '') ? '' : (String(r.amount) + ' 积分');
    return h('tr', null, [
      h('td', { class: 'nowrap small' }, textOf(r.ts, '—')),
      h('td', { class: 'nowrap small' }, textOf(r.name || r.uid, '—')),
      h('td', { class: 'nowrap' }, kind === 'checkin' ? badge('签到', 'info') : badge('任务', 'mute')),
      h('td', { class: 'mono small' }, textOf(r.code || r.task_code, '—')),
      h('td', { class: 'nowrap' }, okTag),
      h('td', { class: 'nowrap small' }, textOf(r.reward, amount || '—')),
      h('td', { class: 'small' }, textOf(r.msg, ''))
    ]);
  });
}

function pageCheckin() {
  var wrap = h('div', null);
  var d = state.checkin;
  if (!d) {
    return state.loading ? loadingBox('正在读取签到与收益账本…') : emptyBox('签到接口没有返回数据');
  }

  if (d._unavailable || d.error) {
    var why = d._unavailable ? d._unavailable : ((d.error && d.error.message) ? d.error.message : String(d.error));
    wrap.appendChild(card('签到日志尚未启用', h('button', {
      class: 'btn btn-sm',
      onclick: function () { return load('checkin'); }
    }, '重试'), h('div', null, [
      emptyBox(String(why)),
      h('p', { class: 'small muted' }, '这个页面由 wb_checkin.py 提供：GET /panel/api/checkin 读 wb_data/tasks_log.jsonl 合成收益账本，并按 checkin.ts 的协议发每日签到（国内版 www.workbuddy.cn、国际版 www.workbuddy.ai 的 /v2/billing/meter/daily-checkin）。'),
      kvList([
        ['接口', h('span', { class: 'mono small' }, 'GET / POST /panel/api/checkin')],
        ['日志文件', h('span', { class: 'mono small' }, 'wb_data/tasks_log.jsonl')],
        ['状态文件', h('span', { class: 'mono small' }, 'data/checkin_state.json')]
      ])
    ])));
    return wrap;
  }

  var accountRows = d.account_rows || [];
  var summary = d.summary || {};
  var ledgerRows = d.ledger || [];
  var rows = d.rows || [];
  var today = textOf(d.today, '');
  var done = 0;
  var pending = 0;
  for (var i = 0; i < accountRows.length; i++) {
    if (accountRows[i].today_done) done++;
    else if (accountRows[i].own_time_passed) pending++;
  }

  wrap.appendChild(card('每日签到', badge(today, 'info'), h('div', null, [
    h('div', { class: 'toolbar' }, [
      h('button', { class: 'btn btn-sm btn-primary', onclick: function () { runCheckin({ catch_up: true }, this, '签到中…'); } }, '立即签到（含补签）'),
      h('button', { class: 'btn btn-sm', onclick: function () { runCheckin({ catch_up: false }, this, '检查时点…'); } }, '只按错峰时点'),
      h('button', { class: 'btn btn-sm', onclick: function () { runCheckin({ catch_up: true, force: true }, this, '强制重签…'); } }, '强制重签'),
      h('button', { class: 'btn btn-sm', onclick: function () { runCheckin({ catch_up: true, dry_run: true }, this, '演练中…'); } }, '演练（不发请求）')
    ]),
    h('p', { class: 'small muted' }, '「立即签到」忽略每个账号自己的错峰时点，把所有还没签的账号一次补上（Catch-up，对应 checkin-scheduler 的补签语义）；「只按错峰时点」要等该账号当天的时点过了才发；「强制重签」连今天已签过的也再打一次（上游会回「今天已签到」，不会重复加分）。同一账号同一时刻只会有一个请求进去。')
  ])));

  wrap.appendChild(h('div', { class: 'grid' }, [
    statCard('今日已签到', String(done) + ' / ' + String(accountRows.length),
      (accountRows.length && done >= accountRows.length) ? '今天的签到都做完了' : (pending ? String(pending) + ' 个已过自己时点、待签' : '还没到自己的错峰时点'), true),
    statCard('账本签到积分', fmtInt(summary.checkin_amount), '共 ' + fmtInt(summary.checkin_count) + ' 次签到'),
    statCard('任务完成', fmtInt(summary.task_ok) + ' / ' + fmtInt(summary.task_count),
      summary.task_skipped ? '另有 ' + fmtInt(summary.task_skipped) + ' 条已跳过' : '账本里记录的任务'),
    statCard('账本条目', fmtInt(summary.entries), (d.log && d.log.exists) ? ('日志 ' + fmtCompact(d.log.size_bytes) + ' B') : '日志文件还没生成')
  ]));
  wrap.appendChild(h('div', { class: 'section-gap' }));

  var statRows = accountRows.map(function (a) {
    var when = a.last_at ? (fmtTs(a.last_at) + ' · ' + relTime(a.last_at)) : '从未';
    var amount = (a.amount === undefined || a.amount === null) ? (a.today_done ? '—' : '') : String(a.amount);
    return h('tr', null, [
      h('td', null, [h('div', null, textOf(a.name, a.uid)), h('div', { class: 'small muted mono' }, textOf(a.uid, ''))]),
      h('td', { class: 'nowrap' }, realmBadge(a.realm)),
      h('td', { class: 'nowrap' }, a.today_done ? badge('今天已签', 'ok') : badge('未签', 'mute')),
      h('td', { class: 'nowrap' }, checkinBadge(a.status)),
      h('td', { class: 'nowrap small muted' }, when),
      h('td', { class: 'nowrap' }, amount === '' ? '—' : amount),
      h('td', { class: 'nowrap small mono' }, textOf(a.own_time, '—')),
      h('td', { class: 'small muted' }, textOf(a.message, ''))
    ]);
  });
  wrap.appendChild(card('每账号签到状态', h('span', { class: 'small muted' }, String(accountRows.length) + ' 个账号'),
    statRows.length ? table(['账号', '区域', '今日', '状态', '上次签到', '积分', '错峰时点', '说明'], statRows)
      : emptyBox('没有可用账号 —— 先用「账号管理」页扫码登录，或把 auths/*.json 放进 auth_dir')));

  var ledRows = ledgerRows.map(function (b) {
    var rewardText = (b.rewards || []).map(function (r) { return r.reward; }).join('、');
    return h('tr', null, [
      h('td', { class: 'nowrap small' }, textOf(b.date, '—') + ' ' + textOf(b.weekday, '')),
      h('td', { class: 'nowrap small' }, fmtInt(b.entries)),
      h('td', { class: 'nowrap small' }, fmtInt(b.checkin_count) + ' 次'),
      h('td', { class: 'nowrap small' }, b.checkin_amount ? ('+' + fmtInt(b.checkin_amount)) : '—'),
      h('td', { class: 'nowrap small' }, fmtInt(b.task_ok) + ' / ' + fmtInt(b.task_count)),
      h('td', { class: 'nowrap small' }, fmtInt(b.task_skipped)),
      h('td', { class: 'nowrap small' }, fmtInt(b.claimed)),
      h('td', { class: 'small muted' }, rewardText || '—')
    ]);
  });
  wrap.appendChild(card('收益账本', h('span', { class: 'small muted' }, '最近 ' + String(toInt(d.days) || 7) + ' 天'),
    ledRows.length ? table(['日期', '条目', '签到', '签到积分', '任务完成', '已跳过', '领奖', '奖励明细'], ledRows)
      : emptyBox('账本还没有数据')));

  var logRows = checkinLogRows(rows);
  wrap.appendChild(card('最近记录', h('div', { class: 'toolbar', style: 'margin:0' }, [
    h('span', { class: 'small muted' }, '共 ' + String(rows.length) + ' 条'),
    h('button', { class: 'btn btn-sm', onclick: function () { return load('checkin'); } }, '刷新')
  ]), logRows.length ? table(['时间', '账号', '类型', '任务', '结果', '奖励', '说明'], logRows)
    : emptyBox('还没有记录 —— 先点上面的「立即签到」，或去「任务中心」跑一次任务')));

  var lastRun = (d.state && d.state.last_run) || {};
  wrap.appendChild(card('明细 / 文件', null, kvList([
    ['日志文件', h('span', { class: 'mono small' }, textOf(d.log && d.log.path, '—'))],
    ['日志状态', (d.log && d.log.exists) ? ('已生成 · ' + fmtCompact(d.log.size_bytes) + ' B') : '尚未生成（还没有执行过签到或任务）'],
    ['状态文件', h('span', { class: 'mono small' }, textOf(d.state_file || (d.state && d.state.file), 'data/checkin_state.json'))],
    ['上次签到任务', lastRun.at ? (lastRun.day + ' · ' + fmtTs(lastRun.at) + '（成功 ' + String(toInt(lastRun.claimed)) + ' / 已签 ' + String(toInt(lastRun.already)) + ' / 失败 ' + String(toInt(lastRun.failed)) + '）') : '—'],
    ['账本口径', '签到与任务都写进同一个 tasks_log.jsonl；任务一行一个 step（含跳过），签到一行一个账号'],
    ['说明', textOf(d.note, '')]
  ])));
  return wrap;
}


/* ================= 页面：设置 ================= */

function cfgPick(cfg, keys) {
  var cur = cfg;
  for (var i = 0; i < keys.length; i++) {
    if (cur === null || cur === undefined) return undefined;
    cur = cur[keys[i]];
  }
  return cur;
}

function showVal(v) {
  if (v === undefined || v === null || v === '') return '—';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

function saveKeyFromInput() {
  var box = $('#keyInput');
  if (!box) return;
  saveKey(box.value);
  toast(state.key ? 'API Key 已保存到本机' : '已清空 API Key');
  location.hash = 'p=settings';
  return load(state.page);
}

function pageSettings() {
  var wrap = h('div', null);
  var cfg = state.config;

  wrap.appendChild(card('API Key', badge(state.key ? '已配置' : '未配置', state.key ? 'ok' : 'warn'), h('div', null, [
    h('div', { class: 'toolbar' }, [
      h('input', {
        id: 'keyInput',
        type: 'password',
        value: state.key,
        placeholder: '网关的 api_key（config.json 里的 api_key）',
        style: 'min-width:280px;flex:1',
        onkeydown: function (e) { if (e.key === 'Enter') saveKeyFromInput(); }
      }),
      h('button', { class: 'btn btn-sm btn-primary', onclick: saveKeyFromInput }, '保存并使用'),
      h('button', {
        class: 'btn btn-sm',
        onclick: function () {
          var box = $('#keyInput');
          if (!box) return;
          box.type = box.type === 'password' ? 'text' : 'password';
        }
      }, '显示 / 隐藏'),
      h('button', {
        class: 'btn btn-sm',
        onclick: function () {
          saveKey('');
          var box = $('#keyInput');
          if (box) box.value = '';
          toast('已清空 API Key');
        }
      }, '清空')
    ]),
    h('p', { class: 'small muted' }, '这里填的是「面板用来访问网关」的那把（存在本机浏览器 localStorage，不会上传到任何地方；也可以用 URL 直接带上 #key=你的key，打开一次就会被存下来）。服务端的密钥是 config.json 里的 api_key，改完要重启服务才生效。')
  ])));

  var listen = cfgPick(cfg, ['listen']) || {};
  var server = cfgPick(cfg, ['server']) || {};
  var host = listen.host || server.host || '127.0.0.1';
  var port = listen.port || server.port || 7863;

  wrap.appendChild(card('网关', null, kvList([
    ['面板 / 网关地址', h('span', { class: 'mono small' }, 'http://' + String(host) + ':' + String(port))],
    ['configListen()', h('span', { class: 'mono small' }, configListen())],
    ['上游地址', h('span', { class: 'mono small' }, showVal(cfgPick(cfg, ['upstream', 'base_url']) || cfgPick(cfg, ['upstream', 'url']) || cfgPick(cfg, ['upstream', 'host'])))],
    ['URL 前缀', h('span', { class: 'mono small' }, showVal(cfgPick(cfg, ['upstream', 'prefix'])))],
    ['建模端点', h('span', { class: 'mono small' }, 'GET /v1/models · POST /v1/chat/completions')],
    ['面板端点', h('span', { class: 'mono small' }, 'GET /panel/ · /panel/static/* · /panel/api/*')],
    ['版本', textOf(state.status && state.status.version, '—')],
    ['已运行', state.status && state.status.uptime ? fmtElapsed(state.status.uptime) : '—']
  ])));

  wrap.appendChild(card('账号池', null, kvList([
    ['当前策略', (function () {
      var p = cfgPick(cfg, ['pool', 'distribution']);
      var lab = labelOf(POLICY_FALLBACK, p);
      return String(p || '—') + '（' + lab + '）';
    })()],
    ['区域', showVal(cfgPick(cfg, ['pool', 'region']))],
    ['最大并发', showVal(cfgPick(cfg, ['pool', 'max_in_flight']))],
    ['积分下限', showVal(cfgPick(cfg, ['pool', 'credit_floor']))],
    ['冷却', h('span', { class: 'mono small' }, showVal(cfgPick(cfg, ['cooldown'])))],
    ['会话粘性', h('span', { class: 'mono small' }, showVal(cfgPick(cfg, ['session_sticky'])))],
    ['调整入口', '去「账号池」页切换四策略分配']
  ])));

  wrap.appendChild(card('用量统计与模型目录', null, kvList([
    ['统计开关', showVal(cfgPick(cfg, ['usage', 'enabled']))],
    ['统计文件', h('span', { class: 'mono small' }, showVal(cfgPick(cfg, ['usage', 'file']) || 'data/usage.json'))],
    ['保留天数', showVal(cfgPick(cfg, ['usage', 'retention_days']))],
    ['落盘间隔', showVal(cfgPick(cfg, ['usage', 'flush_interval_seconds']))],
    ['模型目录开关', showVal(cfgPick(cfg, ['models', 'enabled']))],
    ['模型目录文件', h('span', { class: 'mono small' }, showVal(cfgPick(cfg, ['models', 'file']) || 'data/models.json'))],
    ['状态文件', h('span', { class: 'mono small' }, showVal(cfgPick(cfg, ['state_file'])))],
    ['auth 目录', h('span', { class: 'mono small' }, showVal(cfgPick(cfg, ['auth_dir'])))]
  ])));

  wrap.appendChild(card('外观', h('span', { class: 'small muted' }, '当前：' + (currentTheme() === 'light' ? '浅色' : '深色')), h('div', { class: 'toolbar' }, [
    h('button', {
      class: 'btn btn-sm',
      onclick: function () { applyTheme('dark'); render(); toast('已切换到深色'); }
    }, '深色'),
    h('button', {
      class: 'btn btn-sm',
      onclick: function () { applyTheme('light'); render(); toast('已切换到浅色'); }
    }, '浅色'),
    h('button', {
      class: 'btn btn-sm',
      onclick: function () { applyTheme(currentTheme() === 'light' ? 'dark' : 'light'); render(); }
    }, '切换')
  ])));

  wrap.appendChild(card('完整配置（只读）', h('button', {
    class: 'btn btn-sm',
    onclick: function () { return load('settings'); }
  }, '重新读取'), cfg
    ? h('pre', { class: 'mono small scroll-y', style: 'max-height:340px;overflow:auto;margin:0' }, JSON.stringify(cfg, null, 2))
    : (state.loading ? loadingBox('正在读取配置…') : emptyBox('配置接口没有返回数据'))));

  return wrap;
}

/* ================= 渲染 ================= */

/* ================= 页面：实时日志（积分采集） ================= */

function logArray(log) {
  if (typeof log === 'string') return log.split(/\r?\n/);
  if (Array.isArray(log)) return log;
  return [];
}

function logsUrl(n) { return '/panel/api/logs?n=' + String(n || 400); }

function collectBadgeEl(meta) {
  var el = badge(meta.unavailable ? '采集不可用' : (meta.running ? '已启动 · 运行中' : '未启动'),
    meta.unavailable ? 'bad' : (meta.running ? 'ok' : 'mute'));
  el.id = 'collectMetaBadge';
  return el;
}

function collectMetaLine(meta) {
  if (meta.unavailable) return String(meta.unavailable);
  return 'pid ' + textOf(meta.pid, '—') + ' · 退出码 ' + textOf(meta.exit_code, '—');
}

function pageCollect() {
  var d = state.collect || {};
  var meta = d.collect || {};
  var lines = logArray(d.log);
  var wrap = h('div', { class: 'grid' }, []);

  wrap.appendChild(h('div', { class: 'grid' }, [
    statCard('采集进程', meta.unavailable ? '不可用' : (meta.running ? '已启动 · 运行中' : '未启动'), collectMetaLine(meta), !!meta.running),
    statCard('日志行数', fmtInt(lines.length), '最多保留 2000 行'),
    statCard('实时刷新', state.live ? ('每 ' + state.liveSec + ' 秒') : '已关闭', '点顶部「实时」切换'),
    statCard('日志文件', 'wb_data/collect_log.txt', '采集脚本 wb_credits_fetcher.py')
  ]));

  var toolbar = h('div', { class: 'toolbar' }, [
    h('button', { class: 'btn btn-sm', onclick: function () {
      fire(this, '启动中…', function () {
        return apiPost('/panel/api/collect/start', { action: 'collect' }).then(function (r) {
          toast((r && r.message) || '已启动采集', !(r && r.success));
          return load('collect');
        });
      });
    } }, '开始采集'),
    h('button', { class: 'btn btn-sm btn-ghost', onclick: function () {
      fire(this, '停止中…', function () {
        return apiPost('/panel/api/collect/stop', {}).then(function (r) {
          toast((r && r.message) || '已停止', !(r && r.success));
          return load('collect');
        });
      });
    } }, '停止采集'),
    h('button', { class: 'btn btn-sm btn-ghost', onclick: function () {
      fire(this, '刷新中…', function () { return load('collect'); });
    } }, '立即刷新')
  ]);

  wrap.appendChild(card('采集控制', collectBadgeEl(meta), h('div', null, [
    h('p', { class: 'small muted', style: 'margin:0 0 12px' },
      '这里就是老控制台那个「实时面板」的数据源：wb_credits_fetcher.py 用本机浏览器登录态抓积分与资源包，输出实时写进下面的日志框（同时落盘 wb_data/collect_log.txt）。'),
    toolbar,
    meta.hint ? emptyBox(String(meta.hint)) : null
  ])));

  var body;
  if (d._error) {
    body = emptyBox('读取日志失败：' + d._error);
  } else if (!lines.length) {
    body = emptyBox('还没有日志。点上面的「开始采集」，或等首次自动采集跑完。');
  } else {
    body = h('pre', { class: 'logbox scroll-y', id: 'collectLog' }, lines.join('\n'));
  }
  wrap.appendChild(card('日志输出',
    h('span', { class: 'small muted', id: 'collectCount' }, fmtInt(lines.length) + ' 行 · 新行在最下'),
    body));
  return wrap;
}

function refreshCollectLive() {
  var pre = $('#collectLog');
  if (!pre) return load('collect');            /* 首屏还没渲染出结构时退回整页加载 */
  return apiGet(logsUrl(400)).catch(function (e) {
    return { _error: (e && e.message) ? e.message : '读取失败' };
  }).then(function (d) {
    state.collect = d || {};
    var lines = logArray(state.collect.log);
    var near = (pre.scrollTop + pre.clientHeight) >= (pre.scrollHeight - 24);
    pre.textContent = lines.join('\n');
    if (near) pre.scrollTop = pre.scrollHeight;  /* 原本贴着底部就继续贴底部 */
    var cnt = $('#collectCount');
    if (cnt) cnt.textContent = fmtInt(lines.length) + ' 行 · 新行在最下';
    var meta = state.collect.collect || {};
    var bi = $('#collectMetaBadge');
    if (bi) {
      bi.textContent = meta.unavailable ? '采集不可用' : (meta.running ? '已启动 · 运行中' : '未启动');
      bi.className = 'badge badge-' + (meta.unavailable ? 'bad' : (meta.running ? 'ok' : 'mute'));
    }
    updateChrome();
  });
}

/* ================= 页面：诊断 ================= */

function pageDiagnose() {
  var d = state.diagnose || {};
  var wrap = h('div', { class: 'grid' }, []);
  var again = function (btn) {
    return function () { fire(btn, '自检中…', function () { return load('diagnose'); }); };
  };

  if (d._error || !d.checks) {
    wrap.appendChild(card('系统自检', h('button', { class: 'btn btn-sm', onclick: again(null) }, '开始自检'),
      emptyBox(d._error ? ('自检接口不可用：' + d._error) : '还没有自检结果。')));
    return wrap;
  }

  var checks = d.checks || [];
  var okCount = 0;
  for (var i = 0; i < checks.length; i++) if (checks[i] && checks[i].ok) okCount++;

  wrap.appendChild(h('div', { class: 'grid' }, [
    statCard('通过项', okCount + ' / ' + checks.length, '四项自检', okCount === checks.length),
    statCard('Python', platformOf(d.python), '运行时版本'),
    statCard('网关', d.gateway ? '可连通' : '无响应', textOf(d.gateway, '—')),
    statCard('账号文件', (d.files && d.files.auths) !== undefined ? (fmtInt(d.files.auths) + ' 个') : '—',
      d.files ? ('tasks_log ' + fmtInt(d.files.tasks_log_size) + ' B · collect_log ' + fmtInt(d.files.collect_log_size) + ' B') : '')
  ]));

  var rows = checks.map(function (c) {
    c = c || {};
    return h('tr', null, [
      h('td', { class: 'nowrap' }, c.ok ? badge('通过', 'ok') : badge('需处理', 'bad')),
      h('td', { class: 'nowrap small' }, textOf(c.label, c.name)),
      h('td', { class: 'small' }, textOf(c.detail, '—')),
      h('td', { class: 'small muted' }, textOf(c.hint, '—'))
    ]);
  });
  wrap.appendChild(card('自检结果', h('button', { class: 'btn btn-sm', onclick: again(null) }, '重新自检'),
    table(['结果', '项目', '详情', '建议'], rows)));

  var pairs = [
    ['Python', textOf(d.python, '—')],
    ['网关连通性', textOf(d.gateway, '—')],
    ['依赖与文件', textOf(d.deps, '—')],
    ['面板静态文件', d.files ? (d.files.static_files || []).join(', ') || '缺失' : '—'],
    ['采集日志大小', d.files ? (fmtInt(d.files.collect_log_size) + ' B') : '—']
  ];
  wrap.appendChild(card('原始信息', h('span', { class: 'small muted' }, '数据来自 wb_ops.run_checks()'), kvList(pairs)));
  return wrap;
}

function platformOf(s) {
  var t = String(s || '');
  var m = t.match(/Python\s+[0-9.]+/);
  return m ? m[0].replace('Python ', '') : '—';
}

var PAGES = {
  overview: pageOverview,
  pool: pagePool,
  models: pageModels,
  tasks: pageTasks,
  accounts: pageAccounts,
  tokens: pageTokens,
  credits: pageCredits,
  checkin: pageCheckin,
  collect: pageCollect,
  diagnose: pageDiagnose,
  settings: pageSettings
};

function errorBox() {
  if (!state.error) return null;
  return h('div', { class: 'card', style: 'border-color:var(--red)' }, [
    h('div', { class: 'card-head' }, [h('h2', null, '出错了'), h('div', { class: 'spacer' }),
      h('button', { class: 'btn btn-sm', onclick: function () { return load(state.page); } }, '重试')]),
    h('p', { class: 'small', style: 'margin:0' }, state.error)
  ]);
}

function updateChrome() {
  var meta = PAGE_META[state.page] || PAGE_META.overview;
  var title = $('#pageTitle');
  if (title) title.textContent = meta.title;
  var sub = $('#pageSub');
  if (sub) sub.textContent = meta.sub;

  var links = $$('#nav .nav-item').concat($$('#bottomnav a'));
  for (var i = 0; i < links.length; i++) {
    var el = links[i];
    var dp = el.getAttribute('data-page');
    var cls = String(el.className || '');
    if (cls.indexOf('active') >= 0) {
      var parts = cls.split(' ');
      var keep = [];
      for (var k = 0; k < parts.length; k++) {
        if (parts[k] && parts[k] !== 'active') keep.push(parts[k]);
      }
      cls = keep.join(' ');
    }
    el.className = dp === state.page ? (cls ? cls + ' active' : 'active') : cls;
  }

  var pill = $('#pillAccounts');
  if (pill) {
    var pool = state.pool && state.pool.status ? state.pool.status : null;
    var stats = state.stats || {};
    var av = pool ? toInt(pool.available) : -1;
    var total = pool ? toInt(pool.total) : toInt(stats.accounts);
    pill.textContent = av >= 0 ? (String(av) + '/' + String(total)) : String(total);
    var dotEl = pill.parentNode ? pill.parentNode.querySelector('.dot') : null;
    if (dotEl) dotEl.style.background = (av === 0 ? 'var(--red)' : 'var(--green)');
  }

  var ver = $('#footVersion');
  if (ver) ver.textContent = 'v' + textOf(state.status && state.status.version, '1.0.0');
  var up = $('#footUptime');
  if (up) up.textContent = state.status && state.status.uptime ? ('运行 ' + fmtElapsed(state.status.uptime)) : '未连接';
}

function afterRender(scope) {
  if (state.page !== 'tokens') return;
  var canvas = document.getElementById('trendCanvas');
  if (!canvas) return;
  var node = canvas;
  var series = null;
  while (node && series === null) {
    if (node._series) series = node._series;
    node = node.parentNode;
  }
  if (!series && state.usage) series = state.usage.series || [];
  try {
    drawTrend(canvas, series || [], {});
  } catch (e) { /* 图表失败不影响页面 */ }
}

function render() {
  updateChrome();
  var view = $('#view');
  if (!view) return;
  var body = h('div', { class: 'view' }, [errorBox()]);
  var fn = PAGES[state.page] || PAGES.overview;
  var node = null;
  try {
    node = fn();
  } catch (e) {
    node = emptyBox('页面渲染出错：' + ((e && e.message) ? e.message : String(e)));
  }
  if (node) body.appendChild(node);
  view.innerHTML = '';
  view.appendChild(body);
  afterRender(body);
}

/* ================= 启动 ================= */

function pageFromHash() {
  var raw = String(location.hash || '');
  if (raw.charAt(0) === '#') raw = raw.slice(1);
  if (raw.charAt(0) === '/') raw = raw.slice(1);
  var i = raw.indexOf('p=');
  if (i >= 0) {
    var r = raw.slice(i + 2);
    var j = r.indexOf('&');
    if (j >= 0) r = r.slice(0, j);
    if (PAGE_META[r]) return r;
  }
  if (PAGE_META[raw]) return raw;
  return '';
}

function go(p) {
  setPage(p);
  try {
    var h0 = String(location.hash || '');
    if (h0.indexOf('key=') < 0 && pageFromHash() !== state.page) location.hash = 'p=' + state.page;
  } catch (e) { /* 忽略 */ }
}

function bindEvents() {
  var menu = $('#menuBtn');
  if (menu) menu.addEventListener('click', openSidebar);
  var scrim = $('#scrim');
  if (scrim) scrim.addEventListener('click', closeSidebar);
  var theme = $('#themeBtn');
  if (theme) theme.addEventListener('click', function () {
    var t = currentTheme() === 'light' ? 'dark' : 'light';
    applyTheme(t);
    render();
    toast('已切换到' + (t === 'light' ? '浅色' : '深色'));
  });
  var live = $('#liveBtn');
  if (live) live.addEventListener('click', toggleLive);
  var refresh = $('#refreshBtn');
  if (refresh) refresh.addEventListener('click', function () {
    fire(this, '刷新中…', function () { return load(state.page); });
  });

  function onNav(e) {
    var el = e.target;
    while (el && el !== document && !el.getAttribute) el = el.parentNode;
    if (!el || !el.getAttribute) return;
    var p = el.getAttribute('data-page');
    if (!p && el.parentNode && el.parentNode.getAttribute) p = el.parentNode.getAttribute('data-page');
    if (p && PAGE_META[p]) {
      e.preventDefault();
      go(p);
    }
  }
  var nav = $('#nav');
  if (nav) nav.addEventListener('click', onNav);
  var bnav = $('#bottomnav');
  if (bnav) bnav.addEventListener('click', onNav);

  window.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeSidebar();
  });
  window.addEventListener('hashchange', function () {
    var p = pageFromHash();
    if (p && p !== state.page) setPage(p);
  });
  window.addEventListener('resize', function () {
    if (state.page === 'tokens') afterRender();
  });
}

function tick() {
  if (state.busy) return;
  if (state.page === 'collect') {
    refreshCollectLive();          /* 日志页原地更新，不整页重绘、不跳滚动条 */
    return;
  }
  if (state.page === 'overview' || state.page === 'pool') {
    load(state.page);
  } else {
    updateChrome();
  }
}

/* ================= 实时刷新开关 / Service Worker ================= */

var liveTimer = null;

function readLive() {
  try {
    var v = localStorage.getItem(LIVE_STORE);
    return v === null ? true : v === '1';
  } catch (e) { return true; }
}

function applyLive() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  var btn = $('#liveBtn');
  if (btn) {
    btn.className = 'btn btn-sm ' + (state.live ? 'btn-primary' : 'btn-ghost');
    btn.textContent = state.live ? ('实时 ' + state.liveSec + 's') : '实时已关';
    btn.title = state.live ? '自动刷新已开启（点击关闭）' : '自动刷新已关闭（点击开启）';
  }
  if (state.live) liveTimer = setInterval(tick, state.liveSec * 1000);
}

function toggleLive() {
  state.live = !state.live;
  try { localStorage.setItem(LIVE_STORE, state.live ? '1' : '0'); } catch (e) { /* 忽略 */ }
  applyLive();
  toast(state.live ? ('已开启自动刷新：每 ' + state.liveSec + ' 秒') : '已关闭自动刷新');
  if (state.live && state.page === 'collect') refreshCollectLive();
}

function registerSW() {
  if (!('serviceWorker' in navigator)) return;
  if (String(location.protocol).indexOf('http') !== 0) return;   /* file:// 下不支持 */
  try {
    navigator.serviceWorker.register('/panel/sw.js', { scope: '/panel/' }).catch(function () {
      /* 忽略：不是安全上下文或已禁用，面板照常工作 */
    });
  } catch (e) { /* 忽略 */ }
}

function init() {
  state.key = readKey();
  var p0 = pageFromHash();
  state.page = p0 || 'overview';
  initTheme();
  bindEvents();
  render();
  load(state.page);
  state.live = readLive();
  applyLive();
  registerSW();
  if (!state.key) {
    toast('还没有配置 API Key：可先在「设置」里填写，或用 URL 带上 #key=…', true);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

})();
