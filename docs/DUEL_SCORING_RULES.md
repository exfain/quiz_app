# Bestätigte Duellwertung

Diese Regeln gelten für geplante 2-Spieler-Sessions, manuelle Duelle in
Mehrspielersessions und Turnierbegegnungen. Sie ändern keine Multiplayer-Wertung.
Sie ersetzen die zuvor als offen bezeichneten Fälle im Architekturbericht.

| Spiel / Fall | Punkte A:B | Rundenergebnis |
| --- | --- | --- |
| Estimation / Where: nur A gültig | 1:0 | A gewinnt |
| Estimation / Where: nur B gültig | 0:1 | B gewinnt |
| Estimation / Where: keine gültige Abgabe | 0:0 | kein Sieger |
| Estimation / Where: exakt gleiche Abweichung / Distanz | 1:1 | unentschieden, kein alleiniger Sieger |
| Black Jack: beide über 21 | 0:0 | kein Sieger |
| Black Jack: gleicher Sternestand am Setende | 0:0 | kein Sieger |
| Assign / Sorting Ladder / Wer weiß mehr?: vollständig fehlerfrei gelöst | 0:0 | kein Sieger |
| Gebotsspiel: kein gültiges Gebot am Fristende | 0:0 | kein Sieger |

Ein einzelnes gültiges Gebot erhält nach Ende der Gebotsphase das Antwortrecht.
Der Antworttimer beginnt erst bei serverseitiger Zuteilung; das Gebot muss danach
normal erfüllt werden. Bei zwei Geboten gelten weiter Höhe und serverseitige
Annahmesequenz. Die bestehende Black-Jack-Regel „21 gewinnt gegen Bust“ bleibt
erhalten. Nichtabgaben in Black Jack bleiben fachlich offen und werden weiterhin
nicht automatisch als gültige Null-Sterne-Antworten behandelt.

## Best of N und Stichrunden

Nur Runden mit genau einem Sieger verbrauchen Entscheidungsplätze. Remis und
`no_winner` zählen nicht gegen N. Best of Seven endet beim vierten Rundensieg.
Spielpunkte bleiben davon unabhängig.

Im Zielpunktmodus prüft die Matchverwaltung immer die gesamte Punktebuchung
beider Parteien. Erreichen beide das Ziel, werden alle Punkte gutgeschrieben,
aber noch kein Sieger gespeichert. `DuelGameInstance.tiebreak_unit` hält die
auslösende abgeschlossene Einheit fest. Erst ein alleiniger Rundensieg **nach**
dieser Einheit entscheidet das Match, auch wenn die ursprünglichen Punktesummen
unterschiedlich waren. Weitere Remis oder punktlose Runden verlängern die
Stichphase. Reload und Neustart rekonstruieren die Grenze aus der Datenbank.

## Ergebnisprotokoll und Host-Gleichstandsabschluss

`DuelUnitOutcome.reason` speichert den fachlichen Grund. Die bisherigen
Punkte-, Rundenergebnis- und Matchquellen bleiben getrennt. Die sechs
Duellansichten und die Turnierübersicht zeigen nur bereits freigegebene Gründe.
Eine Nichtabgabe umgeht weder die Deadline noch die Host-Reveal-Reihenfolge.

Wenn alle aktiven ausgewählten Fragen/Sets bereits verwendet wurden und die
letzte Präsentation beendet ist, darf der Host ein noch unentschiedenes Match
ausdrücklich ohne Matchsieger abschließen. Die Materialprüfung erfolgt erneut
innerhalb von `execute_action()`. Verbleibendes unbenutztes Material sperrt den
Befehl. Wiederholtes Spielen alter Inhalte bleibt möglich; der Abschluss ist
eine ausdrückliche Hostentscheidung, kein automatischer Abbruch.

`DuelMatchOutcome` bleibt der einzige Abschlussrecord. Ein leerer Gewinner ist
nur mit dem datenbankseitig geprüften Abschlussgrund `material_exhausted` oder
`tournament_no_winner` zulässig. Gewöhnliche Siege benötigen weiter einen
Gewinner. Punkte werden weder gelöscht noch auf Gleichheit angepasst.

Im Turnier schließt auch ein siegerloses einzelnes Set die Begegnung ab. Das
erfindet keinen Matchsieger und erhält die erspielten Punkte. Die nächste
Auslosung wartet weiterhin auf die abgeschlossene Präsentation aller Paarungen;
die gemeinsame Reveal-Sperre bleibt bestehen. Turnierergebnisse bleiben außerhalb
der Abendwertung.

## Transaktionen und Migration

Punkte, Rundenergebnis, Stichgrenze und gegebenenfalls Matchabschluss werden
weiter in derselben bestehenden Aktions-Transaktion geschrieben. Receipts und
Revision gehören ausschließlich `execute_action()`. Die Grenze kann nach ihrer
ersten Speicherung nicht ersetzt werden. Der Hostabschluss verwendet denselben
HTTP-/Berechtigungs-/Idempotenzpfad; GETs schreiben weiterhin nichts.

Migration `0017_match_draws_and_tiebreak` ergänzt nur Duellfelder und den
Abschluss-CheckConstraint. Alte Siege behalten `reason=win`, alte Instanzen eine
leere Stichgrenze. Leere Schemas lassen sich vor/zurück migrieren. Eine Rücknahme
mit neuen Wertungs-, Remis- oder Stichdaten wird ausdrücklich verweigert, damit
keine Ergebnisse verloren gehen. Supabase-Sync und Restore ignorieren weiter die
gesamte Duell-App.

Offen bleiben nachträgliche Ergebniskorrekturen, Black-Jack-Nichtabgaben sowie eine
Übernahme von Duell-/Turnierergebnissen in die Abendwertung. Sie werden durch
diese Regeln nicht stillschweigend festgelegt.
