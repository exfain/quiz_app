(() => {
  const root=document.getElementById('tournament');if(!root)return;
  const role=root.dataset.role,el=id=>document.getElementById(id);let state=null,busy=false,polling=false,loaded=false,serial=0,appliedSerial=0;
  const messages={tournament_reveal_locked:'Andere Paarungen können noch antworten.',set_not_complete:'Schließe zuerst alle Paarungen einschließlich ihrer Präsentation ab.',
    invalid_request:'Bitte wähle eine Aufgabe und bei Estimation eine positive ganzzahlige Antwortdauer.',unauthorized:'Bitte über den Hub beziehungsweise die Lobby erneut anmelden.',
    buzzer_content_not_supported:'Dieser Inhalt ist nicht turnierfähig; Buzzer sind ausgeschlossen.',not_enough_participants:'Mindestens zwei anwesende spielberechtigte Teilnehmer sind erforderlich.',
    draw_conflict:'Dieses Set wurde bereits mit anderem Material ausgelost.',stale_set:'Der Setstand hat sich geändert.',action_id_conflict:'Diese Auslosung hat bereits anderes Material.'};
  const phases={prepared:'Vorbereitet',answering:'Antwortphase',bidding:'Gebotsphase',playing:'Antworten im Wechsel',
    awaiting_reveal:'Wartet auf Hostfreigabe',awaiting_result:'Wartet auf Auswertung',show_answers:'Antworten aufgedeckt',
    show_solution:'Lösung aufgedeckt',show_result:'Ergebnis',completed:'Abgeschlossen',question_result:'Frage ausgewertet',
    needs_resolution:'Wertung noch ungeklärt',resolution_revealed:'Wertung noch ungeklärt'};
  function outcome(row){return row.outcome==='party_a_wins'?`${row.names[0]} gewinnt`:row.outcome==='party_b_wins'?`${row.names[1]} gewinnt`:
    row.outcome==='draw'?'Unentschieden':row.outcome==='no_winner'?'Kein Rundensieger':row.outcome;}
  function headers(){let token=null;try{token=JSON.parse(localStorage.getItem('hub_lobby_rejoin:'+root.dataset.code)||'null')?.token;}catch(_){}
    return {'Content-Type':'application/json','X-CSRFToken':root.querySelector('[name=csrfmiddlewaretoken]').value,...(role==='participant'&&token?{Authorization:'Bearer '+token}:{})};}
  function text(parent,value){const node=document.createElement('p');node.textContent=value;parent.append(node);return node;}
  function render(data){state=data;root.dataset.set=data.set_number;root.dataset.status=data.status;
    el('status').textContent=data.status==='completed'?'Turnier beendet.':data.set_number?`Set ${data.set_number} · ${data.complete?'Alle Paarungen abgeschlossen.':'Begegnungen laufen. Keine öffentliche Lösung, solange andere noch antworten können.'}`:'Warte auf die erste Auslosung.';
    el('bye').textContent=data.bye.length?'Freilos ohne Punkte: '+data.bye.join(', '):'';
    el('question').textContent=data.question||'';el('options').textContent=(data.options||[]).map(o=>o.text).join(' · ');
    el('pairs').replaceChildren();for(const pair of data.pairs){const card=document.createElement('article');card.className='question-card';card.dataset.pair=pair.position;
      text(card,`${pair.position}. ${pair.names.join(' gegen ')} · ${phases[pair.phase]||'Warte auf den Host'}`);
      if(pair.submitted!==undefined)text(card,`${pair.submitted} / 2 verbindliche Abgaben`);
      if(pair.confirmed_count!==undefined)text(card,`${pair.confirmed_count} bestätigte Einträge`);
      if(pair.counter!==undefined)text(card,`Antwortzähler: ${pair.counter}`);
      if(pair.deadline){const remaining=Math.max(0,Math.ceil((Date.parse(pair.deadline)-Date.parse(pair.server_now))/1000));text(card,`${remaining} Sekunden verbleiben`);}
      if(pair.points)text(card,`${pair.points[0]} : ${pair.points[1]} Punkte · ${outcome(pair)}`);
      if(pair.result_reason)text(card,window.DuelResultReason(pair.result_reason));
      if(pair.blocked)text(card,pair.blocked);
      if(pair.url){const a=document.createElement('a');a.href=pair.url;a.target='_blank';a.rel='noopener';a.textContent=role==='host'?'Begegnung steuern':'Begegnung ansehen';card.append(a);}el('pairs').append(card);}
    el('results').replaceChildren();for(const row of data.results)text(el('results'),`Set ${row.set}: ${row.names.join(' / ')} · ${row.points.join(' : ')} Punkte · ${outcome(row)}${row.result_reason?' · '+window.DuelResultReason(row.result_reason):''}`);
    el('own').hidden=!data.own_url;el('own').href=data.own_url||'#';
    if(role==='participant'&&data.status==='running'&&data.own_url&&!data.own_finished){location.replace(data.own_url);return;}
    if(role==='host'){
      if(!loaded){for(const q of data.choices){const option=new Option(q.question_text,q.id);el('material').add(option);}loaded=true;}
      el('draw').disabled=busy||data.status!=='running'||(data.set_number>0&&!data.complete);el('draw').textContent=data.set_number?'Nächstes Set auslosen':'Erstes Set auslosen';
      el('end').hidden=(!data.complete&&data.set_number>0)||data.status==='completed';el('end').disabled=busy;
    }
    el('return').hidden=data.status!=='completed';el('return').href=data.lobby_url;
  }
  async function request(body){const sequence=++serial;try{const response=await fetch(root.dataset.api,{method:body?'POST':'GET',headers:headers(),cache:'no-store',...(body?{body:JSON.stringify(body)}:{})});
    const data=await response.json();if(!response.ok)throw Error(data.error?.code||'Abruf fehlgeschlagen.');
    if(sequence<appliedSerial||(state&&data.set_number<state.set_number)||(state?.status==='completed'&&data.status!=='completed'))return;appliedSerial=sequence;render(data);
  }catch(e){el('error').textContent=messages[e.message]||e.message;}}
  async function poll(){if(polling||busy)return;polling=true;try{await request();}finally{polling=false;}}
  async function command(body){if(busy)return;busy=true;el('error').textContent='';try{await request(body);}finally{busy=false;await poll();}}
  if(role==='host'){
    el('draw').onclick=()=>{const selection={id:Number(el('material').value)};if(el('duration'))selection.duration_seconds=Number(el('duration').value);command({action:'draw',previous_set_id:state?.set_id||null,selection});};
    el('end').onclick=()=>command({action:'end'});
  }
  poll();setInterval(poll,1000);
})();
