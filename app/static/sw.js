// Service Worker — Menu Planner
// Stratégie :
//  - assets statiques (/static/...) : cache-first (rapides, hors-ligne).
//  - navigations (pages HTML) : network-first avec repli sur le cache, puis
//    sur une page hors-ligne minimale. La liste de courses reste consultable
//    sans réseau (la dernière page planning visitée est en cache).
//  - le reste (API POST, Notion, LLM) : réseau direct, jamais caché.

const CACHE = 'menu-planner-v1';
const PRECACHE = [
  '/',
  '/static/style.css',
  '/static/icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // Ne toucher qu'aux GET de notre origine.
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) {
    return;
  }

  const url = new URL(req.url);

  // Assets statiques : cache-first.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((hit) =>
        hit || fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
      )
    );
    return;
  }

  // Navigations / pages : network-first, repli cache.
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    event.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(() =>
        caches.match(req).then((hit) =>
          hit || caches.match('/') ||
          new Response('<h1>Hors ligne</h1><p>Page non disponible sans connexion.</p>',
            { headers: { 'Content-Type': 'text/html; charset=utf-8' } })
        )
      )
    );
  }
});
