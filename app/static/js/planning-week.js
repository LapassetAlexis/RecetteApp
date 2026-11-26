// Page planning : semaine (validation, sélection, modale « Changer »,
// accompagnements, repas libre, brique, enrichissement, copie du menu).
// Données serveur lues dans #page-data : {pid, day_labels, courses_checked}.
(function () {
    'use strict';
    const {copyText, postJson, pageData} = window.Common;
    const DATA = pageData();
    const pid = DATA.pid;
    const $ = id => document.getElementById(id);

    // Libellés des jours 1..7 dérivés de la date du jour 1 (week_start), fournis
    // par le serveur : [{jour, court, long, date}]. Indexé par (jour-1).
    const DAY_LABELS = DATA.day_labels || [];
    const dayShort = i => (DAY_LABELS[i - 1] || {}).court || ('J' + i);
    const dayLong = i => (DAY_LABELS[i - 1] || {}).long || ('Jour ' + i);

    // ── Validation du brouillon ─────────────────────────────────────────
    async function validerSemaine() {
        const b = $('btn-valider');
        if (b) { b.disabled = true; b.textContent = '⏳ Validation...'; }
        try {
            const d = await postJson(`/planning/${pid}/valider`);
            if (d.success) { toast('Semaine validée ✓', 'success'); setTimeout(() => location.reload(), 700); }
            else { toast(d.error || 'Erreur', 'error'); if (b) { b.disabled = false; b.textContent = '✅ Valider la semaine'; } }
        } catch (e) { toast('Erreur réseau', 'error'); if (b) { b.disabled = false; b.textContent = '✅ Valider la semaine'; } }
    }

    // ── Sélection des repas à changer (une case = 1+ jours fusionnés) ──
    let selection = [];
    const parseJours = el => (el.dataset.jours || '').split(',').filter(Boolean).map(Number);
    const slotLabel = s => (s.jours.length > 1 ? dayShort(s.jours[0]) + '–' + dayShort(s.jours[s.jours.length - 1]) : dayShort(s.jours[0])) + ' ' + s.m;

    function updateBulkBar() {
        selection = [];
        document.querySelectorAll('.ck:checked').forEach(c => selection.push({jours: parseJours(c), m: c.dataset.moment}));
        const b = $('bulk');
        if (selection.length) { b.style.display = 'flex'; $('bulk-nb').textContent = selection.length + ' sélectionné' + (selection.length > 1 ? 's' : ''); }
        else b.style.display = 'none';
    }

    // Ouvre la modale en mode « changer le repas » pour la sélection courante.
    function openChange(ctx) {
        sideMode = false; document.querySelector('#modal h3').textContent = '↻ Changer';
        $('modal-ctx').textContent = ctx;
        openModal();
        $('alt-list').innerHTML = '<p class="empty">⏳</p>';
        loadAlternatives();
    }

    // ── Accompagnements (multiples) : ajouter / changer / retirer ──────
    // sideSlot={jours:[...],m} ; sideReplace = nom de l'accompagnement à remplacer
    // (mode ↻) ou null (mode ➕ ajout).
    let sideMode = false, sideSlot = null, sideReplace = null;
    function openSidePicker(slot, replace) {
        sideMode = true; sideSlot = slot; sideReplace = replace || null;
        $('modal-ctx').textContent = slotLabel(slot) + ' — accompagnement';
        document.querySelector('#modal h3').textContent = replace ? '🥦 Changer l\'accompagnement' : '🥦 Ajouter un accompagnement';
        openModal();
        $('alt-list').innerHTML = '<p class="empty">⏳</p>';
        loadAlternatives();
    }
    // Édition groupée : UNE requête pour tous les créneaux (jours fusionnés,
    // sélection multiple) ; le serveur applique tout ou rien.
    // sel = [{jours:[...], m}] ; extra = champs en plus (ex. remplace).
    async function batchMeals(sel, action, nom, extra) {
        const slots = [];
        sel.forEach(s => s.jours.forEach(j => slots.push({jour: j, moment: s.m})));
        try {
            const d = await postJson(`/api/planning/${pid}/meals`, Object.assign({slots, action, nom}, extra || {}));
            if (d.success) return true;
            toast(d.error || 'Erreur lors de la modification', 'error');
        } catch (e) { toast('Erreur réseau', 'error'); }
        return false;
    }
    // Depuis le picker : ➕ append ; ↻ remplace (retire l'ancien puis ajoute).
    async function setSide(slot, nom) {
        const extra = sideReplace ? {remplace: sideReplace} : null;
        if (await batchMeals([slot], 'add_side', nom, extra)) window.location.reload();
    }
    async function removeSide(slot, nom) {
        if (await batchMeals([slot], 'remove_side', nom)) window.location.reload();
    }

    // ── Alternatives (modale) ──────────────────────────────────────────
    async function loadAlternatives() {
        resetFree();
        const r = await fetch(`/api/alternatives/${pid}`); const d = await r.json();
        let alts = d.alternatives || [];
        // Accompagnement : recettes ET briques dont la Base contient Légume.
        if (sideMode) alts = alts.filter(a => (a.base || []).includes('Légume'));
        const el = $('alt-list');
        if (!alts.length) { el.innerHTML = '<p class="empty">Aucune alternative.</p>'; return; }
        // Construction DOM (textContent) : un nom contenant « " < > » reste intact
        // et le bouton « Choisir » envoie le nom exact (pas d'injection HTML).
        el.replaceChildren(...alts.map(a => {
            const row = document.createElement('div');
            row.className = 'alt';
            row.dataset.n = String(a.nom).toLowerCase();
            const name = document.createElement('span');
            name.textContent=a.nom;
            row.appendChild(name);
            let chipTxt = '';
            if (a.nature === 'Ingrédient') chipTxt = '🧱 ' + (((a.base || []).join('/')) || 'brique');
            else if ((a.repas || []).length) chipTxt = a.repas.join('/');
            if (chipTxt) {
                const chip = document.createElement('span');
                chip.className = 'chip chip-r';
                chip.textContent = chipTxt;
                row.appendChild(chip);
            }
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-sm btn-primary selt';
            btn.textContent = 'Choisir';
            btn.setAttribute('aria-label', 'Choisir ' + a.nom);
            btn.addEventListener('click', () => { if (sideMode) { setSide(sideSlot, a.nom); } else { replaceMeals(a.nom); } });
            row.appendChild(btn);
            return row;
        }));
        $('flt').oninput = function () { const q = this.value.toLowerCase(); document.querySelectorAll('.alt').forEach(e => e.style.display = e.dataset.n.includes(q) ? 'flex' : 'none'); };
    }
    async function replaceMeals(n) {
        $('alt-list').innerHTML = '<p class="empty">⏳ Mise à jour...</p>';
        if (await batchMeals(selection, 'set_main', n)) window.location.reload();
        else loadAlternatives();
    }

    // ── Repas libre : crée une recette minimale et la place dans le(s) créneau(x) ──
    function resetFree() {
        // Libre : plat (crée une recette minimale) OU accompagnement (simple titre
        // sans recette, rappelé dans la liste de courses).
        $('free-meal').style.display = 'block';
        $('free-toggle').textContent = sideMode ? '➕ Accompagnement libre' : '➕ Repas libre';
        $('free-form').style.display = 'none';
        $('free-nom').value = '';
        $('free-nom').placeholder = sideMode
            ? "Nom de l'accompagnement (ex. Salade verte)"
            : 'Nom du repas (ex. Steak + haricots verts)';
        // Pas d'ingrédients pour un accompagnement libre : rien n'est créé en base.
        $('free-ings').style.display = sideMode ? 'none' : 'block';
        $('free-go').textContent = sideMode ? 'Ajouter' : 'Créer et placer';
        $('free-ings').value = '';
        // Brique : disponible dans les deux modes (plat ou accompagnement).
        $('brique-box').style.display = 'block';
        $('brique-form').style.display = 'none';
        $('brique-nom').value = '';
        $('brique-qte').value = '';
        $('brique-unite').value = '';
        // En accompagnement, la Base pertinente est « Légume » (pré-cochée).
        document.querySelectorAll('.brique-base').forEach(cb => { cb.checked = (sideMode && cb.value === 'Légume'); });
    }
    function toggleForm(formId, focusId) {
        const f = $(formId);
        const open = f.style.display !== 'none';
        f.style.display = open ? 'none' : 'block';
        if (!open) $(focusId).focus();
    }
    async function createFreeMeal() {
        const nom = $('free-nom').value.trim();
        const ingredients = $('free-ings').value;
        if (!nom) { toast(sideMode ? "Donne un nom à l'accompagnement" : 'Donne un nom au repas', 'error'); return; }
        // Accompagnement libre : simple ajout (aucune recette Notion créée).
        if (sideMode) {
            if (!sideSlot) { toast('Aucun créneau sélectionné', 'error'); return; }
            this.disabled = true; this.textContent = '⏳ Ajout...';
            await setSide(sideSlot, nom);   // recharge la page si l'ajout a réussi
            this.disabled = false; this.textContent = 'Ajouter';
            return;
        }
        if (!selection.length) { toast('Aucun créneau sélectionné', 'error'); return; }
        this.disabled = true; this.textContent = '⏳ Création...';
        let ok = true;
        for (const s of selection) for (const j of s.jours) {
            const d = await postJson(`/api/free-meal/${pid}`, {jour: j, moment: s.m, nom, ingredients});
            if (!d.success) { ok = false; toast(d.error || 'Erreur repas libre', 'error'); break; }
        }
        if (ok) { toast('Repas libre créé ✓', 'success'); window.location.reload(); }
        else { this.disabled = false; this.textContent = 'Créer et placer'; }
    }

    // ── Brique : crée un composant simple (Nature=Ingrédient) et le place ──
    async function createBrique() {
        const nom = $('brique-nom').value.trim();
        const base = [...document.querySelectorAll('.brique-base:checked')].map(cb => cb.value);
        const quantite = $('brique-qte').value.trim();
        const unite = $('brique-unite').value.trim();
        if (!nom) { toast('Donne un nom à la brique', 'error'); return; }
        const slots = sideMode ? [sideSlot] : selection;
        if (!slots.length) { toast('Aucun créneau sélectionné', 'error'); return; }
        const slot = sideMode ? 'accompagnement' : 'plat';
        this.disabled = true; this.textContent = '⏳ Création...';
        let ok = true;
        for (const s of slots) for (const j of s.jours) {
            const d = await postJson(`/api/brique/${pid}`, {jour: j, moment: s.m, slot, nom, base, quantite, unite});
            if (!d.success) { ok = false; toast(d.error || 'Erreur brique', 'error'); break; }
        }
        if (ok) { toast('Brique créée ✓', 'success'); window.location.reload(); }
        else { this.disabled = false; this.textContent = 'Créer et placer'; }
    }

    // ── Enrichissement des recettes de la semaine ───────────────────────
    async function enrichWeek() {
        const b = $('btn-enrich-week');
        const old = b ? b.textContent : '';
        if (b) { b.disabled = true; b.textContent = '⏳ Enrichissement...'; }
        toast('Enrichissement des recettes de la semaine…', 'info');
        try {
            const d = await postJson(`/api/enrich-week/${pid}`);
            if (d.success) {
                toast(`✅ ${d.enriched} enrichie(s), ${d.skipped} ignorée(s), ${d.errors} erreur(s)`, 'success');
                setTimeout(() => location.reload(), 900);
            } else {
                toast('Erreur : ' + (d.error || '?'), 'error');
                if (b) { b.disabled = false; b.textContent = old; }
            }
        } catch (e) {
            toast('Erreur réseau', 'error');
            if (b) { b.disabled = false; b.textContent = old; }
        }
    }

    // ── Copie du menu de la semaine ─────────────────────────────────────
    function copyMenu() {
        const days = {};  // jour -> {midi, soir}
        document.querySelectorAll('.meal').forEach(cell => {
            const moment = cell.dataset.moment;
            const jours = (cell.dataset.jours || '').split(',').filter(Boolean).map(Number);
            const nameEl = cell.querySelector('.meal-name');
            let txt = nameEl ? nameEl.textContent.trim() : '';
            cell.querySelectorAll('.meal-side .side-txt').forEach(side => {
                txt += ' ' + side.textContent.trim().replace(/\s+/g, ' ');
            });
            jours.forEach(j => { (days[j] = days[j] || {})[moment] = txt.trim(); });
        });
        const lines = ['🍽️ Menu de la semaine', ''];
        for (let j = 1; j <= 7; j++) {
            if (!days[j]) continue;
            lines.push(`${dayLong(j)} :`);
            if (days[j].midi) lines.push(`  ☀️ ${days[j].midi}`);
            if (days[j].soir) lines.push(`  🌙 ${days[j].soir}`);
        }
        copyText(lines.join('\n').trim()).then(ok => toast(ok ? 'Menu copié ✓' : 'Copie impossible', ok ? 'success' : 'error'));
    }

    // ── Modale : ouverture / fermeture accessibles ──────────────────────
    // Mémorise l'élément déclencheur pour lui rendre le focus à la fermeture.
    let modalOpener = null;
    function openModal() {
        const m = $('modal');
        if (m.style.display !== 'flex') modalOpener = document.activeElement;
        m.style.display = 'flex';
        const f = $('flt');
        f.value = '';
        // Pointeur précis (desktop) : focus sur le filtre. Tactile : sur « Fermer »,
        // pour ne pas ouvrir le clavier virtuel d'office.
        const fine = window.matchMedia && window.matchMedia('(pointer: fine)').matches;
        (fine ? f : document.querySelector('#modal .modal-x')).focus();
    }
    function closeModal() {
        $('modal').style.display = 'none';
        sideMode = false; sideSlot = null;
        if (modalOpener && document.contains(modalOpener)) modalOpener.focus();
        modalOpener = null;
    }

    // ── Branchements ────────────────────────────────────────────────────
    $('btn-valider')?.addEventListener('click', validerSemaine);
    $('btn-copy-menu').addEventListener('click', copyMenu);
    $('btn-enrich-week').addEventListener('click', enrichWeek);
    document.querySelectorAll('.ck').forEach(c => c.addEventListener('change', updateBulkBar));
    $('bulk-clr').onclick = () => { document.querySelectorAll('.ck:checked').forEach(c => c.checked = false); updateBulkBar(); };
    $('bulk-go').onclick = () => {
        if (!selection.length) return;
        openChange(selection.map(slotLabel).join(', '));
    };
    document.querySelectorAll('.meal-ch').forEach(b => b.onclick = function () {
        selection = [{jours: parseJours(this), m: this.dataset.moment}];
        openChange(slotLabel(selection[0]));
    });
    document.querySelectorAll('.side-add').forEach(b => b.onclick = function () {
        const s = this.closest('.meal-side'); openSidePicker({jours: parseJours(s), m: s.dataset.moment});
    });
    document.querySelectorAll('.side-ch').forEach(b => b.onclick = function () {
        const s = this.closest('.meal-side'); openSidePicker({jours: parseJours(s), m: s.dataset.moment}, this.dataset.nom);
    });
    document.querySelectorAll('.side-rm').forEach(b => b.onclick = function () {
        const s = this.closest('.meal-side'); removeSide({jours: parseJours(s), m: s.dataset.moment}, this.dataset.nom);
    });
    $('free-toggle').onclick = () => toggleForm('free-form', 'free-nom');
    $('free-go').onclick = createFreeMeal;
    $('brique-toggle').onclick = () => toggleForm('brique-form', 'brique-nom');
    $('brique-go').onclick = createBrique;
    $('modal-close').addEventListener('click', closeModal);
    $('modal-cancel').addEventListener('click', closeModal);
    document.querySelector('.modal-bg')?.addEventListener('click', closeModal);
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && $('modal').style.display === 'flex') { e.preventDefault(); closeModal(); }
    });
})();
