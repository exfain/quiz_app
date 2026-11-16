(() => {
  const root=document.getElementById('duel-match');if(!root)return;
  const role=root.dataset.role,code=root.dataset.code,api=root.dataset.api;
  let credential=null;try{credential=JSON.parse(localStorage.getItem(`hub_lobby_rejoin:${code}`)||'null');}catch(_){}
  if(role==='participant'&&!credential){location.replace(`/hub/lobby/${code}/`);return;}
  const el=id=>document.getElementById(id), show=(id,yes)=>{if(el(id))el(id).hidden=!yes;};
  const errors={intro_not_finished:'Das Spielintro läuft noch. Warte kurz mit dem Fragenstart.',invalid_request:'Prüfe die ausgewählte Frage und eine positive ganzzahlige Antwortdauer.',question_not_in_quiz:'Die gewählte Frage gehört nicht zu diesem Quiz.',invalid_phase:'Diese Freigabe ist in der aktuellen Phase noch nicht möglich.',answer_deadline_passed:'Die Antwortfrist ist abgelaufen.',answering_not_open:'Die Antwortphase ist geschlossen.',already_committed:'Deine Antwort ist bereits verbindlich eingeloggt.',unit_not_finished:'Schließe zuerst die aktuelle Präsentation ab.',stale_unit:'Der Rundenstand hat sich geändert. Bitte erneut prüfen.',match_closed:'Das Match ist bereits abgeschlossen.',unauthorized:'Bitte über die Lobby neu beitreten.',action_id_conflict:'Diese Aktion wurde bereits mit anderem Inhalt verwendet.'};
  let current=null,revision=-1,polling=false,busy=false,timerDeadline=null,serverAt=0,receivedAt=0,lastUnit=null,pending=null;
  const headers=()=>({'Content-Type':'application/json','X-CSRFToken':el('duel-match').querySelector('[name=csrfmiddlewaretoken]').value,...(role==='participant'?{'Authorization':`Bearer ${credential.token}`}:{})});
  function render(data){
    if(data.instance_id!==root.dataset.instance||data.state_revision<revision)return;
    revision=data.state_revision;current=data;
    const s=data.unit,phase=s?.phase||'waiting';root.dataset.phase=phase;el('names').textContent=`${data.names.a} gegen ${data.names.b}`;
    el('phase').textContent=({waiting:'Warte auf die erste Frage.',prepared:'Frage vorbereitet – warte auf den Host.',answering:'Antwortphase läuft.',awaiting_reveal:'Antwortphase geschlossen – warte auf die Freigabe.',show_answers:'Antworten aufgedeckt.',show_solution:'Lösung aufgedeckt.',show_result:'Ergebnis veröffentlicht.',completed:data.match_status==='completed'?'Match beendet.':'Runde abgeschlossen.'})[phase];
    show('waiting',phase==='waiting'||phase==='prepared');show('question-card',!!s&&phase!=='prepared');
    if(s){el('question').textContent=s.question||'';el('unit-label').textContent=s.unit?.label||'';serverAt=Date.parse(s.server_now);receivedAt=performance.now();timerDeadline=phase==='answering'?Date.parse(s.deadline):null;}
    else timerDeadline=null;
    el('timer').textContent=phase==='answering'?'…':'';
    if(lastUnit!==s?.unit_id){if(el('estimate'))el('estimate').value='';['solution','answers','deviations','result','score'].forEach(id=>el(id).replaceChildren());lastUnit=s?.unit_id;pending=null;}
    if(role==='participant'){
      const allowed=!!s?.can_answer&&!busy;el('estimate').disabled=el('commit').disabled=!allowed;show('answer-form',phase==='answering');
      el('own-status').textContent=s?.committed?'Deine Antwort ist eingeloggt.':phase==='answering'?'':'Keine weitere Abgabe möglich.';
      if(s?.committed&&s.own_answer)el('estimate').value=s.own_answer.value;
    }
    show('answers-card',!!s?.answers);if(s?.answers)el('answers').textContent=`${data.names.a}: ${s.answers.a?.formatted??'Keine Antwort'}\n${data.names.b}: ${s.answers.b?.formatted??'Keine Antwort'}`;
    show('solution-card',!!s?.target);if(s?.target){el('solution').textContent=s.target.formatted;el('deviations').textContent=`Abweichung ${data.names.a}: ${s.deviations.a??'–'} · ${data.names.b}: ${s.deviations.b??'–'}`;}
    show('result-card',!!s?.points);if(s?.points)el('result').textContent=s.round_outcome==='no_winner'?'Keine gültige Abgabe: 0:0, kein Rundensieger.':s.round_outcome==='draw'?`Gleiche Abweichung: beide +1 Punkt.`:`${s.round_outcome==='party_a_wins'?data.names.a:data.names.b} gewinnt die Runde. Punkte: ${s.points.a} / ${s.points.b}`;
    const tally=data.match||s?.match;if(tally){const score=el('score');score.replaceChildren();['a','b'].forEach(side=>{const p=tally[`party_${side}`];const line=document.createElement('p');line.textContent=`${data.names[side]}: ${p.score} Punkte · ${p.round_wins} Rundensiege`;score.append(line);if(tally.target_score){const progress=document.createElement('progress');progress.max=tally.target_score;progress.value=Math.max(0,Math.min(tally.target_score,p.score));progress.setAttribute('aria-label',`${data.names[side]}: ${p.score} von ${tally.target_score}`);score.append(progress);}});if(tally.best_of){const label=document.createElement('p');label.textContent=`Best of ${tally.best_of}: ${tally.required_wins} Rundensiege zum Matchsieg.`;score.append(label);}if(tally.winner_id){const winner=tally.winner_id===tally.party_a.participant_id?data.names.a:data.names.b;el('result').textContent+=` Matchsieg: ${winner}.`;}}
    if(role==='host'){
      const select=el('question-select');if(!select.dataset.loaded){(data.questions||[]).forEach(q=>{const o=document.createElement('option');o.value=q.id;o.textContent=q.question_text;select.append(o);});select.dataset.loaded='true';}
      const playable=data.match_status==='in_progress';show('prepare',playable&&(!s||phase==='completed'));show('question-select',playable&&(!s||phase==='completed'));
      ['start','show_answers','show_solution','show_result','complete'].forEach(action=>show(action,({start:'prepared',show_answers:'awaiting_reveal',show_solution:'show_answers',show_result:'show_solution',complete:'show_result'})[action]===phase));
      show('duration',phase==='prepared');
      show('host-controls',true);
    }
    show('return',phase==='completed'&&data.match_status==='completed');
    window.DuelMatchStatus?.render(data);
    window.sessionGameStartIntro?.handleState({intro:data.intro});
  }
  let requestSerial=0,appliedSerial=0;
  async function poll(){if(polling)return;polling=true;const serial=++requestSerial;try{
    const r=await fetch(api,{headers:headers(),cache:'no-store'}),data=await r.json();if(!r.ok)throw Error(data.error?.code||'Abruf fehlgeschlagen.');
    if(serial>=appliedSerial){appliedSerial=serial;render(data);}
  }catch(e){el('error').textContent=errors[e.message]||e.message;if(role==='participant'&&e.message==='unauthorized'){localStorage.removeItem(`hub_lobby_rejoin:${code}`);location.replace(`/hub/lobby/${code}/`);}}finally{polling=false;}}
  async function action(url,body){if(busy)return;busy=true;el('error').textContent='';
    // Preserve the logical action ID on a network retry of the same body.
    const fingerprint=JSON.stringify([url,body]);if(!pending||pending.fingerprint!==fingerprint)pending={fingerprint,id:window.AuthoritativeGameState.createActionId()};
    try{const r=await fetch(url,{method:'POST',headers:headers(),body:JSON.stringify({...body,client_action_id:pending.id})});const data=await r.json();
      if(!r.ok){pending=null;throw Error(data.action?.code||data.error?.code||'Aktion abgelehnt.');}pending=null;
    }catch(e){el('error').textContent=errors[e.message]||e.message;}finally{busy=false;await poll();}}
  if(role==='host'){
    el('prepare').onclick=()=>action(api,{action:'prepare',question_id:Number(el('question-select').value),previous_unit_id:current?.unit?.unit_id??null});
    ['start','show_answers','show_solution','show_result','complete'].forEach(name=>el(name).onclick=()=>{const body={action:name};if(name==='start')body.duration_seconds=Number(el('duration').value);action(current.unit_api,body);});
  }
  if(role==='participant')el('answer-form').onsubmit=e=>{e.preventDefault();if(current?.unit?.can_answer)action(current.unit_api,{payload:{value:el('estimate').value}});};
  setInterval(()=>{if(timerDeadline){const remaining=Math.max(0,Math.ceil((timerDeadline-(serverAt+performance.now()-receivedAt))/1000));el('timer').textContent=`${remaining} Sekunden`;if(!remaining&&role==='participant'){el('estimate').disabled=el('commit').disabled=true;}}},100);
  poll();setInterval(poll,500);
})();
