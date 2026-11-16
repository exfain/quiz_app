(() => {
  const pair=document.getElementById('tournament-pair'),root=document.getElementById('duel-match');if(!pair||!root)return;
  document.body.dataset.tournamentPair='true';let polling=false;
  const duration=document.getElementById('duration');
  if(pair.dataset.role==='host'&&duration&&pair.dataset.duration){duration.value=pair.dataset.duration;duration.readOnly=true;}
  const messages={tournament_reveal_locked:'Andere Paarungen können diese Aufgabe noch beantworten. Die öffentliche Freigabe bleibt gesperrt.',
    tournament_material_fixed:'Die Aufgabe wird gemeinsam über das Turnier ausgelost.',stale_tournament_set:'Diese Auslosung ist beendet.',
    tournament_duration_fixed:'Verwende die im Turnierset gespeicherte Antwortdauer.'};
  const error=document.getElementById('error');new MutationObserver(()=>{const text=error.textContent;if(messages[text])error.textContent=messages[text];}).observe(error,{childList:true,characterData:true,subtree:true});
  // Running encounters already poll their own authoritative state. They only
  // need the group router after completion, when a new draw can change seats.
  // Host/spectator pair tabs use the explicit overview link, without duplicating
  // its full, cross-encounter snapshot on every active device.
  async function poll(){if(polling||pair.dataset.role!=='participant'||root.dataset.phase!=='completed')return;polling=true;try{
    let token=null;try{token=JSON.parse(localStorage.getItem('hub_lobby_rejoin:'+root.dataset.code)||'null')?.token;}catch(_){}
    const response=await fetch(pair.dataset.api,{headers:pair.dataset.role==='participant'&&token?{Authorization:'Bearer '+token}:{},cache:'no-store'});
    if(!response.ok)return;const data=await response.json();
    if(pair.dataset.role==='participant'&&data.status==='running'&&data.own_instance_id!==root.dataset.instance){location.replace(data.own_url||pair.dataset.url);return;}
    if(data.status==='completed')pair.querySelector('[data-message]').textContent='Turnier beendet. Über die Übersicht geht es zurück zur Lobby.';
    else pair.querySelector('[data-message]').textContent='Turnierset '+data.set_number+' · öffentliche Auflösung erst, wenn alle Paarungen nicht mehr antworten können.';
  }catch(_){}finally{polling=false;}}
  poll();setInterval(poll,1000);
})();
