// Service Worker — Menu Planner
// Stratégie :
//  - assets statiques (/static/...) : stale-while-revalidate.
//  - navigations (pages HTML) : network-first avec repli sur le cache, puis
//    sur la page de secours /static/offline.html (pré-cachée). La liste de
//    courses reste consultable sans réseau si elle a déjà été visitée.
//  - le reste (API POST, Notion, LLM) : réseau direct, jamais caché.
// Seules les réponses 200 « normales » sont mises en cache : ni erreurs,
// ni redirections (ex. renvoi vers /login quand l'auth est active).

const CACHE = 'menu-planner-v3';
const OFFLINE_URL = '/static/offline.html';
const PRECACHE = [
  OFFLINE_URL,
  '/static/style.css',
  '/static/icon.svg',
  '/static/icon-192.png',
];

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
          }).catch(() => hit || Response.error());
          return hit || fetching;
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
        caches.match(req)
          .then((hit) => hit || caches.match(OFFLINE_URL))
          .then((page) => page || new Response(
            '<h1>Hors ligne</h1><p>Page non disponible sans connexion.</p>',
            { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
          ))
      )
    );
  }
});
