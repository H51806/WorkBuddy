/* ============================================================
 * 统一面板冒烟 runner（唯一需要维护的源）
 * 用法：取 devtools/_verify_collect.html 的头部切片（到第一个
 *   <script> / (function () { /   var S = window.__WB  之前，固定字符偏移 100014）
 *   再接 <script>window.SMOKE_PART=N;</script><script> + 本文件全文 +
 *   </script></body></html>，得到 devtools/_smoke_<name>.html。
 * 段号：1=11 视图+外壳+导航  2=交互A(pool/models/tokens/credits/tasks)
 *       3=签到+实时日志  4=账号+设置  5=鲁棒性(500/401/hash)
 * 用 headless Chrome 1240 宽 fullPage 截图，读页面顶部横幅的 PASS/FAIL。
 * 详见 ../docs/测试报告.md 第 7 节。
 * ============================================================ */
(function () {
  'use strict';
  var RES = [], CALLS = [], ERRS = [], NOTES = [];
  var FAILP = {}, PATCH = {};

  function respond(status, payload) {
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve({
          ok: status >= 200 && status < 300, status: status,
          text: function () { return Promise.resolve(JSON.stringify(payload)); },
          json: function () { return Promise.resolve(payload); }
        });
      }, 5);
    });
  }

  var baseFetch = window.fetch;
  window.fetch = function (url, opts) {
    var u = String(url);
    var path = u.split('?')[0];
    var query = u.indexOf('?') >= 0 ? u.slice(u.indexOf('?') + 1) : '';
    var method = String((opts && opts.method) || 'GET').toUpperCase();
    var body = null;
    try { body = (opts && opts.body) ? JSON.parse(opts.body) : null; } catch (e) { body = String((opts && opts.body) || ''); }
    CALLS.push({ m: method, p: path, q: query, body: body });
    var st = FAILP[method + ' ' + path] || FAILP[path];
    if (st) return respond(st, { error: { message: 'injected ' + st + ' for ' + path, type: 'invalid_request_error', code: st } });
    var hit = PATCH[method + ' ' + path];
    if (hit === undefined && PATCH[path] !== undefined) hit = PATCH[path];
    if (hit === undefined && typeof DATA !== 'undefined' && DATA[path] !== undefined) hit = DATA[path];
    if (hit === undefined) return respond(404, { error: { message: 'Not Found: ' + path, type: 'invalid_request_error', code: 404 } });
    return respond(200, hit);
  };
  window.confirm = function () { return true; };
  window.alert = function () {};

  function ok(name, cond, detail) {
    RES.push((cond ? 'PASS' : 'FAIL') + ' | ' + name + (detail === undefined || detail === '' ? '' : ' | ' + detail));
    if (window.__livePre) window.__livePre.textContent = RES.join('\n');
    return !!cond;
  }
  function note(s) { NOTES.push(s); if (window.__livePre) window.__livePre.textContent = RES.join('\n') + '\nnote: ' + s; }
  function txt(el) { return el ? String(el.textContent || '').replace(/\s+/g, ' ').trim() : ''; }
  function el(id) { return document.getElementById(id); }
  function view() { return el('view'); }
  function nodes() { var v = view(); return v ? v.querySelectorAll('*').length : 0; }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms || 260); }); }
  function findBtn(label, root) {
    var want = String(label).replace(/\s+/g, '');
    var list = (root || document).querySelectorAll('button');
    for (var i = 0; i < list.length; i++) if (txt(list[i]).replace(/\s+/g, '').indexOf(want) >= 0) return list[i];
    return null;
  }
  function click(label, root) {
    var b = findBtn(label, root);
    if (!b) note('按钮未找到: ' + label);
    else b.click();
    return b;
  }
  function mark() { return CALLS.length; }
  function since(n) { return CALLS.slice(n); }
  function has(list, m, p, pred) {
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (c.m === m && c.p === p && (!pred || pred(c))) return c;
    }
    return null;
  }
  function show(list) {
    return list.map(function (c) { return c.m + ' ' + c.p + (c.q ? '?' + c.q : ''); }).join(' , ');
  }
  function waitIdle(ms) {
    var n = 0;
    return new Promise(function (resolve) {
      (function loop() {
        n++;
        var S = window.__WB;
        if ((!S || !S.state.busy) && n > 1) return resolve();
        if (n > 150) return resolve();
        setTimeout(loop, ms || 12);
      })();
    });
  }

  var live = document.createElement('pre');
  live.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99999;max-height:46%;overflow:auto;margin:0;padding:6px 8px;background:#04160c;color:#8ef0b5;font:11px/1.35 Consolas,monospace;white-space:pre-wrap';
  live.textContent = 'smoke runner 启动中…';
  document.body.appendChild(live);
  window.__livePre = live;

  var S = window.__WB;
  if (!S) {
    return finish(['FAIL | app.js 未暴露 window.__WB（IIFE 未执行到尾部）']);
  }

  /* ---------- 缺失端点的模拟数据（按前端读取的字段构造） ---------- */
  if (typeof DATA !== 'undefined') {
    DATA['/panel/api/models'] = {
      region: 'cn', enabled: 4, disabled: 1, updated: '2026-10-16 09:00',
      regions: [{ key: 'merged', label: '合并' }, { key: 'cn', label: '国内版' }, { key: 'global', label: '国际版' }],
      items: [
        { id: 'deepseek-v4-flash', name: 'DeepSeek V4 Flash', family: 'deepseek', known: true, region_labels: ['国内版', '国际版'], context_window: 262144, max_tokens: 32768, supports_image: true, reasoning_efforts: [], rate: 1, badges: [], enabled: true, sources: { kind: 'builtin', label: '内置' } },
        { id: 'deepseek-v4.1-flash', name: 'DeepSeek V4.1 Flash', family: 'deepseek', known: true, region_labels: ['国内版', '国际版'], context_window: 262144, max_tokens: 32768, supports_image: true, reasoning_efforts: [], rate: 0.79, badges: [{ label: '促销 01:00-09:00', kind: 'promo' }], enabled: true, sources: { kind: 'upstream', label: '上游发现' } },
        { id: 'glm-5.3', name: 'GLM 5.3', family: 'glm', known: true, region_labels: ['国内版'], context_window: 200000, max_tokens: 32768, supports_image: true, reasoning_efforts: ['low', 'high', 'max'], rate: 1, badges: [], enabled: true, sources: { kind: 'builtin', label: '内置' } },
        { id: 'glm-5.1', name: 'GLM 5.1', family: 'glm', known: true, region_labels: ['国内版'], context_window: 128000, max_tokens: 16384, supports_image: false, reasoning_efforts: [], rate: 1, badges: [], enabled: false, sources: { kind: 'builtin', label: '内置' } },
        { id: 'mystery-model-x', name: 'mystery-model-x', family: '', known: false, enabled: false }
      ],
      stats: { builtin: 16, discovered: 3, overrides: 1, promotions: 1, path: './data/models.json', version: 5 }
    };

    var T = { requests: 740, success: 729, failures: 11, input: 151000, output: 32000, total: 230000, cache_miss: 120000, cached_read: 96000, cache_write: 14000, cache_hit_rate: 0.773, duration_ms: 762 };
    var series = [];
    for (var h = 0; h < 24; h++) {
      series.push({ label: (h < 10 ? '0' : '') + h + ':00', slot: h, requests: 12 + ((h * 7) % 41), cached_read: 2000 + h * 310, cache_miss: 1500 + h * 220, output: 700 + h * 90, cache_write: 90 + h * 20, input: 4300 + h * 620, total: 8590 + h * 1240, success: 11 + ((h * 7) % 40), failures: h % 3, cache_hit_rate: 0.7 + (h % 5) / 50, duration_ms: 600 + h * 9 });
    }
    DATA['/panel/api/usage'] = {
      range: '7d', region: 'merged', client: null, model: null, grain: 'hour', since: '2026-10-09 00:00', until: '2026-10-16 09:00',
      series: series, totals: T,
      by_model: [Object.assign({ key: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash' }, T), Object.assign({ key: 'glm-5.3', label: 'GLM 5.3' }, T, { requests: 96, total: 42000 })],
      by_client: [Object.assign({ key: 'workbuddy', label: 'WorkBuddy' }, T), Object.assign({ key: 'dsh', label: 'DSH' }, T, { requests: 96, total: 18000 })],
      by_region: [Object.assign({ key: 'cn', label: '国内版' }, T), Object.assign({ key: 'global', label: '国际版' }, T, { requests: 96, total: 11000 })],
      by_account: [Object.assign({ key: 'u_8831', label: 'workbuddy-4c228c87' }, T)]
    };
    DATA['/panel/api/usage/meta'] = {
      ranges: [{ key: 'today', label: '今天' }, { key: '7d', label: '近 7 天' }, { key: '30d', label: '近 30 天' }, { key: 'all', label: '全部' }],
      regions: [{ key: 'merged', label: '合并' }, { key: 'cn', label: '国内版' }, { key: 'global', label: '国际版' }],
      clients: [{ key: '', label: '全部' }, { key: 'workbuddy', label: 'WorkBuddy' }, { key: 'dsh', label: 'DSH' }, { key: 'codebuddy-cli', label: 'CodeBuddy CLI' }],
      store: { accounts: 1, buckets: 412, path: './data/usage.json', retention_days: 90, version: 5 },
      top_user_agents: [{ user_agent: 'CodeBuddy-CLI/1.0 (darwin)', count: 412 }, { user_agent: 'DSH/1.0', count: 96 }, { user_agent: 'python-requests/2.32', count: 24 }],
      note: '只统计经本网关转发的请求。'
    };

    var acc = { id: 'workbuddy-4c228c87', name: '示例账号', phone: '138****8831', region: 'cn', available: true, active: true, credits: 9000, energy: 812, requests: 806, success: 800, failures: 6, last_used: 1760598724, reason: '' };
    DATA['/panel/api/accounts'] = { policy: { distribution: 'priority', label: '先用完一个' }, current: 'workbuddy-4c228c87', accounts: [acc], status: { total: 1, active: 1, available: 1 }, preview: { order: [acc] }, unavailable: [] };
    DATA['/api/auth-files'] = { accounts: [{ uid: '4c228c87-fc6b-41c0-b236-fafcaee7ab3f', name: '示例账号', phone: '138****8831', file: 'workbuddy-4c228c87-fc6b-41c0-b236-fafcaee7ab3f.json', realm: 'cn', status: 'ok', expires_at_human: '2026-11-09 20:02', expires_in_seconds: 2100000, error: '' }] };
    DATA['/panel/api/snapshots'] = { note: '扫描本机桌面/下载目录，找到可导入的 WorkBuddy 登录状态文件。', snapshots: [
      { uid: '4c228c87-fc6b-41c0-b236-fafcaee7ab3f', name: '工作号', path: 'C:/Users/<你的用户名>/Desktop/login_state_工作号.json', kind: 'login_state', status: 'cookie_only', importable: false, name_suspect: true, note: '只有网页 Cookie', action: '—' },
      { uid: 'a71c9f30-1ea2-4d55-9c3a-6a1b7d2e4410', name: '海外号', path: 'C:/Users/<你的用户名>/Desktop/login_state_海外号.json', kind: 'login_state', status: 'ok', importable: true, name_suspect: false, note: '可以直接导入', action: '导入', file: 'login_state_海外号.json' }
    ] };
    DATA['/api/credits/live'] = { total_remain: 18420, total_size: 26000, expiring_7d: 2100, queried_at: 1760598724, forced: false, accounts: {
      'u_8831': { remain: 9000, total: 12000, expiring: 0, packages: [{ name: '月度积分包', remain: 8000, size: 12000, end_time: '2026-11-01' }, { name: '赠送积分', remain: 1000, size: 1000, end_time: '2026-10-22' }] },
      'u_4412': { remain: 6420, total: 10000, expiring: 2100, packages: [{ name: '月度积分包', remain: 6420, size: 10000, end_time: '2026-11-05' }] }
    } };
    DATA['/tasks/list'] = { tasks: [
      { code: 'first_buddy', desc: '每日首聊', kind: 'auto', reward: '+300c +8e' },
      { code: 'create_canvas', desc: '创建画布', kind: 'auto', reward: '+100c +5e' },
      { code: 'Expert_Philanthropy', desc: '公益专家对话', kind: 'manual', reward: '+50c' }
    ], auto_codes: ['first_buddy', 'create_canvas', 'chat_5'], accounts: 1, uid: 'u_8831' };
    DATA['/panel/api/config'] = { listen: { host: '127.0.0.1', port: 7863 }, api_key: '***1827', auth_dir: './auths', state_file: './data/state.json', pool: { distribution: 'priority', region: '', max_in_flight: 3, credit_floor: 100 }, usage: { enabled: true, file: './data/usage.json', retention_days: 90, flush_interval_seconds: 30 }, models: { enabled: true, file: './data/models.json' }, upstream: { base_url: 'https://www.codebuddy.cn', prefix: '/api/v1' }, cooldown: { failure_seconds: 60, rate_limit_seconds: 300 } };
    PATCH['GET /panel/api/checkin'] = { state_file: 'wb_data/checkin_state.json', today: '2026-10-16', summary: { accounts_total: 1, done: 1 }, report: { days: [] }, ledger: [], rows: [
      { at: 1760598000, name: '示例账号', realm: 'cn', kind: 'checkin', code: '', status: 'ok', amount: 400, message: '签到成功' },
      { at: 1760594400, name: '示例账号', realm: 'cn', kind: 'task', code: 'first_buddy', status: 'ok', amount: 300, message: '已领取' }
    ], account_rows: [{ uid: 'u_8831', name: '示例账号', realm: 'cn', today_done: true, status: 'ok', last_at: 1760598000, amount: 400, own_time: '09:12', message: '签到成功' }], log: [], note: '签到记录来自 wb_data/tasks_log.jsonl' };
    PATCH['POST /panel/api/checkin'] = { ok: true, claimed: 1, already: 0, failed: 0, amount: 400, report: { days: [] }, rows: [] };
  }

  /* ---------- 交互响应的 POST 模拟 ---------- */
  PATCH['POST /panel/api/pool'] = { policy: { distribution: 'round-robin', label: '轮询' }, status: { total: 1, active: 1, available: 1 }, accounts: [], preview: { order: [] }, unavailable: [], note: '已切换' };
  PATCH['POST /panel/api/models'] = typeof DATA !== 'undefined' ? DATA['/panel/api/models'] : {};
  PATCH['POST /panel/api/snapshots'] = { ok: true, imported: 1, applied: 1, account: { uid: 'a71c9f30-1ea2-4d55-9c3a-6a1b7d2e4410' }, note: '已导入' };
  PATCH['POST /panel/api/accounts/switch'] = { ok: true, current: 'workbuddy-4c228c87', message: '已切换' };
  PATCH['POST /panel/api/accounts/remove'] = { ok: true, removed: 'workbuddy-4c228c87', remaining: 1 };
  PATCH['POST /panel/api/collect/start'] = { success: true, pid: 12345, message: '已启动采集' };
  PATCH['POST /panel/api/collect/stop'] = { success: true, message: '已停止采集' };
  PATCH['POST /tasks/scan'] = { ok: true, accounts: [{ uid: 'u_8831', tasks: [] }], scanned: 6, available: 3 };
  PATCH['POST /tasks/keepalive'] = { ok: true, accounts: [{ uid: 'u_8831', kept: true }] };
  PATCH['POST /tasks/run'] = { ok: true, accounts: [{ uid: 'u_8831', claimed: 2 }], claimed: 2 };
  PATCH['GET /panel/api/login/start'] = { ok: true, realm: 'cn', state: 'st-123', url: 'https://www.codebuddy.cn/login?state=st-123', message: '请扫码' };
  PATCH['GET /panel/api/login/poll'] = { ok: true, done: false, state: 'st-123', message: '等待扫码授权…' };
  if (typeof DATA !== 'undefined' && DATA['/panel/api/logs']) {
    DATA['/panel/api/logs'].collect = { running: true, pid: 12345, exit_code: null, hint: '' };
  }

  /* ---------- 断言主流程 ---------- */
  var PAGES = ['overview', 'pool', 'accounts', 'models', 'tokens', 'credits', 'tasks', 'checkin', 'collect', 'diagnose', 'settings'];

  async function stepPages() {
    for (var i = 0; i < PAGES.length; i++) {
      var p = PAGES[i];
      S.state.page = p;
      try { S.render(); } catch (e) { ok('渲染 ' + p, false, 'render 抛错: ' + e.message); continue; }
      try { await S.load(p); } catch (e) { /* load 内部已写 state.error */ }
      await wait(30);
      var n = nodes();
      ok('页面渲染 ' + p, n > 25, 'nodes=' + n + ' 高=' + view().offsetHeight + ' 错误=' + String(S.state.error || '-'));
    }
  }

  async function stepChrome() {
    var before = document.documentElement.getAttribute('data-theme');
    var tb = el('themeBtn');
    if (tb) tb.click();
    await wait(30);
    var after = document.documentElement.getAttribute('data-theme');
    ok('主题切换', !!before && !!after && before !== after, before + ' -> ' + after);
    if (el('themeBtn')) el('themeBtn').click();
    await wait(30);
    var lb = el('liveBtn');
    ok('实时按钮初始文案', txt(lb) === '实时 15s', '文案=' + txt(lb));
    if (lb) lb.click();
    await wait(30);
    ok('实时开关关闭', txt(el('liveBtn')).indexOf('已关') >= 0 && localStorage.getItem(S.LIVE_KEY || 'wb.panel.live') === '0', '文案=' + txt(el('liveBtn')) + ' 存储=' + localStorage.getItem('wb.panel.live'));
    if (el('liveBtn')) el('liveBtn').click();
    await wait(30);
    ok('实时开关恢复', txt(el('liveBtn')).indexOf('实时') === 0 && localStorage.getItem('wb.panel.live') === '1', '文案=' + txt(el('liveBtn')));
    var mb = el('menuBtn');
    if (mb) mb.click();
    await wait(30);
    ok('抽屉打开', String(el('sidebar').className).indexOf('open') >= 0, 'sidebar=' + el('sidebar').className);
    if (el('scrim')) el('scrim').click();
    await wait(30);
    ok('抽屉关闭', String(el('sidebar').className).indexOf('open') < 0, 'sidebar=' + el('sidebar').className);
  }

  async function stepNav() {
    var items = document.querySelectorAll('#nav .nav-item');
    note('#nav 项数=' + items.length + ' #bottomnav 项数=' + document.querySelectorAll('#bottomnav a').length);
    var bad = [];
    for (var i = 0; i < items.length; i++) {
      var want = items[i].getAttribute('data-page');
      items[i].click();
      await wait(30);
      if (S.state.page !== want) bad.push(want + '->' + S.state.page);
    }
    ok('侧栏导航逐项跳转（' + items.length + ' 项）', bad.length === 0, bad.length ? '异常: ' + bad.join(',') : '');
    var bl = document.querySelectorAll('#bottomnav a');
    var bad2 = [];
    for (var j = 0; j < bl.length; j++) {
      var w2 = bl[j].getAttribute('data-page');
      bl[j].click();
      await wait(30);
      if (S.state.page !== w2) bad2.push(w2 + '->' + S.state.page);
    }
    ok('底栏导航逐项跳转（' + bl.length + ' 项）', bad2.length === 0, bad2.length ? '异常: ' + bad2.join(',') : '');
  }

  async function stepActionsA() {
    /* 账号池：切换分配策略 */
    var m0 = mark();
    S.state.page = 'pool'; S.render(); await S.load('pool'); await wait(30);
    var tabs = view().querySelectorAll('.tab');
    var target = null;
    for (var i = 0; i < tabs.length; i++) if (tabs[i].className.indexOf('active') < 0) { target = tabs[i]; break; }
    if (target) target.click();
    await waitIdle(); await wait(30);
    var c = has(since(m0), 'POST', '/panel/api/pool', function (x) { return x.body && x.body.distribution; });
    ok('账号池切换策略 → POST /panel/api/pool', !!c, c ? 'body=' + JSON.stringify(c.body) : '未发出请求；tabs=' + tabs.length);

    /* 模型页：批量启用 + 重新读取 */
    m0 = mark();
    S.state.page = 'models'; S.render(); await S.load('models'); await wait(30);
    click('全部启用');
    await waitIdle(); await wait(30);
    var cm = has(since(m0), 'POST', '/panel/api/models', function (x) { return x.body && x.body.action === 'bulk'; });
    ok('模型页「全部启用」→ POST /panel/api/models{action:bulk}', !!cm, cm ? JSON.stringify(cm.body).slice(0, 90) : show(since(m0)));
    m0 = mark();
    click('重新读取');
    await waitIdle(); await wait(30);
    var gm = has(since(m0), 'GET', '/panel/api/models', function (x) { return x.q.indexOf('region=') >= 0; });
    ok('模型页「重新读取」→ GET /panel/api/models?region=', !!gm, gm ? gm.q : show(since(m0)));

    /* Token 页：维度切换 */
    m0 = mark();
    S.state.page = 'tokens'; S.render(); await S.load('tokens'); await wait(30);
    var t2 = view().querySelectorAll('.tab');
    var tgt = null;
    for (var k = 0; k < t2.length; k++) if (t2[k].className.indexOf('active') < 0) { tgt = t2[k]; break; }
    if (tgt) tgt.click();
    await waitIdle(); await wait(30);
    var cu = has(since(m0), 'GET', '/panel/api/usage', function (x) { return x.q.length > 0; });
    ok('Token 页切换范围 → GET /panel/api/usage?range=…', !!cu, cu ? cu.q.slice(0, 70) : show(since(m0)));

    /* 余额页：强制刷新 */
    m0 = mark();
    S.state.page = 'credits'; S.render(); await S.load('credits'); await wait(30);
    click('强制刷新余额');
    await waitIdle(); await wait(30);
    var cc = has(since(m0), 'GET', '/api/credits/live', function (x) { return x.q.indexOf('refresh=1') >= 0; });
    ok('余额页「强制刷新余额」→ GET /api/credits/live?refresh=1', !!cc, cc ? cc.q : show(since(m0)));

    /* 任务页：扫描 / 演练保活 */
    m0 = mark();
    S.state.page = 'tasks'; S.render(); await S.load('tasks'); await wait(30);
    click('扫描任务进度');
    await waitIdle(); await wait(30);
    var cs = has(since(m0), 'POST', '/tasks/scan', null);
    ok('任务页「扫描任务进度」→ POST /tasks/scan', !!cs, cs ? JSON.stringify(cs.body) : show(since(m0)));
    var cb = view().querySelector('input[type=checkbox]');
    if (cb && !cb.checked) cb.click();
    await wait(30);
    ok('演练模式复选框 → state.dryRun', S.state.dryRun === true, 'dryRun=' + S.state.dryRun);
    m0 = mark();
    click('账号保活');
    await waitIdle(); await wait(30);
    var ck = has(since(m0), 'POST', '/tasks/keepalive', function (x) { return x.body && x.body.dry_run === true; });
    ok('任务页「账号保活」→ POST /tasks/keepalive{dry_run:true}', !!ck, ck ? JSON.stringify(ck.body) : show(since(m0)));
    if (cb && cb.checked) cb.click();
    await wait(30);

  }

  async function stepActionsB() {
    var m0 = 0;
    /* 签到页：演练 */
    m0 = mark();
    S.state.page = 'checkin'; S.render();
    try { await S.load('checkin'); } catch (e) { note('checkin load 抛错: ' + e.message); }
    await wait(30);
    click('演练');
    await waitIdle(); await wait(30);
    var ci = has(since(m0), 'POST', '/panel/api/checkin', function (x) { return x.body && x.body.dry_run === true && x.body.action === 'run'; });
    ok('签到页「演练（不发请求）」→ POST /panel/api/checkin{dry_run:true}', !!ci, ci ? JSON.stringify(ci.body) : show(since(m0)));
    note('签到页 toast = ' + JSON.stringify(txt(el('toast'))) + ' / 提示卡数量=' + view().querySelectorAll('.card').length);

    /* 实时日志页：开始 / 停止 / 立即刷新 */
    m0 = mark();
    S.state.page = 'collect'; S.render(); await S.load('collect'); await wait(30);
    ok('实时日志页渲染日志框', !!el('collectLog'), '行数文案=' + txt(el('collectCount')));
    var beforeLines = el('collectLog') ? String(el('collectLog').textContent).split('\n').length : 0;
    if (typeof DATA !== 'undefined' && DATA['/panel/api/logs']) {
      var lg = DATA['/panel/api/logs'].log;
      if (Array.isArray(lg)) lg.push('[SMOKE] 追加行 A', '[SMOKE] 追加行 B');
      else DATA['/panel/api/logs'].log = String(lg || '') + '\n[SMOKE] 追加行 A\n[SMOKE] 追加行 B';
    }
    S.refreshCollectLive();
    await wait(30);
    var afterLines = el('collectLog') ? String(el('collectLog').textContent).split('\n').filter(function (x) { return x.length > 0; }).length : 0;
    ok('实时刷新原地追加日志', afterLines > beforeLines && String(el('collectLog').textContent).indexOf('[SMOKE]') >= 0, '行数 ' + beforeLines + ' -> ' + afterLines + ' / 计数=' + txt(el('collectCount')));
    m0 = mark();
    click('开始采集');
    await waitIdle(); await wait(30);
    var cs2 = has(since(m0), 'POST', '/panel/api/collect/start', function (x) { return x.body && x.body.action === 'collect'; });
    ok('日志页「开始采集」→ POST /panel/api/collect/start', !!cs2, cs2 ? JSON.stringify(cs2.body) : show(since(m0)));
    m0 = mark();
    click('停止采集');
    await waitIdle(); await wait(30);
    var cs3 = has(since(m0), 'POST', '/panel/api/collect/stop', null);
    ok('日志页「停止采集」→ POST /panel/api/collect/stop', !!cs3, cs3 ? '' : show(since(m0)));
    m0 = mark();
    click('立即刷新');
    await waitIdle(); await wait(30);
    var cs4 = has(since(m0), 'GET', '/panel/api/logs', null);
    ok('日志页「立即刷新」→ GET /panel/api/logs', !!cs4, cs4 ? cs4.q : show(since(m0)));

    await wait(30);
  }

  async function stepActionsB2() {
    var m0 = 0;
    /* 账号页：扫码登录 / 重新扫描 / 导入 / 切换 / 移除 */
    m0 = mark();
    S.state.page = 'accounts'; S.render(); await S.load('accounts'); await wait(30);
    click('扫码登录（国内版）');
    await waitIdle(); await wait(30);
    var cl = has(since(m0), 'GET', '/panel/api/login/start', function (x) { return x.q.indexOf('realm=cn') >= 0; });
    ok('账号页「扫码登录（国内版）」→ GET /panel/api/login/start?realm=cn', !!cl, cl ? cl.q : show(since(m0)));
    ok('登录卡片已渲染（含取消按钮）', !!el('view') && !!findBtn('取消'), '取消按钮=' + !!findBtn('取消'));
    var cbr = findBtn('取消');
    if (cbr) cbr.click();
    await wait(30);
    m0 = mark();
    click('重新扫描');
    await waitIdle(); await wait(30);
    var cp = has(since(m0), 'GET', '/panel/api/snapshots', null);
    ok('账号页「重新扫描」→ GET /panel/api/snapshots', !!cp, show(since(m0)));
    m0 = mark();
    click('导入');
    await waitIdle(); await wait(30);
    var cx = has(since(m0), 'POST', '/panel/api/snapshots', function (x) { return x.body && x.body.action === 'import'; });
    ok('桌面快照「导入」→ POST /panel/api/snapshots{action:import}', !!cx, cx ? JSON.stringify(cx.body).slice(0, 110) : show(since(m0)));
    m0 = mark();
    click('切换');
    await waitIdle(); await wait(30);
    var cw = has(since(m0), 'POST', '/panel/api/accounts/switch', null);
    ok('账号行「切换」→ POST /panel/api/accounts/switch', !!cw, cw ? JSON.stringify(cw.body) : show(since(m0)));
    m0 = mark();
    click('移除');
    await waitIdle(); await wait(30);
    var cr = has(since(m0), 'POST', '/panel/api/accounts/remove', null);
    ok('账号行「移除」→ POST /panel/api/accounts/remove', !!cr, cr ? JSON.stringify(cr.body) : show(since(m0)));

    /* 设置页：密钥保存 / 显示隐藏 / 清空 */
    m0 = mark();
    S.state.page = 'settings'; S.render(); await S.load('settings'); await wait(30);
    var ki = el('keyInput');
    if (ki) { ki.value = 'smoke-key-123'; }
    click('保存并使用');
    await waitIdle(); await wait(30);
    var stored = '';
    try { for (var lk = 0; lk < localStorage.length; lk++) { var kk = localStorage.key(lk); var vv = localStorage.getItem(kk); if (String(vv).indexOf('smoke-key-123') >= 0) stored = kk + '=' + vv; } } catch (e) {}
    ok('设置页「保存并使用」写入 localStorage', stored !== '' && S.state.key === 'smoke-key-123', stored || '未写入');
    var kb = findBtn('显示') || findBtn('隐藏');
    var type0 = el('keyInput') ? el('keyInput').type : '';
    if (kb) kb.click();
    await wait(30);
    var type1 = el('keyInput') ? el('keyInput').type : '';
    ok('设置页「显示 / 隐藏」切换 input.type', type0 !== type1, type0 + ' -> ' + type1);
    click('清空');
    await waitIdle(); await wait(30);
    ok('设置页「清空」清除密钥', !S.state.key, 'state.key=' + JSON.stringify(S.state.key));
  }

  async function stepRobust() {
    FAILP['/panel/api/models'] = 500;
    S.state.page = 'models';
    try { await S.load('models'); } catch (e) {}
    await wait(30);
    var hasErr = !!view().querySelector('.card') && txt(view()).indexOf('出错') >= 0;
    ok('接口 500 → 页面显示「出错了」卡', hasErr && String(S.state.error || '').length > 0, 'state.error=' + String(S.state.error).slice(0, 60));
    delete FAILP['/panel/api/models'];
    S.state.page = 'overview';
    FAILP['/panel/api/stats'] = 401;
    try { await S.load('overview'); } catch (e) {}
    await wait(30);
    ok('接口 401 → 提示鉴权失败', String(S.state.error || '').indexOf('401') >= 0 || String(S.state.error || '').indexOf('鉴权') >= 0, 'state.error=' + String(S.state.error).slice(0, 80));
    delete FAILP['/panel/api/stats'];
    location.hash = 'p=diagnose';
    await wait(30);
    ok('URL hash 路由 #p=diagnose', S.state.page === 'diagnose', 'state.page=' + S.state.page);
    location.hash = '';
    await wait(30);
  }

  function finish(extra) {
    if (extra) for (var e = 0; e < extra.length; e++) RES.push(extra[e]);
    var fails = RES.filter(function (r) { return r.indexOf('FAIL') === 0; }).length;
    var uniq = {}, order = [];
    for (var i = 0; i < CALLS.length; i++) {
      var k = CALLS[i].m + ' ' + CALLS[i].p;
      if (uniq[k] === undefined) { uniq[k] = 0; order.push(k); }
      uniq[k]++;
    }
    var lines = [];
    lines.push('WorkBuddy 统一面板 · 前端冒烟测试   PASS=' + (RES.length - fails) + '  FAIL=' + fails + '  JS错误=' + ERRS.length + '  请求总数=' + CALLS.length);
    lines.push('');
    for (var r = 0; r < RES.length; r++) lines.push(RES[r]);
    if (NOTES.length) { lines.push(''); lines.push('-- 备注 --'); for (var n = 0; n < NOTES.length; n++) lines.push(NOTES[n]); }
    if (ERRS.length) { lines.push(''); lines.push('-- JS 错误 --'); for (var x = 0; x < ERRS.length; x++) lines.push(ERRS[x]); }
    lines.push('');
    lines.push('-- 本次触达的接口（去重） --');
    order.sort();
    for (var o = 0; o < order.length; o++) lines.push(order[o] + '   x' + uniq[order[o]]);
    var pre = document.createElement('pre');
    pre.style.cssText = 'margin:0;padding:16px;font:13px/1.5 Consolas,Menlo,monospace;background:#0b0f18;color:#dbe4f5;white-space:pre-wrap';
    pre.textContent = lines.join('\n');
    document.body.innerHTML = '';
    document.body.style.cssText = 'margin:0;background:#0b0f18';
    document.body.appendChild(pre);
    document.title = 'SMOKE PASS=' + (RES.length - fails) + ' FAIL=' + fails;
  }

  window.addEventListener('error', function (e) { ERRS.push('error: ' + (e.message || '') + ' @' + (e.filename || '') + ':' + (e.lineno || 0)); });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e.reason;
    ERRS.push('rejection: ' + ((r && r.message) ? r.message : String(r)));
  });

  async function main() {
    var part = window.SMOKE_PART || 1;
    live.textContent = 'SMOKE PART ' + part + ' 运行中…';
    try {
      if (part === 1) { await stepPages(); await stepChrome(); await stepNav(); }
      else if (part === 2) { await stepActionsA(); }
      else if (part === 3) { await stepActionsB(); }
      else if (part === 4) { await stepActionsB2(); }
      else { await stepRobust(); }
    } catch (e) {
      ok('主流程未抛异常', false, e && e.message ? e.message : String(e));
    }
    await wait(30);
    finish(null);
  }

  setTimeout(main, 60);
})();
