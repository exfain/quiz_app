# Gebotsspiel, Buzzer und Host-Punktevergabe

Diese Varianten laufen ausschließlich in geplanten `session_type=duel`-Sessions
über den bestehenden Hub-Start, signierte Teilnehmeridentitäten, geprüfte
Hostberechtigungen und den vorhandenen JSON-HTTP-Transport. Zwei Teilnehmer in
einer Mehrspieler-Session ändern deren bisherigen Spielpfad nicht.

## Gebotsspiel

Im bestehenden Admin-Dashboard führt der Link „Gebotsspiel: Spiele und Aufgaben
verwalten“ zur eigenen Inhaltsverwaltung. `BidGame` besitzt einen Titel und
Raumcode; `BidTask` enthält Aufgabentext, ausdrücklich konfigurierte Gebots- und
Antwortdauer sowie den Aktivstatus. Es gibt keine erfundene Standarddauer.
Aufgaben können erstellt, bearbeitet und gelöscht werden; Zugriff haben nur
berechtigte Admins auf ihre eigenen Spiele beziehungsweise Superuser.

Bei der Rundenvorbereitung wird der Aufgabeninhalt in `DuelControlRound`
eingefroren. Bearbeitung oder Löschung der Ausgangsaufgabe verändert die
laufende Runde nicht. Jede erneute Verwendung erstellt eine neue `DuelUnit`.

Bestätigt sind positive ganzzahlige Gebote ab 1. Verbindliche Gebote werden als
`DuelSubmission` mit Art `BID` gespeichert. Es gibt je Partei und Unit nur ein
Commit; Retries verändern weder Wert noch Annahmezeit oder Annahmesequenz.
Solange die Gebotsphase läuft, enthält die Rollenprojektion höchstens das eigene
Gebot und Abgabestatus, niemals das verdeckte gegnerische Gebot. Auch der Host
erhält vor Abschluss keine gemeinsame Gebotsliste.

Die zweite gültige Abgabe beendet die Gebotsphase. Höherer Wert gewinnt; bei
gleichem Wert entscheidet ausschließlich die kleinere serverseitige
`acceptance_sequence`, nicht Browserzeit oder Reihenfolge der Darstellung.
Mit dieser Entscheidung startet innerhalb derselben Transaktion der gespeicherte
Antworttimer. Beide Gebote und das Antwortrecht sind danach sichtbar.

Der Host bestätigt richtige mündliche Antworten einzeln. Jede Aktion enthält
den erwarteten Zählerstand; eine zweite Aktion für denselben Stand wird
abgelehnt, auch bei anderer Aktionskennung. Das Erreichen des Gebots beendet
die Runde sofort mit genau einem Duellpunkt und Rundensieg für die antwortende
Partei. Ab der Antwortdeadline gewinnt der Gegner. Eine verspätete
Zählerbestätigung kann kein abgelaufenes Gebot mehr erfüllen.

Der bestehende lokale `duel_deadlines`-Worker verarbeitet beide gespeicherten
Fristen, auch ohne Browser und nach Neustart. Der bestehende Launcher startet
ihn bereits; es entsteht kein weiterer Scheduler. GET-Abfragen bleiben
schreibfrei. Der Timer in den Ansichten wird aus Serverzeit und Deadline
angezeigt und startet bei Reload nicht neu.

**Offen:** Fehlende Gebote, einschließlich zwei fehlender Gebote. Nach Ablauf der
Gebotsfrist wird `needs_resolution` gespeichert. Es gibt dann weder Antwortrecht
noch Antworttimer, Punkte oder Rundensieger. Der Abschluss bleibt gesperrt.

## Buzzer

Der Adapter erhält die vorhandene Mechanik: Runde vorbereiten und starten,
Buzzer ausdrücklich öffnen, erstes angenommenes Buzzern sichert das mündliche
Antwortrecht. Richtig vergibt die eingefrorene Einstellung `points_per_correct`
an diese Partei und beendet die Runde mit einem Rundensieg. Falsch vergibt
keinen Gegnerpunkt, sperrt diese Partei und öffnet für die andere Partei.
Nach zwei falschen Antworten bleiben beide gesperrt. Der Host kann eine
unentschiedene beziehungsweise ungespielte Runde ausdrücklich ohne Sieger
beenden; es entsteht kein erfundener Rundensieg. Die allgemeine Best-of-Regel
zur Behandlung siegerloser Runden bleibt fachlich offen und wird durch die
bestehende Match-Engine kenntlich gemacht.

`DuelBuzz` und `DuelDecision` protokollieren die konkreten Antwortversuche.
Multiplayer-Buzzer-Modelle und deren Punkte werden nicht verändert.

## Host-Punktevergabe

Bestätigt ist vorerst ausschließlich `TARGET_SCORE`. Best of N wird sowohl in
der Planung als auch serverseitig abgelehnt, bis eine Regel für Rundensieger
vorliegt. Positive und negative manuelle Änderungen erhalten die vorhandenen
Ganzzahlgrenzen; Nulländerungen bleiben ungültig. Jede Änderung ist eine neue
`DuelScoreEntry`, keine Mutation einer zweiten gespeicherten Gesamtpunktzahl.
Der erwartete Zählerstand schützt auch hier gegen doppelte Bestätigung.

Eine Unit ist ein Buchungsabschnitt, ohne Rundensieger. Ein ausdrücklich
beendeter Abschnitt beziehungsweise der Abschnitt beim Erreichen des Punkteziels
erhält `NO_WINNER`, nicht einen aus Punkten abgeleiteten Rundensieg. Das Match
gewinnt beim eindeutigen Erreichen der Zielpunktzahl die entsprechende Partei.
Eine Korrektur nach abgeschlossenem Match ist weiterhin ein eigenes Feature.

## Persistenz, Transport und Isolation

`DuelControlRound` enthält Phase, eingefrorenes Material, Gebots- und Antwortfrist,
Antwortrecht und Zähler. Die autoritativen Gebote bleiben in `DuelSubmission`,
Buzzes in `DuelBuzz`, Bewertungen in `DuelDecision`, Punkte in `DuelScoreEntry`
und Abschlüsse in der bestehenden Unit-/Match-Ergebnispersistenz.

Alle Spielaktionen verwenden genau einmal `execute_action()`, die vorhandenen
Receipts, Instanzsperren, SQLite-IMMEDIATE-Transaktionen und Retry-Mechanismen.
Punkte, Ergebnis, Matchabschluss, Phase, Receipt und Revision werden atomar
gespeichert. Die Inhaltsverwaltung verwendet die üblichen Dashboard-Formulare
mit CSRF-Schutz und Eigentümerprüfung, keine neue Authentifizierungsarchitektur.

Migration `duel.0015` ergänzt ausschließlich lokale Inhalts- und Zustandstabellen;
`games_hub.0028` registriert lediglich den zusätzlichen Spieltyp als Choice.
Die gesamte Duell-App einschließlich der Gebotsinhalte bleibt vom bestehenden
Supabase-Sync und Restore ausgeschlossen. Das Gebotsspiel hat keinen
Multiplayer-Consumer oder Multiplayer-Spectator-Renderer; die Klassifikation
`HubGameStep.DUEL_ONLY_GAME_KEYS` hält dies ausdrücklich fest. Host-, Teilnehmer-
und Spectator-Ansichten verwenden gespeicherte Phasen und dieselbe Projektion
bei Erstabruf, Reload und Rejoin. Die Lobby-Rückkehr bleibt manuell nach dem
Präsentationsabschluss.

## Verifikation und verbleibende Regressionstest-Befunde

Der erste breite Lauf (`duel buzzer host_points games_hub`) umfasste 1334 Tests: 1301 erfolgreich, 26 Fehlschläge und 7 Fehler. Es wird damit keine vollständig erfolgreiche Gesamtabnahme behauptet.

Zwei konkrete Ursachen wurden danach behoben: Die neue Buzzer-Projektion verglich Textschlüssel mit SQLite-UUIDs ohne Feldkonvertierung; außerdem entfernte der bestehende Test der absichtlich verweigerten Black-Jack-Rückmigration bereits spätere Migrationen und stellte sie nicht wieder her. Letzteres verursachte 17 nachfolgende Startfehler wegen einer fehlenden neuen Tabelle. Die fachliche Rückmigrationssperre bleibt unverändert.

Der anschließende Lauf von `duel.test_control`, `duel.test_control_browser` und `duel.test_inputs` bestand mit **63/63 Tests**. Enthalten sind alle drei neuen Vier-Kontext-Browserabläufe, beide Gebots-/Buzzer-Matchformate, Host-Punktevergabe mit Zielpunktzahl, echte Worker-Prozesse, getrennte SQLite-Verbindungen und die zuvor betroffenen Black-Jack-Tests.

Die abschließende gezielte Regression (`buzzer.tests`, `host_points.tests`, Hub-Intro, Hostberechtigungen, Lobby-Rückkehr, Fragenauswahl, Spectator, Matchkonfiguration, Sessiontyp und Spielformat) bestand mit **175/176 Tests**. Erneut fehlgeschlagen ist ausschließlich `games_hub.test_game_start_intro.SessionGameStartIntroBrowserTests.test_server_timeline_reload_reconnect_and_followup_screen`, Zeile 536: Die gemessene Deckkraft war `0.775`, erwartet wird `> 0.9` (im Breitlauf `0.9`). Test und Intro-Implementierung blieben unverändert; eine vollständig erfolgreiche Gesamtabnahme steht aus. `manage.py check`, `makemigrations --check --dry-run` und `git diff --check` waren erfolgreich. Migrationen wurden ausschließlich auf isolierten Testdatenbanken geprüft. Git-Index und bestehende lokale Datenbank blieben bytegleich zum Auftragsbeginn.

Die folgenden weiteren Befunde des ersten Breitlaufs wurden nicht durch Änderungen an fremden Spielpfaden behoben. Insbesondere wurde aus einem einzelnen Browserfehlschlag keine neue Spielregel oder spekulative Produktionskorrektur abgeleitet:

- `ERROR: test_blackjack_start_guard_recalls_player_from_who_that_waiting (games_hub.test_check_in_e2e.SessionCheckInBrowserE2ETest.test_blackjack_start_guard_recalls_player_from_who_that_waiting)`
- `ERROR: test_recall_routes_players_to_lobby_before_next_game_starts (games_hub.test_check_in_e2e.SessionCheckInBrowserE2ETest.test_recall_routes_players_to_lobby_before_next_game_starts)`
- `ERROR: test_who_that_starts_for_players_and_spectator_after_recall (games_hub.test_check_in_e2e.SessionCheckInBrowserE2ETest.test_who_that_starts_for_players_and_spectator_after_recall)`
- `ERROR: test_blackjack_participant_flow (games_hub.tests.ParticipantFlowBrowserTest.test_blackjack_participant_flow)`
- `ERROR: test_where_participant_flow (games_hub.tests.ParticipantFlowBrowserTest.test_where_participant_flow)`
- `ERROR: test_question_end_and_reveal_paths_do_not_switch_to_waiting_states (games_hub.test_inter_question_waiting.InterQuestionWaitingSourceTests.test_question_end_and_reveal_paths_do_not_switch_to_waiting_states)`
- `ERROR: test_pre_game_waiting_cards_use_unified_start_copy (games_hub.test_wait_loader.WaitLoaderTemplateTests.test_pre_game_waiting_cards_use_unified_start_copy) (template='templates/where_is_this/play.html')`
- `FAIL: test_player_templates_show_session_game_number_below_game_title (games_hub.test_session_game_number.SessionGameNumberTests.test_player_templates_show_session_game_number_below_game_title) (template='templates/where_is_this/play.html')`
- `FAIL: test_where_three_maps_zoom_reveal_lock_rejoin_both_formats (duel.test_input_browser.DeviceDuelBrowserTests.test_where_three_maps_zoom_reveal_lock_rejoin_both_formats)`
- `FAIL: test_wann_war_das_uses_absolute_clock_without_full_state_polling (games_hub.test_host_game_runtime.HostGameRuntimeContractTests.test_wann_war_das_uses_absolute_clock_without_full_state_polling)`
- `FAIL: test_server_timeline_reload_reconnect_and_followup_screen (games_hub.test_game_start_intro.SessionGameStartIntroBrowserTests.test_server_timeline_reload_reconnect_and_followup_screen)`
- `FAIL: test_participant_titles_and_waiting_title_use_game_title_font (games_hub.test_game_title_font.GameTitleFontTests.test_participant_titles_and_waiting_title_use_game_title_font) (template='templates/where_is_this/play.html')`
- `FAIL: test_host_end_redirect_to_lobby_is_preserved (games_hub.test_inter_question_waiting.InterQuestionWaitingSourceTests.test_host_end_redirect_to_lobby_is_preserved)`
- `FAIL: test_next_question_entry_points_and_reveals_remain_defined (games_hub.test_inter_question_waiting.InterQuestionWaitingSourceTests.test_next_question_entry_points_and_reveals_remain_defined)`
- `FAIL: test_all_solution_renderers_require_authoritative_reveal_phase (games_hub.test_spectator_browser.HubSpectatorBrowserTests.test_all_solution_renderers_require_authoritative_reveal_phase) (game_key='where')`
