// Fiche recette : « 📅 Ajouter au planning ».
//
// Modale : choix d'un brouillon (ou « Nouveau brouillon » démarrant à une
// date), du jour et du moment, puis POST /api/recette/{id}/planifier.
// Données serveur (#page-data) : {recette_id, drafts: [{id, label,
// days: [{jour, label}], occupied: {"jour:moment": nom}}]}.
(function () {
    'use strict';

    const modal = document.getElementById('plan-modal');
    const openBtn = document.getElementById('btn-planifier');
    if (!modal || !openBtn || !window.Common || !window.Common.modal) return;

    const {pageData, postJson} = window.Common;
    const data = pageData();
    const drafts = data.drafts || [];
    const draftSel = document.getElementById('plan-draft');
    const daySel = document.getElementById('plan-day');
    const dayGroup = document.getElementById('plan-day-g');
    const dateGroup = document.getElementById('plan-date-g');
    const dateInput = document.getElementById('plan-date');
    const warn = document.getElementById('plan-warn');
    const okBtn = document.getElementById('plan-ok');
    const done = document.getElementById('plan-done');
    const doneText = document.getElementById('plan-done-text');
    const doneLink = document.getElementById('plan-done-link');

    const today = () => {
        const d = new Date();
        const pad = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    };
    const currentDraft = () => drafts.find(d => String(d.id) === draftSel.value);
    const momentSel = document.getElementById('plan-moment');
    const moment = () => momentSel.value || 'midi';

    // Jours du brouillon choisi, ou date libre pour un nouveau brouillon.
    function refreshDays() {
        const d = currentDraft();
        const keep = daySel.value;
        dayGroup.hidden = !d;
        dateGroup.hidden = !!d;
        daySel.textContent = '';
        (d ? d.days : []).forEach(day => {
            const o = document.createElement('option');
            o.value = day.jour;
            o.textContent = day.label;
            daySel.appendChild(o);
        });
        if (keep && daySel.querySelector(`option[value="${keep}"]`)) daySel.value = keep;
        refreshWarn();
    }

    // Avertit si le créneau choisi porte déjà un plat (il sera remplacé).
    function refreshWarn() {
        const d = currentDraft();
        const nom = d && (d.occupied || {})[`${daySel.value}:${moment()}`];
        warn.hidden = !nom;
        warn.textContent = nom ? `Ce créneau contient « ${nom} » : il sera remplacé.` : '';
    }

    // Met à jour (ou ajoute en tête) le brouillon renvoyé par le serveur et le
    // sélectionne : un second ajout voit les créneaux à jour.
    function upsertDraft(d) {
        if (!d) return;
        const i = drafts.findIndex(x => x.id === d.id);
        if (i >= 0) {
            drafts[i] = d;
            const opt = draftSel.querySelector(`option[value="${d.id}"]`);
            if (opt) opt.textContent = d.label;
        } else {
            drafts.unshift(d);
            const o = document.createElement('option');
            o.value = d.id;
            o.textContent = d.label;
            draftSel.insertBefore(o, draftSel.firstChild);
        }
        draftSel.value = String(d.id);
        refreshDays();
    }

    // Ouverture par Common.modal : focus initial, piège Tab, Échap, focus rendu.
    let handle = null;
    function open() {
        if (!dateInput.value) dateInput.value = today();
        done.hidden = true;
        refreshDays();
        handle = window.Common.modal(modal, {
            initialFocus: draftSel,
            onClose: () => { handle = null; },
        });
    }
    const close = () => { if (handle) handle.close(); };

    openBtn.addEventListener('click', open);
    document.getElementById('plan-x')?.addEventListener('click', close);
    document.getElementById('plan-cancel')?.addEventListener('click', close);
    modal.querySelector('.modal-bg')?.addEventListener('click', close);
    draftSel.addEventListener('change', refreshDays);
    daySel.addEventListener('change', refreshWarn);
    momentSel.addEventListener('change', refreshWarn);

    document.getElementById('plan-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const d = currentDraft();
        const body = d
            ? {planning_id: d.id, jour: parseInt(daySel.value, 10), moment: moment()}
            : {planning_id: 'new', date: dateInput.value || today(), moment: moment()};
        const label = okBtn.textContent;
        okBtn.disabled = true;
        okBtn.textContent = '⏳ Ajout...';
        try {
            const r = await postJson(`/api/recette/${encodeURIComponent(data.recette_id)}/planifier`, body);
            if (r.success) {
                upsertDraft(r.draft);
                doneText.textContent = `Ajoutée à « ${(r.draft || {}).label || 'brouillon'} ».`;
                doneLink.href = r.url || `/?draft=${r.planning_id}`;
                done.hidden = false;
                window.toast?.('Ajoutée au brouillon', 'success');
                doneLink.focus();
            } else {
                window.toast?.(r.error || 'Erreur', 'error');
            }
        } catch (err) {
            window.toast?.('Erreur réseau', 'error');
        }
        okBtn.disabled = false;
        okBtn.textContent = label;
    });
})();
