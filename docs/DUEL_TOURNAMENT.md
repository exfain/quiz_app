# Turnierbetrieb in Mehrspielersessions

Ein ausdrücklich gestarteter Turnierslot verwendet `GameFormat.TOURNAMENT` in
einer `SessionType.MULTIPLAYER`-Session. Teilnehmerzahl allein aktiviert keinen
Modus. Normales Multiplayer, manuelle Duelle und reine Duellsessions behalten
ihre jeweiligen Einstiege.

## Organisation und Persistenz

`DuelTournament` gehört zu genau einem Hub-Spielslot. `DuelTournamentSet` hält
die fortlaufende Auslosung und die gemeinsame Materialauswahl fest. Jede
`DuelTournamentPair` verweist auf eine eigene vorhandene `DuelGameInstance` mit
zwei unveränderlichen Parteien und einer eigenen `DuelUnit`.
`DuelTournamentSeat` sichert die Teilnahme pro Person und Set auch über beide
Parteipositionen hinweg datenbankseitig eindeutig ab. Ein Platz ohne Paarung
ist das zufällige Freilos; dafür gibt es keine Punkte.

Jede Auslosung verwendet die aktuell aktiven, offiziellen, spielberechtigten
Sessionteilnehmer, ohne dauerhaft ausgeschiedene Personen. Ab zwei Personen
kann ein Set ausgelost werden. Bei ungerader Zahl wird vor der Paarbildung eine
zufällige Person zum Zuschauer mit Freilos. Die nächste Auslosung berücksichtigt
die dann aktuelle Anwesenheit neu. Wiederholungspaarungen sind zulässig.
Weniger als zwei Berechtigte führen zu einer verständlichen Ablehnung; ein noch
leeres Turnier kann beendet werden.

Migration `0016_tournament_sets` ergänzt nur diese vier Organisationstabellen.
Das gesamte Duell-App-Label bleibt aus Supabase-Sync und Restore ausgeschlossen.
Beziehungsprüfungen über Tabellen hinweg erfolgen im Service und mit
`full_clean()`; Django-`save()` ersetzt diese Prüfung nicht.

## Vorhandene Duellregeln

Unterstützt sind Estimation, Quick Quiz mit True/False oder Multiple Choice,
Where is this?, Black Jack Quiz, Assign, Sorting Ladder, Wer weiß mehr? und das
Gebotsspiel. Der Host wählt dieselbe Frage bzw. dasselbe Set für alle Paarungen.
Quick-Quiz-Freitext wird sowohl bei der Auswahl als auch beim Binden abgelehnt.
Buzzer-Spieltypen und Host-Punktevergabe ohne definierte Sets sind ausgeschlossen.

Eine Begegnung umfasst ein Turnierset, technisch Best of One. Bei Black Jack
umfasst es das vollständige ausgewählte Set mit seinen einzelnen Fragen.
Punkte, Rundenergebnis und Präsentationsphase bleiben in den vorhandenen
Duellmodellen. Ein Unentschieden bzw. Ergebnis ohne Sieger erzeugt keinen
erfundenen Matchsieger. Eine vollständig präsentierte Entscheidungseinheit reicht
für die nächste Auslosung aus. Es gibt keine zusätzliche Zielpunktzahl oder
Best-of-Serie über wechselnde Paarungen hinweg.

## Transaktionen und Wiederholungen

`duel.tournaments` bindet Material und alle Begegnungen eines neuen Sets atomar
innerhalb des vorhandenen `execute_action()`-Schreibpfads. Der erste Begegnungs-
datensatz dient als feste Receipt-/Revisionsidentität für reine Organisations-
aktionen. Nur bestätigte interne Hostaktionen `draw` und `end` dürfen diese
Ausnahme verwenden; ein bereits abgeschlossenes Match und seine Units werden
dabei nicht wieder geöffnet oder verändert. Es entsteht keine zweite
Aktionsverarbeitung und kein zusätzliches Kontrollmatch.

Auslosungen haben eine stabile Aktionskennung aus Turnier und Vorgängerset.
Identische Retries liefern denselben Receipt, widersprüchliche Materialauswahl
führt zu einem Konflikt. Paarungs- und Platzdatensätze werden nicht neu gewürfelt.
Fehler beim Binden rollen das gesamte neue Set zurück. SQLite verwendet weiter
IMMEDIATE-Transaktionen und die bestehenden Lock-Retries. Bei anderen Datenbanken
wird zuerst das gemeinsame Set, danach die konkrete Begegnung gesperrt.

## Sichtbarkeit und Bedienung

Die gemeinsame Turnierübersicht zeigt Paarungen, Abgabefortschritt, Fristen,
Freilose und abgeschlossene Ergebnisse. Der Host öffnet die vorhandene
Duellsteuerung jeder Paarung; Teilnehmer werden anhand ihrer signierten
Sessionidentität zur eigenen Begegnung geleitet. Bei Assign/Sorting/Wer weiß
mehr? bleibt die bestehende explizite Wahl der ersten zugberechtigten Partei
erhalten. Die Estimation-Antwortdauer wird für das ganze Set gespeichert.
Die Estimation-Hoststeuerung bleibt bis zum ersten autoritativen Match-Snapshot
verborgen; separat früher geladene Turniermetadaten geben sie nicht frei.
Spielende Geräte laden während ihrer Runde nur die eigene Matchprojektion.
Erst nach deren Abschluss prüft die Teilnehmernavigation die nächste Auslosung;
Host-/Spectator-Paarungsfenster duplizieren die gesamte Turnierübersicht nicht.

Eine Freigabesperre im zentralen Schreibpfad verhindert Antworten-/Lösungs-
und Ergebnis-Reveals, solange eine Paarung dieselbe Aufgabe noch beantworten
kann. Zusätzlich maskiert die gemeinsame Transportprojektion frühe Ergebnisse,
auch bei direktem Zugriff auf die einzelne Spectator-API. Bei Black Jack gilt
die Sperre pro Frage innerhalb des Sets; bereits beendete Begegnungen blockieren
die verbleibenden Paarungen nicht. Eine bereits geschlossene eigene Paarung
wartet ohne öffentliche Lösung auf die übrigen. Snapshot-GETs schreiben nichts.
Auch Snapshot-Lockkonflikte verwenden den vorhandenen SQLite-Retry-Helfer. Nach
einem POST wird ausschließlich die Ergebnisabfrage wiederholt, niemals die
bereits bestätigte Auslosung oder fachliche Aktion.

Nach vollständiger Präsentation aller Paarungen kann der Host neu auslosen oder
das Turnier beenden. Die Rückkehr zur Hub-Lobby bleibt manuell. Ergebnisse werden
als Setprotokoll angezeigt und nicht in die Abendwertung übernommen. Eine
Turnier-Gesamtsieger- oder Ranglistenregel wurde nicht zusätzlich erfunden.
Die vorhandene lokale Fristverarbeitung `duel_deadlines` verarbeitet auch die
Turnierbegegnungen; kein neuer Hintergrunddienst ist erforderlich.

## Bestätigte Wertung und verbleibende Fälle

Die bestätigten Regeln in [DUEL_SCORING_RULES.md](DUEL_SCORING_RULES.md) schließen
Nichtabgaben bei Estimation/Where/Geboten, gleiche Distanzen, Black-Jack-Gleichstand
und beidseitigen Bust sowie vollständig gelöste Wechselzugaufgaben regulär ab.
Siegerlose Begegnungen erhalten einen ausdrücklichen Matchabschluss ohne Sieger;
nach vollständiger Präsentation aller Paarungen ist die nächste Auslosung möglich.
Black-Jack-Nichtabgaben bleiben gesperrt. Eine übergreifende Turnierwertung und
nachträgliche Ergebniskorrekturen benötigen weiterhin eine eigene Entscheidung.
