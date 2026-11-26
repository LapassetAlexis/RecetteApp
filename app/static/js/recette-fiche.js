// Fiche recette : portions ajustables (quantités et format Cooklang) et
// suppression confirmée par modale. « Ajouter au planning » : recette-planifier.js.
// Données serveur (#page-data) : {recette_id, base_servings, drafts}.
(function () {
    'use strict';
    if (!window.Common) return;
    const {pageData, postJson, qtyFr} = window.Common;
    const data = pageData();
    const recetteId = String(data.recette_id || '');
    const BASE_SERVINGS = parseInt(data.base_servings, 10) > 0 ? parseInt(data.base_servings, 10) : 4;
    let servings = BASE_SERVINGS;

    // Parse une quantité : décimales (1,5) et fractions (1/2). null si non numérique.
    function parseQty(s) {
        s = (s || '').toString().trim().replace(',', '.');
        if (!s) return null;
        const frac = s.match(/^(\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)$/);
        if (frac) { const d = parseFloat(frac[2]); return d ? parseFloat(frac[1]) / d : null; }
        if (/^\d+(?:\.\d+)?$/.test(s)) return parseFloat(s);
        return null;
    }
    function fmtQty(n) {
        if (Number.isInteger(n)) return String(n);
        return String(Math.round(n * 100) / 100);
    }

    function render() {
        const ratio = servings / BASE_SERVINGS;
        const cookLines = [`>> Serves: ${servings}`, ''];
        document.querySelectorAll('.ing').forEach(li => {
            const raw = li.dataset.qty, unit = li.dataset.unit || '';
            const n = parseQty(raw);
            const scaled = n !== null ? fmtQty(n * ratio) : raw;
            // Affichage : virgule décimale (« 1,5 kg ») ; Cooklang garde le point.
            const shown = qtyFr(scaled || '');
            li.querySelector('.ing-q').textContent = shown ? (unit ? `${shown} ${unit}` : shown) : '';
            const nom = li.querySelector('.ing-n').textContent;
            if (scaled && unit) cookLines.push(`@${nom}{${scaled}%${unit}}`);
            else if (scaled) cookLines.push(`@${nom}{${scaled}}`);
            else cookLines.push(`@${nom}`);
        });
        const srvN = document.getElementById('srv-n');
        if (srvN) srvN.textContent = servings;  // absent sans ingrédients
        const ck = document.getElementById('btn-cuisine');
        if (ck) ck.href = `/recette/${encodeURIComponent(recetteId)}/cuisine?portions=${servings}`;
        const raw = document.getElementById('cook-raw');
        if (raw) raw.textContent = cookLines.join('\n');
    }

    document.getElementById('srv-minus')?.addEventListener('click', () => {
        servings = Math.max(1, servings - 1); render();
    });
    document.getElementById('srv-plus')?.addEventListener('click', () => {
        servings = Math.min(50, servings + 1); render();
    });
    render();

    // ── Suppression (archive Notion), confirmée par modale ──
    const delModal = document.getElementById('del-modal');
    const delBtn = document.getElementById('btn-delete');
    if (!delModal || !delBtn) return;
    let handle = null;
    const closeDel = () => { if (handle) handle.close(); };
    delBtn.addEventListener('click', () => {
        handle = window.Common.modal(delModal, {
            initialFocus: '#del-cancel',
            onClose: () => { handle = null; },
        });
    });
    document.getElementById('del-x')?.addEventListener('click', closeDel);
    document.getElementById('del-cancel')?.addEventListener('click', closeDel);
    delModal.querySelector('.modal-bg')?.addEventListener('click', closeDel);
    document.getElementById('del-confirm')?.addEventListener('click', async function () {
        const label = this.textContent;
        this.disabled = true; this.textContent = '⏳ Suppression...';
        try {
            const d = await postJson(`/recette/${encodeURIComponent(recetteId)}/supprimer`);
            if (d.success) {
                window.toast?.('Recette supprimée', 'success');
                window.navStart?.();
                window.location.href = '/recettes';
                return;
            }
            window.toast?.(d.error || 'Erreur lors de la suppression', 'error');
        } catch (e) {
            window.toast?.('Erreur réseau', 'error');
        }
        this.disabled = false; this.textContent = label;
    });
})();
