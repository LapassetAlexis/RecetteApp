// Constructeur : modale de choix d'un repas ou d'un accompagnement
// (recettes / briques du catalogue, filtres Nature, Base et filtres rapides,
// saisie libre). Filtres et tri : fonctions pures de window.Builder.
// API : window.BuilderPicker = {open(mode, cell, onChange), close(), isOpen()}.
// À charger après builder.js (utilise window.Builder).
(function () {
    'use strict';
    const { esc } = window.Common;
    const { BASES, PICKER_PAGE, EXTRA_FILTERS, seasonOf, filterCatalogue, sortCatalogue,
            recencyLabel, noteValue } = window.Builder;
    const $ = id => document.getElementById(id);

    let CATALOGUE = [];
    let catStatus = 'loading';   // 'loading' | 'ready' | 'error'
    let catError = '';
    let pkMode = 'main';   // 'main' ou 'side'
    let pkCell = null;
    let pkNature = '';     // '', 'Recette', 'Ingrédient'
    let pkBase = '';       // '' ou une base
    let pkExtra = new Set();   // ids d'EXTRA_FILTERS actifs
    let pkShown = PICKER_PAGE; // nombre de cartes affichées (« Afficher plus »)
    let onChange = null;   // rappel après un choix (re-rendu de la grille)
    let opener = null;     // élément qui a ouvert la modale (focus rendu à la fermeture)

    const SEASON_ICONS = { Printemps: '🌱', 'Été': '☀️', Automne: '🍂', Hiver: '❄️' };

    function chip(label, selected, onClick) {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'pk-f' + (selected ? ' sel' : '');
        b.setAttribute('aria-pressed', selected ? 'true' : 'false');
        b.textContent = label;
        b.addEventListener('click', onClick);
        return b;
    }
    const refilter = () => { pkShown = PICKER_PAGE; buildFilters(); renderPkList(); };

    function buildFilters() {
        const nat = $('pk-nature');
        nat.innerHTML = '';
        [['', 'Tout'], ['Recette', 'Recette'], ['Ingrédient', 'Ingrédient']].forEach(([v, lbl]) => {
            nat.appendChild(chip(lbl, pkNature === v, () => { pkNature = v; refilter(); }));
        });
        const bs = $('pk-base');
        bs.innerHTML = '';
        [['', 'Toutes bases'], ...BASES.map(b => [b, b])].forEach(([v, lbl]) => {
            bs.appendChild(chip(lbl, pkBase === v, () => { pkBase = v; refilter(); }));
        });
        const ex = $('pk-extra');
        ex.innerHTML = '';
        const season = seasonOf(new Date());
        EXTRA_FILTERS.forEach(f => {
            const lbl = f.id === 'saison' ? `${SEASON_ICONS[season] || ''} ${season}`.trim() : f.label;
            ex.appendChild(chip(lbl, pkExtra.has(f.id), () => {
                if (pkExtra.has(f.id)) pkExtra.delete(f.id); else pkExtra.add(f.id);
                refilter();
            }));
        });
    }

    function cardHtml(it, today) {
        const img = it.image
            ? `<img class="recipe-img" src="${esc(it.image)}" alt="" loading="lazy">`
            : `<span class="recipe-img recipe-img-ph" aria-hidden="true">🍽️</span>`;
        const natChip = it.nature === 'Ingrédient'
            ? `<span class="chip chip-base">🧱 Ingrédient</span>` : '';
        const recent = recencyLabel(it.usage && it.usage.last, today);
        const recentChip = recent ? `<span class="chip pk-recent">🕒 ${esc(recent)}</span>` : '';
        const stars = noteValue(it.note);
        const noteChip = stars ? `<span class="chip chip-note" aria-label="Note ${stars} sur 5">${'⭐'.repeat(stars)}</span>` : '';
        const tryChip = it.etat === 'À essayer' ? `<span class="chip chip-etat">🧪 À essayer</span>` : '';
        const duree = it.duree > 0 ? `<span class="chip chip-duree">⏱ ${it.duree} min</span>` : '';
        const repas = (it.repas || []).map(t => `<span class="chip chip-repas">${esc(t)}</span>`).join('');
        const base = (it.base || []).map(b => `<span class="chip chip-base">${esc(b)}</span>`).join('');
        return `${img}<span class="recipe-title">${esc(it.nom)}</span>`
            + `<span class="recipe-meta">${recentChip}${natChip}${tryChip}${noteChip}${duree}${repas}${base}</span>`;
    }

    function renderPkList() {
        const list = $('pk-list');
        const rawQ = $('pk-search').value.trim();
        list.innerHTML = '';
        // Libre : proposer d'utiliser le texte saisi comme titre non lié à une
        // recette — valable pour le plat principal ET l'accompagnement (même
        // pendant le chargement ou si le catalogue est indisponible).
        if (rawQ) {
            const free = document.createElement('button');
            free.type = 'button';
            free.className = 'btn btn-ghost pk-free';
            const lbl = pkMode === 'main' ? '📝 Repas libre' : '📝 Accompagnement libre';
            free.innerHTML = `${lbl} : « <strong>${esc(rawQ)}</strong> » <small>(sans recette, ingrédients à acheter à la main)</small>`;
            free.addEventListener('click', () => chooseFree(rawQ));
            list.appendChild(free);
        }
        if (catStatus === 'loading') {
            list.insertAdjacentHTML('beforeend', '<div class="pk-empty" role="status">⏳ Chargement du catalogue…</div>');
            return;
        }
        if (catStatus === 'error') {
            const box = document.createElement('div');
            box.className = 'pk-empty pk-error';
            box.setAttribute('role', 'alert');
            box.innerHTML = `<p>⚠️ ${esc(catError || 'Catalogue indisponible.')}</p>`;
            const retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'btn btn-secondary';
            retry.textContent = '🔄 Réessayer';
            retry.addEventListener('click', () => { loadCatalogue(); });
            box.appendChild(retry);
            list.appendChild(box);
            return;
        }
        const today = new Date();
        const items = sortCatalogue(filterCatalogue(CATALOGUE, {
            mode: pkMode, nature: pkNature, base: pkBase, q: rawQ, extra: pkExtra, today,
        }));
        if (!items.length) {
            const msg = list.childElementCount ? 'Aucune recette existante.' : 'Aucun résultat.';
            list.insertAdjacentHTML('beforeend', `<div class="pk-empty">${msg}</div>`);
            return;
        }
        // Cartes façon page « Recettes » (classes globales réutilisées), en <button>.
        const grid = document.createElement('div');
        grid.className = 'recettes-grid';
        items.slice(0, pkShown).forEach(it => {
            const card = document.createElement('button');
            card.type = 'button';
            card.className = 'card recipe-card pk-card';
            card.innerHTML = cardHtml(it, today);
            card.addEventListener('click', () => choose(it));
            grid.appendChild(card);
        });
        list.appendChild(grid);
        if (items.length > pkShown) {
            const more = document.createElement('button');
            more.type = 'button';
            more.className = 'btn btn-secondary pk-more';
            more.textContent = `Afficher plus (${items.length - pkShown} restantes)`;
            more.addEventListener('click', () => {
                const firstNew = pkShown;
                pkShown += PICKER_PAGE;
                renderPkList();
                // Focus sur la première carte ajoutée (clavier / lecteur d'écran).
                const cards = $('pk-list').querySelectorAll('.pk-card');
                if (cards[firstNew]) cards[firstNew].focus();
            });
            list.appendChild(more);
        }
    }

    function open(mode, cell, cb) {
        opener = document.activeElement;
        pkMode = mode; pkCell = cell; pkNature = ''; pkBase = ''; pkExtra = new Set();
        pkShown = PICKER_PAGE; onChange = cb || null;
        $('pk-title').textContent = mode === 'main' ? 'Choisir le repas principal' : 'Choisir un accompagnement';
        $('pk-search').value = '';
        buildFilters();
        renderPkList();
        $('pk').classList.add('open');
        $('pk-search').focus();
    }

    function close() {
        $('pk').classList.remove('open');
        pkCell = null;
        // Après un choix, la grille est re-rendue : l'ancien bouton n'existe plus.
        if (opener && document.contains(opener)) opener.focus();
        opener = null;
    }

    const isOpen = () => $('pk').classList.contains('open');

    // Applique l'élément choisi à la case puis ferme et re-rend la grille.
    function apply(item, isDup) {
        if (!pkCell) return;
        if (pkMode === 'main') pkCell.main = item;
        else if (!pkCell.accompagnements.some(isDup)) pkCell.accompagnements.push(item);
        const cb = onChange;
        close();
        if (cb) cb();
    }
    function choose(it) {
        apply({ notion_id: it.id, nom: it.nom, nature: it.nature },
              a => a.notion_id === it.id && a.nom === it.nom);
    }
    // Libre : titre saisi à la main, non lié à une recette Notion (plat ou
    // accompagnement). Aucun ingrédient en base → rappel dans la liste de courses.
    function chooseFree(nom) {
        apply({ notion_id: '', nom: nom, nature: 'Libre', libre: true }, a => a.nom === nom);
    }

    $('pk-close').addEventListener('click', close);
    $('pk').addEventListener('click', e => { if (e.target.id === 'pk') close(); });
    // Recherche temporisée (120 ms) : pas de re-rendu à chaque frappe.
    let searchTimer = null;
    $('pk-search').addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => { pkShown = PICKER_PAGE; renderPkList(); }, 120);
    });

    // ── Chargement du catalogue ────────────────────────────────────────
    // 502 {error} si Notion échoue : message + « Réessayer » dans le picker.
    function loadCatalogue() {
        catStatus = 'loading';
        if (isOpen()) renderPkList();
        return fetch('/api/catalogue')
            .then(r => r.json().catch(() => null).then(data => {
                if (!r.ok || !Array.isArray(data)) {
                    throw new Error((data && data.error) || 'Catalogue indisponible.');
                }
                CATALOGUE = data;
                catStatus = 'ready';
            }))
            .catch(err => {
                CATALOGUE = [];
                catStatus = 'error';
                catError = (err && err.message && err.message !== 'Failed to fetch')
                    ? err.message : 'Impossible de charger le catalogue (réseau).';
            })
            .then(() => { if (isOpen()) renderPkList(); });
    }
    loadCatalogue();

    window.BuilderPicker = { open, close, isOpen };
})();
