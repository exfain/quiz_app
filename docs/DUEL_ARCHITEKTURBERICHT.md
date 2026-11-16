# Architekturbericht: Eigenständiger 2-Spielermodus DUEL

**Architekturentscheidung:** Den Duellmodus als eigene Django-App mit eigener Laufzeit, getrennten Spielcontrollern und einem gemeinsamen Duell-Schreibpfad ergänzen. Fragenbank, Hub-Teilnehmer, Anmeldung, Karten und geeignete Darstellungskomponenten bleiben gemeinsam. Die bestehenden Multiplayer-Consumer und deren Wertungslogik bleiben bestehen.

**Stand der Fortschreibung:** `duel/contracts.py`, `DuelGameInstance`, `DuelUnit` sowie die additiven Migrationen `0001_initial` und `0002_duelunit` sind inzwischen implementiert. Es gibt weiterhin keine Duell-Spielcontroller, Antwortverarbeitung oder erreichbaren Duell-Spielpfade. Die Ausführung der Migrationen auf einer produktiven Datenbank wird hier nicht vorausgesetzt.

**Dieser Architekturauftrag verändert ausschließlich diesen Bericht.** Die neuen Anforderungen an Sessiontypen, Finalduelle, Turniere und „Alle gegen einen“ werden in Abschnitt 19 ergänzt. Dieser Abschnitt präzisiert die früher offenen Sessionfragen und die nächste Implementierungsreihenfolge; die bisherigen Spielregeln und Isolationsentscheidungen bleiben erhalten.

## 1. Aktuelle Architektur

Die App besitzt bereits gemeinsame Infrastruktur, aber keine einheitliche fachliche Spiel-Engine.

Der bestehende Ablauf ist im Wesentlichen:

1. Im Admin-Dashboard werden Spielinstanzen mit Fragen und Einstellungen erstellt.
2. Eine `HubSession` enthält geordnete `HubGameStep`-Einträge.
3. Teilnehmer treten als `HubParticipant` der Session bei.
4. Beim Spielstart werden Teilnehmer in spieltypspezifischen Teilnehmermodellen bereitgestellt.
5. Der jeweilige Consumer steuert Präsentation, Antworten, Auswertung und Spielende.
6. Antworten und Punkte werden in spieltypspezifischen Modellen gespeichert.
7. Der Hub aggregiert die Ergebnisse zur Sessionwertung.
8. Der Spectator erhält eine separate öffentliche Darstellung des aktuellen Spiels.

Wesentliche Konsequenz für DUEL: **Eine vorhandene Spielinstanz ist gleichzeitig Konfiguration und veränderlicher Multiplayer-Spielzustand.** Ihre Fragen können wiederverwendet werden. Ihre Start-, Antwort- und Auswertungsmethoden dürfen dagegen nicht ungeprüft für Duelle aufgerufen werden.

### Technischer Betrieb

- Aktiver ASGI-Einstieg: `games_website.asgi.application`, konfiguriert in `games_website/settings.py`.
- `games_website/asgi.py` registriert die WebSocket-Routen der einzelnen Apps.
- Zusätzlich existiert `games_website/routing.py` mit einem weiteren Router. Änderungen allein dort würden den konfigurierten ASGI-Pfad nicht zuverlässig erweitern.
- Primäre Datenbank: SQLite mit `transaction_mode='IMMEDIATE'` und Timeout.
- Zusätzlich ist eine PostgreSQL-Verbindung unter dem Alias `supabase` konfiguriert.
- Channels verwendet abhängig von der Konfiguration einen In-Memory-Layer oder Redis.

## 2. Relevante vorhandene Dateien und Zuständigkeiten

### Gemeinsames Backend

| Bereich | Tatsächliche Dateien, Klassen und Funktionen |
|---|---|
| Session, Ablauf, Teilnehmer | [games_hub/models.py](../games_hub/models.py): `HubSession`, `HubGameStep`, `HubParticipant` |
| Fester Teilnehmerpool eines Spiels | `HubGameParticipantSnapshot.create_for_step()` |
| Sessionerstellung und Gesamtwertung | [games_hub/views.py](../games_hub/views.py): `create_session()`, `add_step_to_session()`, `activate_session_game()`, `get_leaderboard_data()`, `get_post_game_results()` |
| Lobby-WebSocket und Navigation | `games_hub/consumers.py`: `HubConsumer`, insbesondere `handle_join()`, `handle_navigate_direct()`, `hub_event()`, `complete_games_for_session()` |
| Aktivierung und Konflikterkennung | `games_hub/active_game_guard.py`: `resolve_session_game_activation()`, `_activate_target_game()`, `_end_game_cleanly()` |
| Wiederbeitritt | `games_hub/lobby_join.py`: `join_lobby_participant()`, `issue_rejoin_token()`, `_token_matches_participant()` |
| Hostberechtigung | `games_hub/host_permissions.py`: `authorize_game_host()`, `user_can_manage_hub_session()` |
| Zustandsmetadaten und Antwortschutz | [games_hub/authoritative_state.py](../games_hub/authoritative_state.py): `resolve_runtime_identity()`, `get_runtime_state()`, `observe_snapshot()`, `validate_and_reserve_action()` |
| Gemeinsamer Consumer-Aufsatz | `games_hub/authoritative_consumer.py`: `AuthoritativeGameConsumerMixin` |
| Intro | `games_hub/game_intro.py`: `start_game_intro()`, `serialize_game_intro()`, `serialize_game_step()` |
| Tutorials | `games_hub/tutorial_runtime.py`, `games_hub/unit_tutorial_runtime.py` |
| Bereitschaft und Lobbyrückkehr | `games_hub/readiness.py`, `games_hub/lobby_return_flow.py` |
| Historische Ergebnisse | `games_hub/session_results.py`: `step_status()`, `freeze_step_result()`, `freeze_previous_results()` |
| Spectator | [games_hub/spectator.py](../games_hub/spectator.py): `build_spectator_state()` und spieltypspezifische Serializer |
| HTTP-Hostaktionen | `admin_dashboard/views.py`: unter anderem `_dispatch_quiz_host_action()` und die einzelnen Monitor-/Aktionsviews |

Die Spielregistrierung ist derzeit **auf mehrere Stellen verteilt**: `HubSession._get_game_model_map()`, `HubGameStep.GAME_CHOICES`, Views, Aktivierungslogik, Consumer und Spectator besitzen eigene Zuordnungen. Es gibt keine zentrale Registry, die bereits alle diese Aufgaben vollständig kapselt.

Das sollte für diesen Auftrag **nicht durch eine große Registry-Umstellung aller Multiplayer-Spiele bereinigt werden**.

### Bestehende Spielmodelle

| Spieltyp | Konfiguration und Laufzeit |
|---|---|
| Quick Quiz | `QuizGame.models`: `Quiz`, `QuizQuestion`, `QuizParticipant`, `QuizAnswer`, `QuizSession` |
| Estimation | `Estimation.models`: `EstimationQuiz`, `EstimationQuestion`, `EstimationAnswer`, `EstimationSession`; `Estimation/runtime.py` |
| Assign | `Assign.models`: `AssignQuiz`, `AssignQuestion`, `AssignSetRuntime`, `AssignRoundParticipantState`; `Assign/runtime.py` |
| Sorting Ladder | `sorting_ladder.models`: `SortingLadderGame`, `SortingQuestion`, `SortingItem`, `RoundSubmission`, `SortingLadderSession` |
| Wer weiß mehr | `wer_weiss_mehr.models`: `WerWeissMehrGame`, `WerWeissMehrQuestion`, `WerWeissMehrAnswerOption`, `WerWeissMehrSession`, `WerWeissMehrRoundResponse`; `wer_weiss_mehr/services.py` |
| Clue Rush | `clue_rush.models`: `ClueRushGame`, `ClueQuestion`, `Clue`, `ClueRushSession`; `clue_rush/runtime.py` |
| Where is this | `where_is_this.models`: `WhereQuiz`, `WhereQuestion`, `WhereAnswer`, `WhereSession`; `where_is_this/runtime.py`, `geo.py` |
| Who is lying | `who_is_lying.models`: `WhoQuiz`, `WhoQuestion`, `WhoAnswer`, `WhoSession` |
| Who is that | `who_is_that.models`: `WhoThatQuiz`, `WhoThatQuestion`, `WhoThatAnswer`, `WhoThatSession`; `who_is_that/runtime.py` |
| Black Jack Quiz | `black_jack_quiz.models`: `BlackJackQuiz`, `BlackJackQuestion`, `BlackJackParticipant`, `BlackJackSession`; `black_jack_quiz/runtime.py` |
| Wann war das | `wann_war_das.models`: `WannWarDasGame`, `WannWarDasQuestion`, `WannWarDasAnswer`, `WannWarDasPendingInput` |
| Buzzer | `buzzer.models`: `BuzzerGame`, `BuzzerRound`, `BuzzerBuzz`; `games_hub/manual_game_runtime.py` |
| Host-Punktevergabe | `host_points.models`: `HostPointsGame`, `HostPointsParticipant`, `HostPointsAdjustment`; `games_hub/manual_game_runtime.py` |

## 3. Empfohlene Zielarchitektur

Eine neue App `duel` erhält vier getrennte Verantwortungsbereiche:

| Bereich | Aufgabe |
|---|---|
| Duell-Laufzeit | Instanz, Parteien, aktuelle Entscheidungseinheit, Phasen und Timer |
| Spielcontroller | Spieltypspezifische Regeln und zulässige Aktionen |
| Matchverwaltung | Spielpunkte, Rundensiege, Zielpunktzahl, Best of N, Abschluss |
| Transport und Projektionen | Ein gemeinsamer Duell-Consumer, HTTP-Zugriffe und rollenabhängige Snapshots |

Vorgeschlagene neue Module, **noch nicht vorhanden**:

- `duel/contracts.py`: Kontext, Ergebnisse und Controller-Vertrag.
- `duel/models.py`: dauerhafter Duellzustand.
- `duel/commands.py`: zentraler transaktionaler Schreibpfad.
- `duel/matches.py`: Matchentscheidung.
- `duel/buzzer.py`: exklusives Antwortrecht.
- `duel/judging.py`: Hostbewertung ohne globale Punkteformel.
- `duel/projections.py`: Host-, Teilnehmer- und Spectator-Daten.
- `duel/hub_bridge.py`: schmale Anbindung an bestehende Sessions.
- `duel/controllers/`: je ein Controller pro freigeschaltetem Spieltyp.

**Ein Consumer für DUEL genügt.** Die fachlichen Unterschiede gehören in Controller, nicht in jeweils neue WebSocket-Implementierungen.

Die Controller benötigen einen kleinen Vertrag für:

- Konfiguration prüfen.
- Entscheidungseinheit starten.
- Zulässige Aktionen bestimmen.
- Aktion fachlich verarbeiten.
- Ergebnis mit Punkteänderungen und gegebenenfalls Rundensieger liefern.
- Öffentliche beziehungsweise private Darstellung bereitstellen.

Eine große Basisklasse, die allen Spielen denselben Ablauf vorschreibt, wäre hier unpassend. Buzzerfrage, abwechselndes Set und Black-Jack-Set haben unterschiedliche Zustandsmaschinen.

## 4. Gemeinsam verwendbare Komponenten

### Ohne neue Spielregeln verwendbar

- `HubSession` und `HubParticipant`.
- Vorhandene signierte Wiederbeitrittsidentität.
- Namenvalidierung und Sessionbesitz.
- Fragenbank und vorhandene Fragenmodelle.
- Bilder, Kartenmaterial und statische Assets.
- `Estimation/numbers.py`: insbesondere `finite_number()`, `decimal_number()` und `display_number()`.
- `WhereQuestion.calculate_distance()` sowie die Koordinatenfunktionen aus `where_is_this/geo.py`.
- `WannWarDasQuestion.tolerance_for_step()`, `points_for_step()` und verwandte Berechnungen.
- `AssignQuestion.get_randomized_items()` und die hinterlegten korrekten Zuordnungen.
- `SortingItem.correct_rank`.
- Vorhandene Antwortreihenfolge aus `WerWeissMehrAnswerOption`.

### Frontend-Infrastruktur mit geprüftem Adapter

- [static/js/authoritative-game-state.js](../static/js/authoritative-game-state.js): Revisionen, Serverzeit, Aktionskennungen und initiale Darstellung.
- `static/js/host-game-runtime.js`: `HostGameRuntime.create()`, Wiederverbindung und Schutz ausstehender Hostaktionen.
- `static/js/host-question-selection.js`.
- `templates/includes/_participant_start_waiting.html`.
- Teile von Intro, Tutorialanzeige und Lobbyrückkehr.
- `templates/includes/accessibility_widget.html` für Standard, Arcade und VHS.
- `static/js/where-map.js`: `WhereMap`.

**Wiederverwendung bedeutet hier nicht, bestehende Runtime-Methoden aufzurufen.** Beispielsweise berechnet `Estimation/runtime.py::rank_answers()` nicht nur Abweichungen, sondern schreibt Multiplayer-Punkte. Für DUEL ist die Zahlenberechnung geeignet, dieser gesamte Schreibpfad dagegen nicht.

## 5. Notwendige Trennung im Frontend

Neue Duell-Seiten sollten die bestehenden vollständigen Teilnehmer- und Hosttemplates nicht als Grundlage mit zusätzlichen Modusabfragen erweitern.

Empfohlen sind neue, überschaubare Seiten:

- `templates/duel/host.html`
- `templates/duel/play.html`
- `templates/duel/spectate.html`
- `templates/duel/includes/…`
- `static/js/duel/…`

Die bestehenden großen Inline-Controller bleiben unverändert, beispielsweise:

- `QuizPlayer` in `templates/quiz/play.html`.
- `EstimationPlayer` in `templates/estimation/play.html`.
- `AssignPlayer` in `templates/assign/play.html`.
- `WhoPlayer` in `templates/who_is_lying/play.html`.
- `WhoThatPlayer` in `templates/who_is_that/play.html`.
- `BlackJackPlayer` in `templates/black_jack_quiz/play.html`.

Für kleine Eingaben können Markup und Darstellung gezielt wiederverwendet werden. Eine komplette bestehende Seite einzubinden wäre riskant: Sie bringt auch ihren Multiplayer-Socket, Timer, Navigation und Antwortabschluss mit.

Beim Theme ist besondere Vorsicht nötig: `accessibility_widget.html` verändert DOM-Strukturen, ergänzt Buttonhüllen und verschiebt Inhalte in die VHS-Darstellung. Neue Duell-Komponenten brauchen deshalb klare Container und Tests gegen diese tatsächlichen DOM-Veränderungen.

## 6. Architekturentscheidung pro Spieltyp

**A:** bestehende Komponenten mit kleiner Erweiterung.  
**B:** eigener Duell-Controller bei gemeinsamem Daten-/Darstellungskern.  
**C:** eigener Duell-Controller und deutlich getrennte Frontend-Abläufe.

Für keinen vollständigen Spieltyp empfehle ich ausschließlich A: Schon die gemeinsame Matchverwaltung verlangt eine saubere Trennung der Schreibpfade. A ist aber für einzelne Eingabe- und UI-Komponenten sinnvoll.

### 6.1 Quick Quiz — C; MC/True-False-Eingabe wiederverwendbar

Vorhanden: `QuizConsumer`, `QuizSession`, `QuizAnswer`, `QuizQuestion`, `QuizGame/presentation.py`.

**Short Answer:**

- Eigene Buzzersteuerung anstelle der Freitextabgabe.
- Hostbewertung liefert entweder `+1` für den Antwortberechtigten oder `+1` für den Gegner.
- Keine automatische Textbewertung im mündlichen Ablauf.
- Die Frage wird nach dieser Entscheidung geschlossen.

**Buchstabenaufdeckung:**

Die untersuchte schrittweise Darstellung ist der Typewriter der Frage:

- `question_typewriter_timing()`.
- `static/js/question-typewriter.js::visibleCharacterCount()`, `start()`, `sync()`.

Der vorhandene allgemeine Antwortschutz verlangt bei manueller Präsentation `answering_open`. Ein Buzzer während der laufenden Aufdeckung passt deshalb **nicht** in diesen bestehenden Guard.

DUEL muss Buzzern bereits in der dafür freigegebenen Präsentationsphase erlauben. Beim akzeptierten Buzz speichert der Server:

- Zeitpunkt der Unterbrechung.
- Sichtbare Zeichenzahl.
- Eingefrorenen Präsentationsfortschritt.

Alle Geräte zeigen anschließend diesen gespeicherten Stand. Ein bloßes Stoppen des lokalen JavaScript-Timers genügt nicht.

**Multiple Choice und True/False:**

- Zwei unabhängige, verbindliche Abgaben.
- Jede richtige Antwort `+1`, jede falsche `0`.
- Keine Sperre der zweiten Partei durch die erste Abgabe.
- Vorhandene Optionsdaten und Auswahlgestaltung verwendbar.

**Reveal:** Antworten beider Parteien → korrekte Antwort → Punkte.

**Zusätzlich offen:** Das Datenmodell kennt `double_answer` sowie mehrere Short-Answer-Felder. Für diese vorhandenen Varianten ist die Duellregel noch nicht festgelegt.

### 6.2 Estimation — B

Vorhanden: `EstimationConsumer`, `EstimationAnswer`, `EstimationSession`, `Estimation/runtime.py`.

Verwendbar sind Zahleneingabe, Einheiten und Dezimalarithmetik. Die bestehende Rangwertung darf nicht übernommen werden:

`rank_answers()` vergibt absteigende Punkte und entscheidet bei gleicher Abweichung nach Abgabegeschwindigkeit. DUEL verlangt stattdessen:

- Kleinere absolute Abweichung: `+1`.
- Identische Abweichung: beide `+1`.
- Keine Zeitentscheidung bei gleicher Abweichung.

Neue Phasen:

1. Frage und Timer.
2. Beide Schätzungen.
3. Zielwert, Abweichungen und Ergebnis.
4. Punkte.

Offen bleibt der Umgang mit fehlenden oder nur als Entwurf gespeicherten Eingaben.

### 6.3 Assign — C

Vorhanden:

- `AssignQuestion.get_randomized_items()`.
- `Assign/runtime.py::_check_match()`.
- `AssignSetRuntime`.
- `AssignConsumer.evaluate_current_round()` und `evaluate_and_advance_round()`.

Die vorhandene Laufzeit ist auf mehrere gleichzeitig auswählende Teilnehmer mit individueller Eliminierung ausgerichtet. Sie passt nicht zum abwechselnden mündlichen Duell.

Verwendbar:

- Aufgabeninhalt.
- Deterministische Zuordnung der angezeigten und ursprünglichen Itemindizes.
- Korrektheitsprüfung eines Paares.
- Darstellung bereits gelöster Paare.

Neu:

- Aktuelle antwortende Partei.
- Hostauswahl eines Paares.
- Sofortiger Abschluss bei falscher Zuordnung.
- Synchronisierte sichtbare Zuordnungen.

**Vorhandenes Verhalten:** `AssignQuestion.calculate_score()` zählt korrekte Paare. Das ist keine eindeutige Vorgabe für die gewünschte Duell-Rundenwertung.

### 6.4 Sorting Ladder — C

Vorhanden:

- `SortingQuestion`, `SortingItem.correct_rank`.
- `RoundSubmission.save()`.
- `SortingLadderGameConsumer._save_round_full_order_sync()`.
- `end_round_db()` und `_start_next_round_sync()`.

Die bestehende Prüfung arbeitet mit Teilnehmerabgaben und Eliminierung. Der Duell-Host benötigt einen eigenen Controller für das Einfügen des mündlich genannten Elements.

Verwendbar sind Rangdaten und die Prüfung einer Reihenfolge. Nicht direkt wiederverwenden sollte man `RoundSubmission.save()` als Duell-Schreibpfad: Das Modell gehört zur bisherigen Teilnehmer- und Setverwaltung.

**Vorhandenes Verhalten:** Korrekt absolvierte Sortierrunden tragen zum Punktestand bei. Wie ein erster Fehler das gesamte Duell-Set wertet, ist damit nicht bestimmt.

Offen ist außerdem, ob der Host eine genannte Position auswählt und der Server sie prüft oder ob der Host die mündliche Antwort unmittelbar bewertet.

### 6.5 Wer weiß mehr — C

Vorhanden:

- `WerWeissMehrQuestion.recalculate_answer_sort_order()`.
- `WerWeissMehrAnswerOption`.
- `WerWeissMehrSession.revealed_answers`.
- `sync_review_scores()`, `finalize_review()`.
- `templates/wer_weiss_mehr/play.html::renderTilePresentation()`.

Wiederverwendbar:

- Antwortliste und Sortierreihenfolge.
- Antworttexte und alternative Schreibweisen.
- Gestaltung der Aufdeckung.

Nicht direkt verwendbar ist die bestehende Auswertungsrunde mit gleichzeitigen Texteingaben und Teilnehmereliminierung.

Im Duell klickt der Host eine noch verdeckte Antwort. Der Server prüft unter Sperre, dass sie in dieser Entscheidungseinheit noch nicht aufgedeckt wurde. Erst dann werden Aufdeckung und Zugwechsel gespeichert.

**Vorhandenes Verhalten:** `recalculate_total_score()` summiert überlebte Runden. Eine Duellwertung bei erstem Fehler ergibt sich daraus nicht eindeutig.

### 6.6 Clue Rush — C

Vorhanden:

- `ClueQuestion`, `Clue`.
- `prepare_question_schedule()`, `start_prepared_question_schedule()`.
- `reconcile_clue_schedule()`, `submission_position()`.
- `set_paused()` in `clue_rush/runtime.py`.

Die vorhandenen Hinweisdaten und Zeitplanberechnungen sind geeignet. Die Runtime selbst ist eng mit Multiplayer-Antworten und hintabhängigen Punkten verbunden.

Duellablauf:

1. Hinweise laufen.
2. Erster gültiger Buzz vergibt das Antwortrecht.
3. Hinweisfortschritt wird während der Hostbewertung eingefroren.
4. Richtig: `+1`, Entscheidungseinheit abgeschlossen.
5. Falsch: kein Gegnerpunkt; diese Partei bleibt für die Einheit gesperrt.
6. Der Gegner kann nach Fortsetzung der Hinweise buzzern.
7. Der nächste Einheitenstart setzt beide Sperren zurück.

Wichtig: `build_spectator_state()` ruft für Clue Rush derzeit `reconcile_clue_schedule()` auf. Ein Spectator-Aufruf kann somit Hinweisfortschritt nachführen. Der Duell-Spectator sollte diese Multiplayer-Runtime nicht aufrufen.

### 6.7 Where is this — B

Vorhanden:

- `WhereQuestion.calculate_distance()`.
- `where_is_this/geo.py`.
- `static/js/where-map.js::WhereMap`.
- `where_is_this/runtime.py::save_input()`, `reveal()`, `state()`.

Karten, Zoom, Marker und Koordinatenumrechnung sind wiederverwendbar. Die aktuelle `reveal()`-Funktion verbindet den Abschluss jedoch mit Ranking- beziehungsweise Zonenpunkten.

DUEL braucht daher eine eigene Auswertung der zwei gespeicherten Positionen.

Für die Karte genügt voraussichtlich ein schmaler Darstellungsadapter. `WhereMap.draw()` unterscheidet derzeit ausdrücklich `rank` und `zones`; ein neuer Duellmodus darf dort nicht versehentlich als einer dieser Modi behandelt werden.

Offen:

- Exakt gleiche Entfernung.
- Punktehöhe eines geografischen Rundensieges.
- Verhalten bei fehlender Markierung.
- Gewünschte Linien-/Siegerdarstellung für ein Unentschieden.

Die Entscheidung darf nicht anhand gerundeter Entfernungslabels fallen.

### 6.8 Who is lying — C

Vorhanden:

- `WhoQuestion.people`.
- `get_liars()`, `get_truth_tellers()`, `get_total_duration_seconds()`.
- `WhoSession`.
- `WhoConsumer.handle_person_choice()` und `finalize_person_choices()`.

Die bisherige individuelle Markierungslogik entfällt im Duell vollständig.

Neu:

- Buzzer und Antwortrecht.
- Gespeicherter aktueller Personenstand beim Buzz.
- Hostbewertung.
- Richtig `+1` für die buzzende Partei, falsch `+1` für den Gegner.
- Ohne Buzz keine Punkte.
- Danach gemeinsame Auflösung.

**Vorhandenes Verhalten:** Ein Set kann mehrere Lügner enthalten. Die Daten verlangen nicht genau einen Lügner. Deshalb muss geklärt werden, ob sich die mündliche Antwort auf die aktuell sichtbare Person oder auf eine beliebige Person des Sets beziehen darf.

### 6.9 Who is that — C

Vorhanden: `WhoThatQuestion.image`, `WhoThatConsumer`, `who_is_that/runtime.py`.

Bild und Lösung bleiben gemeinsam. Texteingabe, automatische Namensprüfung und Antwortpersistenz des Multiplayer-Pfades entfallen im Duell.

Ablauf:

- Bild zeigen.
- Exklusiver Buzz.
- Mündliche Antwort.
- Hostentscheidung.
- Richtige Antwort: buzzende Partei `+1`.
- Falsche Antwort: Gegner `+1`.
- Bild/Lösung aufdecken und Einheit abschließen.

Das ist ein kleiner eigener Ablauf, aber kein kleiner Zusatz zum vorhandenen `WhoThatPlayer`.

### 6.10 Black Jack Quiz — B

Vorhanden:

- `BlackJackQuestion.calculate_points()`: absolute Abweichung als Sterne.
- `black_jack_quiz/runtime.py::submit()`, `calculate_participant()`, `end_question()`, `award()`.
- `BlackJackSession.set_rounds`.
- Getrennte aktuelle Sterne und Gesamtpunkte.

DUEL muss diese Größen erhalten und zusätzlich den Set-/Rundensieg bestimmen.

**Empfohlener Auswertungszeitpunkt:** nach der gemeinsamen Auswertung einer Frage, wenn beide verbindlichen Antworten beziehungsweise der geregelte Fristabschluss berücksichtigt wurden.

Eine sofortige Entscheidung nach der ersten einzelnen Abgabe könnte sonst die andere Partei zum Sieger erklären, obwohl deren Antwort derselben Frage noch nicht ausgewertet wurde.

Wenn genau eine Partei über 21 liegt und die andere höchstens 21:

- Set automatisch schließen.
- Weitere Fragen dieses Sets sperren.
- Aktuelle Frage zuerst auflösen.
- Anschließend Setwertung und gegebenenfalls Matchende anzeigen.

**Vorhandenes Verhalten:** Überbotene Teilnehmer erhalten bei `award()` null Setpunkte; der bisherige Ablauf beendet ein Set grundsätzlich anhand seiner vorgesehenen Fragen.

Offen bleiben insbesondere beide über 21, identische gültige Sternestände und die konkrete Punkteübernahme beim vorzeitigen Duell-Setsieg.

### 6.11 Wann war das — C

Vorhanden:

- `WannWarDasQuestion.step_index_for_elapsed()`.
- `tolerance_for_step()`, `points_for_step()`, `get_timer_state()`, `evaluate_answer()`.
- `WannWarDasGame.submit_answer()` und `reveal_current_question()`.

Beim akzeptierten Buzz speichert DUEL atomar:

- Verstrichene aktive Zeit.
- Toleranzstufe.
- Aktuelle Toleranz.
- Zu dieser Stufe gehörende Punkte.

Diese Werte bleiben während der Hostbewertung unverändert.

Die bestehende `evaluate_answer()`-Methode bewertet eine tatsächlich eingegebene Zahl. Bei einer rein mündlichen Antwort mit zwei Hostbuttons fehlt diese Zahl.

Daher ist offen:

- Trägt der Host die genannte Zahl ein, damit der Server die eingefrorene Toleranz prüft?
- Oder beurteilt der Host die Toleranzeinhaltung selbst und bestätigt nur richtig/falsch?

Bei falsch ist die Vorgabe eindeutig: Gegner `+1`, Frage beenden. Die genaue erfolgreiche Wertung bleibt offen.

### 6.12 Host-Punktevergabe — B, nur dünner Adapter

Vorhanden: `HostPointsGame.adjust_score()`, `next_round()`, `HostPointsAdjustment`.

Bedienung und Bedeutung manueller Punkteänderungen bleiben erhalten. Es wird keine neue spieltypspezifische Punkteformel benötigt.

`adjust_score()` schreibt allerdings unmittelbar in `HostPointsParticipant.total_score` und `HostPointsAdjustment`. Der Duelladapter sollte stattdessen die bestehende Aktion „Punktedelta für Partei“ an den gemeinsamen Duell-Buchungspfad übergeben.

So entstehen keine zwei konkurrierenden Punktestände innerhalb desselben Duells.

Für Best of N braucht der Host einen eindeutigen Rundenabschluss. Ob dabei der höhere Rundenzuwachs gewinnt oder der Host den Rundensieger ausdrücklich festlegt, ist noch zu entscheiden.

### 6.13 Buzzer — B, bisherige Mechanik erhalten

Vorhanden: `BuzzerGame.accept_buzz()`, `mark_current_correct()`, `mark_current_wrong()`, `BuzzerRound`, `BuzzerBuzz`.

Die bestehende Mechanik ist bereits nahe an einer wiederverwendbaren Buzzersteuerung:

- Exklusives Antwortrecht.
- Sperre nach falschem Versuch.
- Erneute Freigabe für verbleibende Teilnehmer.
- Atomare Bearbeitung.

Die Methoden schreiben aber auch Spielstatus und Punkte. Sie sind deshalb **Vorbild für die Infrastruktur**, nicht unmittelbar der gemeinsame Controller aller Duellspiele.

Der eigenständige Spieltyp Buzzer erhält einen kleinen Duelladapter mit seiner bisherigen Mechanik und einem Anschluss an die Matchverwaltung.

### 6.14 Neues Gebotsspiel — C

Es gibt keinen bestehenden passenden Spieltyp.

Benötigt werden:

- Aufgabeninhalt.
- Verdeckte Gebotsphase.
- Unveränderliche verbindliche Abgabe.
- Serverseitige Eingangsreihenfolge.
- Auswahl des Antwortberechtigten.
- Separater Erfüllungstimer.
- Hostcounter.
- Erfolg beziehungsweise Nichterfüllung.

Die Auswahl erfolgt nach:

1. Höherem Gebot.
2. Bei gleichem Gebot niedrigerer serverseitiger Annahmesequenz.

Zeitstempel dienen der Dokumentation; eine unter Instanzsperre vergebene Sequenz schafft auch bei identischen Zeitstempeln eine eindeutige Reihenfolge.

Eine wiederholte Abgabe verändert weder das ursprüngliche Gebot noch dessen Eingangszeitpunkt oder Reihenfolge.

**Nicht erfüllt:** Gegner `+1`.  
**Erfüllt:** Punktehöhe ausdrücklich offen.

Auch zulässige Gebotshöhen, fehlende Gebote und eine mögliche Gebotsfrist müssen noch festgelegt werden.

## 7. Geplante Änderungen an Models

Ich empfehle additive Duellmodelle statt Modusfeldern in sämtlichen Antwort- und Teilnehmermodellen der 13 Spiele.

### Bestehende Modelle

| Modell | Geplante Ergänzung |
|---|---|
| `HubSession` | Geplanter Sessiontyp `multiplayer` oder `two_party`; Default und Bestandsdaten bleiben Multiplayer. Nach erster Aktivierung unveränderlich, auch während einer Pause. Noch nicht implementiert. |
| `HubGameStep` | Geplantes Spielformat getrennt vom Sessiontyp: im Normalfall Multiplayer, gegebenenfalls Finalduell oder Turnier; in einer 2-Spieler-Session verbindlich Duell. Aktive Formatläufe erhalten eine unveränderliche Konfigurationskopie. Noch nicht implementiert. |

Die neuen Anforderungen klären diese frühere offene Entscheidung: Eine 2-Spieler-Session verwendet durchgehend Duellregeln. Eine normale Mehrspieler-Session kann einzelne besondere Spielformate enthalten. Die Session selbst wechselt dadurch ihren Typ nicht. Einzelheiten und die zusätzliche Turnierzuordnung stehen in Abschnitt 19.

### Neue Duellmodelle

| Vorgeschlagenes Modell | Aufgabe |
|---|---|
| `DuelGameInstance` | Implementiert: UUID als Primärschlüssel, Session, Spielschritt, Spieltyp, zwei `HubParticipant`-Referenzen, Regelkonfiguration, Revision, Matchstatus und Zeitstempel. Repräsentiert eine konkrete Begegnung mit fester Zweierbindung. Kein separates Modusfeld vorhanden. |
| `DuelUnit` | Implementiert: Integer-Primärschlüssel, zusätzliche eindeutige UUID, Begegnungsreferenz, Nummer innerhalb der Begegnung, Status `prepared`/`active`/`completed` und Zeitstempel. Material, konkrete Spielphasen, Timer, Zug, Antwortrecht und Ergebnis bleiben spätere Erweiterungen. |
| `DuelSubmission` | Eingabe oder Gebot einer Partei; Entwurf/verbindlich, Zeitpunkt und Annahmesequenz |
| `DuelDecision` | Hostentscheidung für einen bestimmten Buzz oder Zug |
| `DuelScoreEntry` | Einmalige Punktebuchung mit fachlicher Ursache |
| `DuelActionReceipt` | Aktionskennung, Ergebnis und Status für wiederholbare Transportanfragen |

`DuelGameInstance` ist zugleich der Rahmen eines einzelnen Zweiermatches. Eine weitere konkurrierende Match-Laufzeit ist dafür nicht nötig. Bei Turnieren bildet eine zusätzliche Formatlauf-/Setzuordnung mehrere solche Begegnungen ab; die Zweierbindung einer vorhandenen Instanz wird dafür nicht geändert.

Die beiden Parteien bleiben normale `HubParticipant`-Datensätze. Es gibt keine zusätzliche Teamverwaltung.

### Wichtige Constraints

- Unterschiedliche Partei-IDs.
- Beide Parteien gehören zur Session.
- Start nur mit genau zwei zugelassenen Parteien.
- Eindeutige Einheitenreihenfolge je Instanz.
- Eindeutige verbindliche Abgabe je Einheit, Partei und Eingabeart.
- Eindeutige Hostbewertung je konkretem Antwortrecht.
- Eindeutige Punktebuchung je fachlicher Ursache und Partei.
- Eindeutige Aktionskennung je Instanz und Akteur.

Nicht alle Beziehungen lassen sich allein durch einen einfachen Datenbank-Check ausdrücken. Sessionzugehörigkeit und Startfreigabe müssen zusätzlich im gesperrten Schreibpfad geprüft werden.

**Migrationen `0001` und `0002` liegen für die beiden implementierten Duellmodelle bereits vor.** Weitere additive Migrationen wären später für Formatlauf-/Turnierzuordnungen und die übrigen neuen Tabellen, die Hub-Ergänzungen sowie die Registrierung des Gebotsspiels erforderlich. Änderungen an den bisherigen Spielantworttabellen sind dafür nicht erforderlich. In diesem Architekturauftrag wird keine Migration erstellt oder ausgeführt.

## 8. Geplante Änderungen an Views, Templates und Hub-Anbindung

Neue Duell-Views übernehmen Erstellen, Starten, Statusabruf und Hostaktionen. Die alten Admin-Hostviews bleiben Multiplayer-Pfade.

Besonders wichtig: `games_hub/views.py::create_session()` setzt ausgewählte vorhandene Spiele derzeit auf `waiting`. **Die Erstellung einer Duell-Session darf diesen Pfad nicht für ihre Inhaltsvorlagen ausführen.**

Duellstart bedeutet:

- Vorhandene Spielkonfiguration lesen.
- Eigene Duellinstanz anlegen.
- Zwei Hub-Teilnehmer binden.
- Eigene Inhalte und Regeln für den Lauf festhalten.
- Keinen `start_quiz()`-Aufruf der Multiplayer-Vorlage ausführen.

Die Hub-Anbindung benötigt wenige ausdrücklich geprüfte Grenzen:

1. Erstellung und Auswahl des Spielschritts.
2. Aktivierung und Erkennung eines bereits laufenden Spiels.
3. Navigation anhand von Schritt und Instanz.
4. Pausieren beziehungsweise Beenden der Session.
5. Bereitschaftscheck und Lobbyrückkehr.
6. Ergebnisanzeige.

Dort wird an `duel/hub_bridge.py` delegiert. Innerhalb der alten Spielconsumer sollen keine Duellabfragen entstehen.

Die derzeitige Identifikation nur über `game_key` und `room_code` ist für diese Anbindung nicht ausreichend. DUEL-Navigation muss den konkreten Spielschritt und die Duellinstanz enthalten.

## 9. Consumer, Channel-Gruppen und Events

Die bisherigen Gruppen heißen beispielsweise:

- `quiz_<room_code>`
- `estimation_<room_code>`
- `cluerush_<room_code>`
- `sortingladder_<room_code>`
- `hub_<session_code>`

Diese Spielgruppen sind nicht nach Modus und neuem Lauf getrennt.

Für DUEL empfehle ich einen eigenen Endpunkt, beispielsweise:

`/ws/duel/<instance_uuid>/`

Gruppen werden an die Instanz gebunden. Öffentliche und private Informationen werden getrennt projiziert. Besonders sicher ist ein Gruppenereignis, das lediglich eine neue Revision ankündigt; jeder Consumer lädt anschließend den für seine Rolle erlaubten Snapshot.

### Gemeinsamer Kontext

Jede verändernde Duellaktion enthält mindestens:

- `protocol_version`
- `mode`
- `session_id`
- `step_id`
- `game_instance_id`
- `unit_id`
- gegebenenfalls `turn_id` und `buzz_id`
- `state_revision`
- `client_action_id`

Der Server bindet Akteur und Rolle aus der bestätigten Verbindung. Er übernimmt keine frei angegebenen Teilnehmer-IDs als Berechtigung.

Vorgeschlagene Befehle sind etwa:

- `duel.submit`
- `duel.buzz`
- `duel.judge`
- `duel.place`
- `duel.reveal_solution`
- `duel.commit_result`
- `duel.next_unit`
- `duel.increment_counter`

Transportereignisse können auf `duel_snapshot`, `duel_action_result`, `duel_action_rejected` und `duel_state_invalidated` begrenzt bleiben. Ein neues Eventsystem pro Spieltyp ist nicht nötig.

### Zentraler Schreibpfad

Eine Transaktion umfasst:

1. Instanz sperren.
2. Identität, Rolle, Instanz und Einheit prüfen.
3. Bereits verarbeitete Aktionskennung erkennen.
4. Phase und Frist prüfen.
5. Fachliche Aktion ausführen.
6. Antwortrecht, Entscheidung und Buchungen speichern.
7. Revision erhöhen und Antwortbeleg speichern.
8. Erst nach Commit benachrichtigen.

Der vorhandene `AuthoritativeGameConsumerMixin` reserviert bestimmte Aktionen vor dem eigentlichen Handler. Für DUEL sollte die Reservierung dagegen mit der fachlichen Änderung in derselben Transaktion liegen.

## 10. Frontend-State und Isolation

Der aktuelle `GameRuntimeState` ist ein nützlicher Schutz für Multiplayer, aber keine passende Duell-Laufzeit zum unveränderten Übernehmen.

Konkrete Grenzen:

- `resolve_runtime_identity()` verwendet vorhandene Spiel-PKs und Hub-Schritte.
- Ein Neustart derselben Konfiguration erhält dadurch nicht automatisch eine neue Lauf-UUID.
- `observe_snapshot()` normalisiert viele spieltypspezifische Multiplayer-Payloads.
- `validate_and_reserve_action()` verlangt bestimmte Antwortphasen und prüft Antwortfristen. Eine Hostbewertung nach Fristende ist eine andere Aktionsart.
- Der gemeinsame JavaScript-Wrapper erkennt endgültige Teilnehmeraktionen derzeit anhand von `participant_…` beziehungsweise `tutorial_completed`.

Für DUEL gilt:

| Vorgang | Verhalten |
|---|---|
| Reload | Dieselbe Instanz und Einheit aus dem Snapshot rekonstruieren |
| Reconnect | Identität erneut bestätigen, aktuellen Snapshot laden |
| Pause/Fortsetzen | Gespeicherten Fortschritt erhalten |
| Nächste Frage | Neue `unit_id` |
| Wiederholte Frage | Ebenfalls neue `unit_id` |
| Tatsächlicher Neustart | Neue `game_instance_id` |
| Alter Socket oder alte Aktion | Keine Änderung einer anderen Instanz zulassen |

Die neue UI prüft die vollständige erwartete Identität. Eine fremde Instanz wird nicht allein deshalb akzeptiert, weil ihre Revision höher ist.

Eine erste Antwort darf die globale Revision erhöhen, ohne dadurch die unabhängig zulässige zweite Antwort ungültig zu machen. Die Prüfung muss zwischen Anzeigeänderungen und einem tatsächlichen Wechsel des Aktionskontexts unterscheiden.

## 11. Gemeinsame Buzzer-Infrastruktur

`BuzzerGame.accept_buzz()` ist das konkrete vorhandene Vorbild:

- `atomic_write`.
- Datenbanksperren.
- Prüfung der Runde und Teilnahmeberechtigung.
- Exklusiver Gewinner.
- Gespeicherte Buzzreihenfolge.
- Sperre nach falschem Versuch.

Ein direktes Aufrufen dieser Methode aus anderen Duellspielen wäre ungeeignet, weil sie `BuzzerGame`, `BuzzerRound` und `BuzzerParticipant` verändert.

Die neue Duell-Buzzerkomponente verwaltet ausschließlich:

- Offen/gesperrt.
- Antwortberechtigte Partei.
- Buzzkennung und Annahmesequenz.
- Für die Einheit gesperrte Parteien.
- Eingefrorenen Zeit-/Präsentationsstand.

Der jeweilige Controller entscheidet über die Folgen einer Hostbewertung.

### Konkurrenz

Der erste gültige Buzz ist derjenige, der im serverseitig serialisierten Annahmepfad akzeptiert wird. Clientzeiten und behauptete Reaktionszeiten sind bedeutungslos.

Auf SQLite darf sich die Absicherung nicht allein auf `select_for_update()` stützen. Der vorhandene Betrieb nutzt bereits unmittelbare Schreibtransaktionen und Wiederholungen bei Datenbanksperren. Genau diese tatsächliche Datenbankkonfiguration muss in Konkurrenztests berücksichtigt werden.

Für PostgreSQL sind dieselben Invarianten mit echten Zeilensperren zu testen. Redis-Broadcasts ersetzen keine Datenbankentscheidung.

## 12. Gemeinsame Hostbewertung

Die Hostkomponente zeigt:

- Aktuelle Partei mit Antwortrecht.
- Zugehörigen Buzz beziehungsweise Zug.
- Eingefrorenen Hinweis-, Zeit- oder Toleranzstand.
- Richtig/Falsch.
- Ausstehende beziehungsweise abgeschlossene Bewertung.

Eine Bewertung muss auf einen konkreten `buzz_id` oder `turn_id` verweisen. Dadurch kann ein verspäteter Klick nicht versehentlich den nächsten Antwortberechtigten bewerten.

Die Komponente liefert eine Entscheidung an den Spielcontroller:

| Spiel | Richtig | Falsch |
|---|---|---|
| Quick Quiz Short Answer | Buzzende Partei `+1`, Ende | Gegner `+1`, Ende |
| Clue Rush | Buzzende Partei `+1`, Ende | Kein Punkt; Partei sperren, Gegner kann fortsetzen |
| Who is lying | Buzzende Partei `+1`, Ende | Gegner `+1`, Ende |
| Who is that | Buzzende Partei `+1`, Ende | Gegner `+1`, Ende |
| Wann war das | Noch zu präzisieren | Gegner `+1`, Ende |
| Gebotsspiel | Erfolgspunkte offen | Bei Nichterfüllung Gegner `+1` |

Doppelklickschutz im Browser ist zusätzlich sinnvoll. Die entscheidende Absicherung liegt in der einmaligen serverseitigen Bewertung.

## 13. Gemeinsame Matchverwaltung

Die Matchverwaltung unterscheidet drei Größen:

1. **Spielpunkte:** spieltypspezifische Buchungen.
2. **Rundensiege:** ausdrücklich ermittelter Gewinner einer Entscheidungseinheit.
3. **Matchsieg:** Ergebnis aus Abschlussmodus und festgelegten Regeln.

Ein Ergebnis sollte ausdrücklich unterscheiden können:

- Partei A gewinnt.
- Partei B gewinnt.
- Unentschieden.
- Kein Sieger.
- Fachliche Entscheidung fehlt.

Ein höherer Punktestand wird nicht pauschal in einen Rundensieg umgewandelt.

### Best of N

Konfiguration:

- Ungerades positives N.
- Benötigte Rundensiege: `(N + 1) / 2`.

Nach jedem abgeschlossenen Ergebnis:

- Rundensieg höchstens einmal verbuchen.
- Prüfen, ob die Siegschwelle erreicht ist.
- Bei feststehendem Sieg weitere Einheiten verhindern.
- Erst die vorgesehene Auflösung, danach Matchergebnis darstellen.

**Offen:** Zählen unentschiedene Einheiten gegen N? Die Vorgaben nennen sowohl eine maximale Zahl als auch „entscheidende Runden“. Daraus folgt noch keine eindeutige Behandlung von Unentschieden.

### Zielpunktzahl

Die vollständige Punkteänderung einer Auswertung wird gemeinsam angewendet. Anschließend wird für beide Parteien geprüft, ob das Ziel erreicht wurde.

So wird beispielsweise ein gleichzeitiges `+1/+1` nicht durch die Reihenfolge zweier Datenbankupdates entschieden.

Die Fortschrittsleisten zeigen:

- Partei.
- Tatsächlichen Punktestand.
- Zielwert.
- Auf den sichtbaren Bereich begrenzte Füllung.

Intern bleiben tatsächliche Punkte erhalten, auch wenn sie über dem Ziel liegen. Bei möglichen negativen manuellen Punkten ist die Beschriftung weiterhin numerisch nötig.

**Eine einzige Duell-Buchungsquelle:** Matchanzeige, Scorebox und Ergebnisansicht lesen dieselben `DuelScoreEntry`-Buchungen. Es gibt keine zweite, unabhängig gepflegte Duell-Punktesumme in den alten Teilnehmermodellen.

Die Übernahme von Duell-Matchsiegen in eine übergreifende Session-Gesamtwertung ist separat festzulegen. Die vorhandene Multiplayer-Gewichtung darf dafür nicht stillschweigend verwendet werden.

## 14. Spectator und Reveal-Steuerung

Der vorhandene Spectator besteht aus:

- `games_hub/spectator.py`.
- `templates/hub/spectate.html`.
- WebSocket-Benachrichtigungen über den Hub.
- Zusätzlichem Statusabruf alle 1,5 Sekunden.
- Eigenen Renderfunktionen wie `renderQuiz()`, `renderEstimation()` und `renderClueRush()`.

Er ist aktuell ausdrücklich als VHS-Seite aufgebaut. Die Teilnehmerdarstellung unterstützt dagegen Standard, Arcade und VHS.

**Empfehlung:** Eigener Duell-Spectator mit wiederverwendbaren Layout- und Darstellungselementen. Keine zusätzlichen Duell-Revealzweige in sämtlichen bestehenden Multiplayer-Renderern.

### Gemeinsame Duellphasen

Für Quick Quiz mit Geräteantworten und Estimation:

`Antwortphase → Teilnehmerantworten → Lösung → Wertung → nächste Einheit/Matchende`

Für Buzzer:

`Präsentation → Antwortrecht → Hostbewertung → Lösung → Wertung`

Für abwechselnde Spiele:

`Aktuelle Partei → Hostaktion → öffentliche Aktualisierung → Zugwechsel oder Abschluss`

### Zeitliche Steuerung

Ich empfehle zunächst hostgesteuerte Übergänge:

- Beide verbindlichen Antworten oder Fristabschluss schließen die Antwortphase.
- Die erste Revealphase zeigt die Teilnehmerantworten.
- Hostaktion „Lösung aufdecken“ öffnet die nächste Phase.
- Eine weitere bestätigte Fortsetzung übernimmt die Wertung und ermöglicht die nächste Einheit.

Damit sind keine willkürlichen Animationswartezeiten erforderlich. Reload und Reconnect rekonstruieren die gespeicherte Phase.

Ob später automatisch nach einer konfigurierbaren Zeit weitergeschaltet wird, ist eine zusätzliche Entscheidung. Dafür müssten Übergangszeitpunkte serverseitig gespeichert werden.

Lösungen, fremde Entwürfe und noch nicht veröffentlichte Wertungen dürfen nicht bereits im Payload stehen und nur per CSS verborgen sein. Insbesondere braucht eine Phase „Teilnehmerantworten“ einen Serializer, der diese Antworten liefert, die richtige Lösung aber weiterhin zurückhält.

## 15. Größte Regressionsrisiken

| Risiko | Konkreter Bezug | Absicherung |
|---|---|---|
| Duellstart setzt Multiplayer zurück | `create_session()`, `_activate_target_game()`, bestehende `start_quiz()`-Methoden | Eigene Instanz aus gelesener Konfiguration; keine Multiplayer-Startmethode |
| Falsche Session wird verändert | Auflösung über Spieltyp und Raumcode | Duellaktionen ausschließlich über Instanz und Schritt |
| Alte Antwort blockiert Wiederholung | Bestehende Frage-/Set-/Antwortzuordnungen | Jede Wiederholung erhält eigene Einheitenkennung |
| Punkte werden doppelt vergeben | Bewertung, Reveal, HTTP und WebSocket | Ein transaktionaler Befehlspfad und eindeutige Buchungsursachen |
| Hostentscheidung trifft nächsten Buzz | Wiederholte oder verspätete Aktionen | Entscheidung an konkrete Buzz-/Zugkennung binden |
| Timer läuft nach Buzz weiter | Typewriter, Clue-Zeitplan, Toleranz | Eingefrorenen Serverstand speichern |
| Gemeinsamer Guard blockiert Duellaktionen | `validate_and_reserve_action()` | Duellspezifische Aktionsregeln außerhalb alter Consumer |
| Öffentliche Lösung erscheint zu früh | Spectator-Serializer und bestehende Ergebnis-Payloads | Rollen- und phasenabhängige Duellprojektionen |
| Bereitschaftscheck während Duell | `readiness.py`, `_iter_session_games()`, Lobbypräsenz | Duelllaufzeit in die zentrale Aktivitäts-/Präsenzprüfung einbeziehen |
| Sessionende beendet Vorlage statt Duell | `HubConsumer.complete_games_for_session()` | Modusgrenze vor bestehenden spieltypspezifischen Abschlusswegen |
| Theme verändert neue Bedienelemente | `accessibility_widget.html` | Browserprüfungen aller drei Themes |
| Duellergebnisse verändern Gesamtwertung | `get_leaderboard_data()`, `result_snapshot` | Eigener Ergebnisadapter; keine automatische Multiplayer-Rangkonvertierung |
| Neue Modelle beeinflussen Sync/Restore | `games_website/services.py` | Sync-Kompatibilität separat prüfen |

Der letzte Punkt ist relevant: `sync_all_models_to_supabase()` entdeckt grundsätzlich alle verwalteten Modelle. Neue Duelltabellen sind dadurch nicht automatisch vom bestehenden Sync ausgeschlossen.

Die Erfahrungen der vorherigen Umstrukturierung sprechen gegen einen gleichzeitigen Umbau von Consumer, State, Scorebox, Navigation und Themes. Diese Grenzen müssen nacheinander integriert und jeweils mit Multiplayer-Vergleichstests abgesichert werden.

## 16. Konkrete Teststrategie

### Duelltests je Spieltyp

Für jeden freigeschalteten Typ wird derselbe Lebenszyklus geprüft:

Start → Intro → Wartescreen → aktive Einheit → Eingabe/Buzz/Hostzug → Bewertung → Reveal → Punkte → nächste Einheit → Matchende.

Zusätzlich jeweils:

- Reload in jeder wesentlichen Phase.
- Verbindungsabbruch und Wiederbeitritt.
- Pause und Fortsetzen.
- Neustart mit neuer Instanz.
- Wiederholung derselben Frage mit neuer Einheit.
- Keine Übernahme alter Antworten oder Sperren.

### Spieltypspezifische Kernfälle

| Typ | Zusätzliche Prüfungen |
|---|---|
| Quick Quiz | Beide Geräte können unabhängig abgeben; `+1/+1`; Typewriter friert beim Buzz ein; zweistufiges Reveal |
| Estimation | Gleiche Abweichung ergibt beide `+1`; keine Geschwindigkeitsentscheidung; negative Werte und Zielwert null |
| Assign | Abwechselnde Züge; bereits gelöstes Paar gesperrt; erster Fehler beendet Einheit |
| Sorting | Gültige Einfügeposition; nur Elemente des aktuellen Sets; Fehler beendet Einheit |
| Wer weiß mehr | Alphabetische Reihenfolge; synchrones Aufdecken; keine doppelte Antwortwertung |
| Clue Rush | Buzz stoppt Fortschritt; falsche Partei bleibt gesperrt; Gegner kann fortsetzen |
| Where | Karten und Koordinaten; beide Markierungen; Distanzvergleich unabhängig von Anzeigerundung |
| Who is lying | Aktuellen Personenstand einfrieren; richtig/falsch/kein Buzz |
| Who is that | Identisches Bild; exklusives Antwortrecht; gemeinsame Auflösung |
| Black Jack | Sterne und Matchpunkte getrennt; vorzeitiger Setsieg; letzte Frage zuerst auflösen |
| Wann war das | Buzz exakt an Stufengrenze; gespeicherte Toleranz bleibt über Reload erhalten |
| Host-Punkte | Positive/negative Deltas; wiederholte Aktion; Rundenabschluss |
| Buzzer | Richtige/falsche Bewertung; Sperren; erneute Freigabe |
| Gebotsspiel | Höheres Gebot gewinnt unabhängig von Zeit; Gleichstand nach Serversequenz; Wiederholung verändert nichts; Counter und Frist |

### Multiplayer-Regressionen

Vorhandene geeignete Ausgangspunkte sind unter anderem:

- `games_hub/test_authoritative_state.py`
- `games_hub/test_active_game_guard.py`
- `games_hub/test_host_security.py`
- `games_hub/test_game_start_intro.py`
- `games_hub/test_readiness.py`
- `games_hub/test_lobby_return_flow.py`
- `games_hub/test_spectator.py`
- `games_hub/test_spectator_browser.py`
- `games_hub/test_session_repairs.py`
- `games_hub/test_session_concurrency.py`
- `games_hub/test_session_evening_simulations.py`
- Die `tests.py`- und Playability-/Browser-Tests der einzelnen Spielapps.

Prüfziel ist nicht nur ein erfolgreicher Endpunkt. Zu vergleichen sind:

- Bisherige Punkte.
- Bisherige Phasen und Hostaktionen.
- Bisherige Spectator-Payloads.
- Anzahl und Art ausgesendeter Events.
- Verhalten alter Sessions ohne Duellkonfiguration.
- Unveränderter Multiplayer-Zustand nach einem parallel bearbeiteten Duell.

Die vorhandenen 25-Teilnehmer-Szenarien bleiben Bestandteil der Multiplayer-Abnahme.

### Konkurrenztests

Mit echten getrennten Datenbankverbindungen und synchronisiertem Start:

- Zwei nahezu gleichzeitige Buzzes.
- Zwei gleiche Gebote.
- Gleiche Zeitstempel mit unterschiedlicher Annahmesequenz.
- Dieselbe Aktion über HTTP und WebSocket.
- Doppelte Hostbewertung.
- Bewertung gegen veraltete Buzzkennung.
- Antwort gleichzeitig mit Fristabschluss.
- Reconnect während Reveal.
- Alte Aktion nach Neustart.
- Zweite gültige Teilnehmerantwort nach Revisionserhöhung durch die erste.

Dateibasierte SQLite-Tests sind hier erforderlich; reine In-Memory-Tests bilden den vorhandenen Betrieb nicht ausreichend ab. Zusätzlich sollten die Transaktionsinvarianten gegen PostgreSQL geprüft werden.

### Matchtests

Best of Three/Five/Seven, frühzeitiger Sieg, Zielpunktzahl, Überschreiten des Ziels, Neustart nach Abschluss und einmalige Buchung.

Für Unentschieden, fehlenden Rundensieger und gleichzeitiges Erreichen des Ziels werden zunächst **offene Testspezifikationen** dokumentiert. Es wird kein beliebiger Sieger als Erwartungswert festgeschrieben.

Tests müssen eine isolierte Testdatenbank verwenden. Die konfigurierte Datei `test_db.sqlite3` sollte nicht ungeprüft als Arbeitsdatenbank vorausgesetzt werden.

## 17. Implementierungsreihenfolge

Die gewünschte Reihenfolge ist sinnvoll. Vor dem ersten spielbaren Typ sind wenige zusätzliche Isolationsschritte notwendig.

| Phase | Kleine, separat abnehmbare Schritte |
|---|---|
| **1. Verträge und Isolation** | Verträge, Sessiontyp/Spielformat-Persistenz, `DuelGameInstance`, `DuelUnit`, `DuelSubmission`, `DuelDecision`, `DuelScoreEntry`, `DuelActionReceipt` und `execute_action()` sind implementiert. Die isolierte Matchverwaltung mit Best of N, Zielpunktzahl, `DuelUnitOutcome`, `DuelMatchOutcome` und internen Snapshots ist ebenfalls implementiert; technische Details und offene Gesamtabnahme stehen in [DUEL_MATCH_ENGINE.md](DUEL_MATCH_ENGINE.md). Keine HTTP-/WebSocket-/UI-Freischaltung. Formatlauf-/Set-/Begegnungszuordnung, Abgabeservice, rollenabhängige Spielprojektionen, Authentifizierungsadapter und einzelne Hub-Anbindungen bleiben weitere getrennte Schritte. Details und Abhängigkeiten in Abschnitt 19. |
| **2. Estimation** | 1. Zwei Zahleneingaben. 2. Eigene Vergleichswertung. 3. Revealphasen. 4. Matchabschluss nach geklärten Regeln. 5. Browser- und Multiplayer-Abnahme. |
| **3. Buzzer und Quick Quiz** | 1. Atomarer Buzzerkern. 2. Hostbewertung. 3. Quick Quiz Short Answer. 4. Einfrieren der Aufdeckung. 5. MC. 6. True/False. Jeweils einzeln testen. |
| **4. Weitere Buzzerspiele** | Who is that vollständig abschließen; anschließend Who is lying; danach Clue Rush; zuletzt Wann war das. Pro Typ zuerst Regeln, dann Controller, dann UI/Reveal und Abnahme. |
| **5. Abwechselnde Spiele** | Nach Klärung der Wertung zuerst Assign, danach Sorting Ladder, anschließend Wer weiß mehr. Keine gleichzeitige Umstellung. |
| **6. Weitere Typen** | Where mit Kartenadapter; danach Black Jack mit vorzeitigem Setsieg; danach Host-Punktevergabe; anschließend eigenständiger Buzzer. |
| **7. Gebotsspiel** | Aufgabenmodell, verdeckte verbindliche Gebote, Annahmesequenz, Auswahl, Erfüllungstimer, Counter, Wertung. Erfolgspunkte vorher klären. |
| **8. Integration** | Vollständige Sessiondurchläufe, alle Themes, Parallelität, Wiederverbindung, Neustart, Migration/Sync und 25-Teilnehmer-Multiplayer-Abnahme. |

Regressionstests laufen bereits nach den einzelnen Schritten. Phase 8 ist die vollständige Abschlussprüfung.

Ein Typ wird erst in der UI freigeschaltet, wenn sein Ablauf und seine erforderlichen Fachentscheidungen vollständig sind. Eine noch unvollständige Duellkonfiguration darf nicht auf Multiplayer zurückfallen.

## 18. Offene fachliche Entscheidungen und kleinster erster Auftrag

### Vor der jeweiligen Implementierung zu klären

1. **Best of N:** Zählen Unentschieden gegen die Höchstzahl? Was passiert nach N gespielten Einheiten ohne ausreichende Rundensiege?
2. **Gleichzeitiges Ziel:** Wie wird entschieden, wenn beide durch dieselbe Auswertung das Ziel erreichen?
3. **Punktlose Einheit:** Wiederholen, fortsetzen, als unentschieden zählen oder anders behandeln?
4. **Assign, Sorting, Wer weiß mehr:** Punkte bei korrektem Zug, beim ersten Fehler und bei vollständig gelöstem Set; Bedeutung einer Match-Entscheidungseinheit.
5. **Abwechselnde Spiele:** Wer beginnt, und wie wechselt der Startspieler zwischen Sets?
6. **Where:** Entfernungsgleichstand und Punktehöhe eines Sieges.
7. **Gebotsspiel:** Erfolgspunkte, Gebotsgrenzen, Gebotsfrist und fehlende Abgabe.
8. **Wann war das:** Host erfasst Zahl oder bewertet Toleranzeinhaltung selbst? Welche erfolgreichen Punkte werden übernommen?
9. **Black Jack:** Sofortentscheidung nach Einzelabgabe oder gemeinsame Fragenauswertung; beide überboten; Gleichstand; Punkte bei vorzeitigem Setsieg.
10. **Quick Quiz:** Behandlung von `double_answer` und mehreren Antwortfeldern.
11. **Who is lying:** Aktuelle Person oder beliebiger Lügner des Sets als mündliche Antwort.
12. **Nichtabgabe und Entwürfe:** Welche Geräteantworten werden beim Fristabschluss berücksichtigt?
13. **Hostausfall:** Übergänge erst mit Host oder automatische Duell-Frist-/Revealverarbeitung? Die bisherige Konvention „auf den Host warten“ ist ein geeigneter Ausgangspunkt, aber keine automatisch übertragene Duellregel.
14. **Abbruch und Korrekturen:** Offene Einheit, bereits gebuchte Punkte und Korrekturen nach feststehendem Matchsieg.
15. **Sessionrahmen:** Sessiontypen und besondere Formate sind durch die neuen Anforderungen festgelegt; siehe Abschnitt 19. Offen bleiben unter anderem Austausch einer Partei sowie die übergreifende Wertung von Begegnungen und Turnieren.
16. **Host-Punkte/Buzzer:** Wie wird ein Rundensieger für Best of N ausdrücklich festgestellt?

Vorhandene Konventionen helfen bei Eingaben, Rangdarstellung, Pausen und historischen Ergebnissen. **Sie lösen diese Duellentscheidungen nicht eindeutig.**

### Beurteilung des Klonens

**Vollständiges Klonen bestehender Spielskripte ist nicht sinnvoll.** Damit würden auch alte Timer, Socketwege, Scoreberechnungen und Navigationsannahmen dupliziert. Spätere Fehlerkorrekturen müssten dann mehrfach erfolgen.

Sinnvoll sind kleine neue Duellcontroller und gezielt gemeinsam genutzte Daten, Berechnungen und UI-Bausteine. Die Multiplayer-Controller bleiben dabei bestehen. Eine nachträgliche gemeinsame Abstraktion kommt nur infrage, wenn zwei konkrete Implementierungen tatsächlich denselben technischen Ablauf benötigen.

### Kleinster erster Implementierungsschritt

Historischer erster Auftrag; inzwischen umgesetzt:

> Lege ausschließlich einen noch nicht eingebundenen Duell-Vertrag in `duel/contracts.py` mit Tests an. Definiere unveränderlichen Instanzkontext, zwei unterschiedliche Teilnehmer-IDs sowie getrennte Angaben für Spielpunkte, Rundensieger und Matchstatus. Unentschieden und ungeklärte Ergebnisse müssen ausdrücklich darstellbar sein. Keine Models, Migrationen, Registrierung, URLs, Consumer, Templates oder Änderungen an bestehenden Multiplayer-Dateien.

Dieser erste Schritt und die anschließenden beiden Persistenzschritte sind umgesetzt. Der jetzt empfohlene kleinste nächste Auftrag steht in Abschnitt 19.12. Die folgenden Architekturergänzungen sind keine Implementierungsfreigabe.

## 19. Erweiterung: Sessiontypen, besondere Spielformate und wechselnde Paarungen

### 19.1 Festgelegte Anforderungen und Grenzen

**Verbindlich durch die neuen Anforderungen:**

- Eine als 2-Spieler-Session geplante Session lässt genau zwei Parteien zu. Alle ihre Spiele verwenden automatisch die Duellvariante.
- Eine normale Mehrspieler-Session verwendet standardmäßig die bisherigen Multiplayer-Regeln. Finalduell und Turnier sind besondere Formate einzelner Spiele innerhalb dieser Session.
- Der Sessiontyp bleibt während einer laufenden Session unverändert. Als technische Sperrgrenze wird die erste Aktivierung empfohlen, entsprechend `HubSession.activate_exclusive()` beziehungsweise `started_at`. Inaktivsetzen/Fortsetzen hebt diese Sperre nicht auf. Historische Sessions werden nicht nachträglich umgedeutet.
- Im Finalduell wählt der Host genau zwei bestehende Teilnehmer. Alle anderen bleiben Sessionteilnehmer und erhalten keine Duell-Aktionsrechte.
- Im Turnier werden pro Set neue zufällige Zweierpaarungen unter den anwesenden Teilnehmern gebildet. Buzzerbasierte Varianten sind ausgeschlossen.
- „Alle gegen einen“ ist ein späteres Format mit mehr als zwei Beteiligten und derzeit offenen Regeln. Es wird weder freigeschaltet noch auf die vorhandenen Zweierverträge umgebogen.

**Weiterhin verbindlich:** Keine Registrierung neuer Multiplayer-Regeln unter alten Endpunkten, keine Änderungen an bestehenden Spielconsumer-Abläufen und keine vollständigen Kopien der Spielskripte. Der bisherige Multiplayer-Pfad bleibt der Default für Bestandsdaten und normale Spiele.

Eine getrennte Zuschauerseite zählt nicht als dritte Partei. Ein Disconnect darf einen belegten Platz der 2-Spieler-Session nicht automatisch für eine neue Identität freigeben; Reconnect verwendet den bestehenden `HubParticipant`. Ob ein Host eine Partei vor oder zwischen Spielen austauschen darf, bleibt offen.

### 19.2 Getrennte Begriffe und Zuständigkeiten

| Begriff | Bedeutung und Verantwortungsbereich |
|---|---|
| **Sessiontyp** | Zulässiger Rahmen der gesamten `HubSession`: normale Mehrspieler-Session oder reine 2-Spieler-Session. Bestimmt insbesondere Zulassung und zulässige Formate. |
| **Spielformat** | Geplante Ausführungsart eines `HubGameStep`: Multiplayer, Duell, Finalduell, Turnier; später gegebenenfalls „Alle gegen einen“. Ein Finalduell ändert nicht den Sessiontyp. |
| **Formatlauf** | Ein konkreter Start dieses Spielschritts mit festgehaltenem Format und Einstellungen. Ein echter Neustart erhält eine neue Laufkennung. Ein Formatlauf kann eine oder mehrere Begegnungen umfassen. |
| **Turnierset** | Gemeinsame Organisationsstufe, für die ein Teilnehmerpool und neue Paarungen festgehalten werden. Es gehört zu genau einem Formatlauf. Seine Nummer ist keine `DuelUnit.number`. |
| **Konkrete Begegnung** | Zwei feste Parteien mit eigener Laufzeit und Wertung: die vorhandene `DuelGameInstance`. Die Parteien werden während dieser Begegnung nicht ersetzt. |
| **Entscheidungseinheit** | Ein konkretes Auftreten von Frage, Runde oder Set innerhalb genau einer Begegnung: die vorhandene `DuelUnit`. Ihre Nummerierung beginnt je Begegnung unabhängig. |
| **Matchwertung** | Spielpunkte, Rundensiege und Matchabschluss innerhalb einer konkreten Begegnung. Die vorhandene Trennung aus Abschnitt 13 bleibt bestehen. |
| **Turnier-/Formatwertung** | Eine gegebenenfalls spätere Auswertung mehrerer Begegnungen desselben Formatlaufs. Es ist noch keine Punkteformel oder Turnierentscheidung festgelegt. |
| **Session-Gesamtwertung** | Die übergreifende Wertung der ganzen Session. Ihre bisherige Multiplayer-Berechnung ist keine automatisch gültige Formel für besondere Formate. |

Der technische Regelpfad einer Zweierbegegnung bleibt `duel`, unabhängig davon, ob sie aus einer 2-Spieler-Session, einem Finalduell oder einem Turnier stammt. Sessiontyp und Spielformat sind keine neuen `game_key`-Werte. `game_key` bezeichnet weiterhin den Spieltyp, beispielsweise `estimation`.

| Sessiontyp | Effektiver Standard | Besondere Formate |
|---|---|---|
| Reine 2-Spieler-Session | Duell für jeden Spielschritt | Keine Abweichung auf Multiplayer, Turnier oder „Alle gegen einen“. |
| Normale Mehrspieler-Session | Multiplayer | Finalduell mit ausdrücklicher Zweierauswahl oder freigegebener Turniervariante. „Alle gegen einen“ bleibt reserviert und gesperrt. Ein Zweierduell wird hier als Finalduell geplant. |

Die tatsächliche Zahl aktuell verbundener Geräte ändert diese Auswahl nicht automatisch. Eine Mehrspieler-Session mit nur zwei Anwesenden bleibt eine Mehrspieler-Session.

### 19.3 Bewertung des implementierten Stands

**`duel/contracts.py`:** `DuelInstanceContext` bindet Session, Spielschritt, Duellinstanz, aktuelle Unit, Spieltyp und zwei unterschiedliche Integer-Teilnehmer-IDs. `ScoreChange`, `RoundResult` und `MatchState` bleiben geeignet. Format- und Turnierkontext werden später aus einer Zuordnung aufgelöst; die bestehenden Verträge müssen dafür jetzt nicht verändert werden.

**`DuelGameInstance`:** Die feste Bindung von `participant_a` und `participant_b` ist für alle geforderten 1-gegen-1-Begegnungen geeignet. Bereits heute können mehrere Instanzen denselben `HubGameStep` referenzieren. UUID und eigene Revision trennen diese Läufe. Die Instanz besitzt noch keinen Verweis auf ein gemeinsames Turnierset oder einen Formatlauf. `clean()` prüft die Sessionzugehörigkeit, führt aber keine Start-, Berechtigungs- oder Turnierprüfung aus; `save()` ruft `full_clean()` weiterhin nicht automatisch auf.

Das implementierte ORM-Modell verhindert den nachträglichen Austausch seiner Parteien noch nicht. Die hier geforderte feste Bindung muss der spätere autoritative Schreibpfad unter Sperre garantieren; sie ist keine bereits vorhandene Datenbankgarantie. Der unveränderliche Datenvertrag allein ersetzt diesen Schutz nicht.

**`DuelUnit`:** Integer-ID, eindeutige UUID, Elternreferenz und die Unique-Constraint auf `(game_instance, number)` passen unverändert. Zwei Begegnungen dürfen jeweils Unit 1 besitzen. Mehrere Units können später dasselbe Material referenzieren. Die Statuswerte sind bewusst allgemein; ein globales Turnierset oder ein Paarungswechsel wird nicht als zusätzlicher Unitstatus eingeführt.

**Nicht geeignet wäre**, `participant_a` und `participant_b` beim nächsten Turnierset an derselben `DuelGameInstance` auszutauschen. Bereits vorhandene Units, künftige Abgaben, Punkte und verspätete Nachrichten würden dadurch mit einer neuen Paarung verbunden. Auch ein A/B-Tausch derselben Personen darf abgeschlossene Historie nicht neu zuordnen.

**Ergebnis:** Beide fertigen Modelle und die Migrationen `0001` und `0002` können unverändert bleiben. Die fehlende Gruppierung wird additiv ergänzt. Modelländerungen an diesen beiden Tabellen sind für die empfohlene Zuordnung nicht erforderlich.

### 19.4 Vergleich der Architekturvarianten

| Kriterium | A: Nur eigenständige `DuelGameInstance` je Begegnung | B: Übergeordneter Formatlauf mit expliziter Set-/Begegnungszuordnung |
|---|---|---|
| Feste Parteien und State-Isolation | Gut; jede Paarung erhält eine eigene UUID. | Ebenfalls gut, wenn die vorhandene `DuelGameInstance` die konkrete Begegnung bleibt. |
| Reine 2-Spieler-Session / Finalduell | Für den einzelnen Spielschritt grundsätzlich ausreichend. | Eine Begegnung unter einem Formatlauf; derselbe Zweier-Regelpfad bleibt nutzbar. |
| Wechselnde Paarungen je Turnierset | Ohne zusätzliche Relation fehlt eine verlässliche gemeinsame Setzuordnung. Raumcode, Reihenfolge oder JSON-Konfiguration ersetzen diese nicht. | Gemeinsames Turnierset, Teilnehmerpool, Begegnungen und Wiederholungen sind explizit zugeordnet. |
| Neustart des gesamten Turnierspiels | Schwer vom nächsten Set oder von mehreren Begegnungen desselben Schritts zu unterscheiden. | Neue Formatlaufkennung trennt Neustart und Setfortschritt. |
| Parallelität und gemeinsamer Hostablauf | Viele lose aktive Instanzen; ein globaler „aktueller Gegner“ oder „aktuelles Spiel“ wäre fehleranfällig. | Gemeinsamer organisatorischer Rahmen mit getrennten untergeordneten Begegnungen. Die Ausführungsstrategie bleibt offen. |
| Duplizierung | Gering, solange keine Gruppierungsinformationen wiederholt in `rule_config` abgelegt werden. | Gering bei normalisierter Zuordnung; Teilnehmer, Spielinhalte und Punkte werden nicht in jedem Organisationsdatensatz dupliziert. |
| Änderung bestehender Modelle | Keine; für ein vollständiges Turnier ist trotzdem zusätzliche Gruppierung nötig. | Keine erforderlich, wenn die Beziehungen zunächst über neue Zuordnungsdatensätze ergänzt werden. |
| Spätere Formate mit mehr Beteiligten | Zweierinstanzen allein bilden diese nicht sauber ab. | Der Formatlauf kann später einen anderen Regelpfad orchestrieren, ohne Zweierverträge zu lockern. |

**Empfehlung: Variante B additiv, mit einer eigenen bestehenden `DuelGameInstance` je Begegnung.** Es entsteht keine zweite konkurrierende Zweier-Begegnungsimplementierung. Die Ergänzung ist eine organisatorische Klammer für die bereits isolierten Begegnungen.

Abgelehnt wird eine andere Auslegung von B: `DuelGameInstance` zum allgemeinen Turniercontainer umzubauen, Parteien daraus zu entfernen und `DuelUnit` auf ein neues Begegnungsmodell umzuhängen. Dafür müssten bereits fertige Verträge, Modelle und Historien neu strukturiert werden. Das ist für die neuen Anforderungen nicht nötig.

### 19.5 Empfohlene additive Zuordnung

Die folgenden Namen sind **Planungsvorschläge, keine implementierten Modelle**:

| Geplanter Datensatz | Minimaler Zweck |
|---|---|
| `GameFormatRun` | Eigene Laufkennung, Session, Spielschritt, effektives Spielformat und festgehaltene organisatorische Einstellungen. Für gewöhnliches Multiplayer wird kein neuer Laufzeitadapter erzwungen. |
| `TournamentSet` | Identität und Nummer innerhalb eines Formatlaufs, gespeicherter Teilnehmerpool zum Auslosungszeitpunkt und organisatorischer Zustand. Konkrete Materialzuordnung bleibt fachlich zu klären. |
| `DuelEncounterBinding` | Eindeutige Zuordnung einer `DuelGameInstance` zu einem Formatlauf und optional zu dessen Turnierset; gegebenenfalls Platz-/Begegnungsnummer. Keine zweite Speicherung der Parteien, Antworten oder Punktestände. |

`DuelEncounterBinding` referenziert die vorhandene Duellinstanz eindeutig. Dadurch können ihre Tabellenfelder unverändert bleiben. Bei reinen Duellspielen und Finalduellen verweist die Zuordnung nur auf den Formatlauf. Bei Turnieren ist zusätzlich das Turnierset erforderlich. Ein Turnierset darf nicht zu einem anderen Formatlauf gehören als die Begegnungszuordnung. Session, Spielschritt und Spieltyp der Begegnung müssen zum Formatlauf passen.

Die Reihenfolge ist künftig:

`HubSession → HubGameStep → GameFormatRun → TournamentSet → Begegnungszuordnung → DuelGameInstance → DuelUnit`

Bei reinem Duell und Finalduell entfällt die Zwischenstufe `TournamentSet`. Eine zweite Team- oder Teilnehmerverwaltung entsteht nicht: Die Parteien referenzieren weiterhin `HubParticipant`.

Beispiel für einen Formatlauf mit vier Teilnehmern; die zweite Auslosung ist nur eine mögliche zufällige Paarung und keine Festlegung einer Wiederholungsvermeidung:

| Turnierset | Begegnung | Feste Parteien | Lokale Unitnummer |
|---|---|---|---|
| Set 1 | Duellinstanz A | Teilnehmer 1 / Teilnehmer 2 | 1 |
| Set 1 | Duellinstanz B | Teilnehmer 3 / Teilnehmer 4 | 1 |
| Set 2 | Duellinstanz C | Teilnehmer 1 / Teilnehmer 3 | 1 |
| Set 2 | Duellinstanz D | Teilnehmer 2 / Teilnehmer 4 | 1 |

Eine Begegnung kann je nach Spiel mehrere lokale Units umfassen. Wie eine spieltypspezifische Frage oder ein Set auf diese Units abgebildet wird, bleibt bei den bestehenden Controllerplänen. Die lokale Nummer 1 bezeichnet nie automatisch das globale Turnierset 1.

Beim nächsten Turnierset entsteht für jede Paarung eine neue Duellinstanz, auch wenn zufällig dieselben beiden Teilnehmer erneut gegeneinander spielen. Eine Instanz gehört höchstens zu einem Turnierset. Ein Neustart des ganzen Spielschritts erzeugt einen neuen Formatlauf mit neuen Set- und Begegnungsidentitäten.

Die gemeinsame Identität einer späteren Aktion umfasst Session, Spielschritt, Formatlauf, gegebenenfalls Turnierset, Begegnung und Unit. Zusätzliche IDs im Payload werden gegen die serverseitig aufgelöste Beziehung geprüft. Sie dürfen diese Beziehung nicht frei festlegen.

**Auslosung und Konkurrenz:** Die spätere Setvorbereitung friert den anwesenden, zugelassenen Teilnehmerpool ein, lost serverseitig aus und speichert die tatsächliche Zuordnung atomar. Reload, Reconnect oder erneuter Klick dürfen keine neue Auslosung desselben Sets erzeugen. Eine Partei darf innerhalb desselben Turniersets nicht in zwei Begegnungen stehen, auch nicht einmal als A und einmal als B. Zwei getrennte Unique-Constraints auf A beziehungsweise B würden diese rollenübergreifende Bedingung nicht vollständig absichern. Der spätere Schreibpfad muss sie unter einer gemeinsamen Setsperre prüfen; eine zusätzliche normalisierte Platzzuordnung ist erst bei der konkreten Persistenzplanung zu entscheiden.

### 19.6 Zulässige Spiele und organisatorische Grenzen des Turniers

„Ohne Buzzer“ ist eine notwendige Freigabebedingung der **tatsächlich gewählten Duellvariante**, nicht bloß ihres Spieltypnamens.

- Quick Quiz MC/True-False kann ein Kandidat sein; Short Answer ist im geplanten Duell buzzerbasiert und damit ausgeschlossen. Ein gemischter Fragenpool darf nicht stillschweigend als vollständig turnierfähig gelten.
- Estimation, Where, Assign, Sorting Ladder, Wer weiß mehr und Black Jack sind nach den geplanten Regeln Kandidaten ohne Buzzer. Das ist noch keine Freigabe ihrer nicht implementierten Turnierabläufe.
- Host-Punktevergabe benötigt vor einer Turnierfreigabe einen definierten Begegnungs-/Setabschluss.
- Buzzer, Clue Rush, Who is lying, Who is that und Wann war das verwenden im geplanten Duell einen Buzzer und werden im Turnier nicht angeboten.
- Das Gebotsspiel verwendet zwar keinen Buzzer, bleibt aber bis zur Klärung und Implementierung seiner Regeln gesperrt.

Eine spätere Fähigkeitenbeschreibung pro Controller sollte mindestens Turnierunterstützung, Buzzerbedarf und die unterstützte Ausführungsart ausdrücken. Buzzerfreiheit beweist noch nicht, dass mehrere mündliche Spiele gleichzeitig von einem Host bedient werden können. Die Prüfung erfolgt sowohl in der Planung als auch bei der serverseitigen Vorbereitung; ein versteckter UI-Eintrag allein ist keine Absicherung.

**Weiter offen:** ungerade Teilnehmerzahl, Freilos oder andere Behandlung, parallele gegenüber nacheinander laufenden Begegnungen, Ende eines gemeinsamen Sets und Regeln für Wiederholungspaarungen. Es wird kein Platzhaltergegner erzeugt und kein `DuelGameInstance`-Datensatz mit nur einer Partei angelegt.

Die Zuordnung muss beide Ausführungsstrategien ermöglichen, ohne sie jetzt zu entscheiden. Ein später erlaubter paralleler Betrieb hat getrennte Begegnungszustände und Timer. Die bestehende Multiplayer-Regel „ein aktives Spiel“ wird nicht global gelockert: Ein Formatlauf ist der aktive Hub-Spielkontext; seine untergeordneten Begegnungen werden innerhalb des neuen Formatpfads verwaltet. Dafür ist später eine schmale Anbindung an `games_hub/active_game_guard.py` nötig, kein Umbau der Multiplayer-Consumer.

### 19.7 Teilnahme, Aktionsrechte und State-Isolation

Sessionmitgliedschaft und Begegnungsberechtigung werden ausdrücklich getrennt:

| Person | Sessionteilnahme | Aktionen in einer konkreten Begegnung |
|---|---|---|
| Ausgewählte Partei A/B | Bleibt erhalten. | Nur mit bestätigter eigener Identität und gültigem Kontext dieser Begegnung. |
| Andere Sessionteilnehmer beim Finalduell | Bleibt unverändert erhalten. | Keine Antwort-, Buzzer- oder anderen Duellaktionen. Nur freigegebene öffentliche Ansicht. |
| Teilnehmer einer anderen Turnierpaarung | Bleibt erhalten. | Ausschließlich Aktionen der eigenen aktuell zugeordneten Begegnung. |
| Noch nicht zugeteilter oder wartender Teilnehmer | Bleibt erhalten. | Keine Begegnungsaktionen bis zur ausdrücklichen Zuordnung. |
| Host | Bestehende Session-/Benutzerberechtigung. | Gesonderte Hostaktionen nach Prüfung des zugehörigen Format- und Begegnungskontexts. |

Die vorhandenen Funktionen `issue_rejoin_token()` und `_token_matches_participant()` in `games_hub/lobby_join.py` belegen die Hub-Identität. Sie erteilen allein keine Begegnungsberechtigung. Der spätere Duell-Schreibpfad prüft bei jeder Mutation die aufgelöste Unit, deren Duellinstanz und die Mitgliedschaft in genau deren festem A/B-Paar. Er prüft außerdem Formatlauf, Setfreigabe, Status und Frist. Ein beliebiger fremder `participant_id` oder eine bekannte Unit-UUID im Payload reichen nicht aus.

Nicht ausgewählte Teilnehmer werden nicht aus `HubSession.participants` entfernt und nicht durch Ändern von `scoring_eligible`, `is_active` oder einem permanenten Ausschluss aus dem restlichen Sessionablauf gedrängt. Diese Felder steuern bestehende Check-in-, Präsenz- und Wertungsabläufe und sind kein Ersatz für Begegnungsrechte.

`HubGameParticipantSnapshot.create_for_step()` friert heute den offiziellen Teilnehmerpool eines Spielschritts ein. Das ist keine Paarungsliste und kein vollständiger Autorisierungsnachweis für ein Finalduell. Die neue Zuordnung ergänzt diesen Kontext, statt die übrigen Sessionteilnehmer aus dem bestehenden Pool zu löschen.

Für die Turnierauslosung kann `games_hub/authoritative_state.py::connected_participant_ids()` die technische Verbindungspräsenz liefern. Ob „anwesend“ zusätzlich Check-in, Lobbyaufenthalt oder Hostbestätigung verlangt, ist fachlich offen. Nach dem Einfrieren eines Sets darf der nächste Präsenz-Poll die Paarung nicht verändern. Späte Beitritte, Ausfälle und Ersatzteilnehmer benötigen eine gesonderte Regel.

WebSocket-Gruppen, Antwortspeicherung, Hostentscheidungen und Revisionen bleiben an Begegnung und Unit gebunden. Organisatorische Gruppen auf Formatlauf-/Setebene dürfen nur dafür freigegebene Daten liefern. Fremde Entwürfe und noch nicht aufgedeckte Lösungen bleiben privat. Alte Nachrichten einer Begegnung aus Set 1 dürfen weder die neue Paarung noch die Gesamtansicht von Set 2 ändern.

### 19.8 Wertung und „Alle gegen einen“

Die bestehende Trennung von Punkten, Rundensiegen und Matchstatus gilt weiterhin **innerhalb einer Begegnung**. Unterschiedliche Paarungen teilen keine laufende Punktesumme und keine Match-Siegserie. Wenn im nächsten Turnierset die Parteien wechseln, wird ein noch laufendes Best-of-Match nicht mit anderen Personen fortgesetzt.

**Offen:** Ob ein Turnierset bereits ein vollständiges Match darstellt, wie viele Entscheidungseinheiten es enthält und ob Best of N beziehungsweise Zielpunktzahl im Turnier pro Begegnung oder in einem anderen fachlich definierten Rahmen gelten. Eine übergreifende Turnierwertung ist ein eigener Ergebnisadapter über abgeschlossene Begegnungen, keine Umdeutung wechselnder Paarungen zu einem einzigen Zweiermatch.

Die spätere Punktebuchung bleibt im gemeinsamen Duell-Buchungspfad je Begegnung. Format- und Sessionanzeigen dürfen daraus Ergebnisse ableiten; sie sollen keinen zweiten unabhängig fortgeschriebenen Duellpunktestand führen.

`games_hub/views.py::get_leaderboard_data()` rechnet derzeit abgeschlossene Spielschritte mit Multiplayer-Teilnehmerpools, Rangpunkten und gegebenenfalls Gewichtung zusammen. Diese Berechnung wird nicht ungeprüft auf Finalduelle oder Turniere angewendet. Nicht beteiligte Finalteilnehmer erhalten den Ergebnisstatus „nicht beteiligt“; fehlende Begegnungsdaten sind keine automatische Nullantwort oder Niederlage. Welche Wirkung ein Finalduell oder mehrere Turnierbegegnungen auf die Session-Gesamtwertung haben, bleibt offen und muss vor deren Anbindung geklärt werden.

**„Alle gegen einen“:** Der neue Formatlauf kann später einen anderen Regelpfad mit mehr als zwei Beteiligten organisieren. `DuelGameInstance`, `DuelInstanceContext` und `DuelUnit` bleiben dabei Zweierbausteine und werden nicht auf beliebige Gruppengrößen erweitert. Die übrigen Teilnehmer werden nicht künstlich zu einem neuen Team-/Sammelteilnehmer zusammengelegt. Ob später ein eigener Gruppen-Begegnungskontext erforderlich ist, hängt von den noch offenen Regeln ab; jetzt werden dafür weder konkrete Modellfelder festgelegt noch Modelle implementiert.

Der aktuell Erstplatzierte müsste aus der offiziellen Session-Gesamtwertung bestimmt werden, nicht aus einem zufälligen Begegnungspunktestand. Die vorhandene Rangdarstellung erlaubt mehrere Teilnehmer auf Rang 1. Auswahl bei mehreren Erstplatzierten, Zeitpunkt des Festhaltens des Spitzenreiters und gemeinsame beziehungsweise individuelle Antworten der übrigen Teilnehmer sind offen. Das Format bleibt bis dahin reserviert und nicht startbar.

### 19.9 Geplante Konfiguration im Admin-Dashboard

Die folgenden UI-/Backend-Ergänzungen sind Planung, keine Änderungen dieses Auftrags:

| Stelle | Vorgesehene Einstellung und Prüfung |
|---|---|
| `templates/hub/create_session.html`, `games_hub/views.py::create_session()` | Sessiontyp mit Default „Mehrspieler“. „2-Spieler“ erzwingt die Duellvariante sämtlicher Schritte. Alte Formulardaten ohne neues Feld bleiben Multiplayer. |
| Schrittplanung über `games_order`, `add_step_to_session()` und `templates/hub/monitor.html` | In einer Mehrspieler-Session Spielformat je geplantem Spiel wählen. Default bleibt Multiplayer. Nicht unterstützte Varianten können nicht vorbereitet oder gestartet werden. |
| Finalduell-Vorbereitung | Genau zwei unterschiedliche bestehende Sessionteilnehmer auswählen. Zugehörigkeit und Berechtigung erneut serverseitig prüfen; Auswahl beim Begegnungsstart festhalten. |
| Turniervorbereitung | Nur freigegebene Varianten ohne Buzzer anbieten. Teilnehmerpool und Setzuordnung anzeigen. Noch offene Optionen wie Freilose oder paralleler Betrieb nicht mit erfundenen Defaults versehen. |
| Laufende Session | Sessiontyp serverseitig sperren; ein deaktiviertes Auswahlfeld allein genügt nicht. Formatänderungen betreffen nur noch nicht gestartete Schritte beziehungsweise künftige Formatläufe. Aktive Konfiguration und Paarung werden nicht überschrieben. |
| Ergebnisansicht | Begegnungsergebnisse, gegebenenfalls Turnierauswertung und Session-Gesamtwertung getrennt anzeigen. Keine automatische Übernahme einer neuen Punkteformel. |

Die spätere Kapazitätsprüfung der 2-Spieler-Session gehört in den transaktionalen Join-/Zulassungspfad. `games_hub/lobby_join.py::_join_once()` sperrt bereits die Session, prüft derzeit aber keine Zweiergrenze. Die spätere Ergänzung muss ausschließlich für den neuen Sessiontyp greifen und zwei konkurrierende Erstbeitritte beziehungsweise einen dritten Beitrittsversuch korrekt behandeln. Zwei Browser desselben bestätigten Teilnehmers sind nicht zwei Parteien. Vor dem ersten Spiel müssen zwei zugelassene Parteien feststehen.

Die bisherigen Karten-, Fragen-, Spieltyp- und Theme-Einstellungen bleiben bestehen. Ein neues Spielformat darf keine alten Multiplayer-Konfigurationen oder `room_code`-Laufzeiten zurücksetzen. Neue Metadaten werden später nur an den dafür vorgesehenen Hub-Grenzen ausgewertet.

### 19.10 Einordnung von `DuelSubmission`

Der geplante Kern bleibt sinnvoll: Eine Abgabe gehört zu genau einer `DuelUnit` und einem berechtigten `HubParticipant`. Die Unit führt eindeutig zu ihrer `DuelGameInstance`; deren feste Parteien bestimmen die zulässigen Absender. Eine Eindeutigkeit auf Session + Frage oder Spielschritt + Teilnehmer wäre für Wiederholungen und wechselnde Paarungen falsch.

**Ein zusätzliches Zweier-Begegnungsmodell ist vorher nicht nötig:** Diese Rolle erfüllt bereits `DuelGameInstance`. Erforderlich ist die noch fehlende organisatorische Formatlauf-/Set-/Begegnungszuordnung, nicht ein paralleles Modell mit erneut gespeicherten Parteien.

Eine isolierte Speicherung von Abgaben für ein einzelnes Duell wäre technisch auch vorher möglich. Empfohlen wird jedoch, zuerst die neuen Kontextbegriffe und anschließend die Zuordnung festzulegen beziehungsweise schrittweise zu persistieren. So muss ein späterer Turnierstart keine Bedeutungen aus `rule_config`, Raumcodes oder einer lokalen Unitnummer rekonstruieren. Vor produktiven Turnierabgaben muss diese Zuordnung vorhanden sein.

Der Abgabevertrag benötigt deshalb keine vorgezogene Turnierpunkteformel oder polymorphe Teilnehmerliste. Format-/Set-IDs in einem späteren Transport-Envelope sind überprüfbarer Kontext; die eigentliche Zugehörigkeit bleibt aus den gespeicherten Beziehungen ableitbar. Datenformat und Verbindlichkeit konkreter Eingaben bleiben im späteren `DuelSubmission`-Auftrag zu präzisieren.

### 19.11 Ergänzende Risiken und spätere Abnahmetests

- **Dritter Teilnehmer einer 2-Spieler-Session:** Zwei gültige Identitäten zulassen, weitere Parteien atomar ablehnen; Reconnect belegt keinen neuen Platz. Mehrspieler-Joins bleiben unverändert.
- **Sessiontypsperre:** Erste Aktivierung, Pause/Fortsetzen und konkurrierender Änderungsversuch; kein Umgehen über direkte HTTP- oder WebSocket-Nachrichten.
- **Finalduell:** Zwei Gewählte dürfen handeln; andere Sessionteilnehmer bleiben angemeldet und können auch mit korrektem Hub-Token keine Duellaktion für diese Begegnung ausführen.
- **Turnierzuordnung:** Dieselbe Partei darf nicht gleichzeitig in zwei Paarungen desselben Sets auftreten, auch nicht über verschiedene A/B-Rollen. Ein zweiter Auslosungsrequest liefert die bestehende Zuordnung statt einer neuen.
- **Paarungswechsel:** Teilnehmer und dieselbe Materialquelle können im nächsten Set wieder vorkommen, aber Begegnungs- und Unitidentitäten sind neu. Alte Antworten, Sperren und Hostentscheidungen bleiben beim alten Kontext.
- **Gleiche lokale Nummer:** Mehrere Begegnungen besitzen Unit 1 ohne Zustandsvermischung. Die gemeinsame Turnierset-ID wird separat geprüft.
- **Neustart:** Neue Formatlaufkennung; keine versehentliche Fortsetzung alter Paarungen desselben `HubGameStep`.
- **Variantenprüfung:** Gemischter Quick-Quiz-Pool mit Short Answer wird nicht als buzzerfreies Turnier freigegeben. Unbekannte oder noch nicht implementierte Varianten fallen nicht auf Multiplayer zurück.
- **Parallele Ausführung:** Erst nach fachlicher Entscheidung testen und freigeben. Getrennte Timer, Aktionen und Ergebnisprojektionen bleiben bereits Architekturvorgabe; konkrete Scheduling-Erwartungen sind noch offen.
- **Wertung:** Nichtbeteiligung wird nicht als falsche Antwort behandelt. Begegnungspunkte gelangen nur über eine ausdrücklich definierte Regel in Turnier- oder Sessionwertungen.
- **„Alle gegen einen“:** Ohne festgelegte Regeln kein Start und keine automatische Auswahl bei mehreren Rang-1-Teilnehmern.
- **Regressionen:** Bestehende Hub- und Multiplayer-Tests bleiben maßgeblich. Neue Formatpfade dürfen `HubGameParticipantSnapshot`, `active_game_guard`, Bereitschaftscheck, Lobbyrückkehr und die bestehende Gesamtwertung nicht global umdeuten.

Das sind zukünftige Testspezifikationen. In diesem reinen Dokumentationsauftrag werden keine Tests oder ausführbaren Änderungen angelegt und keine Datenbankbefehle ausgeführt. Der bestehende App-Ausschluss `DuelConfig.supabase_sync_enabled = False` erfasst auch spätere Organisationsmodelle innerhalb der Duell-App; ein neuer allgemeiner Sync-Mechanismus ist dafür nicht nötig.

### 19.12 Kleinster sinnvoller nächster Implementierungsschritt

**Empfehlung: Zunächst nur frameworkfreie Verträge für Sessiontyp und Spielformat ergänzen.**

Als eigenständiger späterer Auftrag:

> Ergänze in `duel/contracts.py` getrennte Enums für `SessionType` und `GameFormat` sowie eine kleine reine Validierung der bereits festgelegten Kombinationen. Eine 2-Spieler-Session erzwingt Duell; eine Mehrspieler-Session verwendet standardmäßig Multiplayer und kann die ausdrücklich gewählten besonderen Formate beschreiben. „Alle gegen einen“ darf als reservierter Wert beschrieben werden, wird dadurch aber nicht als spielbar oder implementiert freigegeben. Ergänze isolierte Unit-Tests ohne Django oder Datenbank. Verändere die bestehenden Instanz-, Punkte-, Ergebnis- und Matchverträge nicht. Keine Models, Migrationen, Sessionfelder, Controller, URLs oder UI-Anbindungen ergänzen.

Danach folgen getrennt und jeweils abnehmbar: persistenter Formatlauf, Turnierset und Begegnungszuordnung mit Beziehungstests. Die genaue Felddefinition wird jeweils vor dem betreffenden Persistenzschritt geprüft. Die vorhandenen Modelle `DuelGameInstance` und `DuelUnit` bleiben dabei unverändert. `DuelSubmission` folgt mit eindeutigem Begegnungs-/Unitbezug, bevor irgendein aktiver Duell- oder Turnierpfad freigeschaltet wird.

### 19.13 Noch offene fachliche Entscheidungen

1. Welche anwesenden beziehungsweise eingecheckten Teilnehmer gehören zum nächsten Turnierset? Wann wird der Pool eingefroren, und wer bestätigt ihn?
2. Was passiert bei ungerader Teilnehmerzahl? Freilos, Aussetzen, Rotation oder eine andere Regel; wie wirkt das auf Wertung und Zahl der Begegnungen?
3. Werden Begegnungen parallel oder nacheinander gespielt? Welche Varianten unterstützt ein einzelner Host praktisch, und welcher gemeinsame Setabschluss ist erforderlich?
4. Sind Wiederholungspaarungen erlaubt, zu vermeiden oder erst nach bestimmten Begegnungen zulässig? Es wird kein K.-o.- oder Jeder-gegen-jeden-System unterstellt.
5. Verwenden alle Paarungen eines Turniersets dasselbe Fragen-/Setmaterial? Wie werden Antwortwissen und Sichtbarkeit bei nacheinander laufenden Begegnungen behandelt?
6. Ist ein Turnierset ein vollständiges Match oder umfasst es mehrere Entscheidungseinheiten? Wie gelten Best of N und Zielpunktzahl bei wechselnden Paarungen?
7. Wie werden Begegnungsergebnisse zu einer Turnierwertung und gegebenenfalls zur Session-Gesamtwertung zusammengeführt? Was erhalten nicht beteiligte Finalteilnehmer beziehungsweise aussetzende Turnierteilnehmer?
8. Was geschieht bei Disconnect, spätem Beitritt, Ausfall oder gewünschtem Austausch einer bereits gebundenen Partei? Keine automatische Neuauslosung einer begonnenen Begegnung.
9. „Alle gegen einen“: Auswahl bei mehreren Erstplatzierten, Auswahlzeitpunkt, Antwortmechanik, Spielende und sämtliche Punkte-/Wertungsregeln.
10. Die bereits in Abschnitt 18 aufgeführten offenen Spielregeln, Unentschieden und punktlosen Ergebnisse gelten weiterhin. Die neue Organisationsstruktur entscheidet diese Regeln nicht vorweg.

**Abschließende Architekturentscheidung:** Zwei unveränderlich gebundene Parteien je `DuelGameInstance`, eigene Units je Begegnung und eine additive organisatorische Klammer für Formatlauf und Turnierset. Der Sessiontyp steuert den Rahmen; das Spielformat steuert die Ausführung des einzelnen Spiels. Bestehende Multiplayer-Laufzeiten, Teilnehmerverwaltung und Wertungen werden dafür nicht vervielfältigt oder grundlegend umgebaut.


## Erg?nzung: Gebotsspiel und manuelle Spieltypen

Die fr?here offene Erfolgspunkte-Regel des Gebotsspiels ist best?tigt: Erf?llung
gibt der antwortenden Partei einen Duellpunkt und einen Rundensieg; bei Ablauf
der Antwortfrist gewinnt der Gegner entsprechend. Gebote sind positive ganze
Zahlen ab 1. Gebots- und Antwortdauer werden ausdr?cklich pro Aufgabe im
Dashboard konfiguriert. Fehlende Gebote bleiben ohne erfundene Wertung gesperrt.

Buzzer erh?lt seine bestehende Sperr-/Freigabe- und Punktefunktion ?ber einen
eigenen Duelladapter. Host-Punktevergabe ist ausdr?cklich nur f?r Zielpunktzahl
freigegeben; die Best-of-Rundensiegerregel bleibt offen. Beide Varianten buchen
ausschlie?lich in das Duelljournal. Technische Details und Abgrenzungen stehen
in `docs/DUEL_BID_MANUAL_GAMES.md`.

### Ergänzung: bestätigte Wertungsabschlüsse

Die zuvor offenen Regeln für Nichtabgaben bei Estimation/Where/Geboten,
identische Abweichungen/Distanzen, Black-Jack-Gleichstand/beidseitigen Bust und
vollständig gelöste Wechselzugaufgaben sind nun durch
[DUEL_SCORING_RULES.md](DUEL_SCORING_RULES.md) verbindlich konkretisiert.
Siegerlose Runden verbrauchen keine Best-of-N-Entscheidungsplätze.
Gleichzeitige Zielerreichung führt zu persistenten Stichrunden und bei erschöpftem
Material zu einem ausdrücklichen Host-Gleichstandsabschluss. Turnierbegegnungen
können auch ohne Sieger regulär beendet und anschließend neu ausgelost werden.
Die Migration 0017 betrifft ausschließlich die Duell-App; Multiplayer-Regeln
und Abendwertung bleiben unverändert.
