(() => {
  const root=document.getElementById('duel-match');if(!root)return;
  const role=root.dataset.role,el=id=>document.getElementById(id),show=(id,yes)=>{if(el(id))el(id).hidden=!yes;};
  let current=null,unit=null,clock=null,imageURL=null,blobURL=null;
  const messages={intro_not_finished:'Das Spielintro läuft noch.',invalid_phase:'Diese Aktion passt nicht zur aktuellen Phase.',
    invalid_answer_right:'Dieser Antwortversuch ist nicht mehr offen.',party_locked:'Du hast in dieser Runde bereits gebuzzert und darfst nicht erneut antworten.',
    answering_not_open:'Das Antwortrecht ist bereits vergeben oder die Antwortphase ist geschlossen.',answer_deadline_passed:'Die Antwortfrist ist abgelaufen.',
    invalid_question:'Frage, Hinweise, Personen, Bild oder Toleranzkonfiguration sind unvollständig.',question_not_in_quiz:'Diese Frage gehört nicht zum geplanten Spiel.',
    unauthorized:'Bitte über die Lobby neu beitreten.',session_closed:'Diese Session ist nicht aktiv.',match_closed:'Das Match ist abgeschlossen.',
    stale_unit:'Der Rundenstand hat sich geändert.',unit_not_finished:'Schließe zuerst die aktuelle Präsentation ab.',
    no_buzz_policy_unresolved:'Die Wertung einer Runde ohne Buzz ist für diesen Spieltyp noch offen. Es wird kein Sieger erfunden.',
    action_id_conflict:'Diese Aktionskennung wurde bereits mit anderem Inhalt verwendet.',invalid_request:'Ungültige Eingaben.'};
  function tick(){if(!clock)return;const s=clock.state;
    if(s.phase==='answering'){
      const now=Date.parse(s.server_now)+performance.now()-clock.received;
      const remaining=Math.max(0,Math.ceil((Date.parse(s.deadline)-now)/1000));el('timer').textContent=`${remaining} Sekunden`;
      if(s.person_deadline)el('timer').textContent=`${Math.max(0,Math.ceil((Date.parse(s.person_deadline)-now)/1000))} Sekunden für diesen Namen · Set: ${remaining} Sekunden`;
      if(!remaining&&role==='participant')el('buzz').disabled=true;
    }else el('timer').textContent=s.phase==='awaiting_decision'?`Pausiert · ${s.remaining_seconds} Sekunden verbleiben`:'';
  }
  function render(data,busy){
    current=data;const s=data.unit,phase=s?.phase||'waiting',name=id=>id===data.parties.a?data.names.a:data.names.b;
    root.dataset.unit=s?.unit_id||'';el('names').textContent=`${data.names.a} gegen ${data.names.b}`;
    el('phase').textContent=({waiting:'Warte auf die erste Frage.',prepared:'Frage vorbereitet – warte auf den Host.',answering:'Antwortphase läuft.',
      awaiting_decision:'Antwortrecht vergeben – mündliche Antwort und Hostbewertung.',awaiting_result:'Zeit abgelaufen – warte auf die Hostfreigabe.',
      show_result:'Auflösung und Ergebnis.',completed:data.match_status==='completed'?'Match beendet.':'Runde abgeschlossen.'})[phase];
    show('waiting',['waiting','prepared'].includes(phase));show('question-card',!!s&&phase!=='prepared');
    if(unit!==s?.unit_id){unit=s?.unit_id;['question','solution','result','score','people','person','clues','tolerance'].forEach(id=>el(id).replaceChildren());}
    clock=s?(clock?.state===s?clock:{state:s,received:performance.now()}):null;
    el('question').textContent=s?.question||'';
    el('person').textContent=s?.person?.name||'';
    el('clues').replaceChildren();for(const text of s?.clues||[]){const li=document.createElement('li');li.textContent=text;el('clues').append(li);}
    el('tolerance').textContent=s?.tolerance!==undefined?`Toleranz: ±${s.tolerance} ${s.unit||''}`:'';
    el('answer-right').textContent=s?.answer_right_participant_id?`Antwortrecht: ${name(s.answer_right_participant_id)}`:'';
    el('blocked').textContent=s?.blocked_participant_ids?.length?`Bereits gebuzzert: ${s.blocked_participant_ids.map(name).join(', ')}`:'';
    show('photo',!!s?.image_url);
    if(imageURL!==s?.image_url){
      imageURL=s?.image_url;const expected=imageURL;if(blobURL){URL.revokeObjectURL(blobURL);blobURL=null;}el('photo').removeAttribute('src');
      if(expected)client.readImage(expected).then(blob=>{if(imageURL===expected){blobURL=URL.createObjectURL(blob);el('photo').src=blobURL;}}).catch(e=>el('error').textContent=e.message);
    }
    if(role==='participant')el('buzz').disabled=!s?.can_buzz||busy;
    show('result-card',!!s?.points);
    if(s?.points){
      el('solution').textContent=s.solution;
      el('result').textContent=s.round_outcome==='no_winner'?'Kein Rundensieger. Beide erhalten 0 Punkte.':`${s.round_outcome==='party_a_wins'?data.names.a:data.names.b} gewinnt die Runde. Punkte: ${s.points.a} / ${s.points.b}.`;
      if(s.verdict)el('result').textContent=`Hostbewertung: ${s.verdict==='correct'?'richtig':'falsch'}. `+el('result').textContent;
      if(s.buzz_person)el('result').textContent=`Buzz bei ${s.buzz_person.name}: ${s.buzz_person.is_lying?'Lügner':'kein Lügner'}. `+el('result').textContent;
      el('people').replaceChildren();for(const person of s.people||[]){const p=document.createElement('p');p.textContent=`${person.name}: ${person.is_lying?'lügt':'sagt die Wahrheit'}`;el('people').append(p);}
    }
    const tally=data.match||s?.match;
    if(tally){el('score').replaceChildren();for(const side of ['a','b']){const p=tally[`party_${side}`],row=document.createElement('p');row.textContent=`${data.names[side]}: ${p.score} Punkte · ${p.round_wins} Rundensiege`;el('score').append(row);
      if(tally.target_score){const bar=document.createElement('progress');bar.max=tally.target_score;bar.value=Math.max(0,Math.min(tally.target_score,p.score));bar.setAttribute('aria-label',`${data.names[side]}: ${p.score} von ${tally.target_score}`);el('score').append(bar);}}
      if(tally.best_of){const p=document.createElement('p');p.textContent=`Best of ${tally.best_of}: ${tally.required_wins} Rundensiege zum Matchsieg.`;el('score').append(p);}
      if(tally.winner_id)el('result').textContent+=` Matchsieg: ${name(tally.winner_id)}.`;
    }
    if(role==='host'){
      const select=el('question-select');if(!select.dataset.loaded){for(const q of data.questions||[]){const o=document.createElement('option');o.value=q.id;o.textContent=q.question_text;select.append(o);}select.dataset.loaded='true';}
      const prepare=data.match_status==='in_progress'&&(!s||phase==='completed');show('prepare',prepare);show('question-select',prepare);
      show('start',phase==='prepared');show('judgment',phase==='awaiting_decision');show('show_result',phase==='awaiting_result');show('complete',phase==='show_result');
      el('show_result').disabled=s?.resolution_reason==='no_buzz_policy_unresolved';
      if(s?.resolution_reason==='no_buzz_policy_unresolved')el('error').textContent=messages.no_buzz_policy_unresolved;
      el('host-solution').textContent=s?.host_solution?`Lösung (nur Host): ${s.host_solution}`:'';
      el('range').textContent=s?.accepted_range?`Eingefrorener zulässiger Bereich: ${s.accepted_range.min} bis ${s.accepted_range.max}`:'';
    }
    show('return',phase==='completed'&&data.match_status==='completed');tick();
  }
  const client=window.createDuelClient(root,render,messages);if(!client)return;
  if(role==='participant')el('buzz').onclick=()=>client.command(current?.unit_api,{action:'buzz'});
  if(role==='host'){
    el('prepare').onclick=()=>client.command(root.dataset.api,{action:'prepare',question_id:Number(el('question-select').value),previous_unit_id:current?.unit?.unit_id??null});
    for(const action of ['start','show_result','complete'])el(action).onclick=()=>client.command(current?.unit_api,{action});
    for(const verdict of ['correct','incorrect'])el(verdict).onclick=()=>client.command(current?.unit_api,{action:'judge',buzz_id:current.unit.buzz_id,verdict});
  }
  client.poll();setInterval(client.poll,400);setInterval(tick,50);
})();
