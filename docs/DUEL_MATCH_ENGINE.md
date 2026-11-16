# Interne Duell-Matchverwaltung

Stand: 12.11.2026. Die Matchverwaltung ist implementiert und intern getestet,
aber weiterhin ohne HTTP-, WebSocket-, Dashboard- oder Spieloberflächen-Anbindung.
Die vollständige Regressionstest-Abnahme ist durch einen nachgewiesen
vorbestehenden Buzzer-Test-/Template-Abgleich blockiert (siehe Abnahme).

## Module und Datenquellen

| Baustein | Verantwortung |
|---|---|
| `duel/matches.py` | Frameworkfreie Konfiguration und Matchauswertung |
| `duel/models.py::DuelUnitOutcome` | Genau ein endgültiges Ergebnis pro konkreter Unit |
| `duel/models.py::DuelMatchOutcome` | Genau ein nachvollziehbarer Matchabschluss pro Instanz |
| `duel/match_service.py` | Interne Befehle, Ergebnisabfragen und aggregierte Snapshots |
| `duel/commands.py::execute_action()` | Bestehender gemeinsamer Transaktions-, Receipt- und Revisionspfad |
| `DuelScoreEntry` | Einzige Quelle der Spielpunkte; keine neue Gesamtpunktespalte |

`DuelGameInstance` bleibt die führende Instanz. Der bestehende `MatchStatus`
bleibt unverändert: `not_started`, `in_progress`, `completed`, `aborted`.
Es gibt keine zweite Matchlaufzeit, Teilnehmerverwaltung oder Punktequelle.
Ein Neustart benötigt eine neue Instanz; Ergebnisse anderer Instanzen werden
weder anhand von Session, Spieltyp noch anhand einer Fragennummer übernommen.

## Explizite Konfiguration und reine Regeln

Die reservierte Struktur in `DuelGameInstance.rule_config` lautet beispielsweise:

```json
{"match": {"mode": "best_of_n", "best_of": 7}}
```

oder:

```json
{"match": {"mode": "target_score", "target_score": 10}}
```

Andere spielbezogene Schlüssel bleiben erhalten. Fehlende, widersprüchliche
oder unbekannte Konfigurationen erhalten keinen technischen Default; Abfragen
melden `invalid_configuration`. `start_match()` übernimmt einen validierten
`MatchConfig` und setzt eine noch nicht gestartete Instanz auf `in_progress`.
Es erstellt oder aktiviert keine Unit. Bereits vorhandene Punkte-/Ergebnishistorie
verhindert einen solchen Neustart innerhalb derselben Instanz.

Best of N verlangt eine positive ungerade Ganzzahl, einschließlich N=1.
Die Siegschwelle ist `(N + 1) // 2`: 2, 3 beziehungsweise 4 Siege bei N=3, 5, 7.
Ausschließlich ausdrückliche Rundensiege zählen. Spielpunkte beeinflussen
diese Siegzahl nicht. Eindeutiges Erreichen der Schwelle beendet das Match;
weitere gewöhnliche Aktionen werden abgelehnt.

TARGET_SCORE verlangt eine positive Ganzzahl. Negative Punktestände, Nullbuchungen
und Werte über dem Ziel bleiben unverändert erhalten. Erreicht nur eine Partei
das Ziel, ist der Sieger eindeutig. Erreichen beide Parteien das Ziel, lautet
die Auswertung `sudden_death`, auch bei beispielsweise 11:12 für Ziel 10.
Die auslösende Unit bleibt dauerhaft gespeichert. Erst ein alleiniger Rundensieg
in einer nachfolgenden Stichrunde entscheidet; Reihenfolge und höhere Summe
entscheiden den gleichzeitigen Matchball nicht.

Die Auswertung kennt die Zustände `running`, `party_a_wins`, `party_b_wins`,
`sudden_death`, `draw`, `needs_resolution` und `invalid_configuration`. Diese sind **keine zusätzlichen
persistenten Matchstatuswerte**. Ein ungeklärtes Match bleibt ohne Abschlussrecord
und ohne gespeicherten Sieger. Insbesondere wird daraus nicht `completed`.

## Rundenergebnisse und Abschlussprotokoll

`DuelUnitOutcome` enthält UUID, eine eindeutige Unitreferenz, das vorhandene
`RoundOutcome`, einen optionalen Gewinner und den Abschlusszeitpunkt. Zulässige
endgültige Werte sind `party_a_wins`, `party_b_wins`, `draw`, `no_winner`.
`undecided` ist kein finales Datenbankergebnis: Die Unit bleibt offen und erhält
keinen Outcome-Record. Derselbe Frageinhalt kann in verschiedenen Units beliebig
wiederkehren; es gibt keine Eindeutigkeit auf Frage, Material oder Setnummer.

Die Datenbank sichert die Eindeutigkeit pro Unit und Gewinner-/NULL-Konsistenz.
`full_clean()` prüft zusätzlich, dass der Gewinner zur richtigen Seite dieser
Instanz gehört. Der Service schreibt Unitstatus, Zeitstempel und Ergebnis in
derselben Transaktion. Eine bloße Modellspeicherung ändert keine Punkte,
Revisionen oder Elternstatus. `save()` ruft `full_clean()` nicht automatisch auf.

`DuelMatchOutcome` enthält UUID, eine eindeutige Instanzreferenz, Gewinner,
Abschlussmodus, den verwendeten Grenzwert und den Abschlusszeitpunkt. Der
Grenzwert ist N bei Best of N oder die Zielpunktzahl. Er friert nur die tatsächlich
verwendete Abschlussregel ein, nicht Punktesummen oder Rundensiegzahlen.
Gewinner und Status `completed` werden durch den Service gemeinsam gespeichert.

Die Referenz- und Wert-Constraints gelten datenbankseitig; Parteizugehörigkeit
und die ungerade Best-of-Konfiguration benötigen Modell-/Servicevalidierung.
Ein fremder Gewinner ist im autorisierten Schreibpfad unzulässig.

Normale Befehle überschreiben keine abgeschlossenen Units oder Matchabschlüsse.
Korrekturen und Wiedereröffnungen sind nicht implementiert. Beliebige direkte
ORM-/SQL-Schreibzugriffe sind kein autorisierter Servicepfad; es wurden keine
globalen `save()`-Overrides, Löschsignale oder Datenbanktrigger eingeführt.

## Transaktionale Integration

Die internen Befehle `start_match()`, `complete_unit()` und `finalize_match()`
rufen ausschließlich den bestehenden `execute_action()` auf. Sie erhalten einen
intern bereitgestellten, bestätigten `ActorContext`. Eine neue Hostautorisierung
oder eine öffentlich nutzbare Hostschnittstelle wurde nicht geschaffen.
Das Rundenergebnis ist eine vertrauenswürdige Serverentscheidung, kein frei
übernommener Teilnehmer-Payload.

Der vorhandene Receipt identifiziert den logischen Versuch über Instanz,
Akteurtyp, Akteurkennung und `client_action_id`. Identische Retries geben vor
erneuten Phasenprüfungen das ursprüngliche Ergebnis und dessen Revision zurück.
Widersprüchliche Wiederverwendung erzeugt `ActionConflict`. Eine andere Action-ID
für einen bereits erledigten Abschluss erzeugt eine kontrollierte Ablehnung,
keinen zweiten Rundensieg, Gewinner oder Revisionsschritt.

Eine neue angewendete Aktion erhöht die vorhandene Instanzrevision genau einmal.
Identische Retries, Konflikte, zurückgerollte Aktionen und kontrollierte Ablehnungen
erhöhen sie nicht. Die erste Partei erzwingt keine pauschale Ablehnung einer
unabhängig zulässigen zweiten Partei wegen der globalen Revision.

Zwei kleine Ergänzungen in `commands.py` gelten ausschließlich für Duellaktionen:

- Nach Receiptprüfung werden neue Aktionen für `completed`/`aborted` ohne
  Handlerausführung abgelehnt.
- Die Matchkonfiguration einer bereits gestarteten Instanz darf durch gewöhnliche
  Handler nicht verändert werden. Andere Spielkonfiguration bleibt davon getrennt.

SQLite verwendet weiterhin die vorhandene `IMMEDIATE`-Konfiguration und den
vorhandenen Retry-Helfer. Es gibt keine neue Sperrenarchitektur oder globale
Einstellungsänderung. Die vorhandene Instanzsperre serialisiert alle
zusammengehörigen Änderungen. Bei technischer Exception oder Datenbankfehler
werden sämtliche Änderungen zurückgerollt; kontrollierte Ablehnungen nutzen den
vorhandenen Savepoint und können einen `REJECTED`-Receipt hinterlassen.

### Verwendung durch spätere Spielcontroller

Entweder einen eigenständigen Matchbefehl von außerhalb einer Transaktion
aufrufen oder innerhalb eines vorhandenen Command-Handlers die Kompositionspunkte
verwenden:

1. Berechtigung, konkrete Unit, aktiven Antwortversuch, Spielphase und Frist prüfen.
2. Spieltypspezifische Wertung berechnen.
3. **Alle** autorisierten `DuelScoreEntry`-Buchungen dieser gemeinsamen Auswertung
   für beide Parteien innerhalb derselben Transaktion schreiben und validieren.
4. `complete_unit_in_action(context, trusted_round_result)` verwenden, wenn eine
   Unit abgeschlossen wird. Diese Funktion wertet anschließend das Match aus.
5. Andernfalls nach dem vollständigen Batch `reconcile_match_in_action(context)`
   verwenden. Nicht zwischen zwei Buchungen derselben Auswertung entscheiden.
6. Ein strukturiertes `HandlerResult` zurückgeben. Receipt und Revision übernimmt
   ausschließlich `execute_action()`.
7. Erst nach Commit benachrichtigen; externe Seiteneffekte gehören nicht in einen
   möglicherweise wiederholten Transaktionshandler.

Innerhalb eines Handlers nicht erneut einen äußeren Matchbefehl oder
`execute_action()` aufrufen. Die Kompositionspunkte eröffnen keine konkurrierende
Transaktion und erhöhen keine Revision selbst. `finalize_match()` leitet den
Gewinner aus den gespeicherten Daten ab; `expected_winner_id` ist lediglich eine
optionale Vorbedingung und kann keinen fremden oder ungeklärten Sieger auswählen.

## Snapshots und Ergebnisabfragen

`get_match_snapshot(UUID)` liest innerhalb einer konsistenten Transaktion unter
der gleichen Instanzsperre. Eine vorhandene Command-Transaktion wird wiederverwendet.
Enthalten sind ausschließlich:

- Instanz-ID, Matchstatus und State-Revision;
- Abschlussmodus, N/Ziel und benötigte Rundensiege;
- IDs der beiden Parteien mit tatsächlichen Spielpunkten und Rundensiegen;
- Anzahl verbindlich abgeschlossener Units;
- gespeicherter Gewinner und Abschlusszeitpunkt, sofern vorhanden;
- Auswertungszustand und gegebenenfalls ungeklärter Grund.

Scores werden aus `DuelScoreEntry` summiert, Siege aus `DuelUnitOutcome` gezählt.
Negative oder über dem Ziel liegende Werte werden nicht für eine spätere
Fortschrittsleiste abgeschnitten. `winner_id` bezeichnet nur einen dauerhaft
gespeicherten Matchsieg, nicht eine noch unverbuchte Auswertungskandidatur.
`get_match_result()` liefert nur bei konsistentem Abschluss ein Ergebnis,
ansonsten `None`. `evaluate_current_match()` liefert den typisierten Auswertungsvertrag.

Die Projektion enthält keine Quizlösungen, verdeckten Eingaben, Nicknames,
vollständigen Spielkonfigurationen, Hosttokens oder Receipt-Inhalte. Zukünftige
Transportadapter müssen den Zugriff weiterhin autorisieren. Widersprüchliche
gespeicherte Historie erzeugt `MatchIntegrityError`, beispielsweise bei einem
Abschluss ohne Gewinnerrecord oder einer abgeschlossenen Unit ohne Ergebnis.
Es erfolgt keine automatische Reparatur, Siegerwahl oder Wiedereröffnung.

## Bestätigte Ergänzungen und verbleibende Regeln

- autorisierte Korrektur abgeschlossener Ergebnisse und deren Historie;
- Übernahme von Duellsiegen in die Session-Gesamtwertung.

Remis und `no_winner` verbrauchen keine Best-of-N-Entscheidungsplätze.
Die bestätigten Nichtabgabe-/Gleichstandsregeln, die Stichrunde und der explizite
Hostabschluss bei erschöpftem Material stehen in [DUEL_SCORING_RULES.md](DUEL_SCORING_RULES.md).
Es wird keine weitere Unit automatisch erstellt oder aktiviert. Ein unabhängig
eindeutig erreichter Siegschwellenwert kann das Match abschließen.
Spielcontroller entscheiden weiterhin über aktive Unit, Material, Reveal und
Präsentation. Für Turnierorganisation oder neue Sessionzugänge wird nichts freigeschaltet.

## Migrationen und Abnahme

- `0007_duelunitoutcome`: ausschließlich Rundenergebnistabelle und Constraints.
- `0008_duelmatchoutcome`: ausschließlich Matchabschlusstabelle und Constraints.
- Beide Migrationen einschließlich Rücknahme wurden nur auf isolierter SQLite-Testdatenbank geprüft.
- Die gesamte `duel`-App bleibt über ihren bestehenden AppConfig-Ausschluss von
  Supabase-Sync und Restore ausgenommen. Keine echte Supabase-Verbindung wurde verwendet.
- Alte Modellklassen und alle Multiplayer-/Hub-/UI-Dateien sind gegenüber dem
  Auftragsbeginn unverändert; Änderungen am bestehenden Schreibpfad betreffen nur `duel/commands.py`.

Phasenprüfungen: Ausgangssuite 247 erfolgreich; reine Regeln/Verträge 45;
Rundenergebnispersistenz 11; beide Ergebnismodelle/Migrationen/Ausschluss 27;
Service 21 nach Korrektur von drei neuen Test-Payloads (Listen statt Tupel);
Snapshots 12; Konkurrenz 13, einschließlich getrennter Verbindungen und der
bestehenden Tests mit zwei Python-Prozessen. Die anschließende vollständige
Duell-Suite enthält **326 erfolgreiche Tests** einschließlich zusätzlicher Rollbacktests.

Breite Regression: **579 von 580 Tests erfolgreich**, inklusive Hub, Quick Quiz,
Estimation, Buzzer, Spectator und Sessionverwaltung. Offen ist ausschließlich:

`buzzer.tests.BuzzerEndFlowTests.test_participant_return_marks_inactive_and_lobby_suppresses_auto_redirect`

Der Test erwartet wörtlich `if (returnedToLobby)` im Lobby-HTML. Das bereits vor
diesem Auftrag vorhandene Template verwendet `sameReturnedGame`. Der Fehler wurde
mit einer isolierten Kopie des ursprünglichen Duellpakets einschließlich
bytegenau hashverifizierter ursprünglicher `models.py` und `commands.py` reproduziert:
derselbe einzelne Test scheitert identisch. Weder der bestehende Test noch das
Multiplayer-Template wurden angepasst oder aus der Regression entfernt.

**Blockade:** Eine vollständig grüne Multiplayer-Gesamtabnahme benötigt einen
gesondert autorisierten Abgleich dieses vorhandenen Tests mit der vorhandenen
Lobbylogik. Die technische Matchimplementierung und ihre Duelltests sind fertig;
es wird keine vollständige Regressionstest-Freigabe behauptet. Weitere
Spielfunktionen oder Integrationen wurden nach diesem Befund nicht begonnen.

Nach Klärung der Gesamtabnahme ist der kleinste sinnvolle nächste Baustein ein
isolierter Commit-Service für `DuelSubmission`: verbindliche Abgabe, serverseitige
Annahmesequenz und unveränderliche Retries innerhalb von `execute_action()`, noch
ohne Endpunkte oder Spielpunkte. Darauf kann der erste Estimation-Controller aufbauen.
