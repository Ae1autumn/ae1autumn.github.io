/* ═══════════════ CDN 资源缓存（Service Worker） ═══════════════
   只处理第三方静态资源：脚本、样式、字体、图片。
   同源请求（index.html / data.jsonc / posts/*.md）完全不经过这里，
   统计请求（GoatCounter）也明确排除，保证数据与文章内容始终实时。
   策略：stale-while-revalidate —— 命中缓存立即返回，同时后台拉新，
   下次打开就是新版本；CDN 暂时不可达时也能直接用缓存。 */
'use strict';

var CACHE_NAME = 'cdn-assets-v1';
var CACHE_PREFIX = 'cdn-assets-';

/* 根据请求类型判断是否为可缓存的静态资源。
   用 destination 而不是域名白名单：以后换 CDN / 加新库都不必改这里。 */
var CACHEABLE_DESTINATIONS = { script: 1, style: 1, font: 1, image: 1 };

/* 统计 / 一次性接口域名：即使被当作图片加载也不缓存 */
var NEVER_CACHE_HOSTS = ['goatcounter.com'];

function isNeverCacheHost(hostname) {
  for (var i = 0; i < NEVER_CACHE_HOSTS.length; i++) {
    var h = NEVER_CACHE_HOSTS[i];
    if (hostname === h || hostname.slice(-(h.length + 1)) === '.' + h) return true;
  }
  return false;
}

function isCdnAsset(request) {
  if (request.method !== 'GET') return false;
  if (request.cache === 'no-store') return false;
  if (request.headers.has('range')) return false; /* 分片请求不缓存 */
  if (!CACHEABLE_DESTINATIONS[request.destination]) return false;

  var url;
  try { url = new URL(request.url); } catch (e) { return false; }
  if (url.protocol !== 'https:') return false;
  if (url.hostname === self.location.hostname) return false; /* 同源资源不缓存 */
  return !isNeverCacheHost(url.hostname);
}

function canCache(response) {
  if (!response) return false;
  if (response.type === 'opaque') return true; /* no-cors 响应读不到状态码 */
  return response.status === 200;
}

/* 拉网络并写缓存，返回给页面用的响应 */
function fetchAndCache(request, cache) {
  return fetch(request).then(function (response) {
    if (!canCache(response)) return response;
    var copy = response.clone();
    return cache.put(request, copy).then(function () {
      return response;
    }, function () {
      return response; /* 配额不足 / 不可缓存：忽略，不影响本次加载 */
    });
  });
}

function staleWhileRevalidate(event, request) {
  return caches.open(CACHE_NAME).then(function (cache) {
    return cache.match(request).then(function (cached) {
      if (cached) {
        /* 后台更新，等它写完再结束事件，避免 SW 被提前回收 */
        event.waitUntil(fetchAndCache(request, cache).catch(function () {}));
        return cached;
      }
      return fetchAndCache(request, cache);
    });
  });
}

self.addEventListener('fetch', function (event) {
  if (!isCdnAsset(event.request)) return; /* 其余请求交给浏览器默认行为 */
  event.respondWith(staleWhileRevalidate(event, event.request));
});

self.addEventListener('install', function () {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        if (key.indexOf(CACHE_PREFIX) === 0 && key !== CACHE_NAME) return caches.delete(key);
      }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});
