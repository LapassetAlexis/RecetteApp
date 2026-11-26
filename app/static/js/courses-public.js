// Page publique de la liste de courses (/courses/<jeton>) : coches partagées,
// articles cochés repliés sous « Déjà pris », rafraîchissement toutes les 20 s
// quand l'onglet est visible, copie texte. Le jeton ne permet que de cocher.
// Utilisable hors ligne : les coches passent par une file d'attente
// (localStorage) rejouée au retour du réseau ; avec un service worker (HTTPS
// ou localhost), la page se recharge aussi sans réseau.
// Données serveur lues dans #page-data : {version}.
(function () {
    'use strict';
    const {copyText, pageData} = window.Common;
    const BASE = window.location.pathname.replace(/\/+$/, '');
    const REFRESH_MS = 20000;
    const version = pageData().version;
    const $ = id => document.getElementById(id);

    function status(msg) {
        const el = $('pub-status');
        if (!el) return;
        el.textContent = msg || '';
        el.hidden = !msg;
    }

    // Insère `el` dans `grid` en respectant l'ordre d'origine (data-ord).
    function insertSorted(grid, el) {
        const ord = Number(el.dataset.ord);
        const next = [...grid.children].find(c => Number(c.dataset.ord) > ord);
        grid.insertBefore(el, next || null);
    }

    // Range une ligne : cochée → « Déjà pris », sinon dans son rayon.
    function place(el) {
        const checked = el.querySelector('.shop-c').checked;
        let grid = $('pub-pris-g');
        if (!checked) {
            const sec = [...document.querySelectorAll('#shop-g .shop-rayon')]
                .find(s => s.dataset.rayon === el.dataset.rayon);
            grid = sec && sec.querySelector('.shop-grid');
        }
        if (grid && el.parentElement !== grid) insertSorted(grid, el);
    }

    // Compteurs, rayons vides masqués, section « Déjà pris » et message final.
    function refreshCounts() {
        let restants = 0;
        document.querySelectorAll('#shop-g .shop-rayon').forEach(sec => {
            const n = sec.querySelectorAll('.shop-i').length;
            restants += n;
            sec.hidden = n === 0;
            const badge = sec.querySelector('.rayon-n');
            if (badge) badge.textContent = n;
        });
        const pris = document.querySelectorAll('#pub-pris-g .shop-i').length;
        if ($('pub-n')) $('pub-n').textContent = restants;
        if ($('pub-pris-n')) $('pub-pris-n').textContent = pris;
        if ($('pub-pris')) $('pub-pris').hidden = pris === 0;
        if ($('pub-done')) $('pub-done').hidden = restants > 0;
    }

    // Applique un état {clé: coché} aux cases concernées (sans rien envoyer).
    function applyState(want) {
        let moved = false;
        document.querySelectorAll('.pub-i').forEach(el => {
            const k = el.dataset.item;
            if (!(k in want)) return;
            const cb = el.querySelector('.shop-c');
            const v = !!want[k];
            if (cb.checked !== v) { cb.checked = v; place(el); moved = true; }
        });
        if (moved) refreshCounts();
    }

    // ── Stockage local (toujours en try/catch : navigation privée, quota…) ──
    function lsGet(key) {
        try { return JSON.parse(window.localStorage.getItem(key) || 'null'); } catch (e) { return null; }
    }
    function lsSet(key, val) {
        try { window.localStorage.setItem(key, JSON.stringify(val)); } catch (e) { /* mémoire seule */ }
    }
    function lsDel(key) {
        try { window.localStorage.removeItem(key); } catch (e) { /* rien */ }
    }

    // ── File d'attente des coches ──────────────────────────────────────────
    // Chaque coche est d'abord écrite dans la file {clé: état voulu}, puis
    // envoyée. On envoie l'ÉTAT voulu (pas une bascule) : rejouer deux fois la
    // même coche ne change rien côté serveur. Recocher un article remplace son
    // entrée : ni doublon ni perte. Sans localStorage, la file reste en
    // mémoire et couvre une coupure brève sur la page déjà chargée.
    const QKEY = `courses-file:${BASE}`;
    const SKEY = `courses-etat:${BASE}`;   // dernier état connu du serveur
    let memQueue = {};
    let flushing = false;

    function readQueue() {
        const q = lsGet(QKEY);
        return {...memQueue, ...(q && typeof q === 'object' ? q : {})};
    }
    function writeQueue(q) {
        memQueue = q;
        if (Object.keys(q).length) lsSet(QKEY, q); else lsDel(QKEY);
    }
    const queued = () => Object.keys(readQueue()).length > 0;

    function saveSnapshot(checked) {
        lsSet(SKEY, {version, checked: [...checked]});
    }
    function snapshotSet(item, checked) {
        const snap = lsGet(SKEY);
        if (!snap || snap.version !== version || !Array.isArray(snap.checked)) return;
        const s = new Set(snap.checked);
        if (checked) s.add(item); else s.delete(item);
        saveSnapshot(s);
    }

    function setOffline(v) {
        const el = $('pub-offline');
        if (el) el.hidden = !v;
    }

    // Envoie la file, article par article. S'arrête à la première erreur
    // réseau : on réessaiera au retour du réseau (`online`), au chargement et
    // à chaque rafraîchissement.
    async function flush() {
        if (flushing) return;
        // Navigateur sûr d'être hors ligne : inutile d'essayer, `online` relancera.
        if (navigator.onLine === false) { setOffline(true); return; }
        flushing = true;
        let reload = false;
        try {
            for (;;) {
                const q = readQueue();
                const item = Object.keys(q)[0];
                if (item === undefined) break;
                const checked = !!q[item];
                let r;
                try {
                    r = await fetch(`${BASE}/check`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({item, checked}),
                    });
                } catch (e) {
                    setOffline(true);
                    return;
                }
                // 5xx (serveur indisponible) : on garde la file pour plus tard.
                if (r.status >= 500) { setOffline(true); return; }
                // 400 : article disparu (liste modifiée) → abandonné, page
                // rechargée ensuite. 404 : lien révoqué → abandonné.
                if (r.status === 400) reload = true;
                if (r.ok) snapshotSet(item, checked);
                const cur = readQueue();
                if (cur[item] === checked) {   // pas recoché pendant l'envoi
                    delete cur[item];
                    writeQueue(cur);
                }
            }
            setOffline(false);
            status('');
        } finally {
            flushing = false;
        }
        if (reload) window.location.reload();
    }

    function onChange(e) {
        const cb = e.target.closest('.shop-c');
        if (!cb) return;
        const el = cb.closest('.shop-i');
        place(el);
        refreshCounts();
        writeQueue({...readQueue(), [el.dataset.item]: cb.checked});
        flush();
    }

    // Rafraîchissement : nouvelles coches de l'autre appareil ; liste modifiée
    // → rechargement. Les articles encore en file gardent leur état local.
    // Renvoie true si l'état a été appliqué depuis le réseau.
    async function poll(force) {
        if (!force && document.visibilityState !== 'visible') return false;
        if (navigator.onLine === false) { setOffline(true); return false; }
        if (queued()) await flush();
        let d, fromCache;
        try {
            const r = await fetch(`${BASE}/etat`, {cache: 'no-store'});
            if (!r.ok) return false;
            // Réponse servie par le service worker faute de réseau.
            fromCache = r.headers.get('X-SW-Cache') === '1';
            d = await r.json();
        } catch (e) {
            setOffline(true);
            return false;
        }
        if (fromCache) {
            setOffline(true);
            // On préfère le dernier état mémorisé par la page, plus récent.
            if (lsGet(SKEY) || d.version !== version) return false;
        } else {
            setOffline(queued());
            if (d.version !== version) { await flush(); window.location.reload(); return true; }
            saveSnapshot(d.checked || []);
        }
        const q = readQueue();
        const checked = new Set(d.checked || []);
        const want = {};
        document.querySelectorAll('.pub-i').forEach(el => {
            const k = el.dataset.item;
            if (!(k in q)) want[k] = checked.has(k);
        });
        applyState(want);
        return !fromCache;
    }

    // Au chargement : état du serveur, ou hors ligne le dernier état
    // mémorisé ; puis coches en attente par-dessus ; puis envoi de la file.
    async function init() {
        applyState(readQueue());
        const fresh = await poll(true);
        if (!fresh) {
            const snap = lsGet(SKEY);
            if (snap && snap.version === version && Array.isArray(snap.checked)) {
                const want = {};
                document.querySelectorAll('.pub-i').forEach(el => { want[el.dataset.item] = false; });
                snap.checked.forEach(k => { if (k in want) want[k] = true; });
                applyState(want);
            }
            applyState(readQueue());
        }
        if (queued()) { setOffline(true); flush(); }
    }

    // Service worker : seulement en contexte sécurisé (HTTPS ou localhost),
    // sans bruit sinon. Une fois actif, il met en cache cette page et ses
    // CSS/JS (lors de la première visite, la page n'est pas encore contrôlée).
    function registerSW() {
        if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
        navigator.serviceWorker.register('/sw.js')
            .then(() => navigator.serviceWorker.ready)
            .then(reg => {
                const urls = [window.location.href.split('#')[0], `${window.location.origin}${BASE}/etat`];
                document.querySelectorAll('link[rel="stylesheet"][href], script[src]')
                    .forEach(n => urls.push(n.href || n.src));
                if (reg.active) reg.active.postMessage({type: 'cache-urls', urls});
            })
            .catch(() => { /* hors ligne ou refusé : la file d'attente suffit */ });
    }

    // Export texte : repas libres + articles restant à prendre, par rayon.
    function copyList() {
        const lines = [];
        const libres = document.querySelectorAll('.shop-libre li');
        if (libres.length) {
            lines.push('📝 Repas libres (ingrédients à acheter) :');
            libres.forEach(li => lines.push(`- ${li.textContent.trim().replace(/\s+/g, ' ')}`));
            lines.push('');
        }
        document.querySelectorAll('#shop-g .shop-rayon').forEach(sec => {
            const items = [...sec.querySelectorAll('.shop-i')].map(l => {
                const nom = l.querySelector('span').textContent.trim();
                const q = l.querySelector('small')?.textContent.trim();
                return `- ${nom}${q ? ` (${q})` : ''}`;
            });
            if (items.length) lines.push(`— ${sec.dataset.rayon} —`, ...items, '');
        });
        const btn = $('pub-copy');
        copyText(lines.join('\n').trim()).then(ok => {
            btn.textContent = ok ? '✓ Copié' : '✗ Échec';
            setTimeout(() => { btn.textContent = '📋 Copier'; }, 1500);
        });
    }

    // 🍽 « recettes liées » : au doigt, aucun survol possible → un tap déplie la
    // liste des recettes sous l'ingrédient (sans cocher la case du label).
    document.addEventListener('click', e => {
        const b = e.target.closest('.shop-src');
        if (!b) return;
        e.preventDefault();
        b.closest('.shop-i')?.classList.toggle('src-open');
    });
    document.addEventListener('change', onChange);
    $('pub-copy')?.addEventListener('click', copyList);
    setInterval(() => poll(false), REFRESH_MS);
    document.addEventListener('visibilitychange', () => poll(false));
    window.addEventListener('online', () => { flush().then(() => poll(true)); });
    window.addEventListener('offline', () => setOffline(true));
    init();
    if (document.readyState === 'complete') registerSW();
    else window.addEventListener('load', registerSW);
})();
