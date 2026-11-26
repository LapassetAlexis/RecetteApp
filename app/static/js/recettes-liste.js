// Livre de recettes : recherche, filtres combinés, tri et enrichissement de
// masse. La carte entière ouvre la fiche via le lien étiré du titre (voir
// recettes.css) ; la notation est dans rating.js.
(function () {
    'use strict';
    const grid = document.getElementById('recettes-grid');
    if (!grid) return;
    const search = document.getElementById('recipe-search');
    const noResult = document.getElementById('no-result');
    const toastMsg = (m, t) => { if (window.toast) window.toast(m, t); };

    // ── Recherche + filtres combinés (nature / type / base / état / tag) ─
    // Par défaut : nature = "Recette" → les briques (Ingrédient) sont masquées.
    const active = {nature: 'Recette', type: '', base: '', etat: '', tag: '', rapide: false};
    function applyFilters() {
        const q = search.value.trim().toLowerCase();
        let visible = 0;
        // Recherche multi-termes : chaque mot doit matcher le nom OU les ingrédients.
        const terms = q.split(/\s+/).filter(Boolean);
        grid.querySelectorAll('.recipe-card').forEach(card => {
            const hay = card.dataset.name + ' ' + (card.dataset.ings || '');
            const okName = !terms.length || terms.every(t => hay.includes(t));
            const okNature = !active.nature || (card.dataset.nature || 'Recette') === active.nature;
            const okType = !active.type || (card.dataset.type || '').split('|').includes(active.type);
            const okBase = !active.base || (card.dataset.base || '').split('|').includes(active.base);
            const okEtat = !active.etat || card.dataset.etat === active.etat;
            const okTag = !active.tag || (card.dataset.tags || '').split('|').includes(active.tag.toLowerCase());
            const duree = parseInt(card.dataset.duree || '0', 10);
            const okRapide = !active.rapide || (duree > 0 && duree <= 30);
            const show = okName && okNature && okType && okBase && okEtat && okTag && okRapide;
            card.classList.toggle('hidden', !show);
            if (show) visible++;
        });
        noResult.style.display = visible ? 'none' : 'block';
    }

    document.getElementById('chip-rapide')?.addEventListener('click', function () {
        active.rapide = !active.rapide;
        this.classList.toggle('active', active.rapide);
        this.setAttribute('aria-pressed', String(active.rapide));
        applyFilters();
    });
    // Recherche temporisée (120 ms) : on ne refiltre pas à chaque frappe.
    let searchTimer = null;
    search.addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(applyFilters, 120);
    });
    document.querySelectorAll('.stat-chip[data-filter]').forEach(chip => {
        chip.addEventListener('click', function () {
            const group = this.dataset.filter;  // nature | type | base | etat | tag
            document.querySelectorAll(`.stat-chip[data-filter="${group}"]`).forEach(c => {
                c.classList.remove('active');
                c.setAttribute('aria-pressed', 'false');
            });
            this.classList.add('active');
            this.setAttribute('aria-pressed', 'true');
            active[group] = this.dataset.value;
            applyFilters();
        });
    });
    applyFilters();  // au chargement : masque les briques (nature=Recette par défaut)

    // ── Tri : pas faite depuis longtemps / mieux notées / nom ─────
    // « Pas faite depuis longtemps » : recettes déjà cuisinées, la plus
    // ancienne d'abord ; celles jamais planifiées viennent ensuite.
    const noteOf = card => card.querySelectorAll('.recipe-stars .star:not(.empty)').length;
    const SORTS = {
        '': (a, b) => a.dataset.order - b.dataset.order,
        nom: (a, b) => a.dataset.name.localeCompare(b.dataset.name, 'fr'),
        notees: (a, b) => noteOf(b) - noteOf(a) || a.dataset.name.localeCompare(b.dataset.name, 'fr'),
        oubliees: (a, b) => {
            const la = a.dataset.last, lb = b.dataset.last;
            if (!la !== !lb) return la ? -1 : 1;
            return la.localeCompare(lb) || a.dataset.name.localeCompare(b.dataset.name, 'fr');
        },
    };
    document.getElementById('recipe-sort')?.addEventListener('change', function () {
        const cards = Array.from(grid.querySelectorAll('.recipe-card'));
        cards.sort(SORTS[this.value] || SORTS['']);
        cards.forEach(c => grid.insertBefore(c, noResult));
    });

    // ── Enrichissement de masse (flux SSE, progression en direct) ─────
    // POST lu en flux par fetch (ReadableStream) : l'action écrit dans Notion,
    // elle ne doit pas pouvoir partir d'un simple lien ou d'un préchargement.
    // Hors Common.requestJson : la réponse est un flux d'événements, pas du JSON.
    const btn = document.getElementById('btn-enrich');
    const st = document.getElementById('enrich-status');
    const enrichModal = document.getElementById('enrich-modal');

    function echec(message) {
        st.textContent = '❌ ' + (message || 'Erreur (connexion interrompue)');
        toastMsg("Erreur lors de l'enrichissement", 'error');
    }

    // Un bloc SSE (« event: x\ndata: {...} ») → true quand le flux est terminé.
    function traiterBloc(bloc) {
        let type = 'message', data = '';
        bloc.split('\n').forEach(l => {
            if (l.startsWith('event:')) type = l.slice(6).trim();
            else if (l.startsWith('data:')) data += l.slice(5).trim();
        });
        if (!data) return false;
        const d = JSON.parse(data);
        if (type === 'done') {
            st.textContent = `✅ ${d.enriched}/${d.total} enrichies (${d.skipped} ignorées, ${d.errors} erreurs)`;
            toastMsg(`${d.enriched} recettes enrichies`, 'success');
            return true;
        }
        if (type === 'error') { echec(d.error); return true; }
        st.textContent = d.done === 0 ? `⏳ 0/${d.total}...`
            : `⏳ ${d.done}/${d.total} — ${d.enriched} enrichies, ${d.skipped} ignorées, ${d.errors} err.`;
        return false;
    }

    async function lireFlux(r) {
        const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
        let buf = '';
        for (;;) {
            const {value, done} = await reader.read();
            if (done) return false;
            buf += value.replace(/\r\n/g, '\n');
            let k;
            while ((k = buf.indexOf('\n\n')) >= 0) {
                const bloc = buf.slice(0, k);
                buf = buf.slice(k + 2);
                if (traiterBloc(bloc)) { reader.cancel().catch(() => {}); return true; }
            }
        }
    }

    async function enrichirTout() {
        btn.disabled = true;
        st.textContent = '⏳ Démarrage...';
        try {
            const r = await fetch('/api/enrich-all/stream', {method: 'POST', headers: {Accept: 'text/event-stream'}});
            let path = '';
            try { path = new URL(r.url, location.href).pathname; } catch (e) { path = ''; }
            if (r.status === 401 || (r.redirected && path === '/login')) { window.Common.sessionExpired(); return; }
            if (!r.ok || !r.body || !(r.headers.get('content-type') || '').includes('text/event-stream')) {
                let d = {};
                try { d = await r.json(); } catch (e) { d = {}; }
                echec(d.error);
            } else if (!(await lireFlux(r))) {
                echec();
            }
        } catch (e) {
            echec();
        }
        btn.disabled = false;
    }

    btn?.addEventListener('click', async () => {
        if (!enrichModal || !(await window.Common.confirmModal(enrichModal))) return;
        enrichirTout();
    });
})();
