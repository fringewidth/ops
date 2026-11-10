// Network first, cache as fallback, so the app opens offline with the last state it saw.
const CACHE = "ops-v1";
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok && new URL(e.request.url).origin === location.origin) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request.url.split("?")[0], copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request.url.split("?")[0]))
  );
});
