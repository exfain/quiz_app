(function (global) {
    'use strict';

    function unicodeCharacters(value) {
        return Array.from(String(value ?? ''));
    }

    function splitFirstWord(value) {
        const characters = unicodeCharacters(value);
        const firstWhitespace = characters.findIndex((character) => /\s/u.test(character));
        if (firstWhitespace === -1) {
            return { firstWord: characters.join(''), separator: '', rest: '' };
        }
        let restStart = firstWhitespace;
        while (restStart < characters.length && /\s/u.test(characters[restStart])) {
            restStart += 1;
        }
        return {
            firstWord: characters.slice(0, firstWhitespace).join(''),
            separator: characters.slice(firstWhitespace, restStart).join(''),
            rest: characters.slice(restStart).join(''),
        };
    }

    function createStableFirstWordRenderer(element, text) {
        const parts = splitFirstWord(text);
        const firstWordCharacters = unicodeCharacters(parts.firstWord);
        const separatorCharacters = unicodeCharacters(parts.separator);
        const restCharacters = unicodeCharacters(parts.rest);
        const lead = document.createElement('span');
        const leadText = document.createTextNode('');
        const separator = document.createTextNode('');
        const body = document.createElement('span');
        const bodyText = document.createTextNode('');
        const cursor = document.createElement('span');
        lead.className = 'vhs-question-lead';
        body.className = 'vhs-question-body';
        cursor.className = 'question-typewriter-cursor';
        cursor.setAttribute('aria-hidden', 'true');
        lead.append(leadText, separator, cursor);
        body.append(bodyText);
        element.replaceChildren(lead, body);

        return {
            render(visibleCount) {
                const firstWordCount = Math.min(visibleCount, firstWordCharacters.length);
                const separatorCount = Math.min(
                    Math.max(0, visibleCount - firstWordCharacters.length),
                    separatorCharacters.length
                );
                const restCount = Math.min(
                    Math.max(
                        0,
                        visibleCount - firstWordCharacters.length - separatorCharacters.length
                    ),
                    restCharacters.length
                );
                leadText.nodeValue = firstWordCharacters.slice(0, firstWordCount).join('');
                separator.nodeValue = separatorCharacters.slice(0, separatorCount).join('');
                bodyText.nodeValue = restCharacters.slice(0, restCount).join('');
                const activeText = restCount > 0 ? body : lead;
                if (cursor.parentNode !== activeText) activeText.append(cursor);
            },
            finish() {
                cursor.remove();
            },
        };
    }

    function visibleCharacterCount(options) {
        const characters = unicodeCharacters(options.text);
        const presentedAt = Date.parse(options.presentedAt || '');
        const serverNow = Date.parse(options.serverNow || '');
        const receivedAt = Number(options.receivedAt);
        const millisecondsPerCharacter = Math.max(
            1,
            Number(options.millisecondsPerCharacter) || 75
        );
        if (!Number.isFinite(presentedAt) || !Number.isFinite(serverNow)) {
            return characters.length;
        }
        const elapsedSinceReceipt = Number.isFinite(receivedAt)
            ? Math.max(0, performance.now() - receivedAt)
            : 0;
        const elapsed = Math.max(0, serverNow + elapsedSinceReceipt - presentedAt);
        return Math.min(characters.length, Math.floor(elapsed / millisecondsPerCharacter));
    }

    function start(element, options) {
        if (!element) return function () {};
        const characters = unicodeCharacters(options.text);
        const reducedMotion = global.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
        let animationFrame = null;
        let cancelled = false;
        let lastVisibleCount = -1;
        const textRenderer = options.stableFirstWord
            ? createStableFirstWordRenderer(element, options.text)
            : {
                render(visibleCount) {
                    element.textContent = characters.slice(0, visibleCount).join('');
                },
                finish() {},
            };

        function finish() {
            textRenderer.finish();
            element.classList.remove('is-typewriting');
            element.removeAttribute('aria-busy');
            options.onComplete?.();
        }

        function render() {
            if (cancelled) return;
            const visibleCount = reducedMotion
                ? characters.length
                : visibleCharacterCount(options);
            if (visibleCount !== lastVisibleCount) {
                textRenderer.render(visibleCount);
                lastVisibleCount = visibleCount;
            }
            if (visibleCount >= characters.length) {
                finish();
                return;
            }
            element.classList.add('is-typewriting');
            element.setAttribute('aria-busy', 'true');
            animationFrame = global.requestAnimationFrame(render);
        }

        render();
        return function cancel() {
            cancelled = true;
            if (animationFrame !== null) global.cancelAnimationFrame(animationFrame);
            textRenderer.finish();
            element.classList.remove('is-typewriting');
            element.removeAttribute('aria-busy');
        };
    }

    global.QuestionTypewriter = {
        start,
        unicodeCharacters,
        splitFirstWord,
        visibleCharacterCount,
    };
})(window);
