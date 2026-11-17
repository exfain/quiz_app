# Zielansage (Mehrspieler)

Eigener Spieltyp `target`, ausschließlich für geplante normale Mehrspielersessions.
Das Duell-Gebotsspiel und dessen Regeln sind unabhängig. Aufgaben werden unter
„Zielansage verwalten“ im vorhandenen Dashboard gepflegt: eine Lösung pro Zeile,
alternative Schreibweisen mit `|`. Spielkonfiguration: Einfach/Ranking und positive
gemeinsame Antwortdauer. Eine Zielzahl liegt zwischen 1 und der Lösungsanzahl.

Der normale Hub öffnet den Hostmonitor. Erst dessen geschützter Spielstart legt
unter der bestehenden Hub-Transaktion einen Run pro geplantem Slot und einen
festen Teilnehmerpool aus den anwesenden berechtigten Hub-Identitäten an.
Es gibt keine neuen Teamidentitäten oder einzelnen Spieleraccounts.

Je Aufgabe gibt es einen eingefrorenen Fragen-/Lösungssnapshot. Die Phasen sind
Person bestimmen/Ziel ansagen, Antworten, Hostprüfung und veröffentlichtes Ergebnis.
Der Host startet die gemeinsame Antwortzeit erst nach allen Zielansagen. Die
gespeicherte Serverdeadline schließt Abgaben einschließlich der Fristgrenze.
Abgelaufene Antwortphasen werden schreibfrei als Hostprüfung projiziert; dafür
ist kein zusätzlicher Hintergrundprozess erforderlich. Reload und Serverneustart
verwenden dieselbe Deadline. Ein inaktives Spiel nimmt keine Antworten an und
setzt seine Deadline beim Wiederaufnehmen nicht zurück.

Die echte „Wer weiß mehr?“-Erkennung arbeitet auf den eingefrorenen Optionen mit
dem beim Spielstart gespeicherten Session-Schwellenwert. Alternative Schreibweisen
werden derselben Lösungsidentität zugeordnet. Gezählt werden unterschiedliche
Lösungsindizes, nicht Texte oder Übertragungen. Der Host ordnet eine manuell als
korrekt akzeptierte Antwort einer hinterlegten Lösung zu; auch das umgeht die
Duplikatsperre nicht. Korrekturen sind bis zur Veröffentlichung möglich und werden
mit Host und Zeitpunkt protokolliert. Falsche Antworten oder das Erreichen des
Ziels schließen die Abgabe nicht vorzeitig.

Vor Veröffentlichung berechnet der Server Anzahl, Zielerreichung und Punkte neu.
Einfach: Ziel erreicht = angesagte Punkte, sonst 0. Ranking mit N teilnehmenden
Teams: erfolgreiche Teams nach absteigendem Ziel; Rang = 1 + Anzahl erfolgreicher
Teams mit höherem Ziel, Punkte = N + 2 − Rang. Gleiche Ziele teilen Rang/Punkte;
Folgeränge werden übersprungen. Alle erfolglosen Teams erhalten je 1 Punkt und
werden als gemeinsamer letzter Platz N angezeigt. Weitere richtige Lösungen
erhöhen die Punkte nicht. Mehrere Aufgaben addieren ihre veröffentlichten Punkte.
Die Abendwertung verwendet danach weiterhin die bestehenden Sessionregeln.

Alle fachlichen Schreibaktionen sperren denselben Run in der bestehenden
SQLite-IMMEDIATE-/Retry-Strategie. Ein Receipt pro Run, bestätigtem Akteur und
Aktionskennung verhindert Wiederholung; ein Fingerprint unterscheidet Konflikte.
Antworten, Korrekturen, Punkte und Revision werden atomar verarbeitet. Kontrollierte
Ablehnungen erhöhen keine Revision, technische Fehler hinterlassen keine Teilstände.
Veröffentlichte Ergebnisse sind über die normalen Aktionen nicht mehr änderbar.

JSON-HTTP verwendet Django-Hostberechtigungen beziehungsweise das signierte
Hub-Rejoin-Credential. Clientnamen/IDs sind kein Berechtigungsnachweis. Teams sehen
vor Veröffentlichung nur ihre eigene Zielzahl und eigenen gesendeten Texte; der
Spectator sieht während der Antwortphase nur Aufgabe und Timer. Der Host kann
prüfen und korrigieren. Die gemeinsame Spectator-Seite bettet die öffentliche
Projektion ausschließlich same-origin ein. Teilnehmer kehren nach dem Spielende
manuell über den gemeinsamen Lobby-Controller zurück; Host-Recall wird unterstützt.
Ein ausdrücklicher Hostabbruch behält nur bereits veröffentlichte Punkte.

Neue Tabellen und die ergänzte Hub-Spieltyp-Choice werden ausschließlich über
additive Migrationen installiert. Die gesamte App ist vor Modell-Erkennung von
Supabase-Sync/Restore ausgeschlossen. Produktive Migrationen werden nicht durch
Tests oder Seitenaufrufe ausgeführt.

## Verifikation

Die Chromium-Tests durchlaufen beide Wertungsmodi über Aufgabenverwaltung,
Sessionplanung und den regulären Hub-/Monitorstart mit drei getrennten
Teamkontexten, Host und eingebettetem Spectator. Geprüft werden Zielbindung,
alternative Schreibweisen und Duplikate, Rejoin, tatsächlicher Fristablauf,
Hostkorrektur mit nachträglicher Zielerreichung, gleiche erfolgreiche Zielzahlen,
Ergebnisveröffentlichung und manuelle Lobby-Rückkehr. Weitere Tests prüfen
getrennte SQLite-Verbindungen, parallele Starts/Abgaben/Veröffentlichung,
Receipt-Rollback, Rollenrechte, SQL-Constraints und Migrationsrücknahme.
Der abschließende relevante Regressionslauf besteht mit 388/388 Tests;
`manage.py check`, `makemigrations --check --dry-run` und `git diff --check`
sind ebenfalls erfolgreich. Migrationen wurden nur in isolierten SQLite-Dateien
angewendet und zurückgenommen.

Der zusätzliche breite Lauf mit 454 Tests zeigte auch bestehende Fehler. Folgende
neun Testmethoden scheitern ebenfalls am unveränderten Ausgangscommit
`23a6a2e718f0dbaf3f740f2d86ac4ad85dd46ed1` mit separaten Testdatenbanken:

- `admin_dashboard.tests.HostEndStateTemplateTest.test_legacy_quiz_ended_handlers_apply_end_state_without_reload` (acht Template-Unterfälle)
- `admin_dashboard.tests.HostQuestionStartTemplateTest.test_host_does_not_mark_question_sent_before_server_ack`
- `admin_dashboard.tests.HostQuestionStartTemplateTest.test_host_marks_question_sent_only_after_question_started_event`
- `admin_dashboard.tests.HostQuestionStartTemplateTest.test_send_question_buttons_are_non_submit_buttons`
- `admin_dashboard.tests.ManageGamesApiTest.test_tutorial_overlay_is_wired_into_all_active_player_templates`
- `admin_dashboard.tests.ManageGamesApiTest.test_tutorial_toggle_is_gated_by_tutorial_enabled_in_all_monitors`
- `admin_dashboard.tests.SessionsTest.test_end_all_active_games_completes_every_active_game_type` (fehlende BlackJackSession im Fixture)
- `games_hub.test_session_type.HubSessionTypeMigrationTests.test_existing_sessions_become_multiplayer_and_rollback_preserves_data`
- `games_hub.test_game_format.HubGameFormatMigrationTests.test_backfill_and_rollback_preserve_steps_sessions_games_and_duels`

Diese fremden Template-/Migrationsannahmen und Spielregeln werden durch den
Zielansage-Patch nicht verändert. Die Registrierungstests berücksichtigen den
neuen authentifizierten HTTP-Spielpfad zusätzlich zu den bestehenden Consumern.

Die anschließende gezielte Stabilisierung dieser neun Befunde und die
zusammenhängende Hub-Abnahme sind in [STABILIZATION.md](STABILIZATION.md)
dokumentiert. Die obige Fehlerliste beschreibt den ursprünglichen Zielansage-Lauf.
