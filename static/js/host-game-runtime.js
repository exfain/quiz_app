(function () {
    'use strict';

    if (window.HostGameRuntime) return;

    function normalizeError(error, fallbackMessage) {
        if (error && typeof error === 'object' && error.message) {
            return {
                code: error.code || 'host_action_failed',
                message: String(error.message),
                cause: error,
            };
        }
        return {
            code: 'host_action_failed',
            message: String(error || fallbackMessage || 'Die Hostaktion ist fehlgeschlagen.'),
            cause: error,
        };
    }

    function create(config) {
        const options = config || {};
        const pendingActions = new Map();
        const requests = new Map();
        let snapshot = null;
        let revision = -1;
        let socket = null;
        let reconnectTimer = null;
        let reconnectAttempt = 0;
        let connectionGeneration = 0;
        let disposed = false;
        let deferredAction = null;
        let serverNowMillis = null;
        let serverReceivedAt = null;

        function emitPendingChange() {
            if (typeof options.onPendingChange === 'function') {
                options.onPendingChange(pendingActions.size > 0, new Set(pendingActions.keys()));
            }
        }

        function setButtonsPending(buttons, isPending) {
            const elements = Array.from(buttons || []).filter(Boolean);
            elements.forEach(function (button) {
                if (isPending) {
                    if (!button.dataset.hostRuntimeWasDisabled) {
                        button.dataset.hostRuntimeWasDisabled = button.disabled ? 'true' : 'false';
                    }
                    button.disabled = true;
                    button.setAttribute('aria-busy', 'true');
                    return;
                }
                if (!button.isConnected) return;
                const wasDisabled = button.dataset.hostRuntimeWasDisabled === 'true';
                delete button.dataset.hostRuntimeWasDisabled;
                button.removeAttribute('aria-busy');
                button.disabled = wasDisabled;
            });
        }

        function beginAction(key, buttons) {
            const actionKey = String(key || 'host-action');
            if (pendingActions.has(actionKey)) return false;
            const buttonList = Array.from(buttons || []).filter(Boolean);
            pendingActions.set(actionKey, {buttons: buttonList});
            setButtonsPending(buttonList, true);
            emitPendingChange();
            return true;
        }

        function completeAction(key) {
            const actionKey = String(key || 'host-action');
            const pending = pendingActions.get(actionKey);
            if (!pending) return;
            pendingActions.delete(actionKey);
            setButtonsPending(pending.buttons, false);
            emitPendingChange();
        }

        function completeAllActions(exceptKey) {
            Array.from(pendingActions.keys()).forEach(function (key) {
                if (key !== exceptKey) completeAction(key);
            });
        }

        function reportError(error, fallbackMessage) {
            const normalized = normalizeError(error, fallbackMessage);
            if (typeof options.onError === 'function') options.onError(normalized);
            return normalized;
        }

        function syncServerClock(payload) {
            const parsed = Date.parse(payload?.server_now || '');
            if (!Number.isFinite(parsed)) return;
            serverNowMillis = parsed;
            serverReceivedAt = performance.now();
        }

        function serverNow(payload) {
            if (payload) syncServerClock(payload);
            if (Number.isFinite(serverNowMillis) && Number.isFinite(serverReceivedAt)) {
                return serverNowMillis + (performance.now() - serverReceivedAt);
            }
            return Date.now();
        }

        function deadlineFrom(payload) {
            if (typeof payload === 'string') return payload;
            return payload?.answering_deadline_at
                || payload?.ends_at
                || payload?.timer?.ends_at
                || payload?.round?.ends_at
                || null;
        }

        function remainingMilliseconds(payload) {
            const deadline = Date.parse(deadlineFrom(payload) || '');
            if (!Number.isFinite(deadline)) return 0;
            return Math.max(0, deadline - serverNow());
        }

        function actionContext(extra) {
            const current = snapshot || {};
            return Object.assign({
                game_id: current.game_id ?? current.game?.id ?? null,
                question_id: current.current_question_id ?? current.question?.id ?? null,
                round_id: current.current_round_id ?? current.round?.id ?? current.round?.number ?? current.current_round ?? null,
                set_id: current.current_set_id ?? current.set?.id ?? null,
                state_revision: Number.isFinite(Number(current.state_revision))
                    ? Number(current.state_revision)
                    : revision,
                client_action_id: window.AuthoritativeGameState.createActionId(),
            }, extra || {});
        }

        function dispatchDeferredAction() {
            if (!deferredAction || !snapshot) return;
            const pending = deferredAction;
            deferredAction = null;
            sendActionNow(pending.payload, pending.options, pending.key);
        }

        function acceptSnapshot(payload, metadata) {
            if (!payload || typeof payload !== 'object') return false;
            const incomingRevision = Number(payload.state_revision);
            if (Number.isFinite(incomingRevision) && incomingRevision < revision) return false;

            syncServerClock(payload);
            if (Number.isFinite(incomingRevision) && snapshot && incomingRevision === revision) {
                return false;
            }
            if (!window.AuthoritativeGameState.acceptSnapshot(payload)) return false;

            if (Number.isFinite(incomingRevision)) revision = incomingRevision;
            snapshot = payload;
            completeAllActions(deferredAction?.key);
            if (typeof options.render === 'function') {
                options.render(payload, metadata || {});
            }
            if (deferredAction) queueMicrotask(dispatchDeferredAction);
            return true;
        }

        function sendRaw(payload, settings) {
            if (!socket || socket.readyState !== WebSocket.OPEN) {
                if (settings?.reportOffline) reportError({
                    code: 'socket_offline',
                    message: 'Die Verbindung zum Spielserver ist unterbrochen.',
                });
                return false;
            }
            socket.send(JSON.stringify(Object.assign({}, options.messageBase || {}, payload)));
            return true;
        }

        function requestState() {
            return sendRaw(options.stateRequest || {type: 'get_state'});
        }

        function sendActionNow(payload, actionOptions, key) {
            if (!socket || socket.readyState !== WebSocket.OPEN) {
                completeAction(key);
                reportError({
                    code: 'socket_offline',
                    message: 'Die Hostaktion konnte ohne Serververbindung nicht gesendet werden.',
                });
                return false;
            }
            const message = Object.assign({}, payload, actionContext(actionOptions?.context));
            socket.send(JSON.stringify(Object.assign({}, options.messageBase || {}, message)));
            return true;
        }

        function sendAction(payload, actionOptions) {
            const settings = actionOptions || {};
            const key = String(settings.key || payload?.type || 'host-action');
            const buttons = settings.buttons || (settings.button ? [settings.button] : []);
            if (!beginAction(key, buttons)) return false;
            if (!snapshot || revision < 0) {
                if (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState)) {
                    deferredAction = {payload, options: settings, key};
                    if (socket.readyState === WebSocket.OPEN) requestState();
                    return true;
                }
                completeAction(key);
                reportError({
                    code: 'state_unavailable',
                    message: 'Der aktuelle Serverzustand ist noch nicht verfuegbar.',
                });
                return false;
            }
            return sendActionNow(payload, settings, key);
        }

        function rejectAction(payload, settings) {
            deferredAction = null;
            completeAllActions();
            if (!settings?.silent) {
                reportError({
                    code: payload?.code || payload?.type || 'server_rejected',
                    message: payload?.message || 'Die Hostaktion wurde vom Server abgelehnt.',
                });
            }
            if (settings?.refresh !== false) requestState();
        }

        function scheduleReconnect(connectOptions) {
            if (disposed || reconnectTimer) return;
            reconnectAttempt += 1;
            const baseDelay = Number(connectOptions.reconnectDelay || 1500);
            const delay = Math.min(baseDelay * Math.max(1, reconnectAttempt), 10000);
            reconnectTimer = window.setTimeout(function () {
                reconnectTimer = null;
                connectSocket(connectOptions);
            }, delay);
        }

        function connectSocket(connectOptions) {
            const settings = connectOptions || {};
            if (disposed || (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState))) {
                return socket;
            }
            const generation = ++connectionGeneration;
            const url = typeof settings.url === 'function' ? settings.url() : settings.url;
            socket = new WebSocket(url);
            if (typeof options.onConnectionChange === 'function') {
                options.onConnectionChange(snapshot ? 'reconnecting' : 'connecting');
            }
            socket.onopen = function () {
                if (generation !== connectionGeneration) return;
                reconnectAttempt = 0;
                if (typeof options.onConnectionChange === 'function') options.onConnectionChange('connected');
                requestState();
                if (typeof settings.onOpen === 'function') settings.onOpen(socket);
            };
            socket.onmessage = function (event) {
                let payload;
                try {
                    payload = JSON.parse(event.data);
                } catch (error) {
                    reportError({code: 'invalid_message', message: 'Der Spielserver hat ungueltige Daten gesendet.'});
                    return;
                }
                if (typeof settings.onMessage === 'function') settings.onMessage(payload, api);
            };
            socket.onclose = function () {
                if (generation !== connectionGeneration) return;
                socket = null;
                deferredAction = null;
                if (pendingActions.size) {
                    completeAllActions();
                    reportError({
                        code: 'socket_disconnected',
                        message: 'Die Serververbindung wurde waehrend der Hostaktion unterbrochen.',
                    });
                }
                if (typeof options.onConnectionChange === 'function') options.onConnectionChange('reconnecting');
                if (typeof settings.onClose === 'function') settings.onClose();
                scheduleReconnect(settings);
            };
            return socket;
        }

        async function parseSnapshotResponse(response, settings) {
            if (typeof settings.parse === 'function') return settings.parse(response);
            return response.json();
        }

        async function fetchSnapshot(url, fetchOptions, requestOptions) {
            const settings = requestOptions || {};
            const key = String(settings.key || 'host-state');
            const previous = requests.get(key);
            if (previous) previous.controller.abort();
            const controller = new AbortController();
            const request = {controller, sequence: (previous?.sequence || 0) + 1};
            requests.set(key, request);
            try {
                const response = await fetch(url, Object.assign({}, fetchOptions || {}, {signal: controller.signal}));
                const parsed = await parseSnapshotResponse(response, settings);
                const current = requests.get(key);
                if (!current || current !== request) return null;
                const payload = parsed?.payload || parsed;
                if (!response.ok) {
                    throw {
                        code: payload?.code || 'http_error',
                        message: payload?.error || payload?.message || settings.fallbackMessage || 'State konnte nicht geladen werden.',
                    };
                }
                acceptSnapshot(payload, {source: settings.source || 'http'});
                return payload;
            } catch (error) {
                if (error?.name === 'AbortError') return null;
                if (settings.reportError) reportError(error, settings.fallbackMessage);
                throw error;
            } finally {
                if (requests.get(key) === request) requests.delete(key);
            }
        }

        function dispose() {
            disposed = true;
            connectionGeneration += 1;
            if (reconnectTimer) window.clearTimeout(reconnectTimer);
            reconnectTimer = null;
            requests.forEach(function (request) { request.controller.abort(); });
            requests.clear();
            if (socket) socket.close();
            socket = null;
            deferredAction = null;
            completeAllActions();
        }

        const api = {
            acceptSnapshot,
            actionContext,
            beginAction,
            completeAction,
            connectSocket,
            dispose,
            fetchSnapshot,
            rejectAction,
            remainingMilliseconds,
            reportError,
            requestState,
            send: sendRaw,
            sendAction,
            serverNow,
            get revision() { return revision; },
            get snapshot() { return snapshot; },
            get socket() { return socket; },
            get hasPendingAction() { return pendingActions.size > 0; },
        };
        return api;
    }

    window.HostGameRuntime = {create};
}());
