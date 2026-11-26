// Historique des plannings : aperçu d'une semaine (survol et bouton 👁),
// « Refaire » une semaine validée, suppression confirmée par modale.
// Dépend de common.js (Common.esc, requestJson, postJson, modal).
(function () {
    'use strict';
    if (!window.Common) return;
    const {esc, requestJson, postJson} = window.Common;
    const toastMsg = (m, t) => { if (window.toast) window.toast(m, t); };

    // ── Aperçu de la semaine ────────────────────────────────────────────
    const cache = {};
    async function apercu(id) {
        if (!cache[id]) {
            const d = await requestJson(`/api/planning/${encodeURIComponent(id)}/apercu`);
            if (d && d.error) throw new Error(d.error);
            cache[id] = d;
        }
        return cache[id];
    }
    function pvRender(data, titre) {
        if (!data || !data.jours || !data.jours.length) return '<div class="pv-empty">Semaine vide.</div>';
        let h = titre ? '<h4>🍽️ Aperçu</h4>' : '';
        data.jours.forEach(j => {
            const lines = [];
            if (j.midi) lines.push('☀️ ' + j.midi);
            if (j.soir) lines.push('🌙 ' + j.soir);
            h += `<div class="pv-day"><span class="pv-j">${esc(j.jour)}</span><span class="pv-m">${lines.map(esc).join('<br>')}</span></div>`;
        });
        return h;
    }

    // Survol (souris uniquement) : bulle flottante à côté de la carte.
    let pvEl = null;
    function pvHide() { if (pvEl) { pvEl.remove(); pvEl = null; } }
    function pvShow(html, rect) {
        pvHide();
        const el = document.createElement('div');
        el.className = 'hist-preview';
        el.innerHTML = html;
        document.body.appendChild(el);
        let left = rect.right + 10;
        const w = el.offsetWidth;
        if (left + w > window.innerWidth - 8) left = Math.max(8, rect.left - w - 10);
        el.style.left = left + 'px';
        el.style.top = Math.max(8, Math.min(rect.top, window.innerHeight - el.offsetHeight - 8)) + 'px';
        pvEl = el;
    }
    if (window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
        document.querySelectorAll('.hist-item[data-preview]').forEach(card => {
            card.addEventListener('mouseenter', async () => {
                // Aperçu déjà déplié dans la carte : pas de bulle en double.
                const box = card.querySelector('.hist-pv');
                if (box && !box.hidden) return;
                try {
                    const d = await apercu(card.dataset.preview);
                    if (card.matches(':hover')) pvShow(pvRender(d, true), card.getBoundingClientRect());
                } catch (e) { /* silencieux */ }
            });
            card.addEventListener('mouseleave', pvHide);
        });
    }

    // Bouton 👁 : déplie / replie l'aperçu dans la carte (tactile, clavier).
    document.querySelectorAll('.hist-pv-btn').forEach(b => b.addEventListener('click', async function () {
        const box = document.getElementById('pv-' + this.dataset.id);
        const open = this.getAttribute('aria-expanded') === 'true';
        pvHide();
        if (open) {
            box.hidden = true;
            this.setAttribute('aria-expanded', 'false');
            return;
        }
        box.hidden = false;
        box.innerHTML = '<div class="pv-empty">⏳</div>';
        this.setAttribute('aria-expanded', 'true');
        try { box.innerHTML = pvRender(await apercu(this.dataset.id), false); }
        catch (e) { box.innerHTML = '<div class="pv-empty">Aperçu indisponible.</div>'; }
    }));

    // ── Refaire une semaine validée ─────────────────────────────────────
    document.querySelectorAll('.hist-dup').forEach(b => b.addEventListener('click', async function () {
        const label = this.textContent;
        this.disabled = true; this.textContent = '⏳';
        try {
            const d = await postJson(`/planning/${encodeURIComponent(this.dataset.id)}/dupliquer`);
            if (d.success) {
                if (window.navStart) window.navStart();
                window.location.href = `/planning/${d.planning_id}`;
                return;
            }
            toastMsg(d.error || 'Erreur', 'error');
        } catch (e) {
            toastMsg('Erreur réseau', 'error');
        }
        this.disabled = false; this.textContent = label;
    }));

    // ── Suppression (brouillon ou semaine validée), confirmée par modale ──
    const modalEl = document.getElementById('del-modal');
    const okBtn = document.getElementById('del-ok');
    let pending = null, handle = null;

    function openDel(btn) {
        pending = btn;
        const quoi = btn.dataset.kind === 'semaine' ? 'la semaine validée' : 'le brouillon';
        document.getElementById('del-msg').textContent =
            `Supprimer ${quoi} « ${btn.dataset.label} » ? Cette action est définitive.`;
        handle = window.Common.modal(modalEl, {
            initialFocus: '#del-cancel',
            onClose: () => { pending = null; handle = null; },
        });
    }
    const closeDel = () => { if (handle) handle.close(); };

    async function confirmDel() {
        const btn = pending;
        if (!btn) return;
        okBtn.disabled = true;
        try {
            const d = await postJson(`/planning/${encodeURIComponent(btn.dataset.id)}/supprimer`);
            if (d.success) {
                const card = btn.closest('.hist-item');
                const grid = card && card.parentElement;
                closeDel();
                if (card) card.remove();
                // La carte a disparu : le focus va au titre de la page.
                const h1 = document.querySelector('.hero h1');
                if (h1) { h1.setAttribute('tabindex', '-1'); h1.focus(); }
                toastMsg(btn.dataset.kind === 'semaine' ? 'Semaine supprimée ✓' : 'Brouillon supprimé ✓', 'success');
                if (grid && !grid.querySelector('.hist-item')) location.reload();
            } else {
                toastMsg(d.error || 'Erreur', 'error');
                closeDel();
            }
        } catch (e) {
            toastMsg('Erreur réseau', 'error');
            closeDel();
        }
        okBtn.disabled = false;
    }

    if (modalEl) {
        document.querySelectorAll('.hist-del').forEach(b => b.addEventListener('click', function () { openDel(this); }));
        document.getElementById('del-cancel').addEventListener('click', closeDel);
        document.getElementById('del-bg').addEventListener('click', closeDel);
        okBtn.addEventListener('click', confirmDel);
    }
})();
