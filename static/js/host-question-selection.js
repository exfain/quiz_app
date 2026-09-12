(function initializeHostQuestionSelection() {
    function buttonLabel(button, key, fallback) {
        return button.dataset[key] || fallback;
    }

    function setButtonLabel(button, label, icon) {
        button.innerHTML = `<i data-lucide="${icon}"></i> ${label}`;
        try { window.lucide?.createIcons(); } catch (_) {}
    }

    function restoreOverview(selection) {
        selection.classList.remove('host-question-selection-mode');
        selection.querySelectorAll('[data-host-question-item]').forEach(item => {
            item.hidden = false;
            item.classList.remove('host-question-selection-active');
            const text = item.querySelector('[data-host-question-text-target]');
            if (text?.dataset.hostQuestionPreview) {
                text.textContent = text.dataset.hostQuestionPreview;
                delete text.dataset.hostQuestionPreview;
            }
            const button = item.querySelector('[data-host-question-select]');
            if (!button) return;
            const selectLabel = buttonLabel(button, 'hostSelectLabel', 'FRAGE W\u00c4HLEN');
            const sendLabel = buttonLabel(button, 'hostSendLabel', 'FRAGE SENDEN');
            const currentLabel = button.textContent.trim();
            if (
                button.disabled
                && currentLabel !== selectLabel
                && currentLabel !== sendLabel
            ) return;
            delete button.dataset.hostQuestionReadyToSend;
            button.classList.remove('btn-primary');
            button.classList.add('btn-secondary');
            setButtonLabel(
                button,
                selectLabel,
                'mouse-pointer-click'
            );
        });
        selection.querySelector('[data-host-question-selection-heading]')?.remove();
    }

    function selectQuestion(button) {
        const item = button.closest('[data-host-question-item]');
        const selection = item?.closest('[data-host-question-selection]');
        if (!item || !selection) return false;

        restoreOverview(selection);
        selection.classList.add('host-question-selection-mode');
        selection.querySelectorAll('[data-host-question-item]').forEach(candidate => {
            candidate.hidden = candidate !== item;
        });
        item.classList.add('host-question-selection-active');

        const text = item.querySelector('[data-host-question-text-target]');
        const fullText = item.dataset.hostQuestionText || '';
        if (text && fullText) {
            text.dataset.hostQuestionPreview = text.textContent;
            text.textContent = fullText;
        }

        const heading = document.createElement('div');
        heading.dataset.hostQuestionSelectionHeading = '';
        heading.className = 'host-question-selection-heading';
        const noun = buttonLabel(button, 'hostSelectionNoun', 'Frage');
        heading.innerHTML = `
            <h6 class="mb-0">Ausgew\u00e4hlte ${noun}</h6>
            <button type="button" class="btn btn-outline-secondary btn-sm" data-host-question-back>
                <i data-lucide="arrow-left"></i> Zur\u00fcck zur \u00dcbersicht
            </button>
        `;
        selection.insertBefore(heading, selection.firstChild);
        heading.querySelector('[data-host-question-back]').addEventListener('click', () => {
            restoreOverview(selection);
        });

        button.dataset.hostQuestionReadyToSend = 'true';
        button.classList.remove('btn-secondary');
        button.classList.add('btn-primary');
        setButtonLabel(
            button,
            buttonLabel(button, 'hostSendLabel', 'FRAGE SENDEN'),
            'send'
        );
        return true;
    }

    document.addEventListener('click', event => {
        const button = event.target.closest('[data-host-question-select]');
        if (!button || button.disabled || button.dataset.hostQuestionReadyToSend === 'true') return;
        if (!selectQuestion(button)) return;
        event.preventDefault();
        event.stopImmediatePropagation();
    }, true);

    function normalizeOverviewButtons(root = document) {
        const buttons = [
            ...(root.matches?.('[data-host-question-select]') ? [root] : []),
            ...(root.querySelectorAll?.('[data-host-question-select]') || []),
        ];
        buttons.forEach(button => {
            if (button.dataset.hostQuestionReadyToSend === 'true') return;
            const label = buttonLabel(button, 'hostSelectLabel', 'FRAGE W\u00c4HLEN');
            const sendLabel = buttonLabel(button, 'hostSendLabel', 'FRAGE SENDEN');
            const currentLabel = button.textContent.trim();
            if (button.disabled && currentLabel !== label && currentLabel !== sendLabel) return;
            if (currentLabel !== label) {
                setButtonLabel(button, label, 'mouse-pointer-click');
            }
        });
    }

    document.addEventListener('DOMContentLoaded', () => {
        window.setTimeout(() => normalizeOverviewButtons(), 0);
        new MutationObserver(records => {
            records.forEach(record => normalizeOverviewButtons(record.target));
        }).observe(document.body, {childList: true, subtree: true});
    }, {once: true});

    window.HostQuestionSelection = {normalizeOverviewButtons, restoreOverview, selectQuestion};
}());
