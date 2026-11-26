// Page planning : liste de courses (coches persistées + file hors-ligne,
// rafraîchissement des coches, articles ajoutés à la main, rayon, masques,
// placard, recettes liées, copie, partage révocable, génération).
// Données serveur lues dans #page-data : {pid}. L'état coché est rendu côté
// serveur dans les cases ; chaque ligne porte sa clé d'article (data-item).
(function () {
    'use strict';
    const {copyText, postJson, pageData, requestJson, modal, esc} = window.Common;
    const pid = pageData().pid;
    const $ = id => document.getElementById(id);
    const REFRESH_MS = 20000;

    // ── Coches (côté serveur, file d'attente hors-ligne) ───────────────
    // Même format de file que la page publique : objet {clé: état voulu}.
    // On envoie l'ÉTAT voulu (pas une bascule) : rejouer ne change rien.
    // Les changements non envoyés (réseau HS) sont rejoués au prochain
    // chargement et à chaque rafraîchissement.
    const SHOP_KEY = 'shop_checked_' + pid;      // ancien cache local, purgé à la régénération
    const SHOP_QUEUE = 'shop_queue_' + pid;      // file des coches en attente
    const pending = new Set();                   // coches en cours d'envoi

    function loadQueue() {
        let q;
        try { q = JSON.parse(localStorage.getItem(SHOP_QUEUE) || '{}'); } catch (e) { return {}; }
        // Ancienne file (liste [{item, checked}]) : reprise, le dernier état gagne.
        if (Array.isArray(q)) {
            const obj = {};
            q.forEach(e => { if (e && e.item) obj[e.item] = !!e.checked; });
            saveQueue(obj);
            return obj;
        }
        return q && typeof q === 'object' ? q : {};
    }
    function saveQueue(q) {
        try {
            if (Object.keys(q).length) localStorage.setItem(SHOP_QUEUE, JSON.stringify(q));
            else localStorage.removeItem(SHOP_QUEUE);
        } catch (e) { /* mémoire seule */ }
    }
    async function postCheck(item, checked) {
        const d = await postJson(`/api/shopping-check/${pid}`, {item, checked});
        if (!d.success) throw new Error(d.error || 'Erreur serveur');
    }
    function enqueue(item, checked) {
        saveQueue({...loadQueue(), [item]: checked});
    }
    function checkbox(item) {
        return document.querySelector(`.shop-i[data-item="${CSS.escape(item)}"] .shop-c`);
    }
    async function flushQueue() {
        const q = loadQueue();
        const items = Object.keys(q);
        if (!items.length) return;
        // Affiche d'abord l'état en attente (le serveur ne l'a pas encore).
        items.forEach(item => { const cb = checkbox(item); if (cb) cb.checked = !!q[item]; });
        for (const item of items) {
            try {
                await postCheck(item, !!q[item]);
                const cur = loadQueue();
                if (cur[item] === q[item]) { delete cur[item]; saveQueue(cur); }
            } catch (err) { /* on garde ceux qui échouent encore */ }
        }
    }
    async function onToggle(item, checked) {
        pending.add(item);
        try { await postCheck(item, checked); }
        catch (err) { enqueue(item, checked); }  // réseau HS : on rejouera
        finally { pending.delete(item); }
    }

    // Rafraîchissement : coches faites ailleurs (lien public, autre appareil)
    // appliquées toutes les 20 s quand l'onglet est visible. Les articles en
    // file ou en cours d'envoi gardent leur état local.
    async function poll() {
        if (document.visibilityState !== 'visible' || !$('shop-g')) return;
        await flushQueue();
        let d;
        try { d = await requestJson(`/api/courses/${pid}/etat`); } catch (e) { return; }
        if (!d || !Array.isArray(d.checked)) return;
        const q = loadQueue();
        const checked = new Set(d.checked);
        document.querySelectorAll('#shop-g .shop-i').forEach(el => {
            const k = el.dataset.item;
            if (k in q || pending.has(k)) return;
            const cb = el.querySelector('.shop-c');
            if (cb && cb.checked !== checked.has(k)) cb.checked = checked.has(k);
        });
    }

    // ── Articles manuels, masques et placard ────────────────────────────
    // Chaque action renvoie la liste re-rendue côté serveur ({html, nb}).
    function renderList(d) {
        if (typeof d.html !== 'string') return;
        const body = $('shop-body');
        const wasOpen = $('shop-masques')?.open;
        body.innerHTML = d.html;
        if (wasOpen && $('shop-masques')) $('shop-masques').open = true;
        if ($('shop-n')) $('shop-n').textContent = d.nb;
        if ($('placard-n') && typeof d.nb_placard === 'number') $('placard-n').textContent = d.nb_placard;
        // Une liste faite seulement d'articles manuels se partage aussi.
        if ($('shop-share') && d.nb > 0) $('shop-share').hidden = false;
    }
    async function listAction(url, body, method) {
        try {
            const d = await requestJson(url, {method: method || 'POST', body});
            if (d.success) { renderList(d); return true; }
            toast(d.error || 'Erreur', 'error');
        } catch (e) { toast('Erreur réseau', 'error'); }
        return false;
    }
    const ACTIONS = {
        suppr: key => listAction(`/api/courses/${pid}/extra/${encodeURIComponent(key)}`, undefined, 'DELETE'),
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

    function setRayon(sel) {
        const key = sel.closest('[data-item]').dataset.item;
        sel.disabled = true;
        listAction(`/api/courses/${pid}/extra/${encodeURIComponent(key)}/rayon`, {rayon: sel.value})
            .then(ok => { if (!ok) sel.disabled = false; });
    }

    // ── Placard : liste complète, retrait sans ouvrir le planning concerné ──
    function renderPlacard(noms) {
        const ul = $('placard-l');
        ul.innerHTML = noms.map(n => `<li data-nom="${esc(n)}"><span>${esc(n.charAt(0).toUpperCase() + n.slice(1))}</span>`
            + `<button type="button" class="shop-act" data-placard-del>Retirer du placard</button></li>`).join('');
        $('placard-vide').hidden = noms.length > 0;
        if ($('placard-n')) $('placard-n').textContent = noms.length;
    }
    async function openPlacard() {
        const el = $('placard-modal');
        renderPlacard([]);
        $('placard-vide').hidden = true;
        const m = modal(el, {initialFocus: '.modal-x'});
        el.querySelectorAll('[data-close]').forEach(b => { b.onclick = () => m.close(); });
        try {
            const d = await requestJson('/api/placard');
            if (Array.isArray(d.placard)) renderPlacard(d.placard);
            else toast(d.error || 'Erreur', 'error');
        } catch (e) { toast('Erreur réseau', 'error'); }
    }
    async function retirerPlacard(btn) {
        const li = btn.closest('li');
        btn.disabled = true;
        try {
            const d = await requestJson(
                `/api/placard/${encodeURIComponent(li.dataset.nom)}?planning=${pid}`, {method: 'DELETE'});
            if (d.success) {
                renderList(d);
                renderPlacard(d.placard || []);
                ($('placard-l').querySelector('button') || $('placard-modal').querySelector('.modal-x')).focus();
                return;
            }
            toast(d.error || 'Erreur', 'error');
        } catch (e) { toast('Erreur réseau', 'error'); }
        btn.disabled = false;
    }

    // ── Partage public de la liste (coches partagées, lien révocable) ───
    async function partagerListe() {
        const b = $('btn-share');
        if (b) b.disabled = true;
        try {
            const d = await postJson(`/planning/${pid}/partager`);
            if (d.url) {
                if ($('btn-unshare')) $('btn-unshare').hidden = false;
                const abs = window.location.origin + d.url;
                const ok = await copyText(abs);
                toast(ok ? 'Lien copié ✓ (la personne pourra cocher)' : abs, ok ? 'success' : 'info');
            } else { toast(d.error || 'Erreur', 'error'); }
        } catch (e) { toast('Erreur réseau', 'error'); }
        finally { if (b) b.disabled = false; }
    }
    async function revoquerLien() {
        if (!window.confirm('Révoquer le lien de partage ? Il ne fonctionnera plus. « Partager » en créera un nouveau.')) return;
        const b = $('btn-unshare');
        b.disabled = true;
        try {
            const d = await postJson(`/planning/${pid}/partager`, {action: 'revoquer'});
            if (!d.error) {
                b.hidden = true;
                toast('Lien révoqué ✓', 'success');
            } else { toast(d.error, 'error'); }
        } catch (e) { toast('Erreur réseau', 'error'); }
        finally { b.disabled = false; }
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
        // Régénérer efface les coches des articles de recette : on demande avant.
        const coches = document.querySelectorAll('#shop-g .shop-i:not(.shop-extra) .shop-c:checked').length;
        if (regen && coches && !window.confirm(
            `Régénérer la liste ? Les ${coches} article(s) coché(s) seront décochés (les articles ajoutés à la main gardent leur coche).`)) return;
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
        if (e.target.classList.contains('shop-rayon-sel')) { setRayon(e.target); return; }
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
    $('btn-unshare')?.addEventListener('click', revoquerLien);
    $('btn-placard')?.addEventListener('click', openPlacard);
    $('placard-l')?.addEventListener('click', e => {
        const b = e.target.closest('[data-placard-del]');
        if (b) retirerPlacard(b);
    });
    setInterval(poll, REFRESH_MS);
    document.addEventListener('visibilitychange', poll);
    $('btn-print')?.addEventListener('click', () => window.print());
    $('btn-regen')?.addEventListener('click', () => genShop(true));
    // Avertissement recettes non enrichies (posé avant reload par genShop).
    const w = sessionStorage.getItem('shop_warn_' + pid);
    if (w) { sessionStorage.removeItem('shop_warn_' + pid); toast(w, 'error'); }
})();
