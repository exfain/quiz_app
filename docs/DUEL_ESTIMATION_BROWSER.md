# Estimation-Duell im lokalen Browserbetrieb

## Start und Test

Die vorhandenen Migrationen bis `games_hub.0027` und `duel.0010` müssen vorliegen.
Dieser Patch fügt keine Migration hinzu; die Verifikation verwendet isolierte
Testdatenbanken. Lokale Schemaaktualisierung bei Bedarf: `.venv\Scripts\python.exe manage.py migrate`.

1. `launch-website.bat` starten und im Dashboard eine Session erstellen.
2. **2-Spieler-Session (Estimation)** wählen, Estimation-Spiel hinzufügen und
   Best of N (positive ungerade Zahl) oder Zielpunktzahl (positive ganze Zahl)
   ausdrücklich konfigurieren. Leere Altwerte bleiben unkonfiguriert.
3. Im Session-Hub bei Bedarf die Session aktivieren. Zwei getrennte Browserprofile
   oder Geräte treten über die Teilnehmer-Lobby mit unterschiedlichen Teamnamen
   bei. Der dritte Beitritt wird abgewiesen; Rejoin verwendet das signierte Token.
4. Host startet den Check-in, beide Teilnehmer bestätigen „Ich bin bereit“,
   Host schließt den Check-in ab. Die Slot-Konfiguration lässt sich hier erneut
   bearbeiten und speichern, solange der Slot noch nicht gestartet wurde.
5. „Spiel starten“ öffnet das Duell. Teilnehmer und Spectator wechseln aus den
   regulären Hub-Ansichten ins Spielintro und danach in den Wartestatus.
6. Host wählt eine Frage, bereitet sie vor, gibt eine Antwortdauer in Sekunden
   an und startet die Frage nach dem Intro. Beide Teilnehmer loggen ihre Zahlen
   ein. Danach gibt der Host **Antworten → Lösung → Ergebnis** nacheinander frei
   und bestätigt den Präsentationsabschluss. Solange das Match weiterläuft,
   kann eine weitere Frage vorbereitet werden, einschließlich Wiederholungen.
7. Best of Seven endet bei vier Rundensiegen. Im Zielmodus zeigen zwei Balken den
   Fortschritt. Nach dem entscheidenden Ergebnis und Präsentationsabschluss führt
   der sichtbare Rückweg in die Lobby beziehungsweise den Host-Hub.

## Frist-Worker

Der vorhandene Windows-Launcher ruft nun `launch_website.py serve <port>` auf.
Dieser Prozess startet den Django-Server und einen separaten, unter Windows
fensterlosen Worker. Beim normalen Beenden des Servers beendet er den Worker.
Der Django-Autoreloader erzeugt keinen zweiten Worker.

Bei manuellem `manage.py runserver` ist zusätzlich ein eigener Prozess nötig:

```powershell
.venv\Scripts\python.exe manage.py duel_deadlines
```

Alternativ startet `.venv\Scripts\python.exe launch_website.py serve 8000` beide.
`duel_deadlines --once` verarbeitet einmal alle fälligen offenen Fristen.
Der Worker liest ausschließlich gespeicherte Deadlines und verarbeitet nach
einem Neustart auch überfällige Runden. Er benötigt keine Browserverbindung.
Er schließt nur die Antwortphase und löst weder Reveal noch Punktevergabe aus.

## Persistenz, Rechte und Isolation

- Der Sessiontyp, niemals die Teilnehmerzahl, wählt den Duellzweig.
- Die konkrete Startoperation eines Slots hat eine deterministische UUID aus
  Session-/Slot-ID. Session-/Slot-Sperren, Instanzerstellung, Matchstart,
  Intro, Hub-Zuordnung, Receipt und Revision gehören zu **einer** vorhandenen
  `execute_action`-Transaktion. Fehler rollen die gesamte Erstellung zurück.
- Wiederholte Starts öffnen dasselbe Match. Ein expliziter Neustart eines
  abgeschlossenen Slots ist kein Bestandteil dieser Funktion; dafür muss später
  eine eigene neue Laufidentität vorgesehen werden. Weitere geplante Slots sind
  unabhängig und dürfen dieselben Materialfragen verwenden.
- Hostrechte stammen aus den vorhandenen Spiel-/Sessionberechtigungen;
  Teilnehmerrechte aus dem vorhandenen signierten Lobby-Rejoin-Token.
  Spectator bleibt wie im Hub öffentlich und ausschließlich lesend.
- Frage und Ziel werden über die vorhandene Snapshotbindung eingefroren.
  JSON-Projektionen enthalten Lösung und fremde Antworten erst nach der
  passenden gespeicherten Hostfreigabe. GETs schreiben keine Zustandsänderungen.
- Browser verwerfen ältere Revisionen; Zahlenfelder sperren nach dem Commit.
  Timeranzeige basiert auf Serverzeit/Deadline, die Annahme entscheidet der Server.
- Multiplayer verwendet weiterhin seine bisherigen Controller und Vorlagen.
  Duellpunkte werden nicht in Multiplayer-Punktetabellen kopiert. Die bestehende
  Supabase-Ausnahme für die Duell-App bleibt erhalten.

## Fachlich noch offen

Fehlt eine Antwort, können die tatsächlich vorhandenen Antworten und die Lösung
aufgedeckt werden. Ergebnis und Rundenabschluss bleiben gesperrt, der Host sieht
den Grund. Es gibt keine Ersatzwertung und keinen erfundenen Sieger. Ein eigener
Duell-Abbruch-/Korrekturservice existiert noch nicht und wird hier nicht erfunden.

Erreichen beide Parteien gleichzeitig die Zielpunktzahl, bleibt der Matchausgang
ungeklärt. Eine weitere Runde wird daraus nicht automatisch als Tiebreaker
abgeleitet. Die Anrechnung unentschiedener Runden auf ein Best-of-Limit sowie eine
spätere Session-Gesamtwertung sind weiterhin keine neue technische Defaultregel.

## Verifikation dieses Patches

- Isolierter Gesamtlauf: 843 Tests, davon 841 erfolgreich, keine übersprungen.
  Alle 558 Duelltests und 100 Multiplayer-Estimation-Tests waren erfolgreich.
  Zwei vorhandene Hub-Intro-Tests schlagen im unveränderten Ausgangsbestand
  weiterhin fehl: `test_lobby_navigation_uses_only_server_intro_metadata` erwartet
  einen entfernten zweiten Inline-Aufruf; bei
  `test_every_player_template_uses_the_shared_intro_and_live_state_hook` fehlt
  dem bestehenden Where-is-this-Pfad der erwartete Intro-State-Hook. Die
  betroffenen Tests, Lobby-/Where-Templates und Where-Skripte wurden durch
  diesen Patch nicht verändert. Die breite Suite ist deshalb noch nicht grün.
- Abschließender Lauf der Hub-Start-, Worker- und Launcher-Tests: 16/16 erfolgreich,
  einschließlich separater Datenbankverbindungen und zwei konkurrierender Worker.
- Buzzer: 28/28; bestehende Launcher-Tests: 8/8 erfolgreich.
- Chromium: Best of Seven bis zum vierten Rundensieg und Zielpunktzahl bis zum
  Matchabschluss über die reguläre Sessionplanung, mit getrennten Kontexten
  für Host, beide Teilnehmer und Spectator. Geprüft wurden Intro/Warten,
  Abgaben, Reveal-Reihenfolge, geheim gehaltene JSON-Daten, Fortschrittsbalken,
  Reload, verspätete Snapshot-Antworten sowie Rückkehr zur Lobby.
  Ein zweiter Slot prüft neue Instanz/Intro, Rejoin und Fristablauf mit fehlender
  Antwort durch den echten Workerprozess; die ungeklärte Wertung bleibt gesperrt.
- Worker-Prozessneustart, browserloser Fristabschluss und Abgabe genau an der
  Deadline wurden mit einer isolierten dateibasierten SQLite-Datenbank geprüft.
- `manage.py check`, `makemigrations --check --dry-run` und `git diff --check`
  sind erfolgreich. Die früher sporadisch auffälligen Multiplayer-Estimation-
  Browsertests zeigten in diesem Gesamtlauf keinen Timeout.
