(() => {
  const root=document.getElementById('duel-match');if(!root)return;
  const role=root.dataset.role,key=root.dataset.game,el=id=>document.getElementById(id),show=(id,yes)=>{if(el(id))el(id).hidden=!yes;};
  let current=null,identity=null,deadline=null,serverAt=0,receivedAt=0;
  const messages={missing_bids:'Mindestens ein Gebot fehlt. Dafür ist noch keine Wertung festgelegt. Die Runde bleibt ohne Punkte und ohne Antworttimer gesperrt.',
    stale_counter:'Diese Bestätigung wurde bereits verarbeitet. Bitte den aktuellen Stand prüfen.',intro_not_finished:'Das Spielintro läuft noch.',
    invalid_phase:'Diese Aktion passt nicht zur aktuellen Phase.',invalid_task:'Die Aufgabe ist nicht verfügbar.',invalid_request:'Bitte eine positive ganze Zahl beziehungsweise die benötigten Felder eingeben.',
    bid_deadline_passed:'Die Gebotsfrist ist abgelaufen.',bidding_closed:'Die Gebotsphase ist geschlossen.',already_committed:'Dein Gebot ist bereits verbindlich gespeichert.',
    party_blocked:'Du bist für diese Runde gesperrt.',buzzer_closed:'Der Buzzer ist nicht geöffnet.',invalid_attempt:'Dieser Antwortversuch ist nicht mehr aktuell.',
    invalid_delta:'Eine von null verschiedene ganzzahlige Punkteänderung ist erforderlich.',invalid_party:'Diese Partei gehört nicht zum Match.',
    match_closed:'Das Match ist abgeschlossen.',stale_unit:'Diese Runde ist nicht mehr aktuell.',action_id_conflict:'Die Aktionskennung wurde mit anderem Inhalt verwendet.',unit_not_finished:'Bitte zuerst die Präsentation abschließen.'};
  const name=id=>id===current?.parties.a?current.names.a:id===current?.parties.b?current.names.b:'Keine Partei';
  function line(parent,tag,value){const n=document.createElement(tag);n.textContent=value;parent.append(n);return n;}
  function host(action,options={}){return client.command(current?.unit_api,{action,...options});}
  function render(data,busy){
    current=data;const s=data.unit,phase=s?.phase||'waiting';root.dataset.phase=phase;root.dataset.unit=s?.unit_id||'';
    if(identity!==s?.unit_id){identity=s?.unit_id;if(el('bid'))el('bid').value='';}
    el('names').textContent=`${data.names.a} gegen ${data.names.b}`;
    el('phase').textContent=({waiting:'Warte auf die erste Runde.',prepared:'Runde vorbereitet – warte auf den Host.',bidding:'Verdeckte Gebotsphase.',answering:key==='bid'?'Mündliche Antwortphase läuft.':'Manuelle Punktevergabe läuft.',ready:'Buzzer vorbereitet – warte auf die Freigabe.',open:'Buzzer geöffnet.',locked:'Antwortrecht vergeben – Hostbewertung ausstehend.',blocked:'Beide Parteien sind gesperrt. Der Host kann die Runde ohne Sieger beenden.',needs_resolution:'Fehlendes Gebot – Wertung noch offen.',show_result:'Rundenergebnis veröffentlicht.',completed:data.match_status==='completed'?'Match beendet.':'Runde abgeschlossen.'})[phase];
    show('waiting',['waiting','prepared'].includes(phase));show('question-card',!!s&&phase!=='prepared');el('question').textContent=s?.question||'';
    deadline=s?.deadline?Date.parse(s.deadline):null;serverAt=Date.parse(s?.server_now);receivedAt=performance.now();
    el('right').textContent=s?.right_participant_id?`Antwortrecht: ${name(s.right_participant_id)}${key==='bid'?' · Gebot: '+s.winning_bid:''}`:'';
    el('counter').textContent=key==='bid'&&s?.winning_bid?`Richtige Antworten: ${s.counter} / ${s.winning_bid}`:'';
    el('bids').textContent=s?.bids?`${data.names.a}: ${s.bids.a??'Kein Gebot'} · ${data.names.b}: ${s.bids.b??'Kein Gebot'}`:'';
    if(role==='participant'){
      if(key==='bid'){show('bid-form',phase==='bidding');el('bid').disabled=el('commit').disabled=busy||!s?.can_bid;if(s?.own_bid!==null&&s?.own_bid!==undefined)el('bid').value=s.own_bid;el('own-status').textContent=s?.own_bid?'Dein Gebot ist verbindlich eingeloggt.':'';}
      if(key==='buzzer'){show('buzz',['open','locked','blocked'].includes(phase));el('buzz').disabled=busy||!s?.can_buzz;el('own-status').textContent=s?.own_has_right?'Du hast das Antwortrecht.':s?.own_blocked?'Du bist für diese Runde gesperrt.':'';}
    }
    show('result-card',['show_result','completed'].includes(phase));el('result').textContent=s?.round_outcome?(s.winner_id?`${name(s.winner_id)} gewinnt die Runde.`:'Kein Rundensieger.'):'';
    const tally=data.match;el('score').replaceChildren();
    if(tally){for(const side of ['a','b']){const p=tally[`party_${side}`];line(el('score'),'p',`${data.names[side]}: ${p.score} Punkte · ${p.round_wins} Rundensiege`);if(tally.target_score){const bar=document.createElement('progress');bar.max=tally.target_score;bar.value=Math.max(0,Math.min(tally.target_score,p.score));bar.setAttribute('aria-label',`${data.names[side]}: ${p.score} von ${tally.target_score}`);el('score').append(bar);}}
      if(tally.best_of)line(el('score'),'p',`Best of ${tally.best_of}: ${tally.required_wins} Rundensiege zum Matchsieg.`);
      if(key==='bid'&&s?.winner_id)el('result').textContent+=(s.winner_id===s.right_participant_id?' Gebot erfüllt.':' Zeit abgelaufen – Gebot nicht erfüllt.');
      if(tally.winner_id)el('result').textContent+=` Matchsieg: ${name(tally.winner_id)}.`;
    }
    if(s?.resolution_reason)el('error').textContent=messages[s.resolution_reason]||s.resolution_reason;
    if(role==='host'){
      if(!el('question-select').dataset.loaded){for(const q of data.questions||[]){const o=document.createElement('option');o.value=q.id;o.textContent=q.question_text;el('question-select').append(o);}el('question-select').dataset.loaded='true';if(el('party'))for(const side of ['a','b']){const o=document.createElement('option');o.value=data.parties[side];o.textContent=data.names[side];el('party').append(o);}}
      const prepare=data.match_status==='in_progress'&&(!s||phase==='completed');show('prepare',prepare);show('question-select',prepare);show('start',phase==='prepared');show('increment',phase==='answering');show('open',phase==='ready');show('correct',phase==='locked');show('incorrect',phase==='locked');show('complete',phase==='show_result');show('adjust-controls',phase==='answering');
      if(key==='buzzer')show('end_round',['ready','open','locked','blocked'].includes(phase));
      for(const id of ['prepare','start','increment','open','correct','incorrect','adjust','end_round','complete'])if(el(id))el(id).disabled=busy;
    }
    show('return',phase==='completed'&&data.match_status==='completed');
  }
  const client=window.createDuelClient(root,render,messages);if(!client)return;
  if(role==='host'){
    el('prepare').onclick=()=>client.command(root.dataset.api,{action:'prepare',question_id:Number(el('question-select').value),previous_unit_id:current?.unit?.unit_id??null});
    for(const action of ['start','open','end_round','complete'])if(el(action))el(action).onclick=()=>host(action);
    if(el('increment'))el('increment').onclick=()=>host('increment',{expected_counter:current.unit.counter});
    for(const verdict of ['correct','incorrect'])if(el(verdict))el(verdict).onclick=()=>host('judge',{buzz_id:current.unit.buzz_id,verdict});
    if(el('adjust'))el('adjust').onclick=()=>host('adjust',{participant_id:Number(el('party').value),delta:Number(el('delta').value),expected_counter:current.unit.counter});
  }
  if(el('bid-form'))el('bid-form').onsubmit=e=>{e.preventDefault();const raw=el('bid').value;const value=Number(raw);if(!Number.isSafeInteger(value)||value<1){el('error').textContent=messages.invalid_request;return;}client.command(current?.unit_api,{action:'bid',payload:{value}});};
  if(el('buzz'))el('buzz').onclick=()=>client.command(current?.unit_api,{action:'buzz'});
  client.poll();setInterval(client.poll,400);setInterval(()=>{el('timer').textContent=deadline?`${Math.max(0,Math.ceil((deadline-serverAt-(performance.now()-receivedAt))/1000))} s`:'';},100);
})();
