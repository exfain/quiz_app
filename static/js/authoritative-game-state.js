(function () {
    'use strict';

    if (window.AuthoritativeGameState) return;

    const NativeWebSocket = window.WebSocket;
    const socketState = new WeakMap();
    const latestByIdentity = new Map();
    const nonFinalMessages = new Set([
        'join',
        'participant_join',
        'participant_check_round',
        'participant_question_timeout',
        'participant_check_in',
        'participant_ready',
        'get_state',
        'snapshot_request',
        'vote',
        'ready',
        'ping',
        'heartbeat',
    ]);

    function makeActionId() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (char) {
            const value = Math.random() * 16 | 0;
            return (char === 'x' ? value : (value & 0x3 | 0x8)).toString(16);
        });
    }

    function identityOf(payload) {
        if (!payload || typeof payload !== 'object') return '';
        return [
            payload.session_id ?? '',
            payload.game_key ?? '',
            payload.game_id ?? payload.room_code ?? '',
        ].join(':');
    }

    function estimatedServerNow(context) {
        if (
            Number.isFinite(context?.serverNowMillis)
            && Number.isFinite(context?.receivedMonotonicMillis)
        ) {
            return context.serverNowMillis
                + (performance.now() - context.receivedMonotonicMillis);
        }
        return Date.now();
    }

    function isSameTimerContext(previous, candidate) {
        if (!previous) return false;
        const contextFields = [
            'current_question_id',
            'current_round_id',
            'current_set_id',
        ];
        const comparableFields = contextFields.filter(function (field) {
            return previous[field] !== undefined || candidate[field] !== undefined;
        });
        if (comparableFields.length) {
            return comparableFields.every(function (field) {
                return String(previous[field] ?? '') === String(candidate[field] ?? '');
            });
        }
        if (previous.starts_at || candidate.starts_at) {
            return String(previous.starts_at ?? '') === String(candidate.starts_at ?? '');
        }
        return String(previous.phase ?? '') === String(candidate.phase ?? '');
    }

    function contextFrom(payload, previous) {
        const snapshot = payload?.snapshot && typeof payload.snapshot === 'object'
            ? payload.snapshot
            : payload;
        const next = Object.assign({}, previous || {});
        if (!snapshot || typeof snapshot !== 'object') return next;
        [
            'state_revision',
            'game_id',
            'session_id',
            'game_key',
            'room_code',
            'current_question_id',
            'current_round_id',
            'current_set_id',
            'phase',
            'starts_at',
            'ends_at',
            'server_now',
        ].forEach(function (field) {
            if (snapshot[field] !== undefined && snapshot[field] !== null) {
                next[field] = snapshot[field];
            }
        });
        if (
            snapshot.server_now
            && (
                snapshot.server_now !== previous?.server_now
                || !Number.isFinite(previous?.serverNowMillis)
                || !Number.isFinite(previous?.receivedMonotonicMillis)
            )
        ) {
            const serverMillis = Date.parse(snapshot.server_now);
            if (Number.isFinite(serverMillis)) {
                next.serverNowMillis = serverMillis;
                next.receivedMonotonicMillis = performance.now();
            }
        }
        const previousEnd = Date.parse(previous?.ends_at || '');
        const candidateEnd = Date.parse(next.ends_at || '');
        if (
            Number.isFinite(previousEnd)
            && Number.isFinite(candidateEnd)
            && candidateEnd > previousEnd
            && estimatedServerNow(previous) >= previousEnd
            && isSameTimerContext(previous, next)
        ) {
            next.ends_at = previous.ends_at;
        }
        return next;
    }

    function conflictsAtSameRevision(previous, candidate) {
        return [
            'game_id',
            'current_question_id',
            'current_round_id',
            'current_set_id',
            'phase',
            'starts_at',
            'ends_at',
        ].some(function (field) {
            return previous[field] !== undefined
                && candidate[field] !== undefined
                && String(previous[field]) !== String(candidate[field]);
        });
    }

    function isFinalParticipantAction(payload) {
        const type = String(payload?.type || '');
        if (!type || nonFinalMessages.has(type)) return false;
        return type.startsWith('participant_')
            || type === 'tutorial_completed';
    }

    function install(socket) {
        const state = {
            state_revision: -1,
            heartbeat: null,
        };
        socketState.set(socket, state);

        socket.addEventListener('message', function (event) {
            let payload;
            try {
                payload = JSON.parse(event.data);
            } catch (error) {
                return;
            }
            const incomingRevision = Number(payload?.state_revision);
            if (Number.isFinite(incomingRevision)) {
                if (incomingRevision < Number(state.state_revision ?? -1)) {
                    event.stopImmediatePropagation();
                    return;
                }
                const candidate = contextFrom(payload, state);
                if (
                    incomingRevision === Number(state.state_revision)
                    && conflictsAtSameRevision(state, candidate)
                ) {
                    event.stopImmediatePropagation();
                    return;
                }
                Object.assign(state, candidate);
                const identity = identityOf(payload);
                if (identity) {
                    const known = latestByIdentity.get(identity);
                    if (known && incomingRevision < known.state_revision) {
                        event.stopImmediatePropagation();
                        return;
                    }
                    latestByIdentity.set(identity, Object.assign({}, state));
                }
            }
        }, true);

        const nativeSend = socket.send.bind(socket);
        socket.send = function (raw) {
            let payload;
            try {
                payload = JSON.parse(raw);
            } catch (error) {
                nativeSend(raw);
                return;
            }
            if (isFinalParticipantAction(payload) && Number(state.state_revision) < 0) {
                nativeSend(JSON.stringify({type: 'snapshot_request'}));
                return;
            }
            if (isFinalParticipantAction(payload)) {
                payload = Object.assign({}, payload, {
                    game_id: state.game_id ?? null,
                    question_id: state.current_question_id ?? null,
                    round_id: state.current_round_id ?? null,
                    set_id: state.current_set_id ?? null,
                    state_revision: state.state_revision,
                    client_action_id: payload.client_action_id || makeActionId(),
                });
            }
            nativeSend(JSON.stringify(payload));
        };

        socket.addEventListener('open', function () {
            state.heartbeat = window.setInterval(function () {
                if (socket.readyState === NativeWebSocket.OPEN) {
                    nativeSend(JSON.stringify({type: 'heartbeat'}));
                }
            }, 15000);
        });
        socket.addEventListener('close', function () {
            if (state.heartbeat) window.clearInterval(state.heartbeat);
            state.heartbeat = null;
        });
        return socket;
    }

    function WrappedWebSocket(url, protocols) {
        const socket = protocols === undefined
            ? new NativeWebSocket(url)
            : new NativeWebSocket(url, protocols);
        return install(socket);
    }
    WrappedWebSocket.prototype = NativeWebSocket.prototype;
    Object.defineProperties(WrappedWebSocket, {
        CONNECTING: {value: NativeWebSocket.CONNECTING},
        OPEN: {value: NativeWebSocket.OPEN},
        CLOSING: {value: NativeWebSocket.CLOSING},
        CLOSED: {value: NativeWebSocket.CLOSED},
    });

    window.WebSocket = WrappedWebSocket;
    window.AuthoritativeGameState = {
        acceptSnapshot: function (payload) {
            const revision = Number(payload?.state_revision);
            if (!Number.isFinite(revision)) return true;
            const identity = identityOf(payload);
            if (!identity) return true;
            const known = latestByIdentity.get(identity);
            if (known && revision < known.state_revision) return false;
            const candidate = contextFrom(payload, known);
            if (
                known
                && revision === Number(known.state_revision)
                && conflictsAtSameRevision(known, candidate)
            ) {
                return false;
            }
            latestByIdentity.set(identity, candidate);
            return true;
        },
        remainingMilliseconds: function (payload) {
            const identity = identityOf(payload);
            const context = contextFrom(payload, latestByIdentity.get(identity) || {});
            if (identity) {
                latestByIdentity.set(identity, context);
            }
            const endMillis = Date.parse(context.ends_at || '');
            if (!Number.isFinite(endMillis)) return 0;
            const serverNow = estimatedServerNow(context);
            return Math.max(0, endMillis - serverNow);
        },
        createActionId: makeActionId,
    };
}());
