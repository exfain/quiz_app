/* Shared authenticated HTTP/poll boundary. No game rules or reveal permissions. */
window.createDuelClient = function (root, render, messages) {
  const role = root.dataset.role, code = root.dataset.code;
  let credential = null;
  try { credential = JSON.parse(localStorage.getItem(`hub_lobby_rejoin:${code}`) || 'null'); } catch (_) {}
  const error = document.getElementById('error');
  const report = code => { error.textContent = messages[code] || code; };
  if (role === 'participant' && !credential) { location.replace(`/hub/lobby/${code}/`); return null; }
  const headers = () => ({'Content-Type': 'application/json',
    'X-CSRFToken': root.querySelector('[name=csrfmiddlewaretoken]').value,
    ...(role === 'participant' ? {Authorization: `Bearer ${credential.token}`} : {})});
  let revision = -1, pending = null, busy = false, polling = false, state = null;
  let serial = 0, appliedSerial = 0;
  function apply(data, requestSerial) {
    if (data.instance_id !== root.dataset.instance || data.state_revision < revision ||
        (data.state_revision === revision && requestSerial < appliedSerial)) return;
    revision = data.state_revision; appliedSerial = Math.max(appliedSerial, requestSerial); state = data;
    render(data, busy);
    window.DuelMatchStatus?.render(data);
    window.sessionGameStartIntro?.handleState({intro: data.intro});
  }
  async function poll() {
    if (polling) return;
    polling = true;
    const requestSerial = ++serial;
    try {
      const response = await fetch(root.dataset.api, {headers: headers(), cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw Error(data.error?.code || 'Abruf fehlgeschlagen.');
      apply(data, requestSerial);
    } catch (e) {
      report(e.message);
      if (role === 'participant' && e.message === 'unauthorized') {
        localStorage.removeItem(`hub_lobby_rejoin:${code}`); location.replace(`/hub/lobby/${code}/`);
      }
    } finally { polling = false; }
  }
  async function command(url, body) {
    if (busy || !url) return;
    busy = true; error.textContent = '';
    if (state) render(state, true);
    const requestSerial = ++serial;
    const fingerprint = JSON.stringify([url, body]);
    if (!pending || pending.fingerprint !== fingerprint) pending = {fingerprint, id: window.AuthoritativeGameState.createActionId()};
    try {
      const response = await fetch(url, {method: 'POST', headers: headers(), body: JSON.stringify({...body, client_action_id: pending.id})});
      const data = await response.json();
      // A confirmed commit must stay locked even while an older GET is in
      // flight. Use the SAME role-filtered persisted-phase server projection,
      // never locally inferred answer/reveal state or arbitrary receipt data.
      if (data.snapshot && state && data.snapshot.unit_id === state.unit?.unit_id) {
        apply({...state, unit: data.snapshot, state_revision: data.snapshot.state_revision,
          match: data.snapshot.match || null,
          match_status: data.snapshot.match?.match_status || state.match_status}, requestSerial);
      } else if (data.instance_id) apply(data, requestSerial);
      if (!response.ok) {
        if (response.status < 500) pending = null;
        throw Error(data.action?.code || data.error?.code || 'Aktion fehlgeschlagen.');
      }
      pending = null;
    } catch (e) { report(e.message); }
    finally { busy = false; if (state) render(state, false); await poll(); }
  }
  async function readImage(url) {
    const response = await fetch(url, {headers: headers(), cache: 'no-store'});
    if (!response.ok) throw Error('Das Rundenbild konnte nicht geladen werden.');
    return response.blob();
  }
  return {poll, command, readImage, get state() { return state; }};
};
