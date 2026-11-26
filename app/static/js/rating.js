// Notation par étoiles, partagée par la liste des recettes et la fiche.
//
// Balisage attendu (une zone par recette) :
//   <div class="recipe-stars" role="group" aria-label="Note" data-id="<notion_id>" data-note="⭐⭐⭐">
//     <button type="button" class="star" data-val="1" aria-label="Noter 1 sur 5" aria-pressed="false">★</button>
//     … (5 boutons)
//     <button type="button" class="star-clear" aria-label="Retirer la note" hidden>✕</button>
//   </div>
// Les boutons sont atteignables au clavier (Tab) et activables par Entrée ou Espace.
(function () {
    'use strict';

    function notify(msg, type) {
        if (typeof window.toast === 'function') window.toast(msg, type);
    }

    async function postRating(pageId, note) {
        const r = await fetch('/api/rate/' + encodeURIComponent(pageId), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({note: note}),
        });
        return r.json();
    }

    function initRating(container) {
        if (container.dataset.ratingReady) return;
        container.dataset.ratingReady = '1';
        const stars = Array.from(container.querySelectorAll('.star'));
        const clearBtn = container.querySelector('.star-clear');
        const pageId = container.dataset.id;
        let current = Math.min(5, Array.from(container.dataset.note || '').length);

        // Peint n étoiles pleines (aperçu au survol ou au focus).
        const paint = (n) => stars.forEach((s, i) => s.classList.toggle('empty', i >= n));
        // Fixe la note courante : peinture, état pressé, bouton d'effacement.
        const commit = (n) => {
            current = n;
            paint(n);
            stars.forEach((s, i) => s.setAttribute('aria-pressed', String(i + 1 === n)));
            if (clearBtn) clearBtn.hidden = n === 0;
        };

        const save = async (n) => {
            const val = '⭐'.repeat(n);
            container.classList.add('saving');
            try {
                const d = await postRating(pageId, val);
                if (d.success) {
                    commit(n);
                    notify(n ? 'Noté ' + val : 'Note retirée', n ? 'success' : 'info');
                    if (d.warning) notify(d.warning, 'error');
                } else {
                    paint(current);
                    notify(d.error || 'Erreur', 'error');
                }
            } catch (e) {
                paint(current);
                notify('Erreur réseau', 'error');
            } finally {
                container.classList.remove('saving');
            }
        };

        stars.forEach((star, idx) => {
            star.addEventListener('mouseenter', () => paint(idx + 1));
            star.addEventListener('focus', () => paint(idx + 1));
            star.addEventListener('click', () => save(idx + 1));
        });
        container.addEventListener('mouseleave', () => paint(current));
        container.addEventListener('focusout', (e) => {
            if (!container.contains(e.relatedTarget)) paint(current);
        });
        if (clearBtn) clearBtn.addEventListener('click', () => save(0));

        commit(current);
    }

    window.initRating = initRating;
    document.querySelectorAll('.recipe-stars').forEach(initRating);
})();
