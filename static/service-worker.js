/* FinFlow service worker.
 *
 * Scope: the static shell only. Pages and every data request always go to the
 * network, so the ledger, totals and reports are never served stale from cache.
 * Bump CACHE_VERSION whenever a precached asset changes.
 */

const CACHE_VERSION = "finflow-static-v1";
const PRECACHE = [
    "/static/manifest.json",
    "/static/icon.png"
];

self.addEventListener("install", function (event) {
    event.waitUntil(
        caches.open(CACHE_VERSION).then(function (cache) {
            return Promise.all(PRECACHE.map(function (asset) {
                return cache.add(asset).catch(function () {
                    return undefined;   // a missing asset must not break install
                });
            }));
        }).then(function () {
            return self.skipWaiting();
        })
    );
});

self.addEventListener("activate", function (event) {
    event.waitUntil(
        caches.keys().then(function (keys) {
            return Promise.all(keys.map(function (key) {
                return key === CACHE_VERSION ? undefined : caches.delete(key);
            }));
        }).then(function () {
            return self.clients.claim();
        })
    );
});

self.addEventListener("fetch", function (event) {
    const request = event.request;

    if (request.method !== "GET") {
        return;                                   // never cache writes
    }

    const url = new URL(request.url);

    if (url.origin !== self.location.origin) {
        return;
    }

    if (!url.pathname.startsWith("/static/")) {
        return;                                   // pages: always live
    }

    event.respondWith(
        caches.match(request).then(function (cached) {
            if (cached) {
                return cached;
            }
            return fetch(request).then(function (response) {
                if (response && response.status === 200) {
                    const copy = response.clone();
                    caches.open(CACHE_VERSION).then(function (cache) {
                        cache.put(request, copy);
                    });
                }
                return response;
            });
        })
    );
});
