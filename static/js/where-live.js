(function () {
    'use strict';
    const config = JSON.parse(document.getElementById('whereConfig').textContent), host = !!config.host;
    const el = id => document.getElementById(id);
    let socket, state, draft, sending = false, endedRound = '', offset = 0, finalAcknowledged = '';
    let pendingStart = null, startTimer = null;
    function finishStart(message) {
        clearTimeout(startTimer); pendingStart = null;
        if (message) error(message);
        if (state) render();
    }
    function startQuiz() {
        if (pendingStart) return;
        pendingStart = window.AuthoritativeGameState.createActionId();
        el('startQuizBtn').disabled = true;
        error('');
        if (!send({type: 'admin_start_quiz', start_request_id: pendingStart})) {finishStart(); return;}
        startTimer = setTimeout(() => finishStart('Keine Startbestätigung erhalten. Bitte den Spielstand prüfen und erneut versuchen.'), 10000);
    }
    const map = new WhereMap(el('whereMap'), {onSelect(point) {
        draft = {...point, question_id: state.current_question_id, round_id: state.current_round_id};
        send({type: 'participant_input_changed', ...draft}); render();
    }});
    function send(data) {
        if (!socket || socket.readyState !== WebSocket.OPEN) {error('Verbindung unterbrochen. Bitte auf die Wiederverbindung warten.'); return false;}
        const finalContext = data.type === 'participant_submit_answer' ? {
            game_id: state?.game_id, state_revision: state?.state_revision,
            client_action_id: window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`
        } : {};
        socket.send(JSON.stringify({hub_session: config.code, question_id: state?.current_question_id,
            round_id: state?.current_round_id, ...finalContext, ...data})); return true;
    }
    function error(message) {el('whereError').hidden = !message; el('whereError').textContent = message || '';}
    function connect() {
        socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/where/${config.room}/`);
        socket.onopen = () => {error(''); send({type: host ? 'host_join' : 'participant_join', participant_name: config.name, player_token: config.token});};
        socket.onclose = () => {if (host) finishStart(); error('Verbindung unterbrochen. Wiederverbindung läuft …'); sending = false; setTimeout(connect, 1500);};
        socket.onmessage = event => {
            const data = JSON.parse(event.data);
            if (data.type === 'where_state') {
                const changed = String(state?.current_round_id) !== String(data.current_round_id) || state?.current_question_id !== data.current_question_id;
                if (changed || data.revealed) draft = null;
                state = data; offset = Date.parse(data.server_now) - Date.now();
                if (draft && !data.own_answer && data.can_answer) send({type: 'participant_input_changed', ...draft});
                if (data.own_answer || !data.can_answer) sending = false;
                if (!host) window.sessionGameStartIntro?.handleState(data);
                render();
            } else if (data.type === 'host_start_confirmed' && data.start_request_id === pendingStart) {finishStart();}
            else if (data.type === 'error' || data.type === 'action_rejected') {if (host) finishStart(); error(data.message); sending = false; endedRound = ''; send({type: 'get_state'});}
            else if (data.type === 'input_saved' && draft && String(data.round_id) === String(draft.round_id)
                    && Math.abs(Number(data.x_norm) - draft.x_norm) < 1e-10 && Math.abs(Number(data.y_norm) - draft.y_norm) < 1e-10) draft = null;
        };
    }
    function rows(container, headings, values) {
        if (!container) return;
        const table = document.createElement('table'), head = table.createTHead().insertRow();
        for (const text of headings) {const th = document.createElement('th'); th.textContent = text; head.append(th);}
        const body = table.createTBody(); for (const row of values) {const tr = body.insertRow(); for (const text of row) tr.insertCell().textContent = text;}
        container.replaceChildren(table);
    }
    function render() {
        if (!state) return;
        const q = state.question, status = state.game.status, reveal = state.revealed;
        if (!host) {
            el('whereWaiting').hidden = !!q || status === 'completed';
            el('whereQuestion').hidden = !q;
        }
        el('questionText').textContent = q?.text || '';
        el('questionProgress').textContent = `${state.progress.current || 0} / ${state.progress.total || 0}`;
        el('questionImage').hidden = !q?.image_url;
        if (q?.image_url) el('questionImage').src = q.image_url;
        el('whereMessage').textContent = status === 'completed' ? (reveal ? 'Spiel beendet – Auflösung der letzten Frage' : 'Gesamtergebnis') : status === 'inactive' ? 'Spiel pausiert' : reveal ? 'Gemeinsame Auflösung' : state.own_answer ? 'Markierung eingeloggt. Warte auf die gemeinsame Auflösung.' : q ? 'Markiere den gesuchten Ort auf der Karte.' : 'Warte auf die nächste Frage.';
        el('revealExplanation').textContent = reveal ? q?.explanation || '' : '';
        map.update(state, {ownName: config.name, spectator: host, draft});
        if (host) {
            window.adminGameMonitor.isActive = status === 'active';
            el('responseCount').textContent = `${state.response_count} / ${state.participants.length} Antworten`;
            el('startQuizBtn').disabled = !!pendingStart || el('startQuizBtn').dataset.hostStartChecking === '1' || !['waiting', 'inactive'].includes(status);
            el('startQuizBtn').dataset.hostGameResume = String(status === 'inactive');
            el('setInactiveBtn').disabled = status !== 'active'; el('endQuizBtn').disabled = !['active', 'inactive'].includes(status);
            el('sendQuestionBtn').disabled = status !== 'active' || (!!q && !reveal);
            el('endQuestionBtn').disabled = status !== 'active' || !q || reveal;
            document.body.dataset.hostGameStatus = status;
            if (status === 'completed') window.hostEndState?.apply({});
            window.hostGameLeaveGuard?.stateChanged();
            if (reveal && el('questionSelect').dataset.lastRevealed !== String(state.current_round_id)) {
                const played = new Set(state.scoreboard.map(row => String(row.question_id)));
                const next = [...el('questionSelect').options].find(o => !played.has(o.value));
                if (next) el('questionSelect').value = next.value;
                el('questionSelect').dataset.lastRevealed = String(state.current_round_id);
            }
        } else {
            el('submitAnswerBtn').disabled = sending || !state.can_answer || !(draft || state.pending_input);
            const own = state.answers.find(a => a.participant_name === config.name);
            el('answerFeedback').textContent = reveal ? own ? `${own.formatted_distance} · ${own.points_earned} Punkte` : 'Keine Markierung abgegeben · 0 Punkte' : '';
            el('continueRevealBtn').hidden = !(status === 'completed' && reveal && finalAcknowledged !== String(state.current_round_id));
            if (status === 'completed' && finalAcknowledged === String(state.current_round_id)) {
                el('whereMessage').textContent = 'Gesamtergebnis'; el('whereMap').hidden = true;
            } else el('whereMap').hidden = false;
            rows(el('whereScoreboard'), ['Frage', 'Punkte'], state.scoreboard.map(row => [row.number, row.points === null ? '–' : `${row.points} / ${row.max_points}`]));
            window.setReturnToLobbyAvailable?.(status === 'completed');
        }
        rows(el('whereLeaderboard'), ['Rang', 'Team', 'Punkte'], state.participants.map(p => [p.rank, p.name, p.score]));
        window.AuthoritativeGameState?.markInitialPaintReady?.();
        tick();
    }
    function tick() {
        if (!state) return;
        const remaining = state.game.status === 'inactive' ? state.remaining_seconds : Math.max(0, Math.ceil((Date.parse(state.ends_at) - Date.now() - (offset || 0)) / 1000));
        el('timeLeft').textContent = state.question && !state.revealed ? `${remaining || 0} s` : '';
        if (!host && remaining <= 0 && el('submitAnswerBtn')) {el('submitAnswerBtn').disabled = true; map.enabled = false;}
        if (host && state.game.status === 'active' && state.question && !state.revealed && remaining <= 0 && endedRound !== String(state.current_round_id)) {
            endedRound = String(state.current_round_id); send({type: 'admin_end_question'});
        }
    }
    el('zoomIn').onclick = () => map.zoom(.7); el('zoomOut').onclick = () => map.zoom(1 / .7); el('resetMap').onclick = () => map.reset();
    if (host) {
        window.adminGameMonitor = {
            isActive: false,
            get isUnfinished() {return !!state && (state.progress.current < state.progress.total || (!!state.question && !state.revealed));},
            endQuiz() {send({type: 'admin_end_quiz'});},
            setInactive() {send({type: 'admin_set_inactive'});},
        };
        el('startQuizBtn').onclick = startQuiz;
        el('sendQuestionBtn').onclick = () => send({type: 'admin_send_question', question_id: el('questionSelect').value});
        el('endQuestionBtn').onclick = () => send({type: 'admin_end_question'});
        el('endQuizBtn').onclick = () => send({type: 'admin_end_quiz'});
        el('setInactiveBtn').onclick = () => send({type: 'admin_set_inactive'});
    } else {
        el('submitAnswerBtn').onclick = () => {const point = draft || state?.pending_input; if (point) {sending = true; send({type: 'participant_submit_answer', ...point}); render();}};
        el('continueRevealBtn').onclick = () => {finalAcknowledged = String(state.current_round_id); render();};
    }
    setInterval(tick, 200); setInterval(() => {if (socket?.readyState === WebSocket.OPEN) send({type: 'get_state'});}, 2000);
    connect();
})();
