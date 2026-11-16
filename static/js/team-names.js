(function () {
    'use strict';
    const message = 'Erlaubt sind Buchstaben A–Z und a–z, ÄÖÜäöüß, Ziffern 0–9, Leerzeichen, Bindestrich (-) und Unterstrich (_).';
    const validCharacters = value => !/[^A-Za-zÄÖÜäöüß0-9 _-]/.test(value);
    window.TeamNames = {message, validCharacters};
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('#participantName, #participant_name, #nickname').forEach(input => {
            if (input.id === 'nickname' && document.getElementById('joinBtn')) return; // Lobby has its own status.
            const hint = document.createElement('div');
            hint.setAttribute('role', 'status');
            hint.setAttribute('aria-live', 'polite');
            hint.className = 'team-name-hint';
            hint.hidden = true;
            hint.textContent = message;
            input.insertAdjacentElement('afterend', hint);
            function check() {
                const invalid = !validCharacters(input.value);
                hint.hidden = !invalid;
                input.setCustomValidity(invalid ? message : '');
                input.setAttribute('aria-invalid', String(invalid));
                return !invalid;
            }
            input.addEventListener('input', check);
            input.form?.addEventListener('submit', event => {
                if (!check()) { event.preventDefault(); event.stopImmediatePropagation(); input.reportValidity(); }
            }, true);
            check();
        });
    });
})();
