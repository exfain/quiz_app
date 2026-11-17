# Spielabende mit 25 Teilnehmeridentitäten

Die Browserabnahme verwendet 25 getrennte Chromium-Kontexte mit eigenen
Lobby-Zugangsdaten, zusätzlich Host und Spectator. Teilnehmer verwenden
480 × 640 Pixel und abwechselnd Standard/VHS, Host und Spectator Desktopgröße.
Interne HTTP- und WebSocket-Anfragen gehen unverändert an den echten lokalen
Django-/Channels-Testserver. Nur externe UI-Bibliotheken werden wie in den
bisherigen Browserprüfungen lokal ersetzt.

## Geprüfte Konfigurationen

| Abend | Abendwertung | Gewichtung | Zielansage | Manuelles Duell |
| --- | --- | --- | --- | --- |
| A | Einfach | Keine | Einfach, 120 Sekunden | Best of Three |
| B | Ranking | Linear: +0,15 je Spiel, Deckel 1,2 | Ranking, 90 Sekunden | Zielpunktzahl 2 |

Jeder vollständige Browserabend umfasst Planung, Speichern, Aktivierung,
25 echte Lobby-Beitritte, Check-in, Quick Quiz mit zwei Fragen/50 Abgaben,
Zielansage, ein manuelles Estimation-Duell und zwei Estimation-Turniersets.
Pro Turnierset entstehen zwölf Paarungen und ein gespeichertes Freilos ohne
Punkte. Das zweite Set endet in siegerlosen Begegnungen. Frühe Reveals bleiben
gesperrt, solange andere Paarungen noch antworten können. Reload, Rejoin,
manuelle Lobby-Rückwege und die sichtbare Abendwertung gehören zur Prüfung.
Duell- und Turnierpunkte dürfen die Abendwertung nicht verändern.

Bei Zielansage werden Aliasse, doppelte richtige Antworten, verfehlte Ziele,
gleiche erfolgreiche Ziele, die tatsächliche Frist und eine nachträgliche
Hostkorrektur geprüft. Die 25 Geräte senden ihre Antworten über die normalen
Formulare in unabhängigen Gruppen; alle HTTP-Abschlussstatus werden geprüft.

Zusätzlich prüfen sechs unabhängige Wertungsmatrizen je fünf abgeschlossene
Spiele mit 25 offiziellen Identitäten: Einfach/Ranking jeweils ohne Gewichtung,
mit linearer Gewichtung und mit erreichtem Deckel. Die Erwartungswerte werden
unabhängig berechnet. Enthalten sind Nichtabgabe, Ranggleichstände, eingefrorene
Ergebnisse, spätere Änderungen der Ausgangsdaten, verspäteter Beitritt und eine
Abendkorrektur.

## Belegte Korrekturen

- Lobby-Beitritt: Derselbe öffentliche Zustand wurde für jede Verbindung erneut
  aus der Datenbank berechnet. Gemessen wurden unter Last unter anderem
  9,74–11,54 Sekunden bis zur Socket-Annahme und Abbrüche mit Code 1006. Der
  Zustand wird jetzt einmal pro Beitritt berechnet und verteilt. Ein
  serverseitiger Erzeugungszeitpunkt verhindert, dass eine ältere Gruppennachricht
  einen bereits neueren direkt abgefragten Zustand ersetzt. Einzelabfragen
  bleiben frisch; private Rejoin-Tokens gehören nicht in die Gruppennachricht.
- Quick-Quiz-Host: Zwischen HTML-Aufbau und Socket-Anmeldung gingen
  Teilnehmermeldungen verloren (sichtbar 6 statt tatsächlich 25). Nach jeder
  Socket-Verbindung lädt der Monitor die vorhandene, hostgeschützte und nach
  Session gefilterte Teilnehmer-API. Vorhandene Zeilen werden nicht dupliziert
  oder mit möglicherweise älteren Punkten überschrieben. Ein eigener
  Browsertest erzeugt genau dieses Zeitfenster und prüft auch den Reload.
- Lobby-Voting: Ein einzelner Zustandsburst erzeugte im gezielten Browsertest
  20 überlappende HTTP-Abfragen. Jetzt läuft höchstens eine Abfrage zugleich;
  währenddessen eintreffende Aktualisierungen lösen anschließend eine frische
  Abfrage mit der aktuellen Teilnehmeridentität aus. Der Browsernachweis prüft
  auch eine tatsächlich serverseitig gespeicherte Stimme während der Verzögerung.
- Zielansage: Parallele reine Zustandsabfragen reservierten durch `BEGIN IMMEDIATE`
  Schreibsperren; ein Browser-GET scheiterte nach 20 Sekunden mit `database is locked`
  und HTTP 500. Ausschließlich der eigenständige Snapshot verwendet auf seiner
  lokalen SQLite-Verbindung jetzt eine konsistente `DEFERRED`-Lesetransaktion.
  Der vorherige Modus wird auch bei Fehlern wiederhergestellt. Eine bereits
  umgebende Schreibtransaktion und sämtliche Schreibdienste bleiben unverändert.
  Zwei getrennte Verbindungen belegen, dass ein offener Leser einen zweiten
  Snapshot nicht mehr blockiert. Es gibt keine globale Datenbankumstellung.
  Zusätzlich ersetzt eine Sammelabfrage die 25 separaten Antwortabfragen im
  Host-Snapshot. Teilnehmer lesen nur ihre eigene Zeile und gemeinsame Zähler;
  die bisherigen Freigaben und Inhalte bleiben erhalten. Querybudgets sichern
  den 25er-Fall zusätzlich ab.
- Geschlossene Hub-Sockets: Beim Seitenwechsel konnte eine bereits vorbereitete
  Antwort einen Transportfehler auslösen und die normale Disconnect-Bereinigung
  unterbrechen. Der Hub ignoriert ausschließlich `Disconnected`/`OSError` beim
  Senden. Der reguläre Disconnect samt Presence-Bereinigung läuft weiter;
  andere Anwendungsfehler werden ausdrücklich nicht unterdrückt.
- Estimation-Turnier: Auch die reinen Runden-, Match- und Browser-Snapshots
  reservierten SQLite-Schreibsperren. Getrennte Verbindungen reproduzierten das
  Blockieren eines zweiten Lesers. Eigenständige SQLite-Leseabfragen verwenden
  jetzt konsistente DEFERRED-Transaktionen; laufende Command-Transaktionen und
  die Sperrstrategie auf Datenbanken mit Zeilensperren bleiben erhalten.
  Ein Konkurrenztest prüft während einer halbfertigen Punktebuchung ausdrücklich
  den vollständigen alten Stand (9:9) und nach Commit den neuen Stand (10:10).
  Ein weiterer 25er-Test reproduzierte 27 Abfragen für die gemeinsame
  Estimation-Reveal-Sperre. Eine gemeinsame SQL-Prüfung ersetzt diese Einzelreads,
  ohne Ergebnisse zu cachen oder eine offene/fehlende Runde freizugeben.
  Die Turnierübersicht lädt Paarungen, Phasen und Journalzeilen gemeinsam pro
  Snapshot. Das ist insbesondere für bereits abgeschlossene Teilnehmerseiten
  wichtig, die auf die nächste Auslosung warten. Der 25er-Test begrenzt die
  Abfragen auch bei zwölf veröffentlichten Ergebnissen samt Freilos und prüft,
  dass zuvor keine Punkte ausgegeben werden. Andere Turnierspieltypen behalten
  ihren bisherigen Projektionspfad.
  Feld- und tabellenübergreifende Modellprüfungen bleiben aktiv. Bereits von der
  Datenbank garantierte UNIQUE/CHECK-Regeln werden beim Lesen nicht erneut als
  SQL-Prüfabfragen ausgeführt; Schreibdienste behalten ihre vollständige
  Validierung. Der vorbereitete Browser-Snapshot sank dadurch von 69 auf 47
  Abfragen und besitzt ein gesondertes Querybudget.

## Last und Testaufbau

Auf dem belegten 16-GB-Testrechner waren mit 25 Desktopansichten zeitweise nur
rund 140 MB RAM frei. Handy-Auflösungen reduzieren die Renderinglast, ohne die
25 Identitäten oder deren Anfragen zu ersetzen. Offene schwebende Scoreboxen
werden über den vorhandenen Schalter eingeklappt; es gibt keine erzwungenen
Klicks durch überlagernde Elemente.

Das vorhandene Playwright-Routing für externe Bibliotheken deaktiviert den
HTTP-Browsercache auch für lokale Ressourcen. Wiederholte Navigationen laden
diese deshalb im Test erneut. Zusammen mit 27 Browserkontexten auf einem Rechner
ist das eine Funktions-/Lastprüfung, kein Geschwindigkeitsbenchmark für 25
physisch getrennte Geräte.

Beim gleichzeitigen Spieleinstieg wurden bis zu 76 ausstehende Datenbankaufträge
beobachtet. Der neue Lasttest hat deshalb für die genaue Hostbestätigung ein
explizites Budget von 60 Sekunden. Bestehende Tests, Server-Timeouts und
Antwortfristen bleiben unverändert. Es gibt keine automatische Wiederholung
fehlgeschlagener Testaktionen. Navigation allein zählt nicht als bestätigte
Spielsocket-Anmeldung. Intro-Identität, Titel und Spielnummer werden aus dem
aktuellen Serverkontext geprüft. Ein später eintreffendes Gerät darf einen bereits
verstrichenen Introzeitraum nicht neu starten. Vor dem Löschen der Testdaten wird
der ausschließlich für diesen Fall gestartete Daphne-Prozess beendet und vor dem
nächsten unabhängigen Fall neu gestartet. Damit können keine späten Presence-
Schreibvorgänge in die nächste Testdatenbank-Leerung hineinlaufen. Reload und
Rejoin innerhalb eines Abends verwenden weiterhin denselben Serverprozess.
Auch zusätzlich geöffnete Host-Steuertabs verwenden das ausgewiesene
60-Sekunden-Lastbudget; zuvor galt dort versehentlich der 30-Sekunden-Standard.

## Ergebnisstand

- Wertungsmatrix und benachbarte Abend-/Konkurrenz-/Bereitschaftstests: 31/31.
- Lobby-Verteilung und relevante Hub-/Berechtigungsregressionen: 45/45.
- Gezielter Browsernachweis der verlorenen Host-Teilnehmermeldungen: erfolgreich.
- Voting-Browserprüfung und geschützter/Session-gefilterter Host-Roster: 3/3.
- Zielansage einschließlich Lesekonkurrenz, Modus- und Querybudgetprüfungen: 26/26.
- Eigenständiger Zielansage-Browserlauf: 25 Teams, 84 Antworten, Fristende,
  Hostkorrektur, Veröffentlichung, Rejoin und vollständige Lobby-Rückkehr bestanden.
- Vollständiger Ranking-Abend mit 25 Teilnehmern: einschließlich beider
  Turniersets, manueller Rückkehr und sichtbarer Endwertung bestanden.
- Vollständiger einfacher Abend mit 25 Teilnehmern: einschließlich beider
  Turniersets, manueller Rückkehr und sichtbarer Endwertung bestanden
  (gezielter Nachlauf: 1/1, 2133 Sekunden).
- Breite Regression: 1432/1434 im ersten abschließenden Lauf. Beide Befunde
  betrafen Testannahmen: Der Paarungs-Test erzeugte das reine Mehrspieler-Spiel
  Zielansage ohne dessen erforderliche Konfiguration; der Konkurrenzhelfer
  erwartete auch beim schreibfreien Snapshot eine IMMEDIATE-Transaktion.
  Die Tests unterscheiden jetzt die ausdrücklich nicht duellfähige Zielansage
  und die DEFERRED-Lesetransaktion vom weiterhin IMMEDIATE-geschützten
  Schreibpfad. Sämtliche fachlichen Berechtigungs-, Frist-, Revisions- und
  Geheimhaltungsprüfungen bleiben bestehen. Der gezielte Nachlauf dieser
  drei Module bestand mit 33/33. Ein erneuter vollständiger 1434er-Lauf
  wurde nicht durchgeführt.

Alle Datenbanken und Medien liegen in temporären Testverzeichnissen. Auch der
Alias `supabase` zeigt ausschließlich auf lokales SQLite. Produktivdaten,
Migrationen, Duellwertung und Spielregeln werden durch diese Korrekturen nicht
verändert. Der Git-Index bleibt unangetastet.
