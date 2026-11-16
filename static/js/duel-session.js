(() => {
  const root = document.getElementById('duel-session');
  if (!root) return;
  const role=root.dataset.role, code=root.dataset.code, api=root.dataset.api;
  const key=`hub_lobby_rejoin:${code}`, error=document.getElementById('error');
  let credential=null, loaded=false, sending=false;
  try { credential=JSON.parse(localStorage.getItem(key)||'null'); } catch (_) {}
  const csrf=()=>document.querySelector('[name=csrfmiddlewaretoken]').value;
  async function post(url, data) {
    const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:JSON.stringify(data)});
    const result=await response.json();
    if (!response.ok || result.success===false || result.error) throw new Error(result.error?.code||result.error||result.message||'Aktion fehlgeschlagen.');
    return result;
  }
  const showError=e=>error.textContent=e.message||String(e);
  const el=(tag,text)=>{const e=document.createElement(tag); if(text)e.textContent=text;return e;};
  function renderSteps(steps) {
    const list=document.getElementById('steps'); list.replaceChildren();
    steps.forEach(step=>{
      const row=el('section');row.className='duel-slot';row.dataset.id=step.id;
      row.append(el('h3',step.title));
      const mode=el('select');mode.className='match-mode';mode.setAttribute('aria-label','Matchmodus');
      [['','Bitte auswählen'],['best_of_n','Best of N'],['target_score','Zielpunktzahl']].forEach(([value,text])=>{const o=el('option',text);o.value=value;mode.append(o);});
      if(step.game_key==='host_points')mode.querySelector('[value=best_of_n]').disabled=true;
      mode.value=step.config.mode||'';
      const n=el('input');n.type='number';n.min='1';n.className='match-value';n.setAttribute('aria-label','Rundenzahl oder Zielpunkte');n.value=step.config.best_of??step.config.target_score??'';
      mode.disabled=n.disabled=step.status!=='planned';
      const start=el('button',step.status==='planned'?'Spiel starten':'Duell öffnen');start.className='start-slot';
      start.onclick=async()=>{if(sending)return;sending=true;start.disabled=true;error.textContent='';try{
        const result=await post(`/hub/api/session/${code}/activate-game/`,{step_id:step.id,game_key:step.game_key,room_code:step.room_code});
        if(result.host_url)location.assign(result.host_url);
      }catch(e){showError(e);}finally{sending=false;start.disabled=false;}};
      row.append(mode,n,start);list.append(row);
    });
  }
  if(role==='host'){
    document.getElementById('activate-session').onclick=()=>post(api,{action:'activate'}).then(()=>poll()).catch(showError);
    document.querySelectorAll('[data-check]').forEach(b=>b.onclick=()=>post(`/hub/api/session/${code}/check-in/${b.dataset.check}/`,{}).then(poll).catch(showError));
    document.getElementById('save-plan').onclick=async()=>{const configs={};document.querySelectorAll('.duel-slot').forEach(row=>{
      const mode=row.querySelector('.match-mode').value, value=Number(row.querySelector('.match-value').value);
      configs[row.dataset.id]=mode?{mode,[mode==='best_of_n'?'best_of':'target_score']:value}:{};
    });try{await post(api,{action:'save_plan',configs});error.textContent='Konfiguration gespeichert.';loaded=false;await poll();}catch(e){showError(e);}};
  }
  if(role==='participant'){
    const joinForm=document.getElementById('join-form'), joined=document.getElementById('joined');
    const restore=()=>{joinForm.hidden=!!credential;joined.hidden=!credential;};restore();
    document.getElementById('nickname').value=credential?.nickname||new URLSearchParams(location.search).get('nickname')||'';
    joinForm.onsubmit=async e=>{e.preventDefault();try{
      const nickname=document.getElementById('nickname').value;
      const result=await post(api,{action:'join',nickname,rejoin_token:credential?.token||''});
      credential={nickname:result.nickname,token:result.rejoin_token};localStorage.setItem(key,JSON.stringify(credential));restore();await poll();
    }catch(e){showError(e);}};
    document.getElementById('ready').onclick=()=>post(`/hub/api/session/${code}/check-in/participant/`,{nickname:credential.nickname,rejoin_token:credential.token}).then(()=>{error.textContent='Bereitschaft bestätigt.';}).catch(showError);
    // Reconfirm the saved signed identity instead of merely trusting a name.
    if(credential)post(api,{action:'join',nickname:credential.nickname,rejoin_token:credential.token}).catch(e=>{credential=null;restore();showError(e);});
  }
  let polling=false;
  async function poll(){if(polling)return;polling=true;try{
    const response=await fetch(api,{cache:'no-store'});const state=await response.json();if(!response.ok)throw Error('Session nicht erreichbar.');
    document.getElementById('roster').textContent='Teilnehmer: '+state.participants.map(p=>p.nickname).join(' · ');
    if(role==='host'&&!loaded){renderSteps(state.steps);loaded=true;}
    if(role==='participant'&&credential&&state.match_url)location.assign(state.match_url);
    if(role==='spectator'&&state.spectator_url)location.assign(state.spectator_url);
  }catch(e){showError(e);}finally{polling=false;}}
  poll();setInterval(poll,700);
})();
