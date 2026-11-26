// Ajout d'une recette : analyse d'une URL ou d'un texte collé (pré-remplit
// le formulaire), saisie manuelle, aperçu de l'image, retour visuel pendant
// l'envoi. Éditeur d'étapes (addStepRow) : steps-editor.js.
(function () {
    'use strict';
    if (!window.Common) return;
    const {postJson} = window.Common;
    const $ = id => document.getElementById(id);
    const toastMsg = (m, t) => { if (window.toast) window.toast(m, t); };
    const step1 = $('step1'), step2 = $('step2');
    if (!step1 || !step2) return;

    // Instructions en étapes : une par ligne, sinon découpe par phrase.
    function splitInstructions(t) {
        t = (t || '').trim();
        if (!t) return [];
        const lines = t.split(/\n+/).map(s => s.trim()).filter(Boolean);
        if (lines.length > 1) return lines;
        return t.split(/(?<=[.!?])\s+(?=[A-ZÀ-Ý0-9])/).map(s => s.trim()).filter(Boolean);
    }

    // ── Aperçu image ────────────────────────────────────
    const imgInput = $('image_url');
    const imgPreview = $('recipe-preview-img');
    function refreshImgPreview() {
        if (!imgInput || !imgPreview) return;
        const v = imgInput.value.trim();
        if (v) { imgPreview.src = v; imgPreview.style.display = 'block'; }
        else { imgPreview.removeAttribute('src'); imgPreview.style.display = 'none'; }
    }
    imgInput?.addEventListener('input', refreshImgPreview);
    refreshImgPreview();  // saisie ré-affichée après une erreur

    // ── Bascule vers le formulaire (rempli ou vide) ─────
    function showForm() {
        step1.style.display = 'none';
        step2.style.display = 'block';
    }
    function fillForm(d, url) {
        $('url-hidden').value = url || '';
        $('nom').value = d.nom || '';
        document.querySelectorAll('.repas-cb').forEach(cb => { cb.checked = (d.repas || '') === cb.value; });
        $('moment').value = d.moment || 'Les deux';
        $('ingredients_manual').value = d.ingredients || '';
        const steps = splitInstructions(d.instructions || '');
        $('steps').textContent = '';
        steps.forEach(s => window.addStepRow(s));
        if (!steps.length) window.addStepRow('');
        if (imgInput) imgInput.value = d.image_url || '';
        refreshImgPreview();
        document.querySelectorAll('.tag-cb').forEach(cb => { cb.checked = (d.tags || []).includes(cb.value); });
        document.querySelectorAll('.base-cb').forEach(cb => { cb.checked = (d.base || []).includes(cb.value); });
    }

    // Analyse (URL ou texte) : bouton en attente, puis formulaire pré-rempli.
    async function analyse(btn, url, body, idleLabel, busyLabel, statusMsg) {
        const status = $('analyze-status');
        btn.disabled = true; btn.textContent = busyLabel; status.textContent = statusMsg;
        try {
            const d = await postJson(url, body);
            if (d.error) {
                status.textContent = '❌ ' + d.error;
                btn.disabled = false; btn.textContent = idleLabel;
                return null;
            }
            return d;
        } catch (e) {
            status.textContent = '❌ Erreur réseau';
            btn.disabled = false; btn.textContent = idleLabel;
            return null;
        }
    }

    $('btn-analyze')?.addEventListener('click', async function () {
        const url = $('url-input').value.trim();
        if (!url) { toastMsg("Colle une URL d'abord !", 'error'); return; }
        const d = await analyse(this, '/api/analyze-url', {url}, '🔍 Analyser', '⏳ Analyse...', 'Extraction en cours...');
        if (!d) return;
        fillForm(d, url);
        showForm();
        $('analyze-status').textContent = d.source === 'error'
            ? '⚠️ Extraction automatique indisponible (IA) — saisis les infos à la main.'
            : d.source === 'jsonld'
                ? '✅ Données extraites de la fiche recette (exactes) — vérifie et ajoute !'
                : '✅ Analyse terminée (approximative, par IA) — vérifie bien et ajuste !';
    });

    $('btn-structure')?.addEventListener('click', async function () {
        const text = $('text-input').value.trim();
        if (!text) { toastMsg("Colle un texte d'abord !", 'error'); return; }
        const d = await analyse(this, '/api/analyze-text', {text}, '✨ Structurer', '⏳ Structuration...', 'Analyse du texte en cours...');
        if (!d) return;
        fillForm(d, '');
        showForm();
        $('analyze-status').textContent = d.source === 'error'
            ? '⚠️ Structuration automatique indisponible (IA) — saisis les infos à la main.'
            : '✅ Texte structuré par IA — vérifie bien et ajuste !';
    });

    // ── Saisie manuelle (formulaire vide) ───────────────
    $('btn-manual')?.addEventListener('click', () => {
        fillForm({moment: 'Les deux'}, '');
        showForm();
        $('nom').focus();
    });

    // Retour visuel pendant l'ajout (le POST écrit dans Notion).
    step2.addEventListener('submit', () => {
        const b = $('btn-add');
        if (b) { b.disabled = true; b.textContent = '⏳ Ajout...'; }
        $('add-loading')?.classList.add('show');
    });

    $('btn-back')?.addEventListener('click', () => {
        step2.style.display = 'none';
        step1.style.display = 'block';
        const ba = $('btn-analyze');
        if (ba) { ba.disabled = false; ba.textContent = '🔍 Analyser'; }
        const bs = $('btn-structure');
        if (bs) { bs.disabled = false; bs.textContent = '✨ Structurer'; }
        $('analyze-status').textContent = '';
    });
})();
