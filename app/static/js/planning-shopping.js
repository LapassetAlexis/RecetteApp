// Page planning : liste de courses (coches persistées + file hors-ligne,
// recettes liées, copie, partage, génération).
// Données serveur lues dans #page-data : {pid, courses_checked}.
(function () {
    'use strict';
    const {copyText, postJson, pageData} = window.Common;
    const DATA = pageData();
    const pid = DATA.pid;
    const $ = id => document.getElementById(id);

    // ── Persistance des cases cochées (côté serveur, cache offline) ────
    // L'état de référence vient du serveur (courses_checked). On garde localStorage
    // comme cache offline : les changements non envoyés (réseau HS) sont mis en file
    // et rejoués au prochain chargement.
    const SHOP_KEY = 'shop_checked_' + pid;      // cache local de l'état
    const SHOP_QUEUE = 'shop_queue_' + pid;      // file des POST en attente
    const CHECKED = new Set(DATA.courses_checked || []);

    function saveLocal() {
        try { localStorage.setItem(SHOP_KEY, JSON.stringify([...CHECKED])); } catch (e) {}
    }
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
        const rest = [];
        for (const e of q) {
            try { await postCheck(e.item, e.checked); }
            catch (err) { rest.push(e); }  // on garde ceux qui échouent encore
        }
        saveQueue(rest);
    }
    async function onToggle(item, checked) {
        if (checked) CHECKED.add(item); else CHECKED.delete(item);
        saveLocal();
        try { await postCheck(item, checked); }
        catch (err) { enqueue(item, checked); }  // réseau HS : on rejouera au load
    }
    function bindShopItems() {
        document.querySelectorAll('.shop-i').forEach(l => {
            const cb = l.querySelector('.shop-c');
            if (CHECKED.has(l.dataset.item)) cb.checked = true;
            cb.addEventListener('change', () => onToggle(l.dataset.item, cb.checked));
        });
    }

    // ── Partage public de la liste (lecture seule, non coché uniquement) ──
    async function partagerListe() {
        const b = $('btn-share');
        if (b) b.disabled = true;
        try {
            const d = await postJson(`/planning/${pid}/partager`);
            if (d.url) {
                const abs = window.location.origin + d.url;
                try { await navigator.clipboard.writeText(abs); toast('Lien copié ✓', 'success'); }
                catch (e) { toast(abs, 'info'); }
            } else { toast(d.error || 'Erreur', 'error'); }
        } catch (e) { toast('Erreur réseau', 'error'); }
        finally { if (b) b.disabled = false; }
    }

    // ── Copie de la liste (non cochés uniquement) ───────────────────────
    function copyShop() {
        const lines = [];
        // Repas libres (rappel « ingrédients à acheter ») en tête de liste.
        const libres = document.querySelectorAll('.shop-libre li');
        if (libres.length) {
            lines.push('📝 Repas libres (ingrédients à acheter) :');
            libres.forEach(li => lines.push(`- ${li.textContent.trim().replace(/\s+/g, ' ')}`));
            lines.push('');
        }
        document.querySelectorAll('.shop-rayon').forEach(sec => {
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

    // ── Branchements ────────────────────────────────────────────────────
    bindShopItems();
    // 🍽 « recettes liées » : le title ne s'affiche pas au doigt → un tap déplie la
    // liste sous l'ingrédient (sans cocher la case du label).
    document.addEventListener('click', e => {
        const b = e.target.closest('.shop-src');
        if (!b) return;
        e.preventDefault();
        e.stopPropagation();
        b.closest('.shop-i')?.classList.toggle('src-open');
    });
    flushQueue();  // rejoue les changements offline en attente
    $('btn-copy')?.addEventListener('click', copyShop);
    $('btn-share')?.addEventListener('click', partagerListe);
    $('btn-print')?.addEventListener('click', () => window.print());
    $('btn-regen')?.addEventListener('click', () => genShop(true));
    $('btn-shop')?.addEventListener('click', () => genShop());
    // Avertissement recettes non enrichies (posé avant reload par genShop).
    const w = sessionStorage.getItem('shop_warn_' + pid);
    if (w) { sessionStorage.removeItem('shop_warn_' + pid); toast(w, 'error'); }
})();
