// Page publique de la liste de courses (/courses/<jeton>) : coches partagées,
// articles cochés repliés sous « Déjà pris », rafraîchissement toutes les 20 s
// quand l'onglet est visible, copie texte. Le jeton ne permet que de cocher.
// Données serveur lues dans #page-data : {version}.
(function () {
    'use strict';
    const {copyText, pageData} = window.Common;
    const BASE = window.location.pathname.replace(/\/+$/, '');
    const REFRESH_MS = 20000;
    let version = pageData().version;
    const pending = new Set();   // clés en cours d'envoi : le rafraîchissement n'y touche pas
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

    async function sendCheck(el, cb) {
        const item = el.dataset.item;
        pending.add(item);
        try {
            const r = await fetch(`${BASE}/check`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({item, checked: cb.checked}),
            });
            if (r.status === 400) { window.location.reload(); return; }  // liste changée entre-temps
            if (!r.ok) throw new Error(String(r.status));
            status('');
        } catch (e) {
            // Pas de file d'attente ici : on annule la coche et on prévient.
            cb.checked = !cb.checked;
            place(el);
            refreshCounts();
            status('Coche non enregistrée (réseau ?). Réessaie dans un instant.');
        } finally {
            pending.delete(item);
        }
    }

    function onChange(e) {
        const cb = e.target.closest('.shop-c');
        if (!cb) return;
        const el = cb.closest('.shop-i');
        place(el);
        refreshCounts();
        sendCheck(el, cb);
    }

    // Rafraîchissement : nouvelles coches de l'autre appareil ; liste modifiée → rechargement.
    async function poll() {
        if (document.visibilityState !== 'visible') return;
        try {
            const r = await fetch(`${BASE}/etat`, {cache: 'no-store'});
            if (!r.ok) return;
            const d = await r.json();
            if (d.version !== version) { window.location.reload(); return; }
            const checked = new Set(d.checked || []);
            let moved = false;
            document.querySelectorAll('.pub-i').forEach(el => {
                if (pending.has(el.dataset.item)) return;
                const cb = el.querySelector('.shop-c');
                const want = checked.has(el.dataset.item);
                if (cb.checked !== want) { cb.checked = want; place(el); moved = true; }
            });
            if (moved) refreshCounts();
        } catch (e) { /* hors réseau : on réessaiera au prochain tour */ }
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
    setInterval(poll, REFRESH_MS);
    document.addEventListener('visibilitychange', poll);
})();
