// Constructeur de planning (page « / ») : état, grille, groupes de midis,
// période « Du / Au », sérialisation et envoi du formulaire.
// Données serveur lues dans #page-data : {builder, saved}.
//
// Les fonctions pures (sans accès au DOM) sont exposées sur `window.Builder`
// pour être testables : initialState, hydrateBuilder, nbDaysInitial,
// activeDaysFor, midiEditorDay, consolidateGroups, rangeDays, serialize…
(function () {
    'use strict';

    // ══ Fonctions pures ═════════════════════════════════════════════════
    const JOURS_FR = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche'];
    const cap1 = s => s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
    const GROUP_COLORS = ['#e67e22', '#27ae60', '#8e44ad', '#2980b9', '#c0392b'];  // 5 couleurs max
    const DEFAULT_PERS = [2, 2, 2, 2, 2, 4, 4];
    const DEFAULT_GROUPS = [1, 1, 2, 2, 2, 3, 4];
    const BASES = ['Viande', 'Poisson', 'Œuf', 'Légume', 'Féculent', 'Végé'];
    // Repas qui NE sont PAS un plat principal midi/soir (exclus du picker « principal »).
    const NON_REPAS = ['Dessert', 'Boisson', 'Apéro', 'Goûter', 'Petit dej'];

    // « AAAA-MM-JJ » → Date locale, ou null si vide/invalide.
    function parseDate(v) {
        const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v || '');
        if (!m) return null;
        const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
        return isNaN(d.getTime()) ? null : d;
    }
    function isoDate(d) {
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    }
    // Libellé du jour `idx` (1..7) du planning, DÉRIVÉ de la date de début :
    // vrai jour de semaine + date. {court:'Lun', long:'Lundi', date:'13/07'}.
    function dayInfo(start, idx) {
        if (!start) return { court: 'J' + idx, long: 'Jour ' + idx, date: '' };
        const dt = new Date(start);
        dt.setDate(dt.getDate() + (idx - 1));
        const nom = JOURS_FR[(dt.getDay() + 6) % 7];   // JS: 0=dimanche → 0=lundi
        const dd = String(dt.getDate()).padStart(2, '0');
        const mm = String(dt.getMonth() + 1).padStart(2, '0');
        return { court: cap1(nom.slice(0, 3)), long: cap1(nom), date: `${dd}/${mm}` };
    }

    // Bases acceptées comme accompagnement : légume/féculent (briques) + recettes légume.
    function isSideItem(it) {
        const base = it.base || [];
        if (it.nature === 'Ingrédient') return base.includes('Légume') || base.includes('Féculent');
        return base.includes('Légume');  // recette : accompagnement = garniture de légume
    }
    function isMainItem(it) {
        if (it.nature === 'Ingrédient') return true;          // brique = composant de repas
        const repas = it.repas || [];
        if (!repas.length) return true;                        // non classé : on garde
        return repas.some(r => !NON_REPAS.includes(r));        // au moins un type « repas »
    }

    // État : 14 cases indexées "jour:moment".
    function initialState() {
        const state = {};
        for (let d = 1; d <= 7; d++) {
            for (const m of ['midi', 'soir']) {
                state[d + ':' + m] = {
                    jour: d, moment: m,
                    persons: DEFAULT_PERS[d - 1],
                    group: m === 'midi' ? DEFAULT_GROUPS[d - 1] : null,
                    main: null, accompagnements: [],
                };
            }
        }
        return state;
    }

    // Brouillon rouvert : dernier jour porteur d'un repas. Sinon semaine pleine.
    function nbDaysInitial(builder) {
        let maxJour = 0;
        if (Array.isArray(builder)) {
            for (const c of builder) {
                if (c && (parseInt(c.persons) || 0) > 0) maxJour = Math.max(maxJour, parseInt(c.jour) || 0);
            }
        }
        return maxJour > 0 ? maxJour : 7;
    }
    // Remplit `activeDays` avec {1..n} (n borné 1..7).
    function setNbDays(activeDays, n) {
        n = Math.max(1, Math.min(7, n));
        activeDays.clear();
        for (let d = 1; d <= n; d++) activeDays.add(d);
        return activeDays;
    }
    const activeDaysFor = n => setNbDays(new Set(), n);

    // Hydratation depuis un brouillon : on écrase les cases avec les données
    // sauvegardées (persons/group/main/accompagnements). Les noms sont dans le
    // JSON, pas besoin du catalogue.
    function hydrateBuilder(state, cases) {
        if (!Array.isArray(cases)) return state;
        for (const c of cases) {
            if (!c || typeof c !== 'object') continue;
            const key = c.jour + ':' + c.moment;
            const cell = state[key];
            if (!cell) continue;
            const p = parseInt(c.persons);
            cell.persons = Number.isFinite(p) && p >= 0 ? p : cell.persons;
            if (c.moment === 'midi') {
                const g = parseInt(c.group);
                if (Number.isFinite(g)) cell.group = g;
            }
            cell.main = (c.main && c.main.nom)
                ? { notion_id: c.main.notion_id || '', nom: c.main.nom, nature: c.main.nature || 'Recette',
                    libre: !!c.main.libre || !c.main.notion_id }
                : null;
            cell.accompagnements = Array.isArray(c.accompagnements)
                ? c.accompagnements.filter(a => a && a.nom)
                    .map(a => ({ notion_id: a.notion_id || '', nom: a.nom, nature: a.nature || 'Recette',
                                 libre: !!a.libre || !a.notion_id }))
                : [];
        }
        return state;
    }

    // ── Groupes de midis : 1 seul repas par groupe ─────────────────────
    // Le repas d'un groupe est édité sur le PREMIER jour ACTIF du groupe ; les
    // autres midis du groupe le suivent en lecture seule.
    function midiEditorDay(state, activeDays, group) {
        for (let d = 1; d <= 7; d++) {
            const c = state[d + ':midi'];
            if (c.group === group && c.persons > 0 && activeDays.has(d)) return d;
        }
        return null;
    }
    // Rassemble le repas de chaque groupe sur son jour éditeur (migre depuis un
    // suiveur si besoin) et vide les suiveurs → un seul choix par groupe.
    function consolidateGroups(state, activeDays) {
        const byGroup = {};
        for (let d = 1; d <= 7; d++) {
            const c = state[d + ':midi'];
            (byGroup[c.group] = byGroup[c.group] || []).push(c);
        }
        for (const g in byGroup) {
            const all = byGroup[g];                      // triés par jour croissant
            const active = all.filter(c => c.persons > 0 && activeDays.has(c.jour));
            if (!active.length) continue;
            const editor = active[0];
            const src = all.find(c => c.main);           // repas existant (actif ou non)
            if (src && src !== editor) {
                editor.main = src.main;
                editor.accompagnements = src.accompagnements;
            }
            all.forEach(c => { if (c !== editor) { c.main = null; c.accompagnements = []; } });
        }
        return state;
    }

    // Période « Du / Au » : nombre de jours (borné 1..7) et message d'aide.
    // Sans les deux dates, la durée courante est gardée et le message inchangé (null).
    function rangeDays(start, end, current) {
        if (!(start && end)) return { n: current, hint: null };
        const diff = Math.round((end - start) / 86400000);   // jours d'écart
        if (diff < 0) return { n: 1, hint: 'La date de fin doit suivre la date de début — période ramenée à 1 jour.' };
        if (diff > 6) return { n: 7, hint: 'Période limitée à 7 jours maximum.' };
        const n = diff + 1;
        return { n, hint: `Période du planning : ${n} jour${n > 1 ? 's' : ''} (jour 1 = date de début, max 7 jours).` };
    }

    // Grille → liste de cases envoyée au serveur (champ `meals`).
    function serialize(state, activeDays) {
        consolidateGroups(state, activeDays);  // 1 repas par groupe (sur le jour éditeur) avant envoi
        return Object.values(state).map(c => {
            // Jour non sélectionné : émis « absent » (persons 0, sans repas) → il
            // atterrit dans off_meals côté serveur. L'état mémoire n'est pas muté :
            // re-sélectionner le jour restaure le repas saisi.
            const on = activeDays.has(c.jour);
            const o = { jour: c.jour, moment: c.moment, persons: on ? c.persons : 0 };
            if (c.moment === 'midi') o.group = c.group;
            if (on && c.main) o.main = { notion_id: c.main.notion_id, nom: c.main.nom, nature: c.main.nature, libre: !!c.main.libre };
            o.accompagnements = on
                ? c.accompagnements.map(a => ({ notion_id: a.notion_id, nom: a.nom,
                                                nature: a.nature, libre: !!a.libre || !a.notion_id }))
                : [];
            return o;
        });
    }
    // Au moins un repas principal sur une case active avec des convives.
    const hasMainMeal = (state, activeDays) =>
        Object.values(state).some(c => activeDays.has(c.jour) && c.persons > 0 && c.main);

    window.Builder = {
        GROUP_COLORS, BASES, NON_REPAS, parseDate, isoDate, dayInfo, isSideItem, isMainItem,
        initialState, nbDaysInitial, activeDaysFor, hydrateBuilder, midiEditorDay,
        consolidateGroups, rangeDays, serialize, hasMainMeal,
    };

    // ══ Page (DOM) ══════════════════════════════════════════════════════
    const form = document.getElementById('bf');
    if (!form) return;
    const { esc, pageData } = window.Common;
    const DATA = pageData();
    const $ = id => document.getElementById(id);

    // Grille d'un brouillon rouvert (?draft=<id>) : liste de cases sérialisées.
    // Vide pour un nouveau planning.
    const INITIAL_BUILDER = DATA.builder || [];
    const state = hydrateBuilder(initialState(), INITIAL_BUILDER);
    // Période du planning : jours 1..N contigus (N = 1..7), pilotée par le date
    // range « Du / Au ». `activeDays` reste l'ensemble consommé par la grille, la
    // sérialisation et les groupes de midis (jours hors période → « absent »).
    const activeDays = activeDaysFor(nbDaysInitial(INITIAL_BUILDER));

    const startDate = () => parseDate($('week_start').value);
    const info = idx => dayInfo(startDate(), idx);
    // Libellé court « Lun 13/07 » pour les lignes de la grille.
    function dayLabel(idx) { const i = info(idx); return i.date ? `${i.court} ${i.date}` : i.court; }

    // Libellé du repas principal : lien (nouvel onglet) vers la recette, ou simple
    // texte pour une brique (Nature=Ingrédient, pas de fiche recette dédiée).
    function mainLabel(m) {
        // Repas libre : titre saisi à la main, non lié à une recette.
        if (m.libre || !m.notion_id) {
            return `<span title="Repas libre (pense à acheter les ingrédients)">📝 ${esc(m.nom)}</span>`;
        }
        const inner = '🍽️ ' + esc(m.nom);
        if (m.nature === 'Recette' && m.notion_id) {
            return `<a href="/recette/${encodeURIComponent(m.notion_id)}" target="_blank" rel="noopener" class="b-main-link" title="Ouvrir la recette">${inner}</a>`;
        }
        return `<span>${inner}</span>`;
    }
    // Accompagnement libre (saisi à la main, sans recette) : même pictogramme que
    // le repas libre pour rappeler qu'il n'apporte aucun ingrédient en base.
    function sideIcon(a) { return (a.libre || !a.notion_id) ? '📝' : '🥗'; }

    // ── Rendu de la grille ─────────────────────────────────────────────
    function cellHtml(c) {
        const col = c.moment === 'midi' ? GROUP_COLORS[(c.group - 1) % GROUP_COLORS.length] : '';
        const off = c.persons === 0;
        // data-moment-label : entête « ☀️ Midi / 🌙 Soir » affichée DANS la case en
        // mobile (les colonnes disparaissent, cf. media query).
        const momentLabel = c.moment === 'midi' ? '☀️ Midi' : '🌙 Soir';
        let h = `<div class="b-cell${off ? ' off' : ''}" data-moment-label="${momentLabel}" data-key="${c.jour}:${c.moment}"`
              + (c.moment === 'midi' && !off ? ` style="border-color:${col}"` : '') + '>';
        h += '<div class="b-top">';
        h += `<div><span class="b-pers-lbl">👥</span> <input type="number" class="b-pers" min="0" max="20" value="${c.persons}" title="Convives (0 = absent)" aria-label="Convives ${dayLabel(c.jour)} ${c.moment} (0 = absent)"></div>`;
        if (c.moment === 'midi') {
            h += `<button type="button" class="b-group" style="background:${col}" title="Groupe : même couleur = même repas" aria-label="Groupe ${c.group} du midi ${dayLabel(c.jour)} : changer de groupe" aria-haspopup="true">G${c.group}</button>`;
        }
        h += '</div>';
        // Midi suiveur (pas le jour éditeur du groupe) : repas en lecture seule.
        if (c.moment === 'midi' && !off) {
            const ed = midiEditorDay(state, activeDays, c.group);
            if (ed && ed !== c.jour) {
                const src = state[ed + ':midi'];
                if (src.main) {
                    h += `<div class="b-main ro">${mainLabel(src.main)}</div>`;
                    src.accompagnements.forEach(a => { h += `<div class="b-side ro"><span>${sideIcon(a)} ${esc(a.nom)}</span></div>`; });
                    h += `<small class="b-follow-note">↳ même repas que ${info(ed).court}</small>`;
                } else {
                    h += `<small class="b-follow-note">↳ même repas que ${info(ed).court} <em>(à définir)</em></small>`;
                }
                h += '</div>';
                return h;
            }
        }
        if (c.main) {
            h += `<div class="b-main">${mainLabel(c.main)}<button type="button" class="b-x" data-act="clear-main" title="Retirer" aria-label="Retirer ${esc(c.main.nom)}">✕</button></div>`;
            for (let i = 0; i < c.accompagnements.length; i++) {
                const a = c.accompagnements[i];
                h += `<div class="b-side"><span>${sideIcon(a)} ${esc(a.nom)}</span><button type="button" class="b-x" data-act="rm-side" data-i="${i}" title="Retirer" aria-label="Retirer l'accompagnement ${esc(a.nom)}">✕</button></div>`;
            }
            h += `<button type="button" class="b-add" data-act="add-side">+ accompagnement</button>`;
        } else {
            h += `<button type="button" class="b-add" data-act="pick-main">+ choisir un repas</button>`;
        }
        h += '</div>';
        return h;
    }

    // ── Date range « Du / Au » → période contiguë de N jours ───────────
    // Écrit la date de fin = début + (N-1 jours) dans le champ « Au ».
    function syncEndInput() {
        const start = startDate();
        const endEl = $('week_end');
        if (!start) { endEl.value = ''; return; }
        const end = new Date(start);
        end.setDate(end.getDate() + activeDays.size - 1);
        endEl.value = isoDate(end);
        endEl.min = $('week_start').value;
        const maxEnd = new Date(start); maxEnd.setDate(maxEnd.getDate() + 6);
        endEl.max = isoDate(maxEnd);
    }
    // Recalcule la période depuis les deux champs (borné 1..7 jours) puis re-render.
    function applyRange() {
        const { n, hint } = rangeDays(startDate(), parseDate($('week_end').value), activeDays.size);
        const hintEl = $('range-hint');
        if (hint !== null && hintEl) hintEl.textContent = hint;
        setNbDays(activeDays, n);
        renderGrid();   // renderGrid resynchronise le champ « Au » via syncEndInput
    }

    function renderGrid() {
        consolidateGroups(state, activeDays);  // un seul repas par groupe (sur le 1er jour actif)
        syncEndInput();
        const grid = $('grid');
        grid.querySelectorAll('.b-row-el').forEach(e => e.remove());
        for (let d = 1; d <= 7; d++) {
            if (!activeDays.has(d)) continue;   // jour non sélectionné : masqué
            const label = document.createElement('div');
            label.className = 'b-rowlabel b-row-el';
            label.textContent = dayLabel(d);
            grid.appendChild(label);
            for (const m of ['midi', 'soir']) {
                const wrap = document.createElement('div');
                wrap.innerHTML = cellHtml(state[d + ':' + m]);
                const cellEl = wrap.firstChild;
                cellEl.classList.add('b-row-el');  // pour être retiré au prochain rendu
                grid.appendChild(cellEl);
            }
        }
        renderLegend();
        bindCells();
    }

    function renderLegend() {
        const groups = [];
        for (let d = 1; d <= 7; d++) groups.push(state[d + ':midi'].group);
        const uniq = [...new Set(groups)];
        $('group-legend').innerHTML = uniq.map(g => {
            const days = groups.map((v, i) => v === g ? info(i + 1).court : null).filter(Boolean);
            const col = GROUP_COLORS[(g - 1) % GROUP_COLORS.length];
            return `<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:10px;background:${col}22;border:1px solid ${col};">
            <span style="width:8px;height:8px;border-radius:50%;background:${col};display:inline-block;"></span>
            ${days.join(' + ')} ${days.length > 1 ? '= même repas' : ''}</span>`;
        }).join('');
    }

    // ── Interactions des cases ─────────────────────────────────────────
    function bindCells() {
        document.querySelectorAll('.b-cell').forEach(cell => {
            const c = state[cell.dataset.key];
            const pers = cell.querySelector('.b-pers');
            // Saisie : maj légère (pas de re-render → on garde le focus).
            pers.addEventListener('input', e => {
                c.persons = Math.max(0, parseInt(e.target.value) || 0);
                cell.classList.toggle('off', c.persons === 0);
            });
            // Blur : re-render complet (recalcule éditeur/suiveurs des groupes).
            pers.addEventListener('change', () => renderGrid());
            const gb = cell.querySelector('.b-group');
            if (gb) gb.addEventListener('click', e => { e.stopPropagation(); openGroupPicker(gb, c); });
            cell.querySelectorAll('[data-act]').forEach(btn => {
                btn.addEventListener('click', () => {
                    const act = btn.dataset.act;
                    if (act === 'pick-main') window.BuilderPicker.open('main', c, renderGrid);
                    else if (act === 'clear-main') { c.main = null; c.accompagnements = []; renderGrid(); }
                    else if (act === 'add-side') window.BuilderPicker.open('side', c, renderGrid);
                    else if (act === 'rm-side') { c.accompagnements.splice(parseInt(btn.dataset.i), 1); renderGrid(); }
                });
            });
        });
    }

    // ── Sélecteur de groupe (couleur) ──────────────────────────────────
    let groupPicker = null;
    function closeGroupPicker() { if (groupPicker) { groupPicker.remove(); groupPicker = null; } }
    document.addEventListener('click', closeGroupPicker);
    function openGroupPicker(gb, c) {
        closeGroupPicker();
        const pop = document.createElement('div');
        pop.className = 'group-picker';
        GROUP_COLORS.forEach((col, idx) => {
            const sw = document.createElement('button');
            sw.type = 'button';
            sw.className = 'group-sw';
            sw.style.background = col;
            sw.title = 'Groupe ' + (idx + 1);
            sw.setAttribute('aria-label', 'Groupe ' + (idx + 1));
            sw.setAttribute('aria-pressed', c.group === idx + 1 ? 'true' : 'false');
            if (c.group === idx + 1) sw.classList.add('sel');
            sw.addEventListener('click', ev => {
                ev.stopPropagation();
                c.group = idx + 1;
                closeGroupPicker();
                renderGrid();
            });
            pop.appendChild(sw);
        });
        document.body.appendChild(pop);
        const r = gb.getBoundingClientRect();
        pop.style.left = (window.scrollX + r.left) + 'px';
        pop.style.top = (window.scrollY + r.bottom + 4) + 'px';
        groupPicker = pop;
    }

    // Échap : ferme d'abord le sélecteur de groupe, sinon le picker de recettes.
    document.addEventListener('keydown', e => {
        if (e.key !== 'Escape') return;
        if (groupPicker) { closeGroupPicker(); return; }
        const pk = window.BuilderPicker;
        if (pk && pk.isOpen()) { e.preventDefault(); pk.close(); }
    });

    // ── Sérialisation + submit ─────────────────────────────────────────
    // Le bouton cliqué détermine l'action (brouillon vs valider).
    let submitAction = 'valider';
    form.querySelectorAll('button[type="submit"]').forEach(btn => {
        btn.addEventListener('click', () => { submitAction = btn.dataset.action || 'valider'; });
    });
    form.addEventListener('submit', function (e) {
        $('meals-input').value = JSON.stringify(serialize(state, activeDays));
        $('action-input').value = submitAction;
        if (!hasMainMeal(state, activeDays)) {
            e.preventDefault();
            // base.html (écouteur en capture) a déjà passé le bouton en chargement
            // et lancé la barre de progression : on annule les deux.
            form.querySelectorAll('button[type="submit"]').forEach(b => { b.classList.remove('loading'); b.disabled = false; });
            $('nav-progress')?.classList.remove('active');
            toast('Choisis au moins un repas principal (case avec des convives).', 'error');
            return;
        }
        const btn = $(submitAction === 'brouillon' ? 'btn-draft' : 'btn-b');
        btn.disabled = true;
        btn.textContent = submitAction === 'brouillon' ? '⏳ Enregistrement…' : '⏳ Création…';
    });

    // Message « brouillon enregistré » après un retour /?draft=<id>&saved=1.
    if (DATA.saved && window.toast) toast('Brouillon enregistré ✓', 'success');

    // Date de début : garde la durée (N jours), décale la fin, relabelle la grille.
    $('week_start').addEventListener('change', renderGrid);
    // Date de fin : recalcule la durée de la période depuis le range.
    $('week_end').addEventListener('change', applyRange);

    renderGrid();
})();
