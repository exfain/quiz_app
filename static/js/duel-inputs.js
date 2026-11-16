(() => {
  const root=document.getElementById('duel-match');if(!root)return;
  const role=root.dataset.role,key=root.dataset.game,el=id=>document.getElementById(id),show=(id,yes)=>{if(el(id))el(id).hidden=!yes;};
  let current=null,lastQuestion=null,localDraft=null,draftPending=null,saving=false,map=null,mapKey=null,imageKey=null,imageObject=null,serverAt=0,receivedAt=0,deadline=null;
  const messages={invalid_request:'Bitte eine gültige Eingabe oder Frage auswählen.',invalid_input:'Die Position liegt außerhalb der Karte.',
    invalid_question:'Die Frage / das Set ist nicht vollständig konfiguriert.',invalid_phase:'Diese Aktion passt nicht zur aktuellen Phase.',
    already_committed:'Deine Antwort ist bereits verbindlich eingeloggt.',answering_not_open:'Die Antwortphase ist geschlossen.',
    answer_deadline_passed:'Die Antwortfrist ist abgelaufen.',intro_not_finished:'Das Spielintro läuft noch.',stale_question:'Diese Frage ist nicht mehr aktuell.',
    stale_unit:'Diese Runde ist nicht mehr aktuell.',unit_not_finished:'Schließe zuerst die aktuelle Präsentation ab.',match_closed:'Das Match ist abgeschlossen.',
    action_id_conflict:'Diese Aktionskennung wurde mit anderem Inhalt verwendet.',
    missing_answers:'Mindestens eine Antwort fehlt. Die Wertungsregel ist noch offen; Ergebnis und Abschluss bleiben gesperrt.',
    equal_distances:'Exakt gleiche Distanzen. Warte auf die Ergebnisfreigabe durch den Host.',
    both_bust:'Beide Parteien haben mehr als 21 Sterne. Warte auf die Ergebnisfreigabe.',
    equal_stars:'Gleicher Sternestand am Setende. Warte auf die Ergebnisfreigabe.',result_unresolved:'Die Wertung ist fachlich noch ungeklärt.'};
  function text(parent,tag,value){const node=document.createElement(tag);node.textContent=value;parent.append(node);return node;}
  // Only the artwork/interaction surface is shared. This adapter displays both
  // duel guesses without borrowing the Multiplayer ranking or zone scoring.
  if(key==='where'){
    class DuelMap extends window.WhereMap {
      draw(){
        const original=this.state,draft=this.draft,spectator=this.spectator;
        this.state=null;super.draw();this.state=original;this.draft=draft;this.spectator=spectator;
        if(!this.box||!this.duel)return;
        const s=this.duel;
        if(s.target){for(const side of ['a','b']){const point=s.answers?.[side];if(point){this.line({...point,formatted_distance:`${s.distances[side].toFixed(2)} km`},s.target);this.marker(point,side==='a'?'#2867d2':'#9b48c1',this.names[side],side);}}this.marker(s.target,'#d33539','Ziel','target');}
        else if(role==='participant'&&(s.own_answer||this.inputDraft))this.marker(s.own_answer||this.inputDraft,'#2867d2',null,'own');
      }
    }
    map=new DuelMap(el('map'),{onSelect:point=>{
      if(!current?.unit?.can_answer)return;
      localDraft={latitude:point.latitude,longitude:point.longitude};draftPending={unit:current.unit.unit_id,index:current.unit.question_index,payload:localDraft};
      map.inputDraft=localDraft;map.draw();saveDraft();
    }});
    // An internal instance for map interaction, never an authority for scoring.
    el('map').duelMap=map;
    el('zoom-in').onclick=()=>map.zoom(.8);el('zoom-out').onclick=()=>map.zoom(1.25);el('zoom-reset').onclick=()=>map.reset();
  }
  async function saveDraft(){
    if(saving)return;saving=true;
    try{while(draftPending){const item=draftPending;draftPending=null;
      if(item.unit===current?.unit?.unit_id&&item.index===current.unit.question_index&&current.unit.can_answer)
        await client.command(current.unit_api,{action:'draft',question_index:item.index,payload:item.payload});
    }}finally{saving=false;if(current)render(current,false);}
  }
  function command(action){return client.command(current?.unit_api,{action,question_index:current.unit.question_index});}
  function render(data,busy){
    current=data;const s=data.unit,phase=s?.phase||'waiting';root.dataset.unit=s?.unit_id||'';root.dataset.phase=phase;root.dataset.questionIndex=s?.question_index??'';
    el('names').textContent=`${data.names.a} gegen ${data.names.b}`;
    el('phase').textContent=({waiting:'Warte auf die erste Runde.',prepared:'Frage vorbereitet – warte auf den Host.',answering:'Antwortphase läuft.',awaiting_reveal:'Antwortphase geschlossen – warte auf den Host.',show_answers:'Antworten aufgedeckt.',show_solution:'Lösung aufgedeckt.',question_result:'Frage ausgewertet. Das Set geht weiter.',needs_resolution:'Wertung noch ungeklärt.',show_result:'Rundenergebnis veröffentlicht.',completed:data.match_status==='completed'?'Match beendet.':'Runde abgeschlossen.'})[phase];
    const identity=s?`${s.unit_id}:${s.question_index}`:null;
    if(identity!==lastQuestion){lastQuestion=identity;localDraft=null;draftPending=null;mapKey=null;if(el('estimate'))el('estimate').value='';el('map')?.classList.remove('revealing');}
    show('waiting',['waiting','prepared'].includes(phase));show('question-card',!!s&&phase!=='prepared');
    el('question').textContent=s?.question||'';el('question-number').textContent=s?`Frage ${s.question_index+1} von ${s.question_count}`:'';el('hint').textContent=s?.hint||'';
    serverAt=Date.parse(s?.server_now);receivedAt=performance.now();deadline=phase==='answering'?Date.parse(s.deadline):null;
    if(map&&s?.map_type&&phase!=='prepared'){
      const fingerprint=JSON.stringify([identity,s.map_type,s.can_answer,s.own_answer,localDraft||s.draft,s.target,s.answers,s.distances]);
      if(fingerprint!==mapKey){mapKey=fingerprint;map.duel=s;map.names=data.names;map.inputDraft=localDraft||s.draft;
        map.update({question:{map_type:s.map_type},current_round_id:identity,phase:s.target?'reveal':'answering',can_answer:s.can_answer},{spectator:role!=='participant'});
        if(s.target)el('map').classList.add('revealing');}
    }
    const imageUrl=s?.image_url||null;show('question-image',!!imageUrl);
    if(imageUrl!==imageKey){imageKey=imageUrl;if(imageObject)URL.revokeObjectURL(imageObject);imageObject=null;el('question-image')?.removeAttribute('src');
      if(imageUrl)client.readImage(imageUrl).then(blob=>{if(imageKey!==imageUrl)return;imageObject=URL.createObjectURL(blob);el('question-image').src=imageObject;}).catch(()=>{el('error').textContent='Fragebild konnte nicht geladen werden.';});}
    if(role==='participant'){
      show('answer-form',phase==='answering');el('commit').disabled=!s?.can_answer||busy||saving||(key==='where'&&!(localDraft||s.draft));
      if(el('estimate')){el('estimate').disabled=!s?.can_answer||busy;if(s?.own_answer)el('estimate').value=s.own_answer.value;}
      el('own-status').textContent=s?.own_answer?'Deine Antwort ist verbindlich eingeloggt.':phase==='answering'?'': 'Keine weitere Abgabe möglich.';
    }
    show('answers-card',!!s?.answers);el('answers').textContent=s?.answers?['a','b'].map(side=>`${data.names[side]}: ${s.answers[side]?(key==='where'?`${s.answers[side].latitude.toFixed(3)}, ${s.answers[side].longitude.toFixed(3)}`:s.answers[side].value):'Keine Antwort'}`).join('\n'):'';
    show('solution-card',s?.target!==undefined);el('solution').textContent=s?.target!==undefined?(key==='where'?`Ziel: ${s.target.latitude}, ${s.target.longitude}`:`Richtige Antwort: ${s.target}`):'';
    el('distances').textContent=s?.distances?['a','b'].map(side=>`${data.names[side]}: ${s.distances[side]===null?'Keine Antwort':s.distances[side].toFixed(2)+' km'}`).join(' · '):'';
    el('explanation').textContent=s?.explanation||'';show('stars-card',!!s?.stars);
    el('stars').textContent=s?.stars?['a','b'].map(side=>`${data.names[side]}: ${s.stars[side]} Sterne${s.bust[side]?' – BUST':s.stars[side]===21?' – BLACK JACK':''}`).join(' · '):'';
    show('result-card',!!s?.points);el('result').textContent=s?.points&&['draw','no_winner'].includes(s.round_outcome)?`Kein Rundensieger. Duellpunkte: ${s.points.a} / ${s.points.b}.`:s?.points?`${s.round_outcome==='party_a_wins'?data.names.a:data.names.b} gewinnt die Runde. Duellpunkte: ${s.points.a} / ${s.points.b}.`:'';
    const tally=data.match||s?.match;el('score').replaceChildren();
    if(tally){for(const side of ['a','b']){const p=tally[`party_${side}`];text(el('score'),'p',`${data.names[side]}: ${p.score} Punkte · ${p.round_wins} Rundensiege`);
      if(tally.target_score){const bar=document.createElement('progress');bar.max=tally.target_score;bar.value=Math.max(0,Math.min(tally.target_score,p.score));bar.setAttribute('aria-label',`${data.names[side]}: ${p.score} von ${tally.target_score}`);el('score').append(bar);}}
      if(tally.best_of)text(el('score'),'p',`Best of ${tally.best_of}: ${tally.required_wins} Rundensiege zum Matchsieg.`);
      if(tally.winner_id)el('result').textContent+=` Matchsieg: ${tally.winner_id===data.parties.a?data.names.a:data.names.b}.`;
    }
    if(s?.resolution_reason)el('error').textContent=messages[s.resolution_reason]||s.resolution_reason;
    if(role==='host'){
      if(!el('question-select').dataset.loaded){for(const q of data.questions||[]){const option=document.createElement('option');option.value=q.id;option.textContent=q.question_text;el('question-select').append(option);}el('question-select').dataset.loaded='true';}
      const prepare=data.match_status==='in_progress'&&(!s||phase==='completed');show('prepare',prepare);show('question-select',prepare);
      for(const action of ['start','reveal','show_answers','show_solution','evaluate','next','complete']){show(action,({start:'prepared',reveal:'awaiting_reveal',show_answers:'awaiting_reveal',show_solution:'show_answers',evaluate:'show_solution',next:'question_result',complete:'show_result'})[action]===phase);if(el(action))el(action).disabled=busy;}
      if(phase==='needs_resolution'&&['equal_distances','both_bust','equal_stars'].includes(s.resolution_reason))show('evaluate',true);
      el('prepare').disabled=busy;
    }
    show('return',phase==='completed'&&data.match_status==='completed');
  }
  const client=window.createDuelClient(root,render,messages);if(!client)return;
  if(role==='host'){
    el('prepare').onclick=()=>client.command(root.dataset.api,{action:'prepare',question_id:Number(el('question-select').value),previous_unit_id:current?.unit?.unit_id??null});
    for(const action of ['start','reveal','show_answers','show_solution','evaluate','next','complete'])if(el(action))el(action).onclick=()=>command(action);
  }
  if(role==='participant')el('answer-form').onsubmit=e=>{e.preventDefault();const s=current?.unit;if(!s?.can_answer||saving)return;
    const payload=key==='where'?(localDraft||s.draft):{value:el('estimate').value};client.command(current.unit_api,{action:'commit',question_index:s.question_index,payload});};
  function timer(){el('timer').textContent=deadline?`${Math.max(0,Math.ceil((deadline-serverAt-(performance.now()-receivedAt))/1000))} s`:'';}
  client.poll();setInterval(client.poll,400);setInterval(timer,100);
})();
