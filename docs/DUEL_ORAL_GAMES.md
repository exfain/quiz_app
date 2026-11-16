# Mündliche Duellspiele

Für geplante `SessionType.DUEL`-Sessions sind Clue Rush (`clue_rush`), Who is lying
(`who`), Who is that (`who_that`) und Wann war das? (`wann_war_das`) angebunden.
Der reguläre Hub-Einstieg, Check-in, Slotkonfiguration, Intro und Lobby-Rückweg
werden weiterverwendet. Zwei Teilnehmer einer Multiplayer-Session ändern den
Modus nicht. Spielconsumer und Scoring der Multiplayer-Spiele bleiben getrennt.

## Daten und Transaktionen

Die additive Migration `0012_oral_buzzer_rounds` erzeugt nur `DuelOralRound`
und `DuelBuzz`. Ihre einzige Migrationsabhängigkeit ist `duel.0011`; die späteren
Multiplayer-Migrationen bleiben unabhängig rückgängig machbar.

`DuelOralRound` bindet eine konkrete Unit an einen unveränderlichen Snapshot.
Die Herkunft wird durch Spieltyp der Instanz, geplanten Slot, eingefrorene
Spiel-ID und Frage-ID dokumentiert. Der Binder prüft die tatsächliche konkrete
Fragenauswahl des Spielmodells unter der vorhandenen Transaktionssperre. Laufende
Runden lesen ausschließlich den Snapshot; wiederholte Fragen erhalten neue Units.
Es gibt keine generische Fremdschlüsselarchitektur und keine Mutation vorhandener
Spielmodelle. Quellfragen können nach dem Binden geändert werden.

Bilddaten werden einmal als PNG ohne EXIF/Textmetadaten eingefroren und atomar
in der Duelltabelle gespeichert. Ein geschützter Bildendpunkt liefert diese
Bytes aus. Quelldateiname und originale Media-URL werden nicht veröffentlicht;
sie könnten selbst bereits die Lösung enthalten. Die Bildfrage zeigt wie der
bestehende Spieltyp das Foto während der Frage, nach Bewertung zusätzlich die
Namensauflösung. Es wurde keine neue Bildverzerrungsregel eingeführt.

`DuelBuzz` dokumentiert UUID, konkrete Unit, Partei, serverseitige Annahmezeit,
Annahmesequenz, logischen Fortschritt und eingefrorenen Kontext. SQL schützt
je einen Versuch pro Partei und Unit sowie die Sequenz. Derselbe Buzzerkern
bedient die vier neuen Typen; der bestehende Quick-Quiz-Ablauf wird nicht ersetzt.

Alle Mutationen laufen über `execute_action()`, den bestehenden Parent-Lock,
SQLite IMMEDIATE und Retry. Versuch, Hostentscheidung (`DuelDecision`), beide
Punktebuchungen, Rundenergebnis, Matchabschluss, Receipt und Revision werden in
derselben Transaktion geschrieben. Es gibt keine eigene zweite Punktequelle,
Transaktionsarchitektur oder Teilnehmerverwaltung. Normale `save()`-Aufrufe
führen weiterhin nicht automatisch `full_clean()` aus und ersetzen keinen
autorisierten Schreibpfad. Direkte ORM-/SQL-Korrekturen sind kein Servicepfad.

## Regeln und Zeit

- **Clue Rush:** Ein Buzz pausiert den gespeicherten logischen Fortschritt.
  Während einer Hostbewertung läuft keine Hinweiszeit weiter. Bei falsch bleibt
  die Partei durch ihren gespeicherten Versuch gesperrt. Die andere Partei darf
  einmal buzzern. Der neue Fristzeitpunkt ist die Serverzeit der Bewertung plus
  exakt die verbleibenden Mikrosekunden. Hinweise einschließlich Restzeit eines
  begonnenen Hinweises laufen anschließend weiter. Richtig gibt genau einen
  Punkt und einen Rundensieg. Zwei falsche Versuche ergeben null Punkte und
  keinen Rundensieger.
- **Who is lying:** Die eingefrorene Reihenfolge folgt der vorhandenen
  serverseitigen Randomisierung; die konfigurierte Zeit gilt pro Person.
  Der akzeptierte Buzz bindet den zum Annahmezeitpunkt aktiven Index und Namen.
  Exakt an der Namensgrenze gilt die nächste Person. Die gespeicherten
  Wahrheitsdaten bestimmen die Wertung: Lügner gibt dem Buzzenden einen Punkt,
  sonst dem Gegner. Das Set endet sofort. Es wird keine Hostentscheidung
  vorgetäuscht. Ohne Buzz gibt es nach Hostfreigabe null Punkte/keinen Sieger.
- **Who is that:** Ein exklusiver Versuch, danach mündliche Hostbewertung.
  Richtig gibt dem Buzzenden einen Punkt, falsch dem Gegner. Danach Bild/
  Namensauflösung und Rundenergebnis.
- **Wann war das?:** Das vorhandene Stufenschema und die konfigurierte Dauer
  werden eingefroren. Der Server berechnet die Toleranz aus dem logischen
  Fortschritt und speichert sie im Versuch. Gespeicherte Floatwerte werden über
  ihre vorhandene Zeichenrepräsentation in Decimal übernommen; verlorene
  ursprüngliche Eingabegenauigkeit wird nicht erfunden. Die Hostansicht zeigt
  Lösung und zulässigen Bereich anhand dieser festen Toleranz. Richtig gibt dem
  Buzzenden einen Punkt, falsch dem Gegner. Die Bewertung endet die Frage.

Phasen: `prepared → answering → awaiting_decision → show_result → completed`.
Clue Rush darf nach einer falschen Bewertung nach `answering` zurückkehren,
ohne bisherigen Fortschritt zu verlieren. Who is lying wechselt beim Buzz direkt
nach `show_result`. Ein Fristablauf wechselt nach `awaiting_result`; GETs schreiben
nicht und lösen weder Freigaben noch Wertungen aus. Der bestehende lokale
`duel_deadlines`-Worker verarbeitet auch diese Fristen und rekonstruiert sie nach
Prozessneustart. Eine pausierte Bewertungsphase besitzt keine laufende Deadline.

## Transport und Sichtbarkeit

Die vorhandene JSON-HTTP-Anbindung wird für die vier Spieltypen erweitert.
Hostberechtigung und signierte Teilnehmeridentität stammen aus denselben
geprüften Hub-/Duellpfaden wie Quick Quiz. Der Client sendet keinen aktiven Namen,
keine Toleranz, Zeit oder Akteuridentität als Berechtigungs- oder Bewertungsquelle.
Teilnehmer und Spectator sehen nur erreichte Hinweise/den aktuellen Namen,
niemals kommende Hinweise oder Wahrheitsflags vor der Auflösung. Zielwert,
zulässiger Bereich und Bewertungsreferenz sind vor dem Ergebnis nur dem
berechtigten Host zugänglich. Die gemeinsame HTTP-Clientlogik übernimmt bereits
gefilterte POST-Projektionen und ignoriert ältere Snapshots. Reload rekonstruiert
die gespeicherte Phase, Versuchssperren und Fristen.

Best of N, Zielpunktzahl, Fortschrittsleisten und Matchabschluss verwenden die
gemeinsame Matchverwaltung. Best of Seven braucht vier Rundensiege. Spielpunkte,
Rundensiege und Session-Gesamtwertung werden nicht vermischt. Auch nach einem
gespeicherten Matchsieg bleibt ausschließlich der Host-Präsentationsabschluss
möglich; danach erscheint die manuelle Lobby-Rückkehr.

## Noch offene Regeln

Bestätigt ist: Nach Fristablauf ohne erfolgreichen Buzz erhalten in allen vier
Varianten beide Parteien null Punkte und keinen Rundensieg. Auch ein falscher
Clue-Rush-Versuch mit anschließendem Fristablauf ergibt keine Gegnerpunkte.
Die Freigabe und Buchung dieses Ergebnisses erfolgt erst durch den Host.

Die gemeinsame Engine kennzeichnet weiterhin ungeklärte siegerlose Runden im
Best-of-N-Limit sowie das gleichzeitige Erreichen der Zielpunktzahl. Sie erfindet
weder Tiebreaker noch Gewinner. Nachträgliche Korrektur und Wiedereröffnung
abgeschlossener Ergebnisse sind keine neuen Funktionen dieses Abschnitts.

## Verifikation

Die vier Chromium-Fälle verwenden jeweils getrennte Kontexte für Host, beide
Parteien und Spectator. Sie durchlaufen die Sessionplanung, Best of Seven mit
vier Rundensiegen, Lobby-Rückkehr, einen neuen Zielpunktzahl-Slot sowie dessen
Fristablauf ohne Buzz (lokaler Worker, null Punkte) und anschließenden Matchsieg.
Geprüft werden außerdem konkurrierende Buzzes, Reload, beide Hostbewertungen,
der zweite Clue-Rush-Versuch nach langer Bewertung, Namenswechsel und die
eingefrorene Toleranz. Konkurrenztests verwenden getrennte Dateidatenbank-
Verbindungen; Migrationen werden nur in isolierten Testdatenbanken geprüft.

Der breite Lauf führte 1262 Tests aus: 1259 bestanden zunächst. Zwei veraltete
Assertions (Migrationsziel und Who-Lobby-Sonderzweig) sowie das Who-Browserfixture
für einen historischen Sonderzeichennamen wurden korrigiert, ohne den
Multiplayer-Spielcode zu ändern. Der anschließende gezielte Lauf bestand mit
53/53 Tests, einschließlich aller vier erweiterten Duell-Browserfälle und der
drei zuvor fehlgeschlagenen Tests. Der gesamte breite Lauf wurde danach nicht
erneut ausgeführt. Systemcheck, Migrationsdriftprüfung und Diffprüfung bestehen.

Im erfolgreichen Nachlauf protokollierte ein noch laufender alter Who-WebSocket
nach `test_recall_happens_once_before_retrying_start` einmal
`HubSession.DoesNotExist` beim Testdatenabbau. Dies ist kein fehlgeschlagener
Test und kein Duell-Schreibpfad; der gemeinsame Multiplayer-State wurde dafür
nicht verändert. Die Logmeldung bleibt als gesonderter Diagnosepunkt erhalten.
