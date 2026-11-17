# Hub-Abnahme und Zielansage-Regressionsbefunde

Die neun dokumentierten Befunde wurden vor dem Patch reproduziert (zwölf Tests
einschließlich benachbarter Prüfungen, 14 Fehler-Unterfälle und zwei Exceptions).

| Testbefund | Ursache und Korrektur |
| --- | --- |
| `HostEndStateTemplateTest.test_legacy_quiz_ended_handlers_apply_end_state_without_reload` | Der Test las über das Ende des `quiz_ended`-Handlers hinaus in andere Events mit zulässigem Reload. Der Block endet jetzt am nächsten `case`. Where wird über seinen tatsächlichen `where_state`-Renderer geprüft. |
| `HostQuestionStartTemplateTest.test_host_does_not_mark_question_sent_before_server_ack` | Where besitzt keinen Inline-`sendQuestion` mehr. Geprüft wird sein registrierter externer Klickpfad: senden ohne optimistische Zustandsänderung. |
| `HostQuestionStartTemplateTest.test_host_marks_question_sent_only_after_question_started_event` | Where übernimmt den bestätigten `where_state` statt eines alten `question_started`-Events. Der Test prüft diese Zustandsübernahme und die daran gebundenen Controls. |
| `HostQuestionStartTemplateTest.test_send_question_buttons_are_non_submit_buttons` | Where verwendet einen festen Button statt der alten CSS-Klasse. Der aktive Button hat jetzt explizit `type="button"`; der Test prüft ihn. |
| `ManageGamesApiTest.test_tutorial_overlay_is_wired_into_all_active_player_templates` | Tatsächlich fehlende Where-Anbindung. Gemeinsames Overlay und Tutorialhinweis sind wieder angeschlossen; Bestätigung nutzt den authentifizierten Spielsocket, Reload lädt die persistierte Bestätigung. |
| `ManageGamesApiTest.test_tutorial_toggle_is_gated_by_tutorial_enabled_in_all_monitors` | Tatsächlich fehlende Where-Controls und Serveranbindung. Konfigurierte Erläuterung/Tutorialfrage, Warnung bei fehlenden Bestätigungen und der gemeinsame Tutorial-Runtime werden verwendet. Tutorialfragen zählen nicht zur Wertung. |
| `SessionsTest.test_end_all_active_games_completes_every_active_game_type` | Das Fixture erzeugte aktive Spiele ohne die beim regulären Start vorhandenen Black-Jack-/Clue-Rush-Laufzeitdatensätze. Diese gehören jetzt zum Fixture; die Abschlussassertions bleiben bestehen. |
| `HubSessionTypeMigrationTests.test_existing_sessions_become_multiplayer_and_rollback_preserves_data` | Neuere appübergreifende Migrationsziele hielten das neue Hub-Schema aktiv. Historische Ziele schließen abhängige Folgemigrationen aus und setzen die Hub-App ausdrücklich zurück. |
| `HubGameFormatMigrationTests.test_backfill_and_rollback_preserve_steps_sessions_games_and_duels` | Dieselbe inkonsistente Zielmenge; korrigiert, weiterhin mit vollständigem Datenvergleich vorwärts und rückwärts. |

`OlderDatabaseCopyUpgradeTests` baut den historischen Stand vor Hub 0025 mit
Teilnehmern, Punkten und einem Ergebnis auf. Eine echte SQLite-Backupkopie wird
in einem getrennten Prozess bis zum aktuellen Schema migriert. Datenvergleich,
`integrity_check` und `foreign_key_check` prüfen die Kopie; das historische Original
bleibt unverändert. Es wird keine vorhandene Host-Datenbank benutzt.

`FullHubEveningBrowserTests` verwendet Host, Spectator und vier getrennte
Teilnehmerkontexte für Quick Quiz → Zielansage → manuelles Duell → Turnier.
Alle Starts, Abgaben, Reveals und Rückwege laufen über die regulären Oberflächen
und Transporte. Geprüft werden eigene Intro-Identitäten/Spielnummern,
Reload/Rejoin, Teilnehmer des Folgespiels und die getrennte Abendwertung.
`PlayableEstimationBrowserTests` prüft den eigenständigen geplanten
2-Spieler-Einstieg einschließlich Planung und Check-in.

Die zusätzliche Best-of-Seven-Browserprüfung enthielt noch die frühere Sperre
bei einer fehlenden Estimation-Antwort. Sie prüft jetzt die bestätigte Regel:
keine Punkte allein durch Fristablauf, anschließend hostgesteuerte Veröffentlichung,
ein Punkt/Rundensieg für die einzige gültige Abgabe und reguläre Lobby-Rückkehr.
Die Duellwertung selbst wurde dafür nicht verändert.

Ein zusätzlicher WWM-Browserfehler trat nach dem Wechsel von Review zu Runde zwei
auf (HTTP 400 auf `next-round`, anschließend Timeout der Phasenassertion).
Der Test prüfte vorher nur die sichtbare Reviewphase. Er wartet jetzt auf die
zugehörige HTTP-Abschlussbestätigung und das Ende der ausstehenden Hostaktion;
auch die nächste Serverantwort wird ausdrücklich geprüft. Punkte-, Wiederholungs-
und Scoreboxassertions bleiben erhalten, ohne längere Timeouts oder Retry-Schleife.

Für die SQLite-Kopienprüfung werden Verbindungen ausdrücklich geschlossen
(unter Windows hält ein bloßes `with sqlite3.connect(...)` sonst die Datei offen).
Die Prüfabfrage registriert dieselben SQLite-Funktionen wie Django, da ein nackter
SQLite-Client CHECK-Ausdrücke mit `REGEXP` nicht kennt. Das sind Prüfaufbaufragen,
keine Änderung an Migrationen oder Datenbankconstraints.

## Ergebnis der Abnahme

- Abschließender breiter Lauf: **487/487 erfolgreich**, einschließlich aller neun
  ursprünglichen Befunde, historischer Migrationsrücknahme, Upgrade der Kopie,
  Zielansage, Hub, WWM, Where, Duell-Transport und gemeinsamer Intro-Browsertests.
- Zusammenhängender Browserabend einschließlich Sessionende: erfolgreich mit
  Host, Spectator und vier getrennten Teilnehmerkontexten. Die finale sichtbare
  Abendwertung enthält für jedes Team zwei Punkte aus den beiden normalen Spielen;
  die Ergebnisse des Finalduells und Turniers bleiben getrennt.
- Geplante 2-Spieler-Sessions: Best of Seven mit vier Rundensiegen und Zielpunktzahl
  erfolgreich, einschließlich des erneuten Spieleinstiegs und Lobby-Rückwegs.
- Where-Tutorial, bestätigte Erläuterung nach Reload, ungewertete Trainingsfrage,
  reguläre Folgefrage und Where-Intro-/Kartenregressionen: erfolgreich.
- Sämtliche Quick-Quiz-Tests wurden im zusätzlichen Lauf ausgeführt und bestanden.
- Django-Systemprüfung, Migrationsabgleich und `git diff --check`: erfolgreich.

Es bleibt kein bekannter Fehlschlag aus dieser Abnahme. Die am Anfang der
Untersuchung gefundenen Testaufbaufehler und die zwischenzeitlich korrigierten
Overlay-Aufrufe sind durch die abschließenden Läufe abgedeckt. Offene fachliche
Black-Jack-Nichtabgaben und Alle-gegen-einen bleiben unverändert außerhalb des Patches.

## Isolierte Wiederholung auf dem Host-Rechner

Im Repository mit der vorhandenen virtuellen Umgebung:

```powershell
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe -B manage.py test games_hub.test_evening_browser duel.test_playable_browser --settings=games_website.test_settings --noinput
.venv\Scripts\python.exe -B manage.py check --settings=games_website.test_settings
.venv\Scripts\python.exe -B manage.py makemigrations --check --dry-run --settings=games_website.test_settings
```

Der breite Regressionslauf verwendet diese Auswahl (zusätzliche vollständige
Quick-Quiz- und Browser-Abnahmen stehen separat darüber):

```powershell
$suite = @(
  'admin_dashboard.tests', 'target_game',
  'games_hub.test_game_start_intro', 'games_hub.test_host_security',
  'games_hub.test_session_repairs', 'games_hub.test_readiness',
  'games_hub.test_text_matching', 'games_hub.test_spectator',
  'games_hub.test_duel_match_config', 'games_hub.test_session_type',
  'games_hub.test_game_format', 'games_hub.test_lobby_return_flow',
  'games_hub.test_session_concurrency', 'games_hub.test_active_game_guard',
  'wer_weiss_mehr.tests', 'wer_weiss_mehr.test_repairs', 'wer_weiss_mehr.test_browser_repairs',
  'duel.test_control', 'duel.test_tournaments', 'duel.test_hub_playable', 'duel.test_estimation_transport',
  'where_is_this.test_tutorial_live', 'where_is_this.test_live',
  'where_is_this.test_intro', 'where_is_this.test_live_browser', 'games_hub.test_upgrade_copy'
)
.venv\Scripts\python.exe -B manage.py test $suite --settings=games_website.test_settings --noinput
.venv\Scripts\python.exe -B manage.py test QuizGame.tests where_is_this.tests where_is_this.test_tutorial_browser --settings=games_website.test_settings --noinput
```

Die Testsettings erzeugen temporäre dateibasierte SQLite-Datenbanken und ein
eigenes Medienverzeichnis; auch der Alias `supabase` ist ausschließlich lokal.
Chromium verwendet die im Repository üblichen lokalen UI-Bibliotheksstubs.
Der neue Patch verändert keine Wertungsregel, insbesondere nicht Black-Jack-
Nichtabgaben, und ergänzt keinen Alle-gegen-einen-Modus.
