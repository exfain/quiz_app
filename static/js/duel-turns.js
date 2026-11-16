(() => {
  const root=document.getElementById('duel-match');if(!root)return;
  const role=root.dataset.role,key=root.dataset.game,el=id=>document.getElementById(id),show=(id,yes)=>{if(el(id))el(id).hidden=!yes;};
  let current=null,boardKey=null;
  const messages={host_required:'Nur der Host darf Antworten erfassen.',invalid_selection:'Diese Auswahl ist ungültig oder wurde bereits bestätigt.',
    stale_turn:'Der Zug wurde bereits verarbeitet. Bitte den aktuellen Zug prüfen.',invalid_phase:'Diese Aktion passt nicht zur aktuellen Phase.',
    invalid_starting_party:'Wähle eine der beiden Parteien für den ersten Zug.',intro_not_finished:'Das Spielintro läuft noch.',
    question_not_in_quiz:'Diese Aufgabe gehört nicht zum geplanten Spiel.',invalid_question:'Die Aufgabe enthält unvollständige oder ungültige Daten.',
    solved_task_policy_unresolved:'Die Aufgabe ist vollständig gelöst. Warte auf die Ergebnisfreigabe durch den Host.',
    invalid_request:'Bitte alle benötigten Eingaben auswählen.',match_closed:'Das Match ist abgeschlossen.',
    unit_not_finished:'Schließe zuerst die aktuelle Präsentation ab.',stale_unit:'Der Rundenstand hat sich geändert.',
    action_id_conflict:'Diese Aktionskennung wurde mit anderem Inhalt verwendet.'};
  function text(parent,tag,value,cls){const node=document.createElement(tag);node.textContent=value;if(cls)node.className=cls;parent.append(node);return node;}
  function options(id,rows){const select=el(id);if(!select)return;const old=select.value;select.replaceChildren();for(const row of rows){const o=document.createElement('option');o.value=row.id;o.textContent=row.text;select.append(o);}if(rows.some(r=>String(r.id)===old))select.value=old;}
  function selection(value){client.command(current?.unit_api,{action:'record',expected_turn:current.unit.turn,selection:value});}
  function render(data,busy){
    current=data;const s=data.unit,phase=s?.phase||'waiting',name=id=>id===data.parties.a?data.names.a:data.names.b;
    root.dataset.unit=s?.unit_id||'';root.dataset.phase=phase;
    el('names').textContent=`${data.names.a} gegen ${data.names.b}`;
    el('phase').textContent=({waiting:'Warte auf die erste Aufgabe.',prepared:'Aufgabe vorbereitet – warte auf den Host.',playing:'Abwechselnde mündliche Antworten.',
      awaiting_result:'Die Aufgabe ist beendet. Warte auf die Auflösung.',needs_resolution:'Aufgabe vollständig gelöst – warte auf die Ergebnisfreigabe.',
      resolution_revealed:'Lösung aufgedeckt – warte auf die Ergebnisfreigabe.',show_result:'Auflösung und Ergebnis.',completed:data.match_status==='completed'?'Match beendet.':'Runde abgeschlossen.'})[phase];
    show('waiting',['waiting','prepared'].includes(phase));show('question-card',!!s&&phase!=='prepared');
    el('question').textContent=s?.question||'';el('description').textContent=s?.description||'';
    el('turn').textContent=phase==='playing'?`Zug ${s.turn}: ${name(s.current_participant_id)} ist an der Reihe.`:'';
    // Preserve real controls between identical polls: a mouse-down must not
    // lose its click because a background GET rebuilt the answer cards.
    const fingerprint=JSON.stringify([s?.unit_id,phase,s?.confirmed,s?.cards,s?.items,s?.left,s?.right,busy]);
    if(fingerprint!==boardKey){boardKey=fingerprint;el('board').replaceChildren();
      if(key==='assign'&&s?.left){const box=text(el('board'),'div','','turn-pairs');
        for(const left of s.left){const right=s.right.find(r=>r.id===s.confirmed[String(left.id)]);text(box,'p',`${left.text} → ${right?right.text:'?'}`);}
        if(role==='host'){options('left',s.left.filter(x=>s.confirmed[String(x.id)]===undefined));options('right',s.right);}
      }else if(key==='sorting_ladder'&&s?.items){
        text(el('board'),'p',s.upper_label);const list=text(el('board'),'ol','','turn-order');
        for(const id of s.confirmed)text(list,'li',s.items.find(x=>x.id===id).text);
        text(el('board'),'p',s.lower_label);text(el('board'),'p',`Noch verfügbar: ${s.items.filter(x=>!s.confirmed.includes(x.id)).map(x=>x.text).join(' · ')}`);
        if(role==='host'){options('item',s.items.filter(x=>!s.confirmed.includes(x.id)));options('position',Array.from({length:s.confirmed.length+1},(_,i)=>({id:i,text:`Position ${i+1}`})));}
      }else if(s?.cards){const box=text(el('board'),'div','','turn-cards');
        for(const card of s.cards){const clickable=role==='host'&&phase==='playing'&&!card.revealed;
          const node=text(box,clickable?'button':'div',card.text??'Verdeckte Antwort','turn-card'+(card.revealed?' revealed':''));node.dataset.answer=card.id;
          if(clickable){node.disabled=busy;node.onclick=()=>selection({kind:'answer',answer_id:card.id});}
        }
      }
    }
    const revealed=['show_result','completed','resolution_revealed'].includes(phase);show('result-card',revealed);
    el('solution').replaceChildren();el('result').textContent='';
    if(revealed){
      if(key==='assign')for(const left of s.left)text(el('solution'),'p',`${left.text} → ${s.right.find(r=>r.id===s.solution[String(left.id)]).text}`);
      if(key==='sorting_ladder'){const list=text(el('solution'),'ol','');for(const id of s.solution)text(list,'li',s.items.find(x=>x.id===id).text);}
      if(s.points)el('result').textContent=['draw','no_winner'].includes(s.round_outcome)?`Kein Rundensieger. Punkte: ${s.points.a} / ${s.points.b}.`:`${s.round_outcome==='party_a_wins'?data.names.a:data.names.b} gewinnt die Runde. Punkte: ${s.points.a} / ${s.points.b}.`;
    }
    if(s?.resolution_reason==='solved_task_policy_unresolved')el('error').textContent=messages.solved_task_policy_unresolved;
    const tally=data.match||s?.match;el('score').replaceChildren();
    if(tally){for(const side of ['a','b']){const party=tally[`party_${side}`];text(el('score'),'p',`${data.names[side]}: ${party.score} Punkte · ${party.round_wins} Rundensiege`);
      if(tally.target_score){const bar=document.createElement('progress');bar.max=tally.target_score;bar.value=Math.max(0,Math.min(tally.target_score,party.score));bar.setAttribute('aria-label',`${data.names[side]}: ${party.score} von ${tally.target_score}`);el('score').append(bar);}}
      if(tally.best_of)text(el('score'),'p',`Best of ${tally.best_of}: ${tally.required_wins} Rundensiege zum Matchsieg.`);
      if(tally.winner_id)el('result').textContent+=` Matchsieg: ${name(tally.winner_id)}.`;
    }
    if(role==='host'){
      if(!el('question-select').dataset.loaded){options('question-select',[{id:'',text:'Bitte auswählen'},...(data.questions||[]).map(q=>({id:q.id,text:q.question_text}))]);el('question-select').dataset.loaded='true';
        options('starting-party',[{id:'',text:'Partei auswählen'},{id:data.parties.a,text:data.names.a},{id:data.parties.b,text:data.names.b}]);}
      const prepare=data.match_status==='in_progress'&&(!s||phase==='completed');
      show('prepare',prepare);show('question-select',prepare);show('start',phase==='prepared');show('starting-label',phase==='prepared');show('selection',phase==='playing');
      show('show_result',['awaiting_result','needs_resolution','resolution_revealed'].includes(phase));show('complete',phase==='show_result');
      for(const id of ['prepare','start','record','wrong','show_result','complete'])if(el(id))el(id).disabled=busy;
    }
    show('return',phase==='completed'&&data.match_status==='completed');
  }
  const client=window.createDuelClient(root,render,messages);if(!client)return;
  if(role==='host'){
    el('prepare').onclick=()=>client.command(root.dataset.api,{action:'prepare',question_id:Number(el('question-select').value),previous_unit_id:current?.unit?.unit_id??null});
    el('start').onclick=()=>client.command(current?.unit_api,{action:'start',starting_participant_id:Number(el('starting-party').value)});
    el('wrong').onclick=()=>selection({kind:'wrong'});
    if(el('record'))el('record').onclick=()=>selection(key==='assign'?{kind:'pair',left_id:Number(el('left').value),right_id:Number(el('right').value)}:{kind:'place',item_id:Number(el('item').value),position:Number(el('position').value)});
    for(const action of ['show_result','complete'])el(action).onclick=()=>client.command(current?.unit_api,{action});
  }
  client.poll();setInterval(client.poll,400);
})();
