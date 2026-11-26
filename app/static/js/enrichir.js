// Modifier une recette : « Ré-extraire depuis la source » et retour visuel
// pendant l'enregistrement. Éditeur d'étapes : steps-editor.js.
// Données serveur (#page-data) : {recette_id}.
(function () {
    'use strict';
    if (!window.Common) return;
    const recetteId = String(window.Common.pageData().recette_id || '');

    // Recharge le formulaire pré-rempli depuis la source (les champs actuels
    // sont remplacés, rien n'est écrit avant « Valider »).
    // Confirmation par modale (Common.confirmModal), pas par confirm().
    const extractModal = document.getElementById('extract-modal');
    document.getElementById('btn-extract')?.addEventListener('click', async () => {
        const u = document.getElementById('source_url').value.trim();
        if (!u) { window.toast?.("Entre une URL d'abord", 'error'); return; }
        if (!extractModal || !(await window.Common.confirmModal(extractModal))) return;
        window.navStart?.();
        window.location.href = `/recette/${encodeURIComponent(recetteId)}/enrichir?refetch=1&url=` + encodeURIComponent(u);
    });

    // Retour visuel pendant l'enregistrement (le POST fait plusieurs appels Notion).
    document.getElementById('enrich-form')?.addEventListener('submit', () => {
        const b = document.getElementById('enrich-submit');
        if (b) { b.disabled = true; b.textContent = '⏳ Enregistrement...'; }
        document.getElementById('enrich-loading')?.classList.add('show');
    });
})();
