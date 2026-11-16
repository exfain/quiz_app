# Abwechselnde mündliche Duellspiele

Assign, Sorting Ladder und Wer weiß mehr? verwenden ausschließlich in geplanten
`SessionType.DUEL`-Sessions die neue Duellvariante. Zwei Teilnehmer machen eine
Multiplayer-Session weiterhin nicht zum Duell. Quellspiele, deren Teilnehmer,
Antworten und Punkte werden durch das Duell nicht gestartet oder beschrieben.

## Infrastruktur

`duel/turn_material.py` liest die tatsächlich ausgewählten aktiven Quellfragen.
Assign verwendet `AssignQuestion.get_randomized_items()` für seine eingefrorenen
Anzeigeindizes; korrekte Zuordnungen bleiben privat. Wie die bestehende
Verwaltung und Laufzeit akzeptiert der Binder auch ganzzahlige Zeichenfolgen
als Zuordnungsindizes. Er friert sie als Ganzzahlen ein, ohne die Quellfrage zu
ändern oder Floatwerte abzuschneiden. Leere beziehungsweise nicht vollständig
konfigurierte Zuordnungsaufgaben sind noch kein spielbares Duellmaterial und
werden vor der Bindung zurückgewiesen. Sorting übernimmt die
Reihenfolge aus `SortingItem.correct_rank` ohne Floatkonvertierung und beachtet
ein gültiges konfiguriertes Startelement. Öffentlich verwendete Element-IDs
folgen der eingefrorenen Mischreihenfolge, nicht der Rangfolge. Wer weiß mehr?
verwendet die alphabetische Normalisierung aus seinem bestehenden Modell. Es
werden keine Multiplayer-Auswertungs- oder Eliminierungsmethoden aufgerufen.

Die additive Migration `0013_alternating_turn_rounds` erzeugt ausschließlich
`DuelTurnRound`. Ihre einzige Abhängigkeit ist `duel.0012`; bestehende Tabellen
werden nicht geändert. Das Modell speichert pro konkreter Unit genau einen
unveränderlichen Materialsnapshot, die gewählte erste Partei, die Startzeit,
Phase und die nur erweiterbare Liste bestätigter Züge. Eine wiederverwendete
Frage erhält eine neue Unit mit eigener Identität und leerem Fortschritt.

Alle Schreibvorgänge in `duel/controllers/turns.py` laufen über `execute_action()`.
Hub-Zuordnung, Hostberechtigung und Teilnehmeridentität folgen den vorhandenen
JSON-HTTP-Pfaden. `duel/turn_transport.py` erlaubt nur dem bestätigten Host
Start, Antworterfassung und Freigaben. Teilnehmer und Spectator lesen dieselbe
gespeicherte Phase mit gefilterten Daten. GETs verändern keinen Zustand.

Der Host wählt den ersten Teilnehmer ausdrücklich. Danach ergibt sich die
zugberechtigte Partei aus Startpartei und bestätigten Zügen. Jeder Erfassungs-
befehl enthält die erwartete Zugnummer. Ein verspäteter zweiter Befehl für
denselben Zug wird abgelehnt, auch mit anderer Action-ID. Identische Retries
verwenden den ursprünglichen Receipt. Jede Bewertung erhält eine eigene
`DuelDecision` mit `TURN` und einer Kennung aus Runden-UUID und Zugnummer.
Die bestehende SQLite-IMMEDIATE-/Retry-Strategie serialisiert die Schreibvorgänge;
es gibt keine zusätzliche Transaktions- oder Revisionsverwaltung.

## Spielregeln und Sichtbarkeit

- **Assign:** Host wählt das mündlich genannte linke und rechte Element.
  Der Server prüft das Paar. Bestätigte Paare sind allen Rollen sichtbar und
  können nicht erneut als Zug zählen. Nicht bestätigte Zuordnungen bleiben
  bis zur Auflösung verborgen.
- **Sorting Ladder:** Host wählt ein noch verfügbares Element und die genannte
  Einfügeposition. Der Server prüft die gesamte daraus entstehende Teilliste
  gegen die eingefrorene Rangfolge. Nur richtige Einfügungen verändern die
  öffentliche Liste. Die vollständige Lösung erscheint erst nach Hostfreigabe.
- **Wer weiß mehr?:** Nur der Host sieht verdeckte Antworttexte. Sein Klick
  deckt eine noch nicht bestätigte Karte auf und wechselt den Zug. Teilnehmer
  und Spectator erhalten zuvor für diese Karte keinen Antworttext. Eine bereits
  aufgedeckte Karte wird abgelehnt; sie erzeugt weder einen weiteren richtigen
  Zug noch einen Fehler. Für eine falsche mündliche Antwort existiert eine
  ausdrückliche Hostaktion.

Die vom Auftrag vorgegebene Arbeitsannahme lautet: Der erste tatsächliche
Fehler beendet die Runde. Die andere Partei erhält genau einen Spielpunkt und
einen Rundensieg. Korrekte Einzelzüge erzeugen keine zusätzlichen Spielpunkte.
Der Host gibt anschließend die Auflösung und das Ergebnis frei. Erst dabei
werden beide Punktebuchungen, Rundenergebnis und Matchprüfung gemeinsam
gespeichert. Eine zweite Auswertung kann nichts doppelt buchen. Der separate
Präsentationsabschluss bleibt auch nach dem entscheidenden Matchsieg erlaubt;
danach folgt die manuelle Lobby-Rückkehr.

Best of N und Zielpunktzahl verwenden unverändert die gemeinsame Matchengine.
Best of Seven benötigt vier Rundensiege. Zielpunktstände kommen ausschließlich
aus `DuelScoreEntry`; die Browser zeigen die vorhandenen Fortschrittsleisten.
Intro, Warten, Reload/Rejoin und der Einstieg ins nächste geplante Spiel
verwenden die vorhandenen Hub- und Duellkomponenten. Es wurde keine zusätzliche
Timerregel für mündliche Züge erfunden.

## Ausdrücklich offene Entscheidung

Die bestehenden Multiplayer-Punkteformeln bestimmen keine Duellwertung für
eine vollständig gelöste Aufgabe ohne Fehler. Auch der Architekturbericht
führte diesen Fall ausdrücklich als offen. Bis zu einer fachlichen Festlegung
wechselt die Runde nach `needs_resolution`: keine erfundenen Punkte, kein
Rundensieger und kein Matchabschluss. Der Host kann die vollständige Lösung
zeigen (`resolution_revealed`); ein wertungsabhängiger Abschluss bleibt gesperrt.
Insbesondere gewinnt nicht automatisch der letzte antwortende Teilnehmer.

Weitere bestehende offene Matchregeln (Anrechnung siegerloser Runden auf das
Best-of-N-Limit, gleichzeitiges Erreichen des Punkteziels, nachträgliche
Korrekturen) bleiben unverändert. Die neue Tabelle ist durch den bestehenden
Ausschluss der gesamten Duell-App aus Supabase-Sync und Restore erfasst.

## Regressionstest-Aufbau

Der erweiterte Lauf enthält auch die vollständigen Multiplayer-Suiten der drei
Typen. Dabei wurden ausschließlich zwei bestehende Testaufbauten korrigiert:
Der Wer-weiß-mehr?-Migrationstest stellte nach seinem historischen Ziel nicht
das aktuelle Gesamtschema wieder her. Er prüft weiterhin dieselben alten Daten,
stellt aber vor aktuellen Runtime-Aufrufen und per Cleanup alle Migrationsziele
wieder her. Die fehlenden Hub-Spalten verursachten zuvor Folgefehler.

Der Wer-weiß-mehr?-Präsentationsbrowsertest schaltet Live-Updates absichtlich ab.
Er wartet nun zuerst auf den tatsächlichen WebSocket-Zustand seiner Hub-Session,
beendet dann die Live-Verbindungen und lädt einmal den aktuellen HTTP-Zustand.
Er prüft die Auswahlstufe und die erfolgreiche Startantwort ausdrücklich.
Damit kann ein später Bootstrap-Snapshot nicht unbemerkt die Testauswahl
zurücksetzen. Fachliche Assertions, Wartezeitgrenzen und Multiplayer-Spielcode
wurden nicht abgeschwächt beziehungsweise verändert. Der historische
Duellmigrationstest für 0012 wird auf seine eigene Zielversion begrenzt.

## Abnahme

Der abschließende breite Lauf bestand mit **1566/1566 Tests**. Er enthält den
zuvor verwendeten Umfang (Duell, Hub, Estimation, Quick Quiz und die bisherigen
Buzzer-Spieltypen), ergänzt um die vollständigen Suiten von Assign, Sorting
Ladder und Wer weiß mehr?. Die gezielte Zwischenprüfung mit 62 Tests und die
vollständige Wer-weiß-mehr?-Prüfung mit 76 Tests bestanden ebenfalls nach den
beschriebenen Korrekturen des Testaufbaus.

Alle drei neuen Chromium-Fälle spielten über Sessionplanung und regulären
Hub-Start je ein Best-of-Seven-Match bis zum vierten Rundensieg und einen
weiteren Zielpunktzahl-Slot bis zum Matchende. Vier getrennte Browserkontexte
prüften Intro/Warten, Zugwechsel, doppelte Hostklicks, Fehlerwertung,
rollenabhängige Aufdeckung, Teilnehmer-/Spectator-Reload und Lobby-Rückkehr.
Die Backendtests prüfen zusätzlich getrennte SQLite-Verbindungen, Konflikte,
Rollback, SQL-Constraints, wiederholte Fragen und den ausdrücklich ungeklärten
Abschluss vollständig gelöster Aufgaben. Die Migration wurde ausschließlich
in isolierten Dateidatenbanken vorwärts und rückwärts getestet.

Im alten Who-WebSocket-Testabbau wurden weiterhin asynchrone Meldungen nach
geschlossenen Verbindungen beziehungsweise gelöschten Testdaten protokolliert.
Sie verursachten keinen Testfehlschlag; die gemeinsamen Multiplayer-Consumer
und deren State-Management wurden dafür nicht verändert.
