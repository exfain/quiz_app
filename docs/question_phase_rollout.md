# Rolloutplan: gemeinsame Drei-Phasen-Fragenstruktur

## Zweck und Verbindlichkeit

Dieses Dokument ordnet alle Teilnehmer-Spieltypen der bestehenden serverautoritativen Fragenarchitektur zu. Es beschreibt noch keine produktive Umstellung. Quick Quiz ist die technische Referenz, aber weder visuelle Vorlage noch Quelle fuer spieluebergreifende UI-Annahmen.

Die verbindlichen gemeinsamen Phasen sind:

1. `prompt_visible`: Die Frage oder Aufgabenstellung wird nach dem serverzeitsynchronisierten Praesentationsdelay sichtbar. Inhalte, Eingaben und Timer bleiben inaktiv.
2. `content_visible`: Die zur Aufgabe gehoerenden Inhalte werden sichtbar. Teilnehmeraktionen und Timer bleiben gesperrt.
3. `answering_open`: Alle erforderlichen Inhalte sind sichtbar. Erst jetzt werden Teilnehmeraktionen angenommen und eine vorhandene Deadline gestartet.

Fuer alle Umstellungen gelten zusaetzlich:

- `QUESTION_PRESENTATION_DELAY_MS = 1000` und `question_visible_at = question_presented_at + 1000 ms` bleiben die gemeinsame Zeitbasis.
- `state_revision`, Spielinstanz, Frage-, Runden- und Setkontext sowie Action-ID-Schutz muessen vor jeder Zustandsaenderung validiert werden.
- Ein Domainzustand wie Set, Runde, Hinweisfolge, Kartenphase oder Aufloesung bleibt getrennt von `question_phase` erhalten.
- In `prompt_visible` und `content_visible` werden alle als Antwort geltenden Teilnehmeraktionen serverseitig abgelehnt, einschliesslich Pending-Updates.
- Eine Domain-Deadline darf im manuellen Modus erst atomar mit `open_answering` entstehen. Bestehende Domainzeitfelder duerfen nicht als zweite, abweichende Zeitquelle weiterlaufen.
- Teilnehmer- und Spectator-Snapshots muessen Inhalte phasengerecht ausliefern. Die Hostansicht darf die zum Vorlesen erforderlichen Inhalte weiterhin sehen.
- Reload und Reconnect rekonstruieren den Zustand ausschliesslich aus absoluten Serverzeitpunkten und der neuesten Revision.
- Jede spaetere Umsetzung ist ein eigener Patch pro Spieltyp. Die Gruppen unten bestimmen die Reihenfolge, nicht den Umfang eines einzelnen Patches.

## Technische Referenz: Quick Quiz

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | QUICK QUIZ |
| technischer Spieltyp | `quiz` |
| Frage/Aufgabe | Fragetext nach dem Praesentationsdelay |
| Phase-2-Inhalt | Antwortoptionen in Snapshotreihenfolge, mit 300 ms Staffelung |
| Phase-3-Interaktion | Genau eine Antwortoption auswaehlen und absenden |
| Hostbutton Phase 1 | `FRAGE SENDEN` |
| Hostbutton Phase 2 | `ANTWORTEN ANZEIGEN` |
| Hostbutton Phase 3 | `FRAGE FREIGEBEN` |
| Timer vorhanden | ja |
| Deadline entsteht | atomar bei `open_answering` |
| Teilnehmeraktion | bestehende Quick-Quiz-Antwortaktion |
| bestehende Sonderphasen | Aufloesung und Punkteauswertung bleiben getrennt |
| vorgesehene Revealanimation | Antworten einzeln, 300 ms pro Eintrag |
| Reload/Reconnect | Promptdelay, Antwortstaffelung und Restzeit aus absoluten Serverzeitpunkten rekonstruieren |
| Umsetzungsgruppe | Pilotreferenz; bereits umgesetzt |

Quick Quiz legt die Regeln fuer Persistenz, Idempotenz, Deadline und Revisionsschutz fest. Seine Antwortkarten, Animation und Hostoberflaeche werden nicht auf andere Spiele kopiert.

## Ist-Zustand der noch nicht umgestellten Spiele

Die registrierten Werte in `HubGameStep.GAME_CHOICES` wurden mit den jeweiligen Consumer-, Model-, Snapshot- und Teilnehmerpfaden abgeglichen. Neben Quick Quiz existieren genau die folgenden zwoelf Spieltypen:

| Spieltyp | aktueller Hostpfad | heutiger Sofortstart | zu bindender Kontext |
| --- | --- | --- | --- |
| `estimation` | `admin_send_question` | setzt Frage und `question_end_time` sofort | Frage |
| `blackjack` | `admin_send_question` | setzt Frage und `question_end_time` sofort | Spielinstanz, Set, Frage |
| `who_that` | `admin_send_question` | setzt Bildfrage und `question_end_time` sofort | Frage/Bild |
| `where` | `admin_send_question` | setzt Frage und `question_end_time` sofort | Frage |
| `assign` | `admin_send_question`, danach `admin_next_round` | startet Set beziehungsweise Folgerunde samt Rundende sofort | Set und interne Runde |
| `sorting_ladder` | `admin_send_question`, danach `admin_start_round` | startet Thema beziehungsweise Folgerunde samt Rundende sofort | Frage/Topic und interne Runde |
| `wann_war_das` | `admin_start_question` | setzt `question_started_at`; Timer und Wertung laufen sofort | Frage |
| `clue_rush` | `admin_send_question` | erzeugt sofort den gesamten Hinweiszeitplan und die Antwortdeadline | Frage und Hinweisplan |
| `who` | `admin_send_question` | startet sofort Personenzeitachse und Gesamtdeadline | Set/Frage und Personenfolge |
| `wer_weiss_mehr` | `admin_start_set`, danach `admin_next_round` | startet die jeweilige Setrunde samt Rundende sofort | Set und interne Runde |
| `buzzer` | `admin_start_round`, danach `admin_open_buzzer` | besitzt bereits getrennte Bereit-/Offen-Zustaende, aber keine Frage | Runde |
| `host_points` | Spielstart, `admin_adjust_score`, `admin_next_round` | keine Frage, Antwort oder Deadline | Runde/Score |

Die aktuellen Spectator-Serializer geben ausser bei Quick Quiz die fachlichen Frageninhalte noch ohne Drei-Phasen-Filterung aus. Bei jeder spaeteren Umstellung muss deshalb derselbe autoritative Phasenstatus fuer Teilnehmer und Spectator verwendet werden, ohne das Spectatorlayout zu veraendern.

## Standardspiele

Standardspiele besitzen pro Frage genau ein Eingabefeld oder eine gleichartige Antwortaktion und eine einzelne Antwortdeadline.

### Estimation

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | ESTIMATION |
| technischer Spieltyp | `estimation` |
| Frage/Aufgabe | Schaetzfrage (`question_text`) |
| Phase-2-Inhalt | Einheit, Hinweis und numerisches Eingabefeld sichtbar, aber gesperrt |
| Phase-3-Interaktion | Schaetzwert bearbeiten und absenden |
| Hostbutton Phase 1 | `FRAGE SENDEN` |
| Hostbutton Phase 2 | `SCHÄTZFELD ANZEIGEN` |
| Hostbutton Phase 3 | `FRAGE FREIGEBEN` |
| Timer vorhanden | ja |
| Deadline entsteht | bei `open_answering` aus der bestehenden effektiven Antwortdauer; nicht mehr in `EstimationSession.send_question` |
| Teilnehmeraktion | `participant_update_pending_answer`, `participant_submit_answer` |
| bestehende Sonderphasen | Toleranzzonen, Rangfolge, Korrektur und Aufloesung bleiben eigenstaendig |
| vorgesehene Revealanimation | Einheit, Hinweis und Eingabebereich gruppiert mit kurzer Einblendung |
| Reload/Reconnect | Phase, Pending-Wert und Restzeit rekonstruieren; Pending-Updates erst in `answering_open` zulassen |
| Umsetzungsgruppe | Standard |

### Black Jack Quiz

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | BLACK JACK |
| technischer Spieltyp | `blackjack` |
| Frage/Aufgabe | numerische Quizfrage |
| Phase-2-Inhalt | bestehendes Zahlen-Eingabefeld und die dauerhaft sichtbare Set-/Punktinformation, Eingabe gesperrt |
| Phase-3-Interaktion | numerische Antwort absenden |
| Hostbutton Phase 1 | `FRAGE SENDEN` |
| Hostbutton Phase 2 | `ANTWORTFELD ANZEIGEN` |
| Hostbutton Phase 3 | `FRAGE FREIGEBEN` |
| Timer vorhanden | ja |
| Deadline entsteht | bei `open_answering` aus dem bestehenden Fragenzeitlimit; nicht mehr in `BlackJackSession.send_question` |
| Teilnehmeraktion | `participant_submit_answer` einschliesslich bestehender Timeoutbehandlung |
| bestehende Sonderphasen | Sets, Karten-/Sternwertung, Bust, `question_ending`, Setzusammenfassung und Spielende bleiben eigenstaendig |
| vorgesehene Revealanimation | Eingabebereich gruppiert; keine Kartenanimation durch die Fragenphasen |
| Reload/Reconnect | aktuelle Set- und Fragenidentitaet, Phase, eigene Antwort und Restzeit rekonstruieren |
| Umsetzungsgruppe | Standard |

### Who Is That

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | WHO IS THAT |
| technischer Spieltyp | `who_that` |
| Frage/Aufgabe | Fragetext beziehungsweise Aufforderung zur Benennung der Person |
| Phase-2-Inhalt | Bild, Kategorie, Hinweis und Texteingabe sichtbar, aber gesperrt |
| Phase-3-Interaktion | Personennamen eingeben und absenden |
| Hostbutton Phase 1 | `FRAGE SENDEN` |
| Hostbutton Phase 2 | `BILD ANZEIGEN` |
| Hostbutton Phase 3 | `FRAGE FREIGEBEN` |
| Timer vorhanden | ja |
| Deadline entsteht | bei `open_answering` aus dem vorhandenen Fragenzeitlimit; nicht mehr in `WhoIsThatSession.send_question` |
| Teilnehmeraktion | `participant_update_pending_answer`, `participant_submit_answer` |
| bestehende Sonderphasen | Bildreihenfolge, Aufloesung, Namensvalidierung und Scorebox bleiben eigenstaendig |
| vorgesehene Revealanimation | Bild und Metadaten gruppiert mit kurzer Einblendung |
| Reload/Reconnect | Phase, Bildinhalt, Pending-Text, eigener Antwortstatus und Restzeit rekonstruieren |
| Umsetzungsgruppe | Standard |

## Interaktive Spiele

Interaktive Spiele besitzen eine zustandsbehaftete Eingabeflaeche. Phase 2 zeigt die vollstaendige Flaeche, sperrt aber Pointer-, Tastatur- und Serveraktionen.

### Where Is This

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | WHERE IS THIS |
| technischer Spieltyp | `where` |
| Frage/Aufgabe | Ortsfrage (`question_text`) |
| Phase-2-Inhalt | Bild, Hinweis, Karte, Markerflaeche sowie Loeschen-/Senden-Steuerung sichtbar, aber gesperrt |
| Phase-3-Interaktion | Position auf der Karte setzen, aendern, loeschen und absenden |
| Hostbutton Phase 1 | `FRAGE SENDEN` |
| Hostbutton Phase 2 | `KARTE ANZEIGEN` |
| Hostbutton Phase 3 | `FRAGE FREIGEBEN` |
| Timer vorhanden | ja |
| Deadline entsteht | bei `open_answering` aus `question.time_limit`; nicht mehr in `WhereSession.send_question` |
| Teilnehmeraktion | `participant_submit_answer` mit den vorhandenen Kartenkoordinaten; lokale Markerauswahl ebenfalls erst in Phase 3 |
| bestehende Sonderphasen | Kartentypen, Tutorial, korrekter Ort, Distanzwertung und Aufloesung bleiben eigenstaendig |
| vorgesehene Revealanimation | Bild/Hinweis und Kartenflaeche gruppiert; keine Staffelung einzelner Kartenelemente |
| Reload/Reconnect | Kartenart, Frage, Phase, gespeicherte Auswahl und Restzeit aus dem neuesten Snapshot rekonstruieren |
| Umsetzungsgruppe | Interaktiv |

### Assign

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | ASSIGN |
| technischer Spieltyp | `assign` |
| Frage/Aufgabe | Aufgabenstellung und aktuelles linkes Zuordnungselement der jeweiligen Runde |
| Phase-2-Inhalt | rechte Zuordnungselemente, Dropziele und bereits geloeste Zuordnungen sichtbar, aktuelle Eingabeflaeche gesperrt |
| Phase-3-Interaktion | Drag-and-drop beziehungsweise Auswahl fuer die aktuelle Runde; Rundenabgabe |
| Hostbutton Phase 1 | erste Runde `AUFGABE SENDEN`, Folgerunden `NÄCHSTE RUNDE SENDEN` |
| Hostbutton Phase 2 | `ZUORDNUNG ANZEIGEN` |
| Hostbutton Phase 3 | `RUNDE FREIGEBEN` |
| Timer vorhanden | ja, pro interner Runde |
| Deadline entsteht | bei `open_answering` der jeweiligen Runde; `begin_set_db`, `start_set_runtime` und `admin_next_round` duerfen die neue Runde nur vorbereiten |
| Teilnehmeraktion | `participant_update_selection`, `participant_log_round`; Legacywege `participant_check_round` und `participant_submit_answer` ebenfalls sperren |
| bestehende Sonderphasen | `AssignSetRuntime` mit Setnummer, interner Runde, Reveal/Warten, geloesten Paaren und Teilnehmerausscheiden bleibt eigenstaendig |
| vorgesehene Revealanimation | Zuordnungsflaeche gruppiert; keine Einzelanimation jedes Dropziels |
| Reload/Reconnect | Kontext immer mit Spielinstanz, Set-ID und Runden-ID rekonstruieren; Dragstatus nur fuer die aktuelle Runde wiederherstellen |
| Umsetzungsgruppe | Interaktiv; erfordert rundenbezogene Kontextbindung |

### Sorting Ladder

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | SORTING LADDER |
| technischer Spieltyp | `sorting_ladder` |
| Frage/Aufgabe | Thema/Aufgabenstellung und das in der aktuellen Runde einzuordnende Element |
| Phase-2-Inhalt | bestehende Leiter, Labels, bereits platzierte Elemente und Dropbereiche sichtbar, aber gesperrt |
| Phase-3-Interaktion | Element positionieren und Runde absenden |
| Hostbutton Phase 1 | erste Runde `AUFGABE SENDEN`, Folgerunden `NÄCHSTE RUNDE SENDEN` |
| Hostbutton Phase 2 | `LEITER ANZEIGEN` |
| Hostbutton Phase 3 | `RUNDE FREIGEBEN` |
| Timer vorhanden | ja, pro interner Runde |
| Deadline entsteht | bei `open_answering` der jeweiligen Runde; `admin_send_question` und `admin_start_round` duerfen die neue Runde nur vorbereiten |
| Teilnehmeraktion | `participant_update_selection`, `participant_submit_round`; Legacyweg `participant_submit_move` ebenfalls sperren |
| bestehende Sonderphasen | Leiterzustand, `active`/`awaiting`/`revealed`, Loesung, Ausscheiden und Spielende bleiben eigenstaendig |
| vorgesehene Revealanimation | Leiter und Dropbereiche gruppiert; keine Animation bereits platzierter Elemente |
| Reload/Reconnect | Frage/Topic, Runden-ID, Leiterzustand, Phase, Auswahl und Restzeit gemeinsam rekonstruieren |
| Umsetzungsgruppe | Interaktiv; erfordert rundenbezogene Kontextbindung |

### Wann War Das

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | WANN WAR DAS |
| technischer Spieltyp | `wann_war_das` |
| Frage/Aufgabe | Ereignis beziehungsweise Jahresfrage |
| Phase-2-Inhalt | Toleranz-/Punkteskala, aktueller Maximalwert und Jahreseingabe sichtbar, aber eingefroren und gesperrt |
| Phase-3-Interaktion | Jahreszahl eingeben und absenden; Punkt-/Toleranzverlauf startet |
| Hostbutton Phase 1 | `FRAGE SENDEN` |
| Hostbutton Phase 2 | `ANTWORTBEREICH ANZEIGEN` |
| Hostbutton Phase 3 | `FRAGE FREIGEBEN` |
| Timer vorhanden | ja; Countdown und dynamische Punkt-/Toleranzberechnung |
| Deadline entsteht | bei `open_answering` aus dem effektiven Zeitlimit |
| Teilnehmeraktion | `participant_submit_answer` |
| bestehende Sonderphasen | `question_state`, Reveal, Toleranzwertung und Punktabfall bleiben eigenstaendig |
| vorgesehene Revealanimation | Skala und Eingabebereich gemeinsam einblenden |
| Reload/Reconnect | dynamische Punkte und Toleranz ausschliesslich aus `answering_started_at`/Deadline rekonstruieren; `question_started_at` darf nicht parallel ab Praesentation weiterlaufen |
| Umsetzungsgruppe | Interaktiv |

## Sonderfaelle

### Clue Rush

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | CLUE RUSH |
| technischer Spieltyp | `clue_rush` |
| Frage/Aufgabe | Fragetext ohne Hinweise |
| Phase-2-Inhalt | Hinweisbereich und Antwortfeld sichtbar, aber gesperrt; noch kein Hinweis freigegeben |
| Phase-3-Interaktion | Antwortfeld aktiv; gleichzeitig startet die vorhandene autoritative Hinweisfolge mit Hinweis 1 |
| Hostbutton Phase 1 | `FRAGE SENDEN` |
| Hostbutton Phase 2 | `ANTWORTFELD ANZEIGEN` |
| Hostbutton Phase 3 | `HINWEISE STARTEN` |
| Timer vorhanden | ja; serverseitiger Hinweiszeitplan mit gemeinsamer Antwortdeadline |
| Deadline entsteht | bei `open_answering` als Ende des erst dann erzeugten Hinweiszeitplans |
| Teilnehmeraktion | `participant_submit_answer` |
| bestehende Sonderphasen | automatische Hinweisfreigaben, `admin_send_clue`-Reconciliation, Punkte nach Hinweiszahl und Aufloesung bleiben eigenstaendig |
| vorgesehene Revealanimation | Phase-2-Huelle gruppiert; Hinweise danach einzeln gemaess bestehendem autoritativem Zeitplan |
| Reload/Reconnect | Phase, absolute Hinweiszeitpunkte, freigegebene Hinweise und Antwortdeadline rekonstruieren; keinen lokalen Hinweisplan starten |
| Umsetzungsgruppe | Sonderfall: Zeitplan wird erst in Phase 3 angelegt |

Der bestehende Code erzeugt in `start_question_schedule` bereits beim Senden alle Hinweiszeitpunkte und die Antwortdeadline. Die Umstellung muss Vorbereitung und Zeitplanstart trennen. `admin_send_clue` darf dabei nicht in eine neue manuelle Hinweislogik umgedeutet werden; aktuell reconciliert die Aktion nur den serverseitig faelligen Zeitplan.

### Who Is Lying

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | WHO IS LYING |
| technischer Spieltyp | `who` |
| Frage/Aufgabe | Aussage des aktuellen Sets |
| Phase-2-Inhalt | erste Personenkarte und Beschuldigungssteuerung sichtbar, aber gesperrt; noch kein Personenfortschritt |
| Phase-3-Interaktion | Personen im vorhandenen Zeitraster beurteilen und Luegnerauswahl absenden |
| Hostbutton Phase 1 | `SET SENDEN` |
| Hostbutton Phase 2 | `PERSONEN ANZEIGEN` |
| Hostbutton Phase 3 | `SET FREIGEBEN` |
| Timer vorhanden | ja; Zeit pro Person und daraus abgeleitete Gesamtdeadline |
| Deadline entsteht | bei `open_answering` als `people_count * time_per_person` |
| Teilnehmeraktion | `participant_submit_answer` mit der bestehenden Luegnerauswahl |
| bestehende Sonderphasen | Setnummer, zeitgesteuerter Personenwechsel, Set-Reveal, Auswertung und Score bleiben eigenstaendig |
| vorgesehene Revealanimation | erste Personenkarte gruppiert; spaetere Personenwechsel behalten den bestehenden Ablauf |
| Reload/Reconnect | aktuellen Personenindex nur aus `answering_started_at`, Serverzeit und `time_per_person` ableiten; Set- und Spielinstanz pruefen |
| Umsetzungsgruppe | Sonderfall: ein Drei-Phasen-Zyklus pro Set mit interner Personenfolge |

### Wer Weiss Mehr

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | WER WEISS MEHR |
| technischer Spieltyp | `wer_weiss_mehr` |
| Frage/Aufgabe | Setfrage beziehungsweise Thema; bereits in vorherigen Runden aufgedeckte Antworten bleiben als historischer Zustand sichtbar |
| Phase-2-Inhalt | Antworttafel mit den aktuellen Platzhaltern und Texteingabe sichtbar, aber gesperrt |
| Phase-3-Interaktion | genau eine Antwort fuer die aktuelle Runde eingeben und absenden |
| Hostbutton Phase 1 | erste Runde `SET SENDEN`, Folgerunden `NÄCHSTE RUNDE SENDEN` |
| Hostbutton Phase 2 | `ANTWORTTAFEL ANZEIGEN` |
| Hostbutton Phase 3 | `RUNDE FREIGEBEN` |
| Timer vorhanden | ja, pro Runde |
| Deadline entsteht | bei `open_answering` der jeweiligen Runde; `start_set` und `admin_next_round` duerfen die Runde nur vorbereiten |
| Teilnehmeraktion | `participant_submit_answer` |
| bestehende Sonderphasen | Domainphasen `idle`, `round_active`, `review`, `set_completed`, manuelle Korrektur, aufgedeckte Antworten und Ausscheiden bleiben eigenstaendig |
| vorgesehene Revealanimation | Antworttafel gruppiert; bereits aufgedeckte Antworten werden nicht erneut animiert |
| Reload/Reconnect | Set-ID, Runden-ID, Fragephase, Tafelzustand, Teilnehmerstatus und Restzeit rekonstruieren |
| Umsetzungsgruppe | Sonderfall: wiederholter Drei-Phasen-Zyklus innerhalb eines Sets |

### Buzzer

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | BUZZER |
| technischer Spieltyp | `buzzer` |
| Frage/Aufgabe | im aktuellen Datenmodell nicht vorhanden; Teilnehmer sehen nur Rundenstatus |
| Phase-2-Inhalt | vorhandene Buzzeroberflaeche sichtbar und gesperrt |
| Phase-3-Interaktion | Buzzer betaetigen |
| Hostbutton Phase 1 | `RUNDE STARTEN` |
| Hostbutton Phase 2 | `ENTFÄLLT` – kein separater Frage-/Inhaltsdatensatz vorhanden |
| Hostbutton Phase 3 | `BUZZER FREIGEBEN` |
| Timer vorhanden | nein |
| Deadline entsteht | keine |
| Teilnehmeraktion | bestehende Buzz-Aktion; serverseitig nur bei offener Runde und offenem Buzzer zulaessig |
| bestehende Sonderphasen | Runde bereit, Buzzer offen, erster Buzz, Richtig/Falsch und Rundenende bleiben eigenstaendig |
| vorgesehene Revealanimation | keine neue Bewegung; vorhandene Buzzeroberflaeche bleibt erhalten |
| Reload/Reconnect | Runden-ID, Buzzerstatus, erster Buzz und Revision rekonstruieren |
| Umsetzungsgruppe | Sonderfall; Umstellung fachlich blockiert |

Der Code enthaelt weder eine Teilnehmerfrage noch einen Phase-2-Inhalt, der durch eine eigene Hostaktion freigegeben werden koennte. Vor einer Umstellung ist verbindlich zu entscheiden, ob die Frage weiterhin ausschliesslich muendlich gestellt wird und `content_visible` automatisch nach dem Praesentationsdelay gilt, oder ob ein persistenter Fragetext eingefuehrt werden soll. Bis dahin bleibt Buzzer im Kompatibilitaetsmodus; eine kuenstliche dritte Hostaktion wird nicht eingefuehrt.

### Host-Punktevergabe

| Feld | Inhalt |
| --- | --- |
| sichtbarer Spielname | HOST-PUNKTEVERGABE |
| technischer Spieltyp | `host_points` |
| Frage/Aufgabe | nicht vorhanden |
| Phase-2-Inhalt | nicht vorhanden |
| Phase-3-Interaktion | keine Teilnehmerinteraktion |
| Hostbutton Phase 1 | `ENTFÄLLT` – bestehender Spielstart bleibt unveraendert |
| Hostbutton Phase 2 | `ENTFÄLLT` |
| Hostbutton Phase 3 | `ENTFÄLLT` |
| Timer vorhanden | nein |
| Deadline entsteht | keine |
| Teilnehmeraktion | keine; Punkte werden ausschliesslich durch `admin_adjust_score` vergeben |
| bestehende Sonderphasen | Host-Punkteanpassung und `admin_next_round` bleiben der vollstaendige Fachablauf |
| vorgesehene Revealanimation | nicht anwendbar |
| Reload/Reconnect | Runde, Score und Revision wie bisher rekonstruieren |
| Umsetzungsgruppe | Sonderfall; dauerhafte Ausnahme vom manuellen Fragenmodus |

Host-Punktevergabe besitzt absichtlich keine Teilnehmerfrage und keine Teilnehmerantwort. `answering_open` waere fachlich bedeutungslos und wird nicht erzwungen. Das Spiel bleibt im Kompatibilitaetsmodus.

## Verbindliche Umsetzungsreihenfolge

Jeder nummerierte Eintrag ist ein eigener spaeterer Patch mit eigenen Unit-, Consumer-, Snapshot-, Reconnect- und Browserregressionstests.

1. **Who Is That**: einfachster Standardpfad fuer Textantwort plus Medieninhalt.
2. **Estimation**: Standardpfad mit Pending-Wert und konfigurierbarer Deadline.
3. **Black Jack Quiz**: Standardpfad unter Erhalt der Set-, Bust- und Aufloesungslogik.
4. **Where Is This**: erste zustandsbehaftete Eingabeflaeche; Map-Interaktion vollstaendig sperren.
5. **Wann War Das**: dynamische Wertung auf den Start von `answering_open` umstellen.
6. **Assign**: Drei-Phasen-Kontext pro interner Runde einfuehren, ohne `AssignSetRuntime` zu ersetzen.
7. **Sorting Ladder**: Drei-Phasen-Kontext pro interner Runde einfuehren, ohne Leiter-/Revealzustand zu ersetzen.
8. **Who Is Lying**: Personenzeitachse erst mit `open_answering` starten.
9. **Clue Rush**: Fragenvorbereitung vom autoritativen Hinweiszeitplan trennen.
10. **Wer Weiss Mehr**: wiederholten Drei-Phasen-Zyklus pro Setrunde mit bestehender Reviewphase verbinden.
11. **Buzzer**: erst nach fachlicher Entscheidung zum fehlenden Frage-/Phase-2-Inhalt umstellen oder als dokumentierte Ausnahme belassen.
12. **Host-Punktevergabe**: keine Umstellung; automatisierter Nachweis, dass der Kompatibilitaetsmodus erhalten bleibt.

## Testvertrag je Umstellung

Fuer jeden spaeteren Spielpatch sind mindestens folgende Nachweise verbindlich:

1. `present_question` beziehungsweise die fachlich entsprechende Startaktion erzeugt `prompt_visible`, aber keine Domain-Deadline.
2. Der Prompt bleibt bis `question_visible_at` verborgen; eine vorzeitige Phase-2-Aktion wird serverseitig abgelehnt.
3. `content_visible` zeigt nur die in der Spieltabelle definierten Inhalte und lehnt jede Teilnehmeraktion ab.
4. `open_answering` setzt Domainstart und Deadline genau einmal und aktiviert ausschliesslich die in der Spieltabelle definierte Teilnehmeraktion.
5. Doppelte Action-IDs, Doppelklicks und parallele Aktionen verschieben keine Zeitstempel oder Deadline.
6. Teilnehmer- und Spectator-Snapshots geben vor Phase 2 keine geschuetzten Inhalte preis; der Host behält die zum Vorlesen erforderliche Ansicht.
7. Reload in allen drei Phasen und Reconnect waehrend Promptdelay oder Timer rekonstruieren den verbleibenden Fortschritt aus Serverzeit.
8. Eine aeltere `state_revision` kann Phase, Inhalte, Interaktion oder Deadline nicht zuruecksetzen.
9. Eine zweite Frage, Runde oder ein zweites Set verwendet eine neue Kontextidentitaet und uebernimmt keine lokale Auswahl oder Timer.
10. Bestehende Domainphasen, Punkteformel, Aufloesung und Spielende bleiben unveraendert.

## Offene fachliche Punkte

1. **Buzzer**: Es existiert kein persistenter Fragetext und kein separater Inhalt zwischen `admin_start_round` und `admin_open_buzzer`. Ohne Produktentscheidung ist eine echte Drei-Phasen-Zuordnung nicht moeglich.
2. **Host-Punktevergabe**: Der vorhandene Code bestaetigt, dass keine Teilnehmerantwort existiert. Das Spiel ist eine bewusste Ausnahme und kein ausstehender Drei-Phasen-Rollout.
3. **Clue Rush**: Die UI kennt `admin_send_clue`, der produktive Ablauf ist jedoch ein serverseitig terminierter Hinweisplan. Der Plan erhaelt diesen automatischen Ablauf; eine Umstellung auf manuelle Einzelhinweise waere eine separate fachliche Aenderung.

Alle anderen Zuordnungen sind aus den vorhandenen Consumeraktionen, persistierten Domainzustaenden, Snapshots, Deadlines und Teilnehmeraktionen eindeutig ableitbar.
