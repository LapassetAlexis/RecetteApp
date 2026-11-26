// Utilitaires partagés : pages planning, constructeur et liste de courses publique.
// Exposés sur `window.Common`.
(function () {
    'use strict';

    // Échappe un texte pour l'insérer dans du HTML.
    function esc(s) {
        return String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
    }

    // Copie robuste : hors contexte sécurisé (http via IP locale sur mobile),
    // navigator.clipboard est ABSENT et l'appel jetterait une exception synchrone.
    // On teste son existence, sinon repli execCommand sur un textarea temporaire.
    // Renvoie une promesse résolue à true (copié) ou false (échec).
    function copyText(txt) {
        const legacy = () => {
            const ta = document.createElement('textarea');
            ta.value = txt;
            ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
            document.body.appendChild(ta); ta.focus(); ta.select();
            let ok = false;
            try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
            ta.remove();
            return ok;
        };
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(txt).then(() => true, legacy);
        }
        return Promise.resolve(legacy());
    }

    // POST JSON et lecture de la réponse JSON. Sans `body`, POST sans corps.
    async function postJson(url, body) {
        const opts = {method: 'POST'};
        if (body !== undefined) {
            opts.headers = {'Content-Type': 'application/json'};
            opts.body = JSON.stringify(body);
        }
        const r = await fetch(url, opts);
        return r.json();
    }

    // Quantité affichée avec la virgule décimale française : un nombre est
    // arrondi à 2 décimales (1.5 → « 1,5 »), un texte garde sa forme mais son
    // point décimal devient une virgule. Même règle que `fmt_quantite` (serveur).
    function qtyFr(v) {
        const s = typeof v === 'number' ? String(Math.round(v * 100) / 100) : String(v ?? '').trim();
        return s.replace(/(\d)\.(?=\d)/g, '$1,');
    }

    // Données serveur de la page : bloc <script type="application/json" id="page-data">.
    let _data = null;
    function pageData() {
        if (_data === null) {
            const el = document.getElementById('page-data');
            try { _data = el ? JSON.parse(el.textContent) : {}; } catch (e) { _data = {}; }
        }
        return _data;
    }

    window.Common = {esc, copyText, postJson, pageData, qtyFr};
})();
