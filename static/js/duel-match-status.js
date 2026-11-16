/* Presentation of authoritative results; no point formulas or reveal authority. */
(() => {
  const reasons={only_valid_answer:'Nur eine gültige Abgabe: diese Partei erhält einen Punkt und den Rundensieg.',
    no_answers:'Keine gültige Abgabe: 0:0, kein Rundensieger.',equal_deviation:'Gleiche Abweichung: beide erhalten einen Punkt, kein Rundensieger.',
    closer_answer:'Die nähere Schätzung erhält einen Punkt und den Rundensieg.',equal_distances:'Exakt gleiche Distanz: beide erhalten einen Punkt, kein Rundensieger.',
    closer_location:'Der nähere Marker erhält einen Punkt und den Rundensieg.',both_bust:'Beide über 21: 0:0, kein Rundensieger.',
    equal_stars:'Gleicher Sternestand: 0:0, kein Rundensieger.',task_fully_solved:'Vollständig fehlerfrei gelöst: 0:0, kein Rundensieger.',
    no_bids:'Kein gültiges Gebot: 0:0, kein Rundensieger.',bid_fulfilled:'Gebot erfüllt: ein Punkt und der Rundensieg.',
    bid_timeout:'Antwortzeit abgelaufen: der Gegner erhält einen Punkt und den Rundensieg.',opponent_error:'Fehler: der Gegner erhält einen Punkt und den Rundensieg.'};
  window.DuelResultReason=code=>reasons[code]||'';
  const root=document.getElementById('duel-match'),box=document.getElementById('duel-match-status');if(!root||!box)return;
  let busy=false,pending=null;
  window.DuelMatchStatus={render(data){
    const unit=data.unit,tally=data.match||unit?.match;
    document.getElementById('duel-round-reason').textContent=['show_result','completed'].includes(unit?.phase)?reasons[unit.result_reason]||'':'';
    document.getElementById('duel-match-notice').textContent=tally?.draw?'Match unentschieden. Die gebuchten Punkte bleiben erhalten.':
      tally?.evaluation==='sudden_death'?'Beide haben das Punkteziel erreicht. Stichrunden folgen, bis eine Partei allein eine Runde gewinnt.':
      tally?.best_of?'Siegerlose Runden verbrauchen keinen Entscheidungsplatz.':'';
    const button=document.getElementById('finish-match-draw');if(button){button.hidden=!data.can_finish_draw;button.disabled=busy;}
  }};
  const button=document.getElementById('finish-match-draw');if(button)button.onclick=async()=>{
    if(busy)return;busy=true;button.disabled=true;pending??=window.AuthoritativeGameState.createActionId();
    try{
      const response=await fetch(root.dataset.api,{method:'POST',headers:{'Content-Type':'application/json',
        'X-CSRFToken':root.querySelector('[name=csrfmiddlewaretoken]').value},body:JSON.stringify({action:'finish_draw',client_action_id:pending})});
      const data=await response.json();
      if(!response.ok){pending=null;throw Error(data.action?.code||data.error?.code||'Abschluss abgelehnt.');}
      location.reload();
    }catch(error){document.getElementById('error').textContent=({material_available:'Es sind noch unverwendete Aufgaben verfügbar.',
      unit_not_finished:'Schließe zuerst die aktuelle Präsentation ab.',match_not_drawable:'Für dieses Match ist kein Gleichstandsabschluss zulässig.'})[error.message]||error.message;}
    finally{busy=false;button.disabled=false;}
  };
})();
