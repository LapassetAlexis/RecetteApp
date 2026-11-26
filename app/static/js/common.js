// Utilitaires partagés : pages planning, constructeur et liste de courses publique.
// Exposés sur `window.Common`.
(function () {
    'use strict';
    // Chargé deux fois (gabarit de base et page) : la première copie reste.
    if (window.Common && window.Common.requestJson) return;

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

    // Session expirée : message, puis retour au formulaire de connexion qui
    // ramènera sur la page courante.
    let _sessionExpired = false;
    function sessionExpired() {
        if (_sessionExpired) return;
        _sessionExpired = true;
        if (window.toast) window.toast('Session expirée, reconnecte-toi', 'error');
        const next = location.pathname + location.search;
        setTimeout(() => { location.href = '/login?next=' + encodeURIComponent(next); }, 1200);
    }

    // Requête JSON et lecture de la réponse. `opts` : {method = 'GET', body}.
    // - réseau coupé : l'exception de fetch remonte (les appelants la gèrent) ;
    // - 401, ou redirection vers /login (session expirée sur une page) : toast,
    //   redirection, et la promesse ne se résout jamais (la page s'en va) ;
    // - réponse non JSON : {error: 'Réponse inattendue du serveur'} ;
    // - statut d'erreur sans champ `error` : `error` repris de `detail`.
    async function requestJson(url, opts) {
        const {method = 'GET', body} = opts || {};
        const init = {method, headers: {Accept: 'application/json'}};
        if (body !== undefined) {
            init.headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(body);
        }
        const r = await fetch(url, init);
        let path = '';
        try { path = new URL(r.url, location.href).pathname; } catch (e) { path = ''; }
        if (r.status === 401 || (r.redirected && path === '/login')) {
            sessionExpired();
            return new Promise(() => {});
        }
        let data;
        try { data = await r.json(); } catch (e) { return {error: 'Réponse inattendue du serveur'}; }
        if (!r.ok && data && typeof data === 'object' && !Array.isArray(data) && !data.error) {
            data.error = typeof data.detail === 'string' ? data.detail : `Erreur serveur (${r.status})`;
        }
        return data;
    }

    // POST JSON. Sans `body`, POST sans corps.
    function postJson(url, body) {
        return requestJson(url, {method: 'POST', body});
    }

    // Modale accessible. `el` : la racine `.modal` (masquée par display:none).
    // Ouvre, place le focus (`initialFocus` : élément ou sélecteur, sinon le
    // premier élément focalisable), piège Tab / Maj+Tab, ferme sur Échap et
    // rend le focus à l'élément qui l'avait à l'ouverture.
    // Renvoie {close()} ; `onClose` est appelé à chaque fermeture.
    const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])';
    function modal(el, opts) {
        const {onClose, initialFocus} = opts || {};
        const opener = document.activeElement;
        let open = true;
        const focusables = () => Array.from(el.querySelectorAll(FOCUSABLE))
            .filter(f => !f.disabled && f.offsetParent !== null);

        function onKey(e) {
            if (e.key === 'Escape') { e.preventDefault(); close(); return; }
            if (e.key !== 'Tab') return;
            const f = focusables();
            if (!f.length) { e.preventDefault(); return; }
            const first = f[0], last = f[f.length - 1];
            if (!el.contains(document.activeElement)) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
            else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
        function close() {
            if (!open) return;
            open = false;
            document.removeEventListener('keydown', onKey);
            el.style.display = 'none';
            if (opener && opener.focus && document.contains(opener)) opener.focus();
            if (onClose) onClose();
        }

        el.hidden = false;
        el.style.display = 'flex';
        document.addEventListener('keydown', onKey);
        const target = typeof initialFocus === 'string' ? el.querySelector(initialFocus) : initialFocus;
        (target || focusables()[0])?.focus();
        return {close};
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
    // Ajouts du socle front (M01), sur une ligne à part : la ligne ci-dessus
    // est vérifiée telle quelle par tests/test_static_js.py.
    Object.assign(window.Common, {requestJson, modal});
})();
