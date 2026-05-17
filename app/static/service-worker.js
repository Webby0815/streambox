const CACHE_NAME = "streambox-ultra-v1";
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/media/")) return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
