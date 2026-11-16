# Manuell besetztes Duell in einer Mehrspielersession

Der normale Hostmonitor bietet für unterstützte, noch nicht gestartete Slots
zusätzlich „Duell starten“ mit zwei ausdrücklich ausgewählten Parteien und
Best-of-N beziehungsweise Zielpunktzahl an. Host-Punktevergabe bleibt auf
Zielpunktzahl beschränkt. Der normale Spieleinstieg bleibt daneben erhalten.

`duel.hub.start_slot()` prüft unter der vorhandenen Session-/Slotsperre die
Hostberechtigung, den abgeschlossenen Check-in, zwei unterschiedliche anwesende
Sessionidentitäten und die bestehende `MatchConfig`. Ein laufendes
Mehrspielerspiel muss vorher beendet sein. Der Slot erhält `game_format=duel`
und seine Konfiguration; die bestehende `DuelGameInstance` speichert die festen
Parteien. Neue Tabellen oder Migrationen sind nicht erforderlich.

Provisionierung, Intro, Start-Receipt und Revision werden weiterhin gemeinsam
über `execute_action()` gespeichert. Die deterministische Slotidentität macht
identische und parallele Starts idempotent. Eine nachträglich abweichende
Paarung oder Konfiguration wird abgelehnt. Ein normaler Start kann ein offenes
Duell einschließlich seiner Ergebnispräsentation nicht überschreiben.

Die zusätzliche schreibfreie Routingabfrage verwendet für Teilnehmer dieselben
signierten Lobby-Zugangsdaten wie der Duelltransport. Nur die festen Parteien
werden weitergeleitet. Andere Teilnehmer bleiben in der normalen Lobby und
können den öffentlichen Spectator View öffnen. Alle bestehenden Duellcontroller
und rollenbezogenen Projektionen werden weiterverwendet. Reload und Rejoin
rekonstruieren dieselbe Begegnung, Phase, Abgabe und Deadline. Der bestehende
Frist-Worker verarbeitet ausdrücklich als Duell gestartete Slots auch in
Mehrspielersessions; Standard-Slots bleiben ausgeschlossen.

Host und Spectator zeigen Matchstand und Sieger; der Host kann abgeschlossene
Duellergebnisse erneut öffnen. Nach dem Präsentationsabschluss kehren die
Parteien manuell in ihre normale Lobby zurück. Der nächste normale Spielstart
verwendet wieder den vollständigen Sessionteilnehmerpool. Wiederverwendete
Spielräume liefern dabei das Intro des normalen Slots statt das alte Duellintro.

Duellpunkte und Matchsiege werden ausschließlich in der Duell-Datenhaltung
gespeichert. Die Abendwertung überspringt diese Slots; vorhandene Ranglisten und
Korrekturen bleiben erhalten. Ob ein Stechen später Platzierungen überschreibt,
bleibt ausdrücklich offen. Turniere und „Alle gegen einen“ sind nicht aktiviert.
Der Ausschluss der gesamten Duell-App aus Supabase-Sync und Restore bleibt bestehen.

Der Intro-Browsertest wartet jetzt auf die tatsächlichen endlichen
Zahlenanimationen. Die Zahl baut sich bis 630 ms auf und flackert absichtlich
zwischen 920 und 1100 ms; eine unmittelbare Deckkraftmessung konnte deshalb
`0.775` oder `0.9` statt `>0.9` liefern. Die fachlichen Assertions und das
Produktionsintro wurden beibehalten.

## Abnahme

- Abschließender kombinierter Lauf: **596/596 Tests erfolgreich** auf einer
  isolierten dateibasierten SQLite-Testdatenbank. Enthalten: neue Begegnungs-
  und Browsertests, bisherige Hub-Start- und Fristtests, vollständiges
  Hub-Intro-Modul, betroffene Duellcontroller, Hub-Sicherheits-, Lobby-,
  Spectator-, Session- und Konkurrenztests sowie Multiplayer-Estimation,
  Quick Quiz, Buzzer und Host-Punktevergabe.
- Fünf getrennte Chromium-Kontexte: Host, Spectator und drei Teilnehmer.
  Estimation mit Best of Seven (vier Rundensiege) und Zielpunktzahl,
  veränderte Paarung im nächsten Slot, Intro/Warten, verbindliche Abgaben,
  Reveal-Reihenfolge, Reload, tatsächlicher Rejoin über die Lobby,
  Matchsieg und manuelle Rückkehr. Der Unbeteiligte blieb in der Lobby,
  konnte den Spectator View öffnen und erhielt bei direkter Duell-API-Abfrage
  HTTP 403. Anschließend spielten alle drei im regulären Quick Quiz mit.
- Serverprüfungen decken zudem alle unterstützten Duell-Transporte ab,
  identische und parallele Starts, abweichende Wiederholungen, ungültige
  Besetzung, fremde Hosts/Slots, vollständigen Rollback und den Ausschluss
  der Duellergebnisse aus der Abendwertung. Eine bestehende Führung blieb
  trotz eines anderen Duellsiegers erhalten.
- `manage.py check`, `makemigrations --check --dry-run` und
  `git diff --check` erfolgreich. Keine neuen Migrationen.

**Offener Diagnosebefund:** Im erfolgreichen Gesamtlauf erschien unmittelbar
nach dem erfolgreichen Test
`QuizGame.tests.QuickQuizBrowserLiveFlowTests.test_true_false_first_click_survives_duplicate_rejoin_state_across_questions`
ein asynchroner Serverlogeintrag `FOREIGN KEY constraint failed`.
Der Trace führt über `handle_participant_update_pending_answer`,
`AuthoritativeGameConsumerMixin.send()` und `get_runtime_state()` zur Erstellung
eines Multiplayer-`GameRuntimeState`. Ein Zusammenhang mit dem Aufräumen der
Testdaten ist plausibel, aber nicht abschließend belegt. Der betroffene Test
und sein Nachfolger bestanden. Die zugehörigen Multiplayer-Implementierungen
wurden unverändert gelassen; der Logfehler wird nicht als behoben ausgegeben.
