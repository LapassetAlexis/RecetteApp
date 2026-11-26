// Mode cuisine : ingrédients cochables, étapes une à une (boutons, balayage,
// flèches du clavier), écran maintenu allumé et minuteurs détectés dans le texte.
// Fonctions pures exposées sur `window.Cuisine` (testables sans DOM, y compris
// sous Node : `require('app/static/js/cuisine.js')` remplit `globalThis.Cuisine`).
(function (root) {
    'use strict';

    // ── Fonctions pures ──────────────────────────────────────────────────────

    // Durées : « 20 min », « 45 minutes », « 10 mn », « 1 h 30 », « 1h30 »,
    // « 2 heures », « 1,5 h », « 30 secondes ». Pour une fourchette
    // (« 15 à 20 min », « 15-20 min »), seule la borne haute porte l'unité et
    // est retenue. Les lettres qui suivent l'unité l'invalident (« 2 huiles »,
    // « 2 minces »), un nombre collé à un chiffre ou une virgule aussi.
    const NOT_LETTER = '(?![a-zà-ÿœæ])';
    const DUR_RE = new RegExp(
        '(?<![\\d.,])(\\d+(?:[.,]\\d+)?)\\s*(?:' +
            '(?:h(?:eures?)?)' + NOT_LETTER +
                '(?:\\s*(\\d{1,2})(?!\\d)(?:\\s*(?:min(?:utes?)?|mn)' + NOT_LETTER + ')?)?' +
            '|(min(?:utes?)?|mn)' + NOT_LETTER +
            '|(sec(?:ondes?)?)' + NOT_LETTER +
        ')',
        'gi'
    );
    const MAX_SECONDS = 24 * 3600;

    // Liste des durées trouvées dans `text` :
    // [{start, end, text, seconds, label}] (positions dans la chaîne d'origine).
    function parseDurations(text) {
        const out = [];
        const s = String(text == null ? '' : text);
        DUR_RE.lastIndex = 0;
        let m;
        while ((m = DUR_RE.exec(s)) !== null) {
            const n = parseFloat(m[1].replace(',', '.'));
            let seconds;
            if (m[3]) seconds = n * 60;                   // minutes
            else if (m[4]) seconds = n;                   // secondes
            else seconds = n * 3600 + (m[2] ? parseInt(m[2], 10) * 60 : 0);  // heures
            seconds = Math.round(seconds);
            if (seconds <= 0 || seconds > MAX_SECONDS) continue;
            out.push({start: m.index, end: m.index + m[0].length, text: m[0],
                seconds, label: formatDuration(seconds)});
        }
        return out;
    }

    // 1200 → « 20 min » ; 5400 → « 1 h 30 » ; 3600 → « 1 h » ; 90 → « 1 min 30 ».
    function formatDuration(seconds) {
        const t = Math.max(0, Math.round(seconds));
        const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
        if (h) return m ? `${h} h ${String(m).padStart(2, '0')}` : `${h} h`;
        if (m) return sec ? `${m} min ${String(sec).padStart(2, '0')}` : `${m} min`;
        return `${sec} s`;
    }

    // Affichage du décompte : 1200 → « 20:00 » ; 5400 → « 1:30:00 ».
    function formatClock(seconds) {
        const t = Math.max(0, Math.ceil(seconds));
        const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
        const mm = String(m).padStart(2, '0'), ss = String(sec).padStart(2, '0');
        return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    }

    // HTML d'une étape : texte échappé, chaque durée devient un bouton minuteur.
    function stepHtml(text, esc) {
        const s = String(text == null ? '' : text);
        let html = '', pos = 0;
        for (const d of parseDurations(s)) {
            html += esc(s.slice(pos, d.start));
            html += `<button type="button" class="ck-dur" data-s="${d.seconds}" data-label="${esc(d.label)}"` +
                ` aria-label="Lancer un minuteur de ${esc(d.label)}">⏱ ${esc(d.label)}</button>`;
            pos = d.end;
        }
        return html + esc(s.slice(pos));
    }

    const Cuisine = {parseDurations, formatDuration, formatClock, stepHtml};
    root.Cuisine = Cuisine;
    if (typeof module !== 'undefined' && module.exports) module.exports = Cuisine;
    if (typeof document === 'undefined') return;

    // ── Page ─────────────────────────────────────────────────────────────────

    const esc = (root.Common && root.Common.esc) ||
        (v => String(v).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])));
    const data = (root.Common && root.Common.pageData()) || {};
    const KEY = 'cuisine:' + (data.recette_id || location.pathname);
    const body = document.body;
    body.classList.add('js');

    // sessionStorage peut être absent ou refusé (navigation privée) : on se
    // contente alors d'un état en mémoire.
    function load() {
        try { return JSON.parse(sessionStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
    }
    const state = Object.assign({ings: [], step: 0, tab: null, timers: []}, load());
    if (!Array.isArray(state.ings)) state.ings = [];
    if (!Array.isArray(state.timers)) state.timers = [];
    function save() {
        try { sessionStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* stockage indisponible */ }
    }

    // ── Volets (onglets sur mobile, côte à côte sur grand écran) ──
    const ingBoxes = Array.from(document.querySelectorAll('.ck-ing input[type=checkbox]'));
    function setTab(tab) {
        state.tab = tab;
        body.dataset.tab = tab;
        document.getElementById('tab-ings')?.setAttribute('aria-selected', String(tab === 'ings'));
        document.getElementById('tab-steps')?.setAttribute('aria-selected', String(tab === 'steps'));
        save();
    }
    document.getElementById('tab-ings')?.addEventListener('click', () => setTab('ings'));
    document.getElementById('tab-steps')?.addEventListener('click', () => setTab('steps'));
    document.getElementById('ck-go')?.addEventListener('click', () => { setTab('steps'); root.scrollTo(0, 0); });

    // ── Ingrédients cochables ──
    const count = document.getElementById('ings-count');
    function refreshIngs() {
        let left = 0;
        ingBoxes.forEach(b => {
            b.closest('.ck-ing').classList.toggle('done', b.checked);
            if (!b.checked) left++;
        });
        if (count) count.textContent = left ? String(left) : '✓';
    }
    ingBoxes.forEach(b => {
        b.checked = state.ings.includes(Number(b.dataset.i));
        b.addEventListener('change', () => {
            state.ings = ingBoxes.filter(x => x.checked).map(x => Number(x.dataset.i));
            refreshIngs(); save();
        });
    });
    refreshIngs();

    // ── Étapes ──
    const steps = Array.from(document.querySelectorAll('.ck-step'));
    steps.forEach(li => { li.innerHTML = stepHtml(li.textContent.trim(), esc); });
    const prev = document.getElementById('ck-prev');
    const next = document.getElementById('ck-next');
    const bar = document.getElementById('ck-bar');
    const posEl = document.getElementById('ck-pos');
    let cur = 0;
    function go(i) {
        if (!steps.length) return;
        cur = Math.max(0, Math.min(steps.length - 1, i | 0));
        steps.forEach((li, k) => li.classList.toggle('on', k === cur));
        if (posEl) posEl.textContent = `Étape ${cur + 1} / ${steps.length}`;
        if (bar) bar.style.width = `${((cur + 1) / steps.length) * 100}%`;
        if (prev) prev.disabled = cur === 0;
        if (next) next.textContent = cur === steps.length - 1 ? '✓ Terminer' : 'Suivant →';
        state.step = cur; save();
    }
    function finish() {
        try { sessionStorage.removeItem(KEY); } catch (e) { /* rien à nettoyer */ }
        state.timers = [];
        location.href = data.retour || '/recettes';
    }
    prev?.addEventListener('click', () => go(cur - 1));
    next?.addEventListener('click', () => (cur === steps.length - 1 ? finish() : go(cur + 1)));
    const stepsVisible = () => body.dataset.tab === 'steps' ||
        root.matchMedia('(min-width: 900px)').matches;
    document.addEventListener('keydown', e => {
        if (!steps.length || !stepsVisible() || e.altKey || e.ctrlKey || e.metaKey) return;
        if (e.target.closest && e.target.closest('input, textarea, select')) return;
        if (e.key === 'ArrowRight') { e.preventDefault(); go(cur + 1); }
        else if (e.key === 'ArrowLeft') { e.preventDefault(); go(cur - 1); }
    });
    // Balayage horizontal sur le volet des étapes.
    const pane = document.getElementById('pane-steps');
    let sx = null, sy = 0;
    pane?.addEventListener('touchstart', e => {
        if (e.touches.length !== 1) { sx = null; return; }
        sx = e.touches[0].clientX; sy = e.touches[0].clientY;
    }, {passive: true});
    pane?.addEventListener('touchend', e => {
        if (sx === null) return;
        const t = e.changedTouches[0];
        const dx = t.clientX - sx, dy = t.clientY - sy;
        sx = null;
        if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
        go(dx < 0 ? cur + 1 : cur - 1);
    }, {passive: true});

    // ── Écran allumé (Wake Lock) : absent en HTTP simple → rien ne se passe ──
    let lock = null;
    async function keepAwake() {
        if (lock || !('wakeLock' in navigator) || document.visibilityState !== 'visible') return;
        try {
            lock = await navigator.wakeLock.request('screen');
            lock.addEventListener('release', () => { lock = null; });
        } catch (e) { lock = null; }
    }
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') keepAwake();
    });

    // ── Minuteurs ──
    const zone = document.getElementById('ck-timers');
    let audio = null;
    // Le contexte audio doit naître d'un geste de l'utilisateur (sinon le
    // navigateur le bloque et le signale en console).
    function unlockAudio() {
        if (audio) return;
        const AC = root.AudioContext || root.webkitAudioContext;
        if (!AC) return;
        try { audio = new AC(); } catch (e) { audio = null; }
    }
    function beep() {
        if (!audio) return;
        try {
            if (audio.state === 'suspended') audio.resume().catch(() => {});
            const t0 = audio.currentTime;
            for (let k = 0; k < 3; k++) {
                const osc = audio.createOscillator(), gain = audio.createGain();
                osc.type = 'sine'; osc.frequency.value = 880;
                gain.gain.setValueAtTime(0.0001, t0 + k * 0.4);
                gain.gain.exponentialRampToValueAtTime(0.4, t0 + k * 0.4 + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, t0 + k * 0.4 + 0.3);
                osc.connect(gain).connect(audio.destination);
                osc.start(t0 + k * 0.4); osc.stop(t0 + k * 0.4 + 0.32);
            }
        } catch (e) { /* son indisponible */ }
    }
    function ring() {
        beep();
        const ua = navigator.userActivation;
        if (typeof navigator.vibrate === 'function' && (!ua || ua.hasBeenActive)) {
            try { navigator.vibrate([400, 200, 400, 200, 400]); } catch (e) { /* pas de vibreur */ }
        }
    }
    const RING_EVERY = 5000, RING_FOR = 60000;
    let ticker = null;
    function renderTimers() {
        if (!zone) return;
        zone.hidden = !state.timers.length;
        zone.innerHTML = state.timers.map(t => `
            <div class="ck-timer" data-id="${esc(t.id)}" role="timer">
                <span class="ck-timer-l">${esc(t.label)}</span>
                <span class="ck-timer-c"></span>
                <button type="button" class="ck-timer-x" data-id="${esc(t.id)}" aria-label="Arrêter le minuteur ${esc(t.label)}">✕</button>
            </div>`).join('');
        tick();
    }
    function tick() {
        const now = Date.now();
        for (const t of state.timers) {
            const left = (t.end - now) / 1000;
            const el = zone && zone.querySelector(`.ck-timer[data-id="${CSS.escape(t.id)}"]`);
            if (left <= 0 && !t.done) { t.done = true; t.rang = 0; save(); }
            if (t.done && now - t.end < RING_FOR && now - (t.rang || 0) >= RING_EVERY) {
                t.rang = now; ring();
            }
            if (!el) continue;
            el.classList.toggle('done', !!t.done);
            el.querySelector('.ck-timer-c').textContent = t.done ? 'Terminé !' : formatClock(left);
            const x = el.querySelector('.ck-timer-x');
            if (t.done && x.textContent !== 'OK') x.textContent = 'OK';
        }
        if (state.timers.length && !ticker) ticker = setInterval(tick, 250);
        if (!state.timers.length && ticker) { clearInterval(ticker); ticker = null; }
    }
    function startTimer(seconds, label) {
        unlockAudio();
        const id = String(Date.now()) + Math.random().toString(36).slice(2, 6);
        const step = steps.length ? `Étape ${cur + 1} · ` : '';
        state.timers.push({id, label: step + label, end: Date.now() + seconds * 1000, done: false});
        save(); renderTimers();
    }
    document.addEventListener('click', e => {
        const d = e.target.closest('.ck-dur');
        if (d) { startTimer(Number(d.dataset.s), d.dataset.label); return; }
        const x = e.target.closest('.ck-timer-x');
        if (x) {
            state.timers = state.timers.filter(t => t.id !== x.dataset.id);
            save(); renderTimers();
        }
    });
    // Minuteurs restaurés après rechargement : le son se débloque au premier geste.
    document.addEventListener('pointerdown', () => { if (state.timers.length) unlockAudio(); }, {passive: true});

    // ── Démarrage ──
    const hasIngs = ingBoxes.length > 0;
    setTab(state.tab === 'ings' || state.tab === 'steps' ? state.tab : (hasIngs ? 'ings' : 'steps'));
    go(state.step || 0);
    renderTimers();
    keepAwake();
})(typeof window !== 'undefined' ? window : globalThis);
