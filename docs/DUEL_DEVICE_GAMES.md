# Where is this? und Black Jack Quiz im Duell

## Einstieg und Datenquellen

Nur geplante Sessions mit `session_type=duel` verwenden diese Controller.
Sessionplanung, Slot-Konfiguration, Check-in, deterministische Slot-Identität,
Hostberechtigung, Intro und manuelle Lobby-Rückkehr verwenden die vorhandenen
Duellpfade. Zwei Teilnehmer in einer normalen Session aktivieren keinen Duellmodus.

`DuelInputRound` speichert genau eine eingefrorene Where-Frage beziehungsweise
ein vollständiges konfiguriertes Black-Jack-Set je `DuelUnit`. Darin stehen
Fragetext, Lösung, Antwortdauer und benötigte Darstellung; bei Where zusätzlich
Kartenregion, Hinweis und optional ein bereinigtes PNG ohne Dateinamen/EXIF.
Bearbeitungen der Ausgangsfragen verändern eine vorbereitete Runde nicht.
Dieselbe Frage beziehungsweise dasselbe Set darf in neuen Units erneut vorkommen.

## Transaktion und Abgaben

Alle Aktionen verwenden `execute_action()` mit vorhandenen Receipts,
SQLite-IMMEDIATE-Transaktionen, Sperren-Retry und autoritativer Revision.
Teilnehmer kommen ausschließlich aus dem signierten Hub-Rejoin-Kontext.
Hostaktionen verwenden bestehende Django-/Hub-Berechtigungen.

Black Jack benötigt mehrere unabhängige Antworten einer Partei in **derselben**
Unit, da eine ganze Setrunde nur einen Rundensieg ergeben darf. Deshalb ergänzt
Migration `0014_device_input_rounds` ein serverseitiges `DuelSubmission.input_key`.
Für sämtliche bisherigen Spiele bleibt der Schlüssel leer; ihre Eindeutigkeit,
API-Aufrufe und Request-Fingerprints bleiben erhalten. Black Jack setzt intern
`question:0`, `question:1`, …; Clients dürfen diesen Schlüssel nicht übermitteln.
SQL-Constraints sichern weiterhin genau eine verbindliche Antwort je Partei,
Unit, Eingabeart und konkreter Frage. Annahmesequenzen bleiben Unit-weit eindeutig.
Ein früherer Retry liefert seinen Receipt, verändert jedoch die nächste Frage nicht.

## Ablauf, Frist und Veröffentlichung

Die erste Runde ist vorbereitet; erst Host-Start setzt Startzeit und Deadline.
Die zweite gültige Antwort oder der vorhandene lokale `duel_deadlines`-Worker
schließt die Antwortphase. `launch_website.py` startet diesen Worker bereits;
separat lautet der Befehl `python manage.py duel_deadlines`.
Die Frist gilt bei Abgabe unter derselben Transaktionssperre: Annahme nur vor,
nicht genau an der Deadline. GET-Abfragen verändern keine Daten.

Where: Host löst die Karte auf, danach veröffentlicht er die Wertung und
bestätigt abschließend die Rundenpräsentation. Die originale `WhereMap` liefert
Welt-/Europa-/Deutschlandkarte, Zoom und horizontale Weltwiederholung. Ein
Duelladapter zeigt beide freigegebenen Marker und Distanzen. Beim Beginn des
Reveals wird auf Standardzoom zurückgesetzt. Eigene Entwürfe lassen sich bewegen;
nach Commit bleiben Marker auch nach Reload gesperrt.

Black Jack: Antworten zeigen → Lösung zeigen → gemeinsam werten. Solange das
Set weiterläuft: nächste Frage vorbereiten → Host startet deren neue Frist.
Vergangene Fragefenster werden einmal angehängt; Antworten verbleiben ausschließlich
in `DuelSubmission`. Sternestände werden aus den freigegebenen Fragen rekonstruiert,
nicht als unabhängige zweite Summe gespeichert.

Rollenprojektionen geben vor Hostfreigabe weder gegnerische Antworten noch Zielwert
aus. Auch ein Host-GET gibt die Lösung nicht vorzeitig aus. Das geschützte
Bild-Endpoint verwendet dieselben Rollenprüfungen. Snapshots enthalten keine
rohen Materialobjekte oder beliebigen Receipt-Daten. Wiederholte und veraltete
Browserantworten können eine neuere Revision nicht überschreiben.

## Wertung

Where verwendet die vorhandene reine Haversine-Distanzberechnung. Die geringere
ungerundete Distanz gewinnt einen Rundensieg und einen Duellpunkt.

Black Jack verwendet die vorhandene Ganzzahlvalidierung und Sterneformel
`abs(Antwort - Zielwert)`. Beide Antworten derselben Frage werden vollständig
verarbeitet, **bevor** ein Bust oder Setsieger feststeht. Ein einzelner Bust
gegen höchstens 21 Sterne beendet das Set sofort: Der Gegner erhält einen
Rundensieg und einen Duellpunkt. Das schließt die ausdrücklich bestätigte
Konstellation **exakt 21 gegen Bust** ein. Ohne Bust gewinnt am regulären
Setende die eindeutige beste Annäherung an 21.

Sterne sind keine Duellpunkte. Punktbuchungen stehen nur in `DuelScoreEntry`,
Rundensieger nur in `DuelUnitOutcome`, Matchabschlüsse in der bestehenden Engine.
Best of Seven benötigt vier Rundensiege; Zielpunktzahl verwendet die
Punktbuchungen und liefert Füllleisten. Ergebnis, Rundenschluss, Matchprüfung,
Receipt und Revision werden atomar gespeichert. Kein zweiter Ergebnisweg.

## Fachlich offen und bewusst gesperrt

- Exakt gleiche Where-Distanzen.
- Fehlende Antworten bei beiden Spielen.
- Beidseitiger Bust oder gleicher Sternestand am Black-Jack-Setende.
- Allgemeine spätere Korrektur-/Abbruchregeln für ungeklärte Runden.

Diese Fälle wechseln nach der erlaubten Auflösung in `needs_resolution` und
zeigen dem Host den konkreten Grund. Keine erfundenen Punkte, Rundensiege oder
automatischen Matchabschlüsse. Die allgemeine Matchkonvention für gleichzeitig
erreichte Zielpunkte bleibt unverändert; diese beiden Spiele vergeben bei einem
eindeutigen Rundenergebnis nur einer Partei einen Punkt.

## Migration und Isolation

0014 ergänzt ausschließlich die Duelltabelle, den Abgabeschlüssel und notwendige
Constraints. Leere bestehende Schlüssel behalten ihre Bedeutung. Ein Rückwärtslauf
ist bei bereits gespeicherten mehrteiligen Black-Jack-Abgaben nicht verlustfrei
darstellbar; ein expliziter Guard verweigert ihn, statt Antworten zu löschen oder
zusammenzufassen. Migrationstests verwenden ausschließlich isolierte Datenbanken.
Die gesamte Duell-App bleibt vor Modell-Erkennung aus Supabase-Sync/-Restore
ausgeschlossen. Multiplayer-Modelle, Consumer, Templates und Kartencontroller
werden nicht verändert.
