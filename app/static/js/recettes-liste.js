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

    // ── Enrichissement de masse (SSE, progression en direct) ─────
    // Flux d'événements, hors Common.requestJson (pas une réponse JSON).
    const btn = document.getElementById('btn-enrich');
    const st = document.getElementById('enrich-status');
    btn?.addEventListener('click', () => {
        if (!confirm('Enrichir toutes les recettes ? Cela lance des appels au LLM et écrit dans Notion pour chaque recette sans ingrédients.')) return;
        btn.disabled = true;
        st.textContent = '⏳ Démarrage...';
        const es = new EventSource('/api/enrich-all/stream');
        es.onmessage = (ev) => {
            const d = JSON.parse(ev.data);
            if (d.done === 0) { st.textContent = `⏳ 0/${d.total}...`; return; }
            st.textContent = `⏳ ${d.done}/${d.total} — ${d.enriched} enrichies, ${d.skipped} ignorées, ${d.errors} err.`;
        };
        es.addEventListener('done', (ev) => {
            const d = JSON.parse(ev.data);
            st.textContent = `✅ ${d.enriched}/${d.total} enrichies (${d.skipped} ignorées, ${d.errors} erreurs)`;
            toastMsg(`${d.enriched} recettes enrichies`, 'success');
            es.close(); btn.disabled = false;
        });
        es.addEventListener('error', () => {
            st.textContent = '❌ Erreur (connexion interrompue)';
            toastMsg("Erreur lors de l'enrichissement", 'error');
            es.close(); btn.disabled = false;
        });
    });
})();
