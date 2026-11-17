/* Zielansage: server phase/deadline, confirmed Hub identity, no local grading. */
(() => {
  const root=document.getElementById('target-game');if(!root)return;
  const role=root.dataset.role,el=id=>document.getElementById(id),show=(id,yes)=>{if(el(id))el(id).hidden=!yes;};
  let state=null,busy=false,polling=false,pending=null,serial=0,applied=0,serverAt=0,receivedAt=0,deadline=0,roundId=null,hostFingerprint=null,started=root.dataset.started==='true';
  function headers(){let token='';try{token=JSON.parse(localStorage.getItem('hub_lobby_rejoin:'+root.dataset.code)||'null')?.token||'';}catch(_){}
    return {'Content-Type':'application/json','X-CSRFToken':root.querySelector('[name=csrfmiddlewaretoken]').value,...(role==='participant'?{Authorization:'Bearer '+token}:{})};}
  if(role==='participant'){
    const lobbyReturn=createHubLobbyReturnController({sessionCode:root.dataset.code,gameKey:'target',roomCode:root.dataset.room,
      participantName:el('team-name').textContent,csrfToken:root.querySelector('[name=csrfmiddlewaretoken]').value,
      buttonId:'return',identityHeaders:headers});
    lobbyReturn.connectHubRecallSocket();
  }
  function text(parent,tag,value){const node=document.createElement(tag);node.textContent=value;parent.append(node);return node;}
  function render(data){
    state=data;const r=data.round,phase=data.completed?'completed':data.game_status==='inactive'?'inactive':r?.phase||'waiting';root.dataset.phase=phase;
    el('phase').textContent=({waiting:'Warte auf die erste Aufgabe.',inactive:'Das Spiel ist inaktiv. Warte auf den Host.',nomination:'Person bestimmen und Zielzahl festlegen.',answering:'Gemeinsame Antwortzeit läuft.',review:'Antwortzeit beendet. Der Host prüft die Antworten.',published:'Ergebnis verbindlich veröffentlicht.',completed:'Spiel beendet.'})[phase];
    if(roundId!==r?.id){roundId=r?.id;hostFingerprint=null;if(el('answer'))el('answer').value='';}
    show('waiting',!r);show('question-card',!!r?.question);el('question').textContent=r?.question||'';
    serverAt=Date.parse(data.server_now);receivedAt=performance.now();deadline=!data.completed&&r?.phase==='answering'?Date.parse(r.deadline):0;
    el('target-label').textContent=r?.own?.target?`Eure verbindliche Zielzahl: ${r.own.target}`:'';
    if(role==='participant'){
      const own=r?.own;show('nomination',phase==='nomination'&&!own?.ready);show('goal-form',phase==='nomination'&&own?.ready&&!own.target);
      el('team-name').textContent=own?.name||'';
      if(r)el('goal').max=r.solution_count;
      show('answer-form',phase==='answering');el('commit-answer').disabled=busy||!own?.can_answer;el('answer').disabled=busy||!own?.can_answer;
      el('ready').disabled=el('commit-goal').disabled=busy;
      el('own-status').textContent=phase==='nomination'&&own?.target?'Zielzahl eingeloggt. Wartet ohne weitere Eingabe, bis eure Person zurück ist und der Host startet.':
        phase==='answering'?'Jede Eingabe ist eine einzelne Antwort. Ihr könnt bis zum Fristende weiter antworten.':phase==='review'?'Die Abgabe ist geschlossen.':'';
      el('own-answers').replaceChildren();for(const a of own?.answers||[])text(el('own-answers'),'li',a.text);
    }
    if(role==='host'){
      show('startGameBtn',data.game_status==='inactive');
      show('host-controls',data.game_status==='active'&&!data.completed);
      const select=el('task');if(!select.dataset.loaded){for(const task of data.tasks||[])select.add(new Option(task.question_text,task.id));select.dataset.loaded='true';}
      show('prepare',!data.completed&&(!r||r.phase==='published'));show('task',!data.completed&&(!r||r.phase==='published'));
      show('start',phase==='nomination');show('publish',phase==='review');show('finish',phase==='published');
      el('start').disabled=busy||r?.target_count!==r?.team_count;
      for(const id of ['prepare','publish','finish'])el(id).disabled=busy;
      el('progress').textContent=`Antwortzeit: ${data.answer_seconds} Sekunden. `+(r?`${r.ready_count}/${r.team_count} bereit · ${r.target_count}/${r.team_count} Zielzahlen eingeloggt`:'');
      // Keep controls stable while polling: never swallow a mouse-down/click.
      const fingerprint=JSON.stringify([r?.id,r?.phase,r?.teams,busy]);
      if(fingerprint!==hostFingerprint){hostFingerprint=fingerprint;el('corrections').replaceChildren();
        for(const team of r?.teams||[]){const section=text(el('corrections'),'section','');
          text(section,'h3',`${team.name} · Ziel ${team.target??'–'} · ${team.correct_count} unterschiedliche richtige Lösungen`);
          for(const answer of team.answers){const line=text(section,'div',answer.text+' ');
            const index=answer.override_index??answer.matched_index;text(line,'span',index===null?'Nicht erkannt':`Lösung: ${r.solutions[index]}`);
            if(answer.corrected_at)text(line,'span',' · Hostkorrektur');
            if(['answering','review'].includes(r.phase)){
              const options=document.createElement('select');options.setAttribute('aria-label','Richtige Lösung für '+answer.text);
              options.add(new Option('Lösung wählen',''));r.solutions.forEach((solution,i)=>options.add(new Option(solution,i)));line.append(options);
              const button=text(line,'button','Als korrekt werten');button.dataset.answerId=answer.id;button.disabled=busy;
              button.onclick=()=>{if(options.value!=='')command('correct',{answer_id:answer.id,solution_index:Number(options.value)});};
            }
          }
        }
      }
    }
    show('results',r?.phase==='published'||data.completed);el('result-rows').replaceChildren();el('totals').replaceChildren();
    if(r?.phase==='published')for(const team of r.teams||[])text(el('result-rows'),'p',`${team.name}: Ziel ${team.target} · ${team.correct_count} richtig · ${team.achieved?'Ziel erreicht':'Ziel verfehlt'} · Platz ${team.rank} · ${team.points} Punkte`);
    const totals=data.totals||r?.teams||[];if(r?.phase==='published'||data.completed)for(const team of totals)text(el('totals'),'p',`${team.name}: ${team.score??team.total_score} Punkte`);
    show('return',data.completed);window.sessionGameStartIntro?.handleState({intro:data.intro});
  }
  function apply(data,n){if(n<applied||state&&data.revision<state.revision)return;applied=n;render(data);}
  async function poll(){if(polling||!started)return;polling=true;const n=++serial;try{const response=await fetch(root.dataset.api,{headers:headers(),cache:'no-store'});const data=await response.json();if(!response.ok)throw Error(data.error||'Abruf fehlgeschlagen.');apply(data,n);}catch(error){el('error').textContent=error.message;}finally{polling=false;}}
  async function command(action,extra={}){
    if(busy)return false;busy=true;el('error').textContent='';
    const body={action,...(state?.round?{round_id:state.round.id}:{}),...extra};
    const signature=JSON.stringify(body);if(!pending||pending.signature!==signature)pending={signature,id:window.AuthoritativeGameState.createActionId()};
    if(state)render(state);
    try{const n=++serial;const response=await fetch(root.dataset.api,{method:'POST',headers:headers(),body:JSON.stringify({...body,client_action_id:pending.id})});
      const data=await response.json();pending=null;if(!response.ok){if(data.revision!==undefined)apply(data,n);throw Error(data.action?.error||data.error||'Aktion abgelehnt.');}
      apply(data,n);return true;
    }catch(error){el('error').textContent=error.message;return false;}finally{busy=false;if(state)render(state);}
  }
  if(role==='participant'){
    el('ready').onclick=()=>command('ready');el('goal-form').onsubmit=event=>{event.preventDefault();command('goal',{target:Number(el('goal').value)});};
    el('answer-form').onsubmit=async event=>{event.preventDefault();if(await command('answer',{text:el('answer').value})){el('answer').value='';el('answer').focus();}};
  }
  if(role==='host'){
    show('host-controls',started);show('startGameBtn',!started);
    el('startGameBtn').onclick=async()=>{
      if(busy)return;busy=true;el('startGameBtn').disabled=true;el('error').textContent='';
      try{const response=await fetch(`/hub/api/session/${encodeURIComponent(root.dataset.code)}/activate-game/`,{method:'POST',headers:headers(),
        body:JSON.stringify({game_key:'target',room_code:root.dataset.room})});const result=await response.json();
        if(!response.ok||!result.success)throw Error(result.error||'Start fehlgeschlagen.');started=true;show('host-controls',true);show('startGameBtn',false);await poll();
      }catch(error){el('error').textContent=error.message;}finally{busy=false;el('startGameBtn').disabled=false;}
    };
    window.adminGameMonitor={get isActive(){return started&&!state?.completed&&state?.game_status!=='inactive';},
      get isUnfinished(){return started&&!state?.completed;},endQuiz(){return command('abort');},setInactive(){return command('pause');}};
    el('prepare').onclick=()=>command('prepare',{task_id:Number(el('task').value),previous_round_id:state?.round?.id??null});
    for(const action of ['start','publish','finish'])el(action).onclick=()=>command(action);
  }
  if(role!=='participant')el('return').onclick=()=>location.assign(state.lobby_url);
  function tick(){const remaining=deadline?Math.max(0,Math.ceil((deadline-serverAt-(performance.now()-receivedAt))/1000)):0;el('timer').textContent=deadline?remaining+' Sekunden':'';
    if(deadline&&!remaining&&role==='participant'){el('answer').disabled=el('commit-answer').disabled=true;}}
  poll();setInterval(poll,500);setInterval(tick,100);
})();
