// Service Worker — Menu Planner
// Stratégie :
//  - assets statiques (/static/...) : stale-while-revalidate. Une seule
//    entrée par chemin : ranger `style.css?v=B` retire `style.css?v=A` (et la
//    copie pré-cachée sans `?v=`). Sans cela, chaque redémarrage du serveur
//    (nouvel `asset_version`) ajouterait une copie de chaque fichier.
//  - navigations (pages HTML) : réseau. Seule la liste de courses publique
//    (/courses/<jeton>) est mise en cache, pour rester consultable sans
//    réseau ; les autres pages (protégées par l'authentification) ne sont
//    jamais conservées et tombent, hors ligne, sur la page de secours
//    /static/offline.html (pré-cachée).
//  - au plus MAX_PAGES listes publiques en cache : les plus anciennes sont
//    retirées, avec leur état des coches.
//  - état des coches de la liste publique (/courses/<jeton>/etat) : réseau
//    puis cache ; la réponse tirée du cache porte l'en-tête X-SW-Cache: 1
//    pour que la page sache qu'elle est hors ligne.
//  - la page publique demande (message 'cache-urls') la mise en cache
//    d'elle-même et de ses CSS/JS : lors de la toute première visite, elle
//    n'est pas encore contrôlée par le service worker. Seules ces URL-là
//    (statiques, liste publique, état) sont acceptées.
//  - lien de partage révoqué : un 404 sur la liste ou son état (ou le
//    message 'forget-page' de la page) retire ce lien du cache.
//  - le reste (API POST, Notion, LLM) : réseau direct, jamais caché.
// Seules les réponses 200 « normales » sont mises en cache : ni erreurs,
// ni redirections (ex. renvoi vers /login quand l'auth est active).

const CACHE = 'menu-planner-v6';
const OFFLINE_URL = '/static/offline.html';
const MAX_PAGES = 20;
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
const PAGE_RE = /^\/courses\/[^/]+\/?$/;

function cacheable(res) {
  return res && res.status === 200 && !res.redirected && res.type === 'basic';
}

function toUrl(req) {
  return new URL(typeof req === 'string' ? req : req.url, self.location.origin);
}

// Asset statique : range la réponse puis retire les autres versions du même
// chemin (autre `?v=`, ou sans paramètre).
function putStatic(cache, req, res) {
  const url = toUrl(req);
  return cache.put(url.href, res).then(() => cache.keys()).then((keys) => Promise.all(
    keys.filter((k) => {
      const u = new URL(k.url);
      return u.pathname === url.pathname && u.href !== url.href;
    }).map((k) => cache.delete(k))
  ));
}

// Liste publique ou son état : rangé par chemin (sans paramètre), puis borne
// MAX_PAGES. cache.put replace l'entrée en fin de liste : les premières clés
// sont les plus anciennes.
function putPage(cache, pathname, res) {
  return cache.put(pathname, res).then(() => cache.keys()).then((keys) => {
    const pages = keys.map((k) => new URL(k.url).pathname).filter((p) => PAGE_RE.test(p));
    const excess = pages.slice(0, Math.max(0, pages.length - MAX_PAGES));
    return Promise.all(excess.map((p) => Promise.all([
      cache.delete(p),
      cache.delete(p.replace(/\/$/, '') + '/etat'),
    ])));
  });
}

// Lien de partage révoqué (404) : la liste et son état quittent le cache,
// elle ne s'ouvre plus hors ligne.
function forgetPage(pathname) {
  const page = pathname.replace(/\/etat$/, '').replace(/\/$/, '');
  return caches.open(CACHE).then((c) => Promise.all([
    c.delete(page), c.delete(page + '/'), c.delete(page + '/etat'),
  ]));
}

// Range une réponse selon le type d'URL ; ignore toute autre URL.
function store(cache, req, res) {
  const url = toUrl(req);
  if (url.origin !== self.location.origin) return Promise.resolve();
  if (url.pathname.startsWith('/static/')) return putStatic(cache, url.href, res);
  if (PAGE_RE.test(url.pathname) || ETAT_RE.test(url.pathname)) return putPage(cache, url.pathname, res);
  return Promise.resolve();
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

// Mise en cache à la demande d'une page (URL de même origine uniquement, et
// seulement celles que `store` accepte). Une URL après l'autre : la purge des
// versions et la borne des pages ne se croisent pas.
self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type === 'forget-page' && typeof data.path === 'string'
      && (PAGE_RE.test(data.path) || ETAT_RE.test(data.path))) {
    event.waitUntil(forgetPage(data.path));
    return;
  }
  if (data.type !== 'cache-urls' || !Array.isArray(data.urls)) return;
  const urls = data.urls.filter((u) => {
    try { return new URL(u, self.location.origin).origin === self.location.origin; } catch (e) { return false; }
  });
  event.waitUntil(
    caches.open(CACHE).then((cache) => urls.reduce((chain, u) => chain.then(() =>
      fetch(u, { credentials: 'same-origin' })
        .then((res) => { if (cacheable(res)) return store(cache, u, res); })
        .catch(() => {})
    ), Promise.resolve()))
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
            if (cacheable(res)) event.waitUntil(putStatic(cache, req, res.clone()));
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
          event.waitUntil(caches.open(CACHE).then((c) => putPage(c, url.pathname, copy)));
        } else if (res.status === 404) {
          event.waitUntil(forgetPage(url.pathname));
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

  // Navigations / pages : réseau. Hors ligne : la liste publique depuis le
  // cache, toute autre page → page de secours (jamais de page privée gardée).
  if (req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html')) {
    const publique = PAGE_RE.test(url.pathname);
    event.respondWith(
      fetch(req).then((res) => {
        if (!publique) return res;
        if (cacheable(res)) {
          const copy = res.clone();
          event.waitUntil(caches.open(CACHE).then((c) => putPage(c, url.pathname, copy)));
        } else if (res.status === 404) {
          event.waitUntil(forgetPage(url.pathname));
        }
        return res;
      }).catch(() =>
        (publique ? caches.match(url.pathname, { ignoreVary: true }) : Promise.resolve(undefined))
          .then((hit) => hit || caches.match(OFFLINE_URL))
          .then((page) => page || new Response(
            '<h1>Hors ligne</h1><p>Page non disponible sans connexion.</p>',
            { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
          ))
      )
    );
  }
});
