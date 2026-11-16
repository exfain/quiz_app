/* Explicit mixed-session encounters. Never infer a mode from player count. */
(() => {
  const root = document.getElementById('session-encounter');
  if (!root) return;
  const {code, role} = root.dataset;
  let polling = false, busy = false, rosterKey = '';
  const error = root.querySelector('[role=alert]');
  function credential() {
    try { return JSON.parse(localStorage.getItem(`hub_lobby_rejoin:${code}`) || 'null'); } catch (_) { return null; }
  }
  async function poll() {
    if (polling) return;
    const token = role === 'participant' ? credential()?.token : null;
    if (role === 'participant' && !token) return;
    polling = true;
    try {
      const response = await fetch(`/duel/api/encounter/${encodeURIComponent(code)}/${role}/`, {
        headers: token ? {Authorization: `Bearer ${token}`} : {}, cache: 'no-store'
      });
      if (!response.ok) return;
      const data = await response.json();
      if (role !== 'host' && data.url) { location.assign(data.url); return; }
      const watch = root.querySelector('[data-watch]');
      if (watch) { watch.hidden = !data.spectator_url; if (data.spectator_url) watch.href = data.spectator_url; }
      const result = root.querySelector('[data-result]');
      if (result) result.textContent = data.result?.draw ? 'Duell unentschieden. Das Ergebnis zählt nicht zur Abendwertung.' : data.result?.winner ? `Duellsieger: ${data.result.winner}. Das Ergebnis zählt nicht zur Abendwertung.` : '';
      const resultLink = root.querySelector('[data-result-link]');
      if (resultLink) { resultLink.hidden = !data.result; if (data.result) resultLink.href = data.result.url; }
      if (role !== 'host') return;
      const roster = JSON.stringify(data.participants);
      if (roster !== rosterKey) {
        rosterKey = roster;
        for (const select of document.querySelectorAll('.encounter-party')) {
          const selected = select.value;
          select.replaceChildren(new Option('Partei auswählen', ''));
          for (const party of data.participants) select.add(new Option(party.nickname, party.id));
          select.value = selected;
        }
      }
      const resume = root.querySelector('[data-resume]');
      resume.hidden = !data.url; if (data.url) resume.href = data.url;
      for (const button of document.querySelectorAll('.encounter-start,.tournament-start')) button.disabled = busy || !!data.url || !data.can_start;
    } catch (_) { /* A reconnect uses the same persisted encounter. */ }
    finally { polling = false; }
  }
  for (const button of document.querySelectorAll('.encounter-start')) button.addEventListener('click', async () => {
    if (busy) return;
    const form = button.closest('.encounter-config'), row = button.closest('[data-step-id]');
    const parties = [...form.querySelectorAll('.encounter-party')].map(s => Number(s.value));
    const mode = form.querySelector('.encounter-mode').value, value = Number(form.querySelector('.encounter-value').value);
    if (!parties.every(Number.isSafeInteger) || parties.some(id => id < 1) || parties[0] === parties[1] || !mode || !Number.isSafeInteger(value)) {
      error.textContent = 'Bitte zwei verschiedene Parteien und eine gültige Matchkonfiguration wählen.'; return;
    }
    busy = true; button.disabled = true; error.textContent = '';
    try {
      const response = await fetch(`/hub/api/session/${encodeURIComponent(code)}/activate-game/`, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': root.dataset.csrf},
        body: JSON.stringify({action: 'start_duel', step_id: Number(row.dataset.stepId), game_key: row.dataset.gameKey,
          room_code: row.dataset.roomCode, participant_ids: parties, match_config: {mode, [mode === 'best_of_n' ? 'best_of' : 'target_score']: value}})
      });
      const data = await response.json();
      if (!response.ok || !data.success) throw Error(data.error || 'Das Duell konnte nicht gestartet werden.');
      location.assign(data.host_url);
    } catch (e) { error.textContent = e.message; }
    finally { busy = false; button.disabled = false; }
  });
  for (const button of document.querySelectorAll('.tournament-start')) button.addEventListener('click', async () => {
    if (busy) return;
    busy=true;button.disabled=true;error.textContent='';
    const row=button.closest('[data-step-id]');
    try {
      const response=await fetch(`/hub/api/session/${encodeURIComponent(code)}/activate-game/`,{method:'POST',
        headers:{'Content-Type':'application/json','X-CSRFToken':root.dataset.csrf},
        body:JSON.stringify({action:'start_tournament',step_id:Number(row.dataset.stepId),game_key:row.dataset.gameKey,room_code:row.dataset.roomCode})});
      const data=await response.json();if(!response.ok||!data.success)throw Error(data.error||'Turnierstart fehlgeschlagen.');
      location.assign(data.host_url);
    } catch(e){error.textContent=e.message;} finally{busy=false;button.disabled=false;}
  });
  poll(); setInterval(poll, 1000);
})();
