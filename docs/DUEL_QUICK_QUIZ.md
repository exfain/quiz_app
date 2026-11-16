# Quick Quiz für geplante 2-Spieler-Sessions

Der gespeicherte Sessiontyp entscheidet über den Einstieg. `quiz` und
`estimation` verwenden in Duellsessions den vorhandenen Hub-Start, Check-in,
Slotkonfiguration und die signierten Teilnehmeridentitäten. Multiplayer-Sessions
verwenden auch bei zwei Teilnehmern weiterhin ihren bisherigen Startpfad.

## Autoritative Daten und Befehle

Migration `0011_quick_quiz_duel` ergänzt ausschließlich zwei Duelltabellen:
`DuelQuizQuestionBinding` friert Frage, Optionen, Lösung, Antwortdauer und
Aufdeckungsgeschwindigkeit pro konkreter Unit ein. `DuelQuizRoundState` speichert
Phase, Frist und das einmalig vergebene Buzzerrecht einschließlich eingefrorener
Zeichenanzahl. Wiederholte Fragen erhalten jeweils neue Units und Snapshots.

`duel/controllers/quiz.py` nutzt `execute_action()` und den Submission-Service.
Der erste unter der bestehenden Instanzsperre/SQLite-IMMEDIATE-Transaktion
akzeptierte Buzz gewinnt. Clientzeit oder Reihenfolge der Darstellung entscheidet
nicht. Die Hostbewertung referenziert die serverseitige Buzz-UUID und wird als
`DuelDecision` gespeichert. Bewertung, beide ScoreEntries, Rundenergebnis,
gegebenenfalls Matchabschluss, Receipt und Revision committen gemeinsam.

Geräteantworten: `prepared → answering → awaiting_reveal → show_answers →
show_solution → show_result → completed`. Ein Buzz wechselt stattdessen von
`answering` nach `awaiting_decision`; die Hostbewertung führt nach `show_result`.
Der vorbereitete Zustand startet keinen Timer. Erst der Hoststart übernimmt die
eingefrorene Fragenzeit als Serverdeadline. Die gemeinsame Fristverarbeitung
`duel_deadlines` schließt abgelaufene Antwortphasen ohne Reveal oder Wertung.
Der vorhandene lokale Launcher startet diesen Worker bereits mit. Nach einem
Neustart rekonstruiert er fällige Runden aus der Datenbank; GETs schreiben nicht.

## Wertung

- Auswahlantworten: je Partei richtig `+1`, falsch oder fehlend `0`.
- Beide richtig: beide `+1`, Unentschieden ohne Rundensieg.
- Keine richtige Antwort beziehungsweise kein Buzz: beide `0`, kein Rundensieger.
- Mündliche Antwort richtig: buzzende Partei `+1`; falsch: Gegner `+1`.
- Ein eindeutiger Fragensieger erhält einen Rundensieg; Punkte und Rundensiege
  bleiben getrennte Quellen. Die gemeinsame Matchengine entscheidet über den
  Matchabschluss. Best of Seven benötigt vier Rundensiege.

Die beiden Nullbuchungen einer punktlosen Auswertung bleiben nachvollziehbare
Journalbuchungen. Keine Duellwertung schreibt in Multiplayer-Antworten,
Multiplayer-Scores oder die Session-Gesamtwertung.

## Transport und Darstellung

Quick Quiz verwendet denselben JSON-HTTP-Weg wie Estimation, mit eigenen
`duel/quiz/…`-Seiten und `duel/api/quiz/…`-Aktionen. Hostrechte stammen aus der
bestehenden Hub-Autorisierung, Teilnehmerrechte aus dem geprüften Rejoin-Token.
Die Spectatorprojektion ist ausschließlich lesend. Die gespeicherte Phase
bestimmt die Antwort-/Lösungs-/Ergebnisfreigabe bei jedem Abruf, auch nach einem
Retry. Nur der berechtigte Host erhält beim mündlichen Antwortrecht zusätzlich
die Bewertungsreferenz und die Lösungshilfe. Öffentliche Projektionen enthalten
diese Felder nicht. Alte Snapshots mit kleinerer Revision überschreiben keine
neuere Darstellung. Die Seiten verwenden das vorhandene VHS-Intro und Warten
vor dem ersten expliziten Fragenstart. Nach dem entscheidenden Ergebnis bleibt
der Präsentationsabschluss möglich; erst danach erscheint die Lobby-Rückkehr.

## Weiterhin offene Matchregeln

Erreichen beide Parteien das Punkteziel, bleibt der gemeinsame Zustand
`needs_resolution` bestehen, auch bei unterschiedlichen Summen oberhalb des
Ziels. Es wird kein Sieger gespeichert und keine nächste Frage freigegeben.
Ein autorisierter Auflösungs-/Abbruchpfad für diesen Fall ist noch festzulegen.

Ebenso bleibt offen, wie unentschiedene oder siegerlose Fragen gegen eine
Best-of-N-Gesamtzahl zählen. Sie erzeugen keine Rundensiege. Die bestehende
Engine kennzeichnet dies als `unit_outcome_policy_unresolved`, erlaubt weiterhin
Fragen und beendet das Match beim eindeutigen Erreichen der Siegschwelle. Eine
neue Tiebreaker-, Wiederholungs- oder Korrekturregel wurde nicht eingeführt.

## Verifikation

`duel/test_quiz.py` prüft Regeln, den tatsächlichen Hub-/HTTP-Weg, Datenschutz,
Rollbacks, den echten Frist-Worker und Konkurrenz auf getrennten Verbindungen.
`duel/test_quiz_migration.py` prüft die additive Migration und Rücknahme.
`duel/test_quiz_browser.py` spielt vier unabhängige Browserkontexte über Planung,
Check-in, Intro, Buzzer/Hostbewertung, zwei True/False-Fragen, Matchende und den
nächsten Multiple-Choice-Slot mit Zielpunktzahl bis zur Lobby-Rückkehr durch.
