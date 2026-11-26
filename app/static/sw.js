// Service Worker — Menu Planner
// Stratégie :
//  - assets statiques (/static/...) : stale-while-revalidate.
//  - navigations (pages HTML) : network-first avec repli sur le cache, puis
//    sur la page de secours /static/offline.html (pré-cachée). La liste de
//    courses reste consultable sans réseau si elle a déjà été visitée.
//  - état des coches de la liste publique (/courses/<jeton>/etat) : réseau
//    puis cache ; la réponse tirée du cache porte l'en-tête X-SW-Cache: 1
//    pour que la page sache qu'elle est hors ligne.
//  - la page publique demande (message 'cache-urls') la mise en cache
//    d'elle-même et de ses CSS/JS : lors de la toute première visite, elle
//    n'est pas encore contrôlée par le service worker.
//  - le reste (API POST, Notion, LLM) : réseau direct, jamais caché.
// Seules les réponses 200 « normales » sont mises en cache : ni erreurs,
// ni redirections (ex. renvoi vers /login quand l'auth est active).

const CACHE = 'menu-planner-v4';
const OFFLINE_URL = '/static/offline.html';
// Inclut les CSS/JS de la liste de courses publique (demandés avec ?v=… :
// repli ignoreSearch hors ligne).
const PRECACHE = [
  OFFLINE_URL,
  '/static/style.css',
  '/static/icon.svg',
  '/static/icon-192.png',
  '/static/css/courses.css',
  '/static/js/common.js',
  '/static/js/courses-public.js',
];
const ETAT_RE = /^\/courses\/[^/]+\/etat$/;

function cacheable(res) {
  return res && res.status === 200 && !res.redirected && res.type === 'basic';
}

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

// Mise en cache à la demande d'une page (URL de même origine uniquement).
self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type !== 'cache-urls' || !Array.isArray(data.urls)) return;
  const urls = data.urls.filter((u) => {
    try { return new URL(u, self.location.origin).origin === self.location.origin; } catch (e) { return false; }
  });
  event.waitUntil(
    caches.open(CACHE).then((cache) => Promise.all(urls.map((u) =>
      fetch(u, { credentials: 'same-origin' })
        .then((res) => { if (cacheable(res)) return cache.put(u, res); })
        .catch(() => {})
    )))
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // Ne toucher qu'aux GET de notre origine.
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) {
    return;
  }

  const url = new URL(req.url);

  // Assets statiques : stale-while-revalidate.
  // On sert le cache immédiatement (rapide, offline OK) MAIS on refetch en
  // arrière-plan et on met le cache à jour → le rechargement suivant a la
  // dernière version. Évite de servir indéfiniment un vieux CSS/JS.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(req).then((hit) => {
          const fetching = fetch(req).then((res) => {
            if (cacheable(res)) cache.put(req, res.clone());
            return res;
          }).catch(() => hit || cache.match(req, { ignoreSearch: true })
            .then((any) => any || Response.error()));
          return hit || fetching;
        })
      )
    );
    return;
  }

  // État des coches de la liste publique : réseau puis cache, marqué.
  if (ETAT_RE.test(url.pathname)) {
    event.respondWith(
      fetch(req).then((res) => {
        if (cacheable(res)) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(url.pathname, copy));
        }
        return res;
      }).catch(() =>
        caches.match(url.pathname).then((hit) => {
          if (!hit) return Response.error();
          const headers = new Headers(hit.headers);
          headers.set('X-SW-Cache', '1');
          return hit.text().then((body) => new Response(body, { status: 200, headers }));
        })
      )
    );
    return;
  }

  // Navigations / pages : network-first, repli cache, puis page de secours.
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    event.respondWith(
      fetch(req).then((res) => {
        if (cacheable(res)) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() =>
        caches.match(req, { ignoreVary: true })
          .then((hit) => hit || caches.match(OFFLINE_URL))
          .then((page) => page || new Response(
            '<h1>Hors ligne</h1><p>Page non disponible sans connexion.</p>',
            { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
          ))
      )
    );
  }
});
