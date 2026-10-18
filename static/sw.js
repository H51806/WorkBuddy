/* WorkBuddy 统一管理器 — Service Worker
 * 目标：装到手机主屏后能秒开、断网时至少还能看到面板外壳。
 * 原则：/panel/api/* 与 /v1/* 永远不缓存（数据必须实时），静态资源网络优先 + 缓存兜底。
 */
var CACHE = 'wb-panel-v1';
var SHELL = [
  '/panel/',
  '/panel/static/app.css',
  '/panel/static/app.js',
  '/panel/static/icon-192.png',
  '/panel/static/manifest.webmanifest'
];

self.addEventListener('install', function (e) {
  e.waitUntil(
    caches.open(CACHE).then(function (c) {
      return Promise.all(SHELL.map(function (u) {
        return c.add(new Request(u, { cache: 'reload' })).catch(function () { /* 单个失败不影响安装 */ });
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) { return k === CACHE ? null : caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url;
  try { url = new URL(req.url); } catch (err) { return; }
  if (url.origin !== self.location.origin) return;
  if (url.pathname.indexOf('/panel/api/') === 0) return;   /* 面板数据：直连 */
  if (url.pathname.indexOf('/v1/') === 0) return;          /* 模型接口：直连 */
  if (url.pathname.indexOf('/tasks') === 0) return;        /* 任务接口：直连 */
  e.respondWith(
    fetch(req).then(function (res) {
      if (res && res.ok && res.type === 'basic') {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy).catch(function () {}); });
      }
      return res;
    }).catch(function () {
      return caches.match(req).then(function (hit) {
        return hit || caches.match('/panel/');
      });
    })
  );
});
