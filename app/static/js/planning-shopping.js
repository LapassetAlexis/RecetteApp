// Page planning : liste de courses (coches persistées + file hors-ligne,
// articles ajoutés à la main, masques, placard, recettes liées, copie,
// partage, génération).
// Données serveur lues dans #page-data : {pid}. L'état coché est rendu côté
// serveur dans les cases ; chaque ligne porte sa clé d'article (data-item).
(function () {
    'use strict';
    const {copyText, postJson, pageData} = window.Common;
    const pid = pageData().pid;
    const $ = id => document.getElementById(id);

    // ── Coches (côté serveur, file d'attente hors-ligne) ───────────────
    // Les changements non envoyés (réseau HS) sont mis en file et rejoués au
    // prochain chargement.
    const SHOP_KEY = 'shop_checked_' + pid;      // ancien cache local, purgé à la régénération
    const SHOP_QUEUE = 'shop_queue_' + pid;      // file des POST en attente

    function loadQueue() {
        try { return JSON.parse(localStorage.getItem(SHOP_QUEUE) || '[]'); }
        catch (e) { return []; }
    }
    function saveQueue(q) {
        try { localStorage.setItem(SHOP_QUEUE, JSON.stringify(q)); } catch (e) {}
    }
    async function postCheck(item, checked) {
        const d = await postJson(`/api/shopping-check/${pid}`, {item, checked});
        if (!d.success) throw new Error(d.error || 'Erreur serveur');
    }
    function enqueue(item, checked) {
        // Dédoublonne : le dernier état d'un item écrase le précédent.
        const q = loadQueue().filter(e => e.item !== item);
        q.push({item, checked});
        saveQueue(q);
    }
    async function flushQueue() {
        const q = loadQueue();
        if (!q.length) return;
        // Affiche d'abord l'état en attente (le serveur ne l'a pas encore).
        q.forEach(e => {
            const cb = document.querySelector(`.shop-i[data-item="${CSS.escape(e.item)}"] .shop-c`);
            if (cb) cb.checked = e.checked;
        });
        const rest = [];
        for (const e of q) {
            try { await postCheck(e.item, e.checked); }
            catch (err) { rest.push(e); }  // on garde ceux qui échouent encore
        }
        saveQueue(rest);
    }
    async function onToggle(item, checked) {
        try { await postCheck(item, checked); }
        catch (err) { enqueue(item, checked); }  // réseau HS : on rejouera au load
    }

    // ── Articles manuels, masques et placard ────────────────────────────
    // Chaque action renvoie la liste re-rendue côté serveur ({html, nb}).
    function renderList(d) {
        const body = $('shop-body');
        const wasOpen = $('shop-masques')?.open;
        body.innerHTML = d.html;
        if (wasOpen && $('shop-masques')) $('shop-masques').open = true;
        if ($('shop-n')) $('shop-n').textContent = d.nb;
    }
    async function listAction(url, body, method) {
        try {
            const r = await fetch(url, {
                method: method || 'POST',
                headers: body ? {'Content-Type': 'application/json'} : {},
                body: body ? JSON.stringify(body) : undefined,
            });
            const d = await r.json();
            if (d.success) { renderList(d); return true; }
            toast(d.error || 'Erreur', 'error');
        } catch (e) { toast('Erreur réseau', 'error'); }
        return false;
    }
    const ACTIONS = {
        suppr: key => listAction(`/api/courses/${pid}/extra/${encodeURIComponent(key)}`, null, 'DELETE'),
        masquer: key => listAction(`/api/courses/${pid}/masquer`, {key, masque: true}),
        restaurer: key => listAction(`/api/courses/${pid}/masquer`, {key, masque: false}),
        placard: key => listAction(`/api/courses/${pid}/placard`, {key, present: true})
            .then(ok => ok && toast('Rangé dans « Toujours à la maison » ✓', 'success')),
        deplacard: key => listAction(`/api/courses/${pid}/placard`, {key, present: false}),
    };

    async function addExtra(e) {
        e.preventDefault();
        const input = $('shop-add-t');
        const texte = input.value.trim();
        if (!texte) return;
        input.disabled = true;
        const ok = await listAction(`/api/courses/${pid}/extra`, {texte});
        input.disabled = false;
        if (ok) input.value = '';
        input.focus();
    }

    // ── Partage public de la liste (coches partagées) ───────────────────
    async function partagerListe() {
        const b = $('btn-share');
        if (b) b.disabled = true;
        try {
            const d = await postJson(`/planning/${pid}/partager`);
            if (d.url) {
                const abs = window.location.origin + d.url;
                const ok = await copyText(abs);
                toast(ok ? 'Lien copié ✓ (la personne pourra cocher)' : abs, ok ? 'success' : 'info');
            } else { toast(d.error || 'Erreur', 'error'); }
        } catch (e) { toast('Erreur réseau', 'error'); }
        finally { if (b) b.disabled = false; }
    }

    // ── Copie de la liste (non cochés uniquement, hors masqués) ─────────
    function copyShop() {
        const lines = [];
        // Repas libres (rappel « ingrédients à acheter ») en tête de liste.
        const libres = document.querySelectorAll('.shop-libre li');
        if (libres.length) {
            lines.push('📝 Repas libres (ingrédients à acheter) :');
            libres.forEach(li => lines.push(`- ${li.textContent.trim().replace(/\s+/g, ' ')}`));
            lines.push('');
        }
        document.querySelectorAll('#shop-g .shop-rayon').forEach(sec => {
            // Uniquement les articles NON cochés (déjà achetés = exclus).
            const items = [];
            sec.querySelectorAll('.shop-i').forEach(l => {
                if (l.querySelector('.shop-c')?.checked) return;
                const nom = l.querySelector('span').textContent.trim();
                const q = l.querySelector('small')?.textContent.trim();
                items.push(`- ${nom}${q ? ` (${q})` : ''}`);
            });
            if (!items.length) return;   // rayon entièrement coché → omis
            lines.push(`\n— ${sec.dataset.rayon} —`, ...items);
        });
        copyText(lines.join('\n').trim()).then(ok => toast(ok ? 'Liste copiée ✓' : 'Copie impossible', ok ? 'success' : 'error'));
    }

    // ── Génération / régénération de la liste ───────────────────────────
    async function genShop(regen) {
        const btn = $('btn-shop') || $('btn-regen');
        const st = $('shop-status');
        if (btn) { btn.disabled = true; if (!regen) btn.textContent = '⏳ Génération...'; }
        if (st) st.textContent = 'Extraction des ingrédients en cours...';
        if (regen) toast('Régénération de la liste…', 'info');
        try {
            const d = await postJson(`/generate-shopping/${pid}`);
            if (d.success) {
                // Nouveaux items : on repart de zéro côté cache local aussi.
                localStorage.removeItem(SHOP_KEY);
                localStorage.removeItem(SHOP_QUEUE);
                if (d.non_enrichis && d.non_enrichis.length) {
                    sessionStorage.setItem('shop_warn_' + pid,
                        `${d.non_enrichis.length} recette(s) non enrichie(s), ingrédients manquants : ${d.non_enrichis.join(', ')}`);
                }
                window.location.reload();
            }
            else {
                toast('Erreur : ' + (d.error || '?'), 'error');
                if (st) st.textContent = 'Erreur: ' + (d.error || '?');
                if (btn) { btn.disabled = false; btn.textContent = '🛒 Réessayer'; }
            }
        } catch (e) {
            toast('Erreur réseau', 'error');
            if (st) st.textContent = 'Erreur réseau';
            if (btn) { btn.disabled = false; btn.textContent = '🛒 Réessayer'; }
        }
    }

    // ── Branchements (délégation : la liste est re-rendue après chaque action) ──
    const body = $('shop-body');
    body?.addEventListener('change', e => {
        const cb = e.target.closest('.shop-c');
        if (cb) onToggle(cb.closest('.shop-i').dataset.item, cb.checked);
    });
    body?.addEventListener('click', e => {
        const b = e.target.closest('button');
        if (!b || !body.contains(b)) return;
        if (b.id === 'btn-shop') { genShop(); return; }
        const line = b.closest('[data-item]');
        if (!line) return;
        // Bouton dans un <label> : ne pas cocher la case au passage.
        e.preventDefault();
        if (b.classList.contains('shop-src')) {
            // 🍽 « recettes liées » : le title ne s'affiche pas au doigt → un tap
            // déplie la liste sous l'ingrédient.
            line.classList.toggle('src-open');
        } else if (b.classList.contains('shop-more')) {
            const open = line.classList.toggle('acts-open');
            b.setAttribute('aria-expanded', String(open));
        } else if (ACTIONS[b.dataset.act]) {
            b.disabled = true;
            ACTIONS[b.dataset.act](line.dataset.item);
        }
    });
    flushQueue();  // rejoue les changements offline en attente
    $('shop-add')?.addEventListener('submit', addExtra);
    $('btn-copy')?.addEventListener('click', copyShop);
    $('btn-share')?.addEventListener('click', partagerListe);
    $('btn-print')?.addEventListener('click', () => window.print());
    $('btn-regen')?.addEventListener('click', () => genShop(true));
    // Avertissement recettes non enrichies (posé avant reload par genShop).
    const w = sessionStorage.getItem('shop_warn_' + pid);
    if (w) { sessionStorage.removeItem('shop_warn_' + pid); toast(w, 'error'); }
})();
