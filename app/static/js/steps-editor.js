// Éditeur d'étapes d'instructions, partagé par « Ajouter » et « Enrichir ».
//
// Balisage attendu : un conteneur #steps (lignes .step-row éventuellement
// pré-rendues côté serveur) et un bouton #add-step.
// Expose renumberSteps, bindDel et addStepRow (utilisés par ajouter.html).
(function () {
    'use strict';

    function renumberSteps() {
        document.querySelectorAll('#steps .step-row').forEach((row, i) => {
            const n = i + 1;
            const num = row.querySelector('.step-num');
            if (num) num.textContent = n + '.';
            const ta = row.querySelector('textarea');
            if (ta) ta.setAttribute('aria-label', 'Étape ' + n);
            const del = row.querySelector('.step-del');
            if (del) del.setAttribute('aria-label', "Supprimer l'étape " + n);
        });
    }

    function bindDel(btn) {
        btn.addEventListener('click', () => {
            btn.closest('.step-row').remove();
            renumberSteps();
        });
    }

    function addStepRow(text) {
        const row = document.createElement('div');
        row.className = 'step-row';
        const num = document.createElement('span');
        num.className = 'step-num';
        const ta = document.createElement('textarea');
        ta.name = 'steps';
        ta.rows = 2;
        ta.className = 'inp';
        ta.value = typeof text === 'string' ? text : '';
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'step-del';
        del.title = "Supprimer l'étape";
        del.textContent = '✕';
        row.append(num, ta, del);
        document.getElementById('steps').appendChild(row);
        bindDel(del);
        renumberSteps();
        return row;
    }

    window.renumberSteps = renumberSteps;
    window.bindDel = bindDel;
    window.addStepRow = addStepRow;

    document.querySelectorAll('#steps .step-del').forEach(bindDel);
    document.getElementById('add-step')?.addEventListener('click', () => {
        addStepRow('').querySelector('textarea').focus();
    });
    renumberSteps();
})();
