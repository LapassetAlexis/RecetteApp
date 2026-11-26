// Constructeur : modale de choix d'un repas ou d'un accompagnement
// (recettes / briques du catalogue, filtres Nature et Base, saisie libre).
// API : window.BuilderPicker = {open(mode, cell, onChange), close(), isOpen()}.
// À charger après builder.js (utilise window.Builder).
(function () {
    'use strict';
    const { esc } = window.Common;
    const { BASES, isSideItem, isMainItem } = window.Builder;
    const $ = id => document.getElementById(id);

    let CATALOGUE = [];
    let pkMode = 'main';   // 'main' ou 'side'
    let pkCell = null;
    let pkNature = '';     // '', 'Recette', 'Ingrédient'
    let pkBase = '';       // '' ou une base
    let onChange = null;   // rappel après un choix (re-rendu de la grille)
    let opener = null;     // élément qui a ouvert la modale (focus rendu à la fermeture)

    function buildFilters() {
        const nat = $('pk-nature');
        nat.innerHTML = '';
        [['', 'Tout'], ['Recette', 'Recette'], ['Ingrédient', 'Ingrédient']].forEach(([v, lbl]) => {
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'pk-f' + (pkNature === v ? ' sel' : '');
            b.textContent = lbl;
            b.addEventListener('click', () => { pkNature = v; buildFilters(); renderPkList(); });
            nat.appendChild(b);
        });
        const bs = $('pk-base');
        bs.innerHTML = '';
        [['', 'Toutes bases'], ...BASES.map(b => [b, b])].forEach(([v, lbl]) => {
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'pk-f' + (pkBase === v ? ' sel' : '');
            b.textContent = lbl;
            b.addEventListener('click', () => { pkBase = v; buildFilters(); renderPkList(); });
            bs.appendChild(b);
        });
    }

    function renderPkList() {
        const q = $('pk-search').value.trim().toLowerCase();
        const list = $('pk-list');
        let items = CATALOGUE.slice();
        if (pkMode === 'side') items = items.filter(isSideItem);
        else items = items.filter(isMainItem);   // principal : pas de dessert/boisson/…
        if (pkNature) items = items.filter(it => it.nature === pkNature);
        if (pkBase) items = items.filter(it => (it.base || []).includes(pkBase));
        if (q) items = items.filter(it => it.nom.toLowerCase().includes(q));
        items.sort((a, b) => a.nom.localeCompare(b.nom));
        list.innerHTML = '';
        // Libre : proposer d'utiliser le texte saisi comme titre non lié à une
        // recette — valable pour le plat principal ET l'accompagnement.
        const rawQ = $('pk-search').value.trim();
        if (rawQ) {
            const free = document.createElement('button');
            free.type = 'button';
            free.className = 'btn btn-ghost pk-free';
            free.style.cssText = 'width:100%;text-align:left;margin-bottom:12px;';
            const lbl = pkMode === 'main' ? '📝 Repas libre' : '📝 Accompagnement libre';
            free.innerHTML = `${lbl} : « <strong>${esc(rawQ)}</strong> » <small>(sans recette, ingrédients à acheter à la main)</small>`;
            free.addEventListener('click', () => chooseFree(rawQ));
            list.appendChild(free);
        }
        if (!items.length) {
            if (!list.childElementCount) list.innerHTML = '<div class="pk-empty">Aucun résultat.</div>';
            else list.insertAdjacentHTML('beforeend', '<div class="pk-empty">Aucune recette existante.</div>');
            return;
        }
        // Cards façon page « Recettes » (classes globales réutilisées).
        const grid = document.createElement('div');
        grid.className = 'recettes-grid';
        items.slice(0, 300).forEach(it => {
            const card = document.createElement('div');
            card.className = 'card recipe-card';
            const img = it.image
                ? `<img class="recipe-img" src="${esc(it.image)}" alt="" loading="lazy">`
                : `<div class="recipe-img recipe-img-ph" aria-hidden="true">🍽️</div>`;
            const natChip = it.nature === 'Ingrédient'
                ? `<span class="chip chip-base">🧱 Ingrédient</span>` : '';
            const repas = (it.repas || []).map(t => `<span class="chip chip-repas">${esc(t)}</span>`).join('');
            const base = (it.base || []).map(b => `<span class="chip chip-base">${esc(b)}</span>`).join('');
            card.innerHTML = `${img}<h3 class="recipe-title">${esc(it.nom)}</h3>`
                + `<div class="recipe-meta">${natChip}${repas}${base}</div>`;
            card.addEventListener('click', () => choose(it));
            grid.appendChild(card);
        });
        list.appendChild(grid);
    }

    function open(mode, cell, cb) {
        opener = document.activeElement;
        pkMode = mode; pkCell = cell; pkNature = ''; pkBase = ''; onChange = cb || null;
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
    $('pk-search').addEventListener('input', renderPkList);

    // ── Chargement du catalogue ────────────────────────────────────────
    fetch('/api/catalogue')
        .then(r => r.json())
        .then(data => { CATALOGUE = Array.isArray(data) ? data : []; })
        .catch(() => { CATALOGUE = []; });

    window.BuilderPicker = { open, close, isOpen };
})();
