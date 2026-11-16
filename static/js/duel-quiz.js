(() => {
  const root = document.getElementById('duel-match'); if (!root) return;
  const role = root.dataset.role, el = id => document.getElementById(id);
  const show = (id, visible) => { if (el(id)) el(id).hidden = !visible; };
  let unitId = null, selected = null, clock = null, current = null;
  const messages = {
    intro_not_finished: 'Das Spielintro läuft noch.', invalid_phase: 'Diese Freigabe ist noch nicht möglich.',
    answer_deadline_passed: 'Die Antwortfrist ist abgelaufen.', answering_not_open: 'Die Antwortphase ist geschlossen.',
    already_committed: 'Deine Antwort ist bereits verbindlich eingeloggt.', buzz_already_taken: 'Das Antwortrecht wurde bereits vergeben.',
    invalid_answer_right: 'Dieser Antwortversuch ist nicht mehr zur Bewertung offen.', unit_not_finished: 'Schließe zuerst die aktuelle Präsentation ab.',
    stale_unit: 'Der Rundenstand hat sich geändert.', match_needs_resolution: 'Beide Parteien haben das Punkteziel erreicht. Die Matchentscheidung ist noch offen.',
    match_closed: 'Das Match ist bereits abgeschlossen.', invalid_option: 'Wähle eine der angebotenen Antworten.',
    question_not_in_quiz: 'Diese Frage gehört nicht zum geplanten Quiz.', unauthorized: 'Bitte über die Lobby neu beitreten.',
    invalid_question: 'Die Frage enthält keine gültige richtige Antwort. Bitte prüfe die Fragenkonfiguration.',
    invalid_state: 'Die Frage oder der gespeicherte Rundenstand ist ungültig. Bitte prüfe die Konfiguration.',
    invalid_request: 'Die übermittelten Eingaben sind ungültig.', session_closed: 'Diese Session ist nicht mehr aktiv.',
    action_id_conflict: 'Die Aktionskennung wurde mit anderem Inhalt verwendet.'
  };
  function score(tally, data) {
    if (!tally) return;
    el('score').replaceChildren();
    for (const side of ['a', 'b']) {
      const p = tally[`party_${side}`], line = document.createElement('p');
      line.textContent = `${data.names[side]}: ${p.score} Punkte · ${p.round_wins} Rundensiege`; el('score').append(line);
      if (tally.target_score) {
        const progress = document.createElement('progress'); progress.max = tally.target_score;
        progress.value = Math.max(0, Math.min(tally.target_score, p.score));
        progress.setAttribute('aria-label', `${data.names[side]}: ${p.score} von ${tally.target_score}`); el('score').append(progress);
      }
    }
    if (tally.best_of) { const p = document.createElement('p'); p.textContent = `Best of ${tally.best_of}: ${tally.required_wins} Rundensiege zum Matchsieg.`; el('score').append(p); }
    if (tally.winner_id) el('result').textContent += ` Matchsieg: ${tally.winner_id === tally.party_a.participant_id ? data.names.a : data.names.b}.`;
  }
  function tick() {
    if (!clock) return;
    const {state: s, received} = clock, now = Date.parse(s.server_now) + performance.now() - received;
    const remaining = Math.max(0, Math.ceil((Date.parse(s.deadline) - now) / 1000));
    el('timer').textContent = s.phase === 'answering' ? `${remaining} Sekunden` : '';
    const chars = Array.from(s.question || '');
    const revealing = s.question_type === 'short_answer' && ['answering', 'awaiting_decision'].includes(s.phase);
    const count = s.frozen_characters ?? Math.max(0, Math.floor((now - Date.parse(s.started_at)) / s.reveal_ms));
    el('question').textContent = revealing ? chars.slice(0, count).join('') : chars.join('');
    if (role === 'participant' && s.phase === 'answering' && !remaining) {
      el('buzz').disabled = el('commit').disabled = true;
      el('options').querySelectorAll('button').forEach(b => b.disabled = true);
    }
  }
  function render(data, busy) {
    current = data; const s = data.unit, phase = s?.phase || 'waiting';root.dataset.phase = phase;
    el('names').textContent = `${data.names.a} gegen ${data.names.b}`;
    el('phase').textContent = ({waiting: 'Warte auf die erste Frage.', prepared: 'Frage vorbereitet – warte auf den Host.',
      answering: 'Antwortphase läuft.', awaiting_decision: 'Antwortrecht vergeben – mündliche Antwort und Hostbewertung.',
      awaiting_reveal: 'Antwortphase geschlossen – warte auf die Freigabe.', show_answers: 'Antworten aufgedeckt.',
      show_solution: 'Lösung aufgedeckt.', show_result: 'Ergebnis veröffentlicht.',
      completed: data.match_status === 'completed' ? 'Match beendet.' : 'Runde abgeschlossen.'})[phase];
    show('waiting', phase === 'waiting' || phase === 'prepared'); show('question-card', !!s && phase !== 'prepared');
    if (unitId !== s?.unit_id) {
      unitId = s?.unit_id; selected = null;
      ['answers', 'solution', 'result', 'score'].forEach(id => el(id).replaceChildren());
      if (role === 'participant') {
        el('options').replaceChildren();
      }
    }
    clock = s ? (clock?.state === s ? clock : {state: s, received: performance.now()}) : null; tick();
    el('answer-right').textContent = s?.buzz_participant_id ? `Antwortrecht: ${s.buzz_participant_id === data.parties.a ? data.names.a : data.names.b}` : '';
    if (role === 'participant') {
      show('buzz', s?.question_type === 'short_answer' && ['answering', 'awaiting_decision'].includes(phase));
      el('buzz').disabled = !s?.can_buzz || busy;
      show('answer-form', !!s && s.question_type !== 'short_answer' && phase === 'answering');
      // Prepared projections deliberately omit options; build only when public.
      if (s?.options?.length && !el('options').children.length) for (const option of s.options) {
        const button = document.createElement('button'); button.type = 'button'; button.dataset.value = option.key; button.textContent = option.text;
        button.onclick = () => { selected = option.key; render(current, false); };
        el('options').append(button);
      }
      if (s?.own_answer) selected = s.own_answer;
      for (const button of el('options').children) { button.disabled = !s?.can_answer || busy; button.setAttribute('aria-pressed', String(button.dataset.value === selected)); }
      el('commit').disabled = !s?.can_answer || !selected || busy;
      el('own-status').textContent = s?.committed ? 'Deine Antwort ist eingeloggt.' : '';
    }
    show('answers-card', !!s?.answers);
    if (s?.answers) el('answers').textContent = `${data.names.a}: ${s.answers.a?.formatted ?? 'Keine Antwort'}\n${data.names.b}: ${s.answers.b?.formatted ?? 'Keine Antwort'}`;
    show('solution-card', s?.solution !== undefined); if (s?.solution !== undefined) el('solution').textContent = s.solution;
    show('result-card', !!s?.points);
    if (s?.points) {
      const winner = s.round_outcome === 'party_a_wins' ? data.names.a : data.names.b;
      el('result').textContent = s.round_outcome === 'draw' ? 'Beide richtig: beide +1 Punkt, kein Rundensieger.' : s.round_outcome === 'no_winner' ? 'Keine richtige Antwort: keine Punkte, kein Rundensieger.' : `${winner} gewinnt die Runde. Punkte: ${s.points.a} / ${s.points.b}.`;
      if (s.verdict) el('result').textContent = `Hostbewertung: ${s.verdict === 'correct' ? 'richtig' : 'falsch'}. ` + el('result').textContent;
    }
    const tally = data.match || s?.match; score(tally, data);
    if (role === 'host') {
      const select = el('question-select');
      if (!select.dataset.loaded) { for (const q of data.questions || []) { const option = document.createElement('option'); option.value = q.id; option.textContent = q.question_text; select.append(option); } select.dataset.loaded = 'true'; }
      const prepare = data.match_status === 'in_progress' && (!s || phase === 'completed');
      show('prepare', prepare); show('question-select', prepare);
      for (const [action, source] of Object.entries({start: 'prepared', show_answers: 'awaiting_reveal', show_solution: 'show_answers', show_result: 'show_solution', complete: 'show_result'})) show(action, source === phase);
      show('judgment', phase === 'awaiting_decision'); if (s?.host_solution) el('host-solution').textContent = `Lösung (nur Host): ${s.host_solution}`;
    }
    show('return', phase === 'completed' && data.match_status === 'completed');
    tick();
  }
  const client = window.createDuelClient(root, render, messages); if (!client) return;
  if (role === 'host') {
    el('prepare').onclick = () => client.command(root.dataset.api, {action: 'prepare', question_id: Number(el('question-select').value), previous_unit_id: current?.unit?.unit_id ?? null});
    for (const action of ['start', 'show_answers', 'show_solution', 'show_result', 'complete']) el(action).onclick = () => client.command(current?.unit_api, {action});
    for (const verdict of ['correct', 'incorrect']) el(verdict).onclick = () => client.command(current?.unit_api, {action: 'judge', buzz_id: current.unit.buzz_id, verdict});
  }
  if (role === 'participant') {
    el('buzz').onclick = () => client.command(current?.unit_api, {action: 'buzz'});
    el('answer-form').onsubmit = e => { e.preventDefault(); if (current?.unit?.can_answer && selected) client.command(current.unit_api, {action: 'answer', payload: {option_id: selected}}); };
  }
  client.poll(); setInterval(client.poll, 400); setInterval(tick, 50);
})();
