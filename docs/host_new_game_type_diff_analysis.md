# Analyse der hostseitigen Unterschiede neuer Spieltypen

Stand der Analyse: 2026-08-23

## Umfang und Methode

Diese Analyse vergleicht ausschliesslich Hostpfade. Untersucht wurden die aktuell
registrierten Spieltypen, die gemeinsamen Hosttemplates, die Monitor-Templates,
deren JavaScript, die Start- und Endaktionen, die Hub-/Lobby-Helfer, die
WebSocket-Consumer sowie vorhandene Regressionstests.

Die Produktklassifikation folgt der Aufgabenstellung:

- Neuere Spiele: `buzzer`, `host_points`, `wann_war_das`, `wer_weiss_mehr`.
- Aeltere Vergleichsspiele: `quiz`, `estimation`, `blackjack`, `who_that`,
  `where`, `assign`, `sorting_ladder`, `clue_rush`, `who`.

`HubGameStep.GAME_CHOICES` registriert genau diese 13 Spieltypen. Es wurden keine
weiteren registrierten Spieltypen gefunden.

Die Reproduktion des Lobbyproblems erfolgte durch den vollstaendigen statischen
Event-Trace der gerenderten Monitorpfade und durch vier bestehende Django-
Regressionstests. Die Tests bestaetigen den Presence-Endpunkt, die serverseitige
Startblockade, das gemeinsame Modalfenster und die bewusste Anbindung von
`host_points` ueber `#startQuizBtn`.

## A. Executive Summary

### Kernergebnis

Das Lobby-Pop-up ist keine spielbezogene Serverfunktion. Es wird in
`templates/admin_dashboard/base.html` durch einen globalen Capture-Listener nur
fuer einen Button mit dem Selektor `#startQuizBtn` aktiviert.

- `buzzer` und `wann_war_das` verwenden `#startGameBtn`. Ihre eigenen Click-
  Handler senden deshalb direkt `admin_start_game`; der gemeinsame Dialog wird
  vollstaendig umgangen.
- `host_points` und `wer_weiss_mehr` verwenden im aktuellen Quellstand
  `#startQuizBtn`. Bei normaler Navigation aus dem Hub-Monitor mit
  `?hub_session=...` verwenden sie den gemeinsamen Lobbycheck und das Pop-up.
  Die pauschale Fehlerbeschreibung fuer alle vier neuen Spiele ist daher durch
  den aktuellen Quellstand nicht bestaetigt.
- Alle vier neuen Spiele wiederholen die Lobbypruefung serverseitig. Ein Start
  bei aktiven Teilnehmerdatensaetzen wird daher auch bei fehlendem Pop-up
  blockiert. Bei `buzzer` folgt nur ein `alert`; bei `wann_war_das` wird der
  Eventtyp `participants_not_in_lobby` clientseitig gar nicht behandelt.

Es wurden zusaetzlich 12 hostseitige Abweichungscluster und 5 gemeinsame
Alt-/Neupfad-Risiken erfasst. Die groessten vergleichenden Risiken sind der
fehlende Start-Recovery-Pfad bei `buzzer`/`wann_war_das`, fehlende
Active-Game-Leave-Guards bei drei neuen Monitoren, uneinheitliche Ownerpruefungen
und uneinheitlicher Idempotenz-/Revisionsschutz. Es wurde kein neuer
spieltypspezifischer P0-Startfehler nachgewiesen, weil der Server den Start
weiterhin blockiert.

### Ausgangszustand des Working Trees

Der Working Tree war vor der Analyse bereits stark veraendert. Der initiale
`git diff --stat` umfasste 110 getrackte Dateien mit 22.731 Einfuegungen und
4.125 Loeschungen. Zusaetzlich existierten zahlreiche ungetrackte Migrationen,
Runtime-Helfer, Tests, Designreferenzen und generierte Mediendateien. Diese
vorhandenen Aenderungen wurden weder veraendert noch zurueckgesetzt. Durch diese
Aufgabe wurde ausschliesslich dieses Analysedokument hinzugefuegt.

## B. Lobby-Pop-up

### Vergleichsmatrix

Die Angaben gelten fuer den regulaeren Aufruf aus dem Hub-Monitor, der den
Parameter `hub_session` an die Spielmonitor-URL anhaengt.

| Spiel | Teilnehmer-Lobbycheck | Pop-up | Force-Return | Start blockiert |
| --- | --- | --- | --- | --- |
| Quick Quiz (`quiz`) | Client + Server | ja | ja | Client + Server |
| Estimation (`estimation`) | Client + Server | ja | ja | Client + Server |
| Black Jack (`blackjack`) | Client + Server | ja | ja | Client + Server |
| Who Is That (`who_that`) | Client + Server | ja | ja | Client + Server |
| Where Is This (`where`) | Client + Server | ja | ja | Client + Server |
| Assign (`assign`) | Client + Server | ja | ja | Client + Server |
| Sorting Ladder (`sorting_ladder`) | Client + Server | ja | ja | Client + Server |
| Clue Rush (`clue_rush`) | Client + Server | ja | ja | Client + Server |
| Who Is Lying (`who`) | Client + Server | ja | ja | Client + Server |
| Buzzer (`buzzer`) | nur Server | nein im Startpfad | nein im Startpfad | Server |
| Host-Punktevergabe (`host_points`) | Client + Server | ja | ja | Client + Server |
| Wann war das (`wann_war_das`) | nur Server | nein im Startpfad | nein im Startpfad | Server |
| Wer weiss mehr (`wer_weiss_mehr`) | Client + Server | ja | ja | Client + Server |

### Vollstaendiger etablierter Pfad

1. Der Hub-Monitor baut die Ziel-URL mit `buildMonitorUrl()` und fuegt
   `hub_session` an.
2. Der Spielmonitor rendert `admin_dashboard/base.html`, damit auch
   `#lobbyReturnGuardModal` vorhanden ist.
3. Ein Capture-Listener in `base.html` faengt ausschliesslich Klicks auf
   `#startQuizBtn` ab, deaktiviert den Button und ruft
   `/hub/api/session/<code>/lobby-presence/` auf.
4. `get_session_lobby_presence()` sucht pro Session-Step aktive
   spieltypspezifische Teilnehmerdatensaetze mit `is_active=True`.
5. Bei fehlenden Teilnehmern wird das Modal mit Anzahl und Namen geoeffnet. Der
   eigentliche Start-Click wird verworfen.
6. Der Primaerbutton startet ueber `/start-recall-countdown/` einen persistenten
   10-Sekunden-Countdown und sendet das Countdown-Event an die Teilnehmer.
7. Nach Ablauf setzt `/recall-to-lobby/` alle aktiven Spielteilnehmer der Session
   auf `is_active=False` und broadcastet `players_recalled_to_lobby`.
8. Die Teilnehmernavigation verwendet den gemeinsamen Include
   `_hub_return_to_lobby_player.html` und wechselt in die Hub-Lobby.
9. Das Modal schliesst und der Startbutton wird wieder freigegeben. Der Start
   wird nicht automatisch fortgesetzt; der Host muss erneut klicken.
10. Beim erneuten Klick prueft `requestActivation(..., check_only=true)` einen
    Konflikt, aktiviert danach den Session-Step und gibt den urspruenglichen
    Buttonclick mit `data-session-start-approved` frei.
11. Erst jetzt sendet der spielspezifische Monitor seinen WebSocket- oder
    HTTP-Startbefehl. Der Server wiederholt sowohl Lobby- als auch Active-Game-
    Guard vor dem eigentlichen Start.

### Abweichender Pfad: Buzzer

1. Der Monitor rendert zwar das gemeinsame Base-Template und damit das Modal.
2. Der Startbutton heisst aber `#startGameBtn`.
3. Der gemeinsame Capture-Listener passt nicht; der lokale Listener sendet
   sofort `admin_start_game` per WebSocket.
4. `BuzzerConsumer.handle_admin_start_game()` ruft den gemeinsamen serverseitigen
   Lobbycheck auf.
5. Bei negativem Ergebnis wird der Start blockiert und
   `participants_not_in_lobby` gesendet.
6. Der Monitor zeigt lediglich `alert(...)`. Es gibt von dort keinen Countdown-
   oder Force-Return-Pfad.

### Abweichender Pfad: Wann war das

Der Pfad entspricht Buzzer bis zur serverseitigen Blockade. Der Unterschied ist
noch deutlicher: `wann_war_das_monitor.html` verarbeitet weder
`participants_not_in_lobby` noch `active_game_conflict`. Der serverseitig
abgelehnte Start erzeugt im Monitor deshalb keine verwertbare Rueckmeldung.

### Host-Punktevergabe und Wer weiss mehr

Beide verwenden `#startQuizBtn` und werden bei regulaerer Hubnavigation vom
gemeinsamen Guard abgefangen. `host_points/tests.py` schreibt dies explizit als
Regression fest: Das Template muss `#startQuizBtn` enthalten und darf
`#startGameBtn` nicht enthalten.

Nach dem Client-Preflight unterscheiden sich die Transporte:

- `host_points` sendet `admin_start_game` per WebSocket.
- `wer_weiss_mehr` startet primaer ueber den owner-geprueften HTTP-Endpunkt
  `/wer-weiss-mehr/start/<room>/`.

Falls sich der Presence-Zustand zwischen Client-Preflight und Serverstart
aendert, zeigen beide nur einen normalen Alert beziehungsweise eine generische
HTTP-Fehlermeldung. Der Force-Return-Dialog wird in diesem Race-Pfad nicht erneut
geoeffnet.

### Fragilitaet des gemeinsamen Guards

`getSessionCode()` im Base-Template liest nur `?hub_session=...` oder einen
`/hub/monitor/<code>/`-Pfad. Wird irgendein alter oder neuer Spielmonitor direkt
ohne Queryparameter aufgerufen, existiert kein Monitor-Kontext und der
Client-Guard bleibt fuer alle Spiele inaktiv. Der Serverguard bleibt erhalten.
Der regulaere Hub-Monitor setzt den Queryparameter korrekt.

### Bedeutung von "nicht in der Lobby"

Die bestehende Logik prueft keine explizite Browserroute und keinen Domainstatus
wie `review`, `result` oder `waiting`. Ein Teilnehmer gilt als ausserhalb der
Lobby, wenn:

- er als Hub-Teilnehmer der Session beruecksichtigt wird und
- zu seinem Nicknamen in mindestens einem Session-Step ein
  spieltypspezifischer Teilnehmerdatensatz mit `is_active=True` existiert.

Folgen:

- Altes Spiel, Aufloesung, Scoreboard und spielbezogene Wartezustaende werden nur
  dann erfasst, wenn `is_active` dort weiterhin wahr ist.
- Sobald mindestens eine `HubSocketConnection` in der Session existiert, werden
  nur aktuell verbundene Teilnehmer innerhalb der Presence-TTL betrachtet.
  Getrennte Teilnehmer werden ignoriert, auch wenn ihr Spielteilnehmerdatensatz
  aktiv bleibt.
- Ein reconnectender Teilnehmer wird beruecksichtigt, sobald wieder eine frische
  Socketverbindung existiert.
- Die Zuordnung erfolgt ueber Nicknamen statt ueber die Hub-Participant-ID.

Diese Semantik ist fuer alle Spieltypen gemeinsam; die neueren Spiele vergleichen
keine anderen Zustandswerte.

## C. Host-UI-Unterschiede

| Bereich | Aeltere Spiele | Neuere Spiele | Bewertung |
| --- | --- | --- | --- |
| Base-Template | Alle Monitorseiten erweitern `admin_dashboard/base.html`. | Alle vier erweitern ebenfalls dasselbe Base-Template. | gewollt; kein Unterschied |
| Startbutton-Hook | Durchgaengig `#startQuizBtn`. | Buzzer/WWD: `#startGameBtn`; Host Points/WWM: `#startQuizBtn`. | P1, klar unbeabsichtigt fuer den Lobbyguard |
| Lobbydialog-Markup | Ueber Base vorhanden und ueber Startclick erreichbar. | Markup bei allen vorhanden, aber bei Buzzer/WWD aus dem Startpfad unerreichbar. | P1, klar unbeabsichtigt |
| Zuruecksteuerung | `#backToHubBtn` plus `window.adminGameMonitor`. | Nur WWM hat beides. Buzzer, Host Points und WWD nutzen direkte Links. | P1, wahrscheinlich unbeabsichtigt |
| Aktives Spiel verlassen | Gemeinsames Modal bietet Beenden, Inaktivsetzen oder Uebersicht. | Nur WWM nimmt teil. Drei neue Monitore koennen den Guard umgehen. | P1, klarer Funktionsunterschied/Bugrisiko |
| Hostlink zur Teilnehmerlobby | In den etablierten Monitoren nicht der normale Host-Rueckweg. | Buzzer und WWD zeigen `Zur Lobby` zur Teilnehmerlobby. Host Points vermeidet dies; WWM nutzt `Zurueck`. | P2, wahrscheinlich unbeabsichtigt |
| Start-Pending | Gemeinsamer Startguard deaktiviert den Startbutton. Danach spielabhaengig. | HP/WWM: deaktiviert; Buzzer/WWD: lokaler Startclick wird nicht sofort gesperrt. | P1/P2, wahrscheinlich unbeabsichtigt |
| Runden-/Fragebuttons | Grosse, spielbezogene Monitorklassen; Zustandsableitung heterogen. | Buzzer/HP kompakt, WWD phasenbasiert, WWM HTTP-/Review-basiert. | gewollt, soweit durch Spielmechanik bedingt |
| Endzustand | Gemeinsames `window.hostEndState.apply()` wird genutzt. | Alle vier nutzen es ebenfalls. | gewollt; kein Unterschied |
| Result-/Scorebereich | Spielbezogene Tabellen und Aufloesungen. | Ebenfalls spielbezogen; HP ist absichtlich reine Hostwertung, WWM hat Review/Korrektur. | gewollt |
| Fehleranzeige | Mischung aus Inlinefehlern, Alert und Resync. | Buzzer/HP meist Alert, WWD hat keinen Lobby-/Konflikthandler, WWM Alert plus HTTP-Refetch. | P1/P2, wahrscheinlich unbeabsichtigt |
| Verbindungsstatus | Aeltere Monitore besitzen haeufig Onerror-/Hubsocket-Anzeigen. | Buzzer/HP/WWD haben keinen `onerror`; WWM reconnectet beide Sockets, zeigt aber ebenfalls keinen konsistenten Status. | P2, wahrscheinlich unbeabsichtigt |

## D. Host-JavaScript

| Helper/Funktion | Aeltere Spiele | Buzzer | Host Points | Wann war das | Wer weiss mehr |
| --- | --- | --- | --- | --- | --- |
| Gemeinsamer `#startQuizBtn`-Guard | ja | nein (`#startGameBtn`) | ja | nein (`#startGameBtn`) | ja |
| `fetchLobbyPresence()`/Lobby-Modal | ja bei Hub-Kontext | nur indirekt vorhanden | ja | nur indirekt vorhanden | ja |
| `window.adminGameMonitor` | ja | nein | nein | nein | ja |
| `#backToHubBtn` | ja | nein | nein | nein | ja |
| `AuthoritativeGameState` geladen | ueberwiegend, `where` ist eine Ausnahme | nein | ja | ja | ja |
| `acceptSnapshot()` explizit | nicht einheitlich | nein | ja | nein | nein |
| Host-`client_action_id` | meist fuer neue Fragenphasen, nicht universell | nein | alle mutierenden Hostaktionen | Fragenphasen | Set-/Rundenphasen |
| Pending-/Doppelklickschutz | heterogen; gemeinsamer Startbutton plus lokale Guards | keiner | ein wartender autoritativer Action-Slot | `pendingPhaseAction` nur fuer Fragenphasen | pro HTTP-Aktion, Startbutton und lokale Flags |
| Offline-Aktion | meist verworfen oder spielbezogen | verworfen | eine Guarded Action wird gepuffert | verworfen | WS-Nachrichten werden gesammelt; Hauptaktionen laufen per HTTP |
| Reconnect-State | meist WebSocket plus Hubsocket/State-Refresh | `get_state` beim Reconnect | `get_state`, danach Pending Action | `get_state` plus 1-s-Polling | HTTP-`fetchState()` plus Spiel- und Hubsocket |
| Lobby-Server-Race | nur Black Jack und Clue Rush behandeln den Event explizit | Alert | Alert | unbehandelt | generischer HTTP-Alert |
| Timerzeitbasis | spielabhaengig, neue Phasen verwenden Serverzeit-Helfer | kein Antworttimer | kein Antworttimer | Snapshot-/Serverzeit | Hosttimer verwendet direkt `Date.now()` |

### Gemeinsame Helper und ihre Nutzung

| Tatsaechlicher Helper | Zweck | Aeltere Spiele | Neuere Nutzung/Abweichung |
| --- | --- | --- | --- |
| Base-Capture auf `#startQuizBtn` | Lobbycheck, Active-Game-Preflight, Start-Release | alle neun | HP/WWM nutzen ihn; Buzzer/WWD umgehen ihn |
| `fetchLobbyPresence()` | liest zentralen Presence-Endpunkt | alle neun ueber Base | nur HP/WWM ueber Base |
| `openLobbyReturnModal()` | zeigt Namen und Force-Return | alle neun ueber Base | nur HP/WWM ueber Startpfad |
| `requestActivation()` | Check-only und Aktivierung des Session-Steps | alle neun ueber Base | nur HP/WWM clientseitig; alle vier serverseitig nochmals |
| `window.adminGameMonitor` | API fuer Beenden/Inaktivsetzen vor Navigation | alle neun | nur WWM |
| `window.hostEndState.apply()` | normalisiert abgeschlossene Host-UI | alle neun | alle vier |
| `AuthoritativeGameState.createActionId()` | eindeutige Action-ID | neue Fragenphasen vieler alter Spiele | HP vollstaendig, WWD/WWM teilweise, Buzzer gar nicht |
| `AuthoritativeGameState.acceptSnapshot()` | verwirft alte Revisionen | nicht konsistent in alten Monitoren | nur HP explizit |

## E. Serveractions

| Spielgruppe | Primaerer Spielstart | Lobbyguard | Active-Game-Guard | Host-Idempotenz | HTTP-Ownerpruefung |
| --- | --- | --- | --- | --- | --- |
| Aeltere Spiele | ueberwiegend WebSocket `admin_start_quiz` | ja | ja | gemischt; Fragenphasen moderner, Start/Ende oft ohne Action-ID | Monitorviews ueberwiegend Creator/Superuser; WebSockets ungeprueft |
| Buzzer | WebSocket `admin_start_game` | ja | ja | keine Host-Action-ID; Domainstatus begrenzt Wiederholungen | Monitor/Endpunkt nur `admin_required`, kein Creatorcheck |
| Host Points | WebSocket `admin_start_game` | ja | ja | `validate_and_reserve_action()` fuer alle mutierenden Hostaktionen | Monitor/Endpunkt nur `admin_required`, kein Creatorcheck |
| Wann war das | WebSocket `admin_start_game` | ja | ja | Fragenphasen mit Revision/Action-ID; Start/Ende ohne | Monitor/Endpunkt nur `admin_required`, kein Creatorcheck |
| Wer weiss mehr | primaer owner-gepruefte HTTP-Endpunkte; parallele WS-Aktionen existieren | ja | ja | Set-/Rundenphasen mit Revision/Action-ID; Start ohne | ja fuer primaere HTTP-Pfade und Monitor |

### Validierungen

Alle 13 Startconsumer/-views rufen den gemeinsamen Lobbyguard und den zentralen
Active-Game-Guard auf. Der Active-Game-Guard sperrt die HubSession transaktional,
prueft Session-/Stepstatus, Check-in, bestehende aktive Spiele und den Zielstatus.
Die neuen Spiele umgehen diese Servervalidierungen nicht.

Spielbezogene Domainvalidierungen bleiben unterschiedlich und sind groesstenteils
fachlich gewollt: Buzzer validiert Runden-/Buzzstatus, Host Points Rundennummer
und Scoreziel, WWD Fragenphase/-identitaet, WWM Set-/Reviewstatus.

### Berechtigungen

- Die Monitorviews von Buzzer, Host Points und WWD pruefen lediglich
  `admin_required`; ein anderer Admin kann einen fremden Raum oeffnen und ueber
  die separaten Endpunkte beenden. WWM und die meisten alten Monitorviews pruefen
  Creator oder Superuser.
- Kein untersuchter Spielconsumer prueft bei WebSocket-Connect oder vor
  `admin_*`-Aktionen `scope['user']`, Staffstatus oder Ownership. Dieses Risiko
  ist alt und neu gemeinsam und widerlegt die Annahme, der Altpfad sei generell
  sicher.
- `activate_session_game`, Lobby-Presence, Recall-Countdown und Force-Recall sind
  nur `login_required`; sie pruefen keine Session-Ownership. Das ist ebenfalls
  ein gemeinsames Risiko, kein neuer spieltypspezifischer Unterschied.

## F. Spielstart

### Schutzmatrix

| Spiel | Start trotz aktivem Altteilnehmer | Bestaetigung | Rueckfuehrung aus Startdialog | Doppelklickschutz | Aktives Altspiel |
| --- | --- | --- | --- | --- | --- |
| Alte Gruppe | serverseitig nein | ja | ja | gemeinsamer Startguard; danach gemischt | zentraler Konfliktguard + Modal |
| Buzzer | serverseitig nein | nein | nein | keine Action-ID/kein sofortiges Disable | serverseitig blockiert; nur Alert |
| Host Points | serverseitig nein | ja | ja | Base + autoritative Hostaction | zentraler Guard; Race nur Alert |
| Wann war das | serverseitig nein | nein | nein | Start ungeschuetzt, Phaseaktionen geschuetzt | serverseitig blockiert; Event unbehandelt |
| Wer weiss mehr | serverseitig nein | ja | ja | Base + deaktivierter HTTP-Start | zentraler Guard; HTTP-Fehler als Alert |

Disconnected Teilnehmer werden nach Anlage der ersten Socket-Presence-Zeile
nicht als Startblocker gewertet. Ein gerade reconnectender Teilnehmer kann je
nach frischer Presence-Zeile wieder in die Entscheidung fallen. Dieses Verhalten
ist fuer alle Spiele gleich.

## G. Spielende und Lobby-Rueckkehr

- Alle Spieltypen senden/bauen einen abgeschlossenen Domainzustand und nutzen
  `hostEndState` zur finalen Hostdarstellung.
- Aeltere Monitore und WWM stellen `window.adminGameMonitor` bereit. Beim Klick
  auf `#backToHubBtn` kann der Host ein aktives Spiel beenden, auf inaktiv setzen
  oder ohne Zustandsaenderung nur zur Uebersicht wechseln.
- Buzzer, Host Points und WWD besitzen weder dieses Interface noch den erwarteten
  Back-Button. Direkte Links verlassen die Seite ohne gemeinsamen
  Active-Game-Dialog. Das Spiel kann aktiv bleiben und den naechsten Session-Step
  blockieren.
- Buzzer und WWD verlinken den Host zusaetzlich auf die Teilnehmerlobby. Dieser
  Link setzt den Spielzustand nicht zurueck und ist semantisch nicht der
  etablierte Host-Rueckweg.
- Die Teilnehmer-Rueckkehr nach echtem Spielende ist fuer alle 13 Spiele ueber
  den gemeinsamen Participant-Return-Pfad vorhanden. Das ist vom fehlenden
  Host-Startdialog zu unterscheiden.

## H. Reconnect/Reload

| Spiel/Gruppe | Reload | Socket-Reconnect | Risiko |
| --- | --- | --- | --- |
| Alte Gruppe | servergerenderter Initialzustand plus spielbezogene State-Updates | meist Spielsocket und Hubsocket, aber uneinheitliche Revisionfilter | heterogen, Altpfad nicht einheitlich korrekt |
| Buzzer | Zustand wird beim Socket-Open mit `get_state` geladen | 2 s; Offline-Klicks gehen verloren | P2 |
| Host Points | `get_state`, `acceptSnapshot()` | 2 s; eine wartende Guarded Action wird nach Snapshot erneut gesendet | robustester neuer Pfad |
| Wann war das | `get_state`, zusaetzlich sekündlicher State-Request | 2 s; Offline-Klicks gehen verloren | alte Snapshots werden nicht explizit per `acceptSnapshot()` verworfen |
| Wer weiss mehr | HTTP-`fetchState()` auf Open und Events | Spiel- und Hubsocket; WS-Nachrichten werden gesammelt | parallele Fetchantworten haben keinen Request-/Revisionfilter |

WWD und WWM koennen daher clientseitig einen spaeter eingetroffenen aelteren
Snapshot rendern. Die serverseitigen Domain-/Revisionspruefungen begrenzen
Folgeschaeden, die Hostbuttons koennen aber voruebergehend in einen alten Zustand
zurueckspringen. Bei WWM verwendet der sichtbare Hosttimer ausserdem direkt die
lokale Uhr (`Date.now()`) statt der bereits synchronisierten Serverzeitschaetzung.

## I. Action-ID / Idempotenz

### Aeltere Spiele

Die Altgruppe ist nicht homogen. Die nachtraeglich eingefuehrten Fragenphasen
verwenden vielfach `client_action_id`, `state_revision` und Frage-/Rundenkontext.
Viele traditionelle Start-, End-, Reveal- oder Navigationsevents verlassen sich
aber weiterhin nur auf Domainzustand und Buttonzustand.

### Neue Spiele

- Buzzer besitzt fuer keine Hostaktion eine Action-ID. Schnelle Mehrfachklicks
  koennen mehrere WebSocketnachrichten erzeugen. Modelmethoden lehnen manche
  Wiederholungen ab, ein expliziter Exactly-once-Schutz fehlt.
- Host Points nutzt `validate_and_reserve_action()` fuer Start, Scoreaenderung,
  naechste Runde und Ende. Kontext und Revision werden clientseitig angehaengt.
- WWD schuetzt `admin_start_question` und `admin_open_answering`; Spielstart,
  Frageende und Spielende verwenden keinen gleichwertigen Host-Action-Guard.
- WWM schuetzt die neuen Set-/Rundenaktionen ueber den gemeinsamen
  Fragenaktionskontext. Der primaere HTTP-Spielstart hat keine Action-ID, wird
  aber lokal deaktiviert und durch Domainstatus begrenzt.

## J. Weitere Unterschiede und Priorisierung

### P0 - Spielzustand kann beschaedigt werden

Keine neue-spieltyp-spezifische P0-Abweichung wurde fuer das Lobbyproblem
nachgewiesen. Zwei gemeinsame Risiken betreffen jedoch alte und neue Spiele:

1. **WebSocket-Adminaktionen ohne Host-/Ownerpruefung** - alle untersuchten
   Consumer akzeptieren `admin_*`-Nachrichten ohne Auswertung von `scope.user`.
   Klassifikation: klar unbeabsichtigt, gemeinsam.
2. **Mutierende Hub- und Recall-Endpunkte nur mit Loginpruefung** - ein
   authentifizierter Benutzer kann bei bekanntem Sessioncode Aktivierung und
   Teilnehmer-Recall einer fremden Session anstossen. Klassifikation: klar
   unbeabsichtigt, gemeinsam.

### P1 - funktionaler Hostfehler

1. **Kein Lobby-Pop-up/Force-Return bei Buzzer und WWD** durch `#startGameBtn`.
   Klassifikation: klar unbeabsichtigt.
2. **WWD zeigt bei serverseitiger Lobby- oder Active-Game-Ablehnung keine
   Rueckmeldung.** Klassifikation: klar unbeabsichtigt.
3. **Buzzer, Host Points und WWD umgehen den Active-Game-Leave-Guard.** Direkte
   Navigation kann ein aktives Spiel hinterlassen. Klassifikation:
   wahrscheinlich unbeabsichtigt.
4. **Fehlende Creatorpruefung in Monitor- und Endviews von Buzzer, Host Points
   und WWD.** WWM und die meisten alten Monitore sind strenger. Klassifikation:
   klar unbeabsichtigt.
5. **Buzzer-Hostaktionen ohne Action-ID oder lokalen Pending-Schutz.** Besonders
   Start, Punkteentscheidungen und Rundenwechsel sind doppelklickanfaellig.
   Klassifikation: wahrscheinlich unbeabsichtigt.

### P2 - inkonsistentes Verhalten / UX-Risiko

1. **Race nach erfolgreichem Client-Preflight** wird bei HP/WWM nicht wieder in
   den Recall-Dialog ueberfuehrt, sondern nur als Alert angezeigt.
2. **Direkter Monitoraufruf ohne `hub_session`** deaktiviert den Clientguard bei
   allen Spielen, obwohl einzelne Monitore den Sessioncode serverseitig kennen.
3. **Disconnected Teilnehmer werden aus der Readinessentscheidung entfernt**,
   sobald Socket-Presence in der Session existiert. Ein aktiver, getrennter
   Teilnehmer kann daher einen Start nicht blockieren.
4. **Recall setzt den abgebrochenen Start nicht automatisch fort.** Nach dem
   10-Sekunden-Recall ist ein zweiter Hostklick erforderlich.
5. **WWD/WWM filtern alte Render-Snapshots nicht konsistent.** Bei WWM koennen
   parallele HTTP-Fetches ausser Reihenfolge eintreffen.
6. **Offline-Aktionen werden unterschiedlich behandelt:** Buzzer/WWD verwerfen,
   HP puffert eine Aktion, WWM puffert alle WS-Nachrichten.
7. **WWM-Hosttimer verwendet lokale Uhrzeit.** Uhrabweichung kann die sichtbare
   Restzeit und den lokalen Auto-End-Impuls verschieben.
8. **Uneinheitliche Connection-/Error-Anzeige.** Buzzer/HP/WWD besitzen keinen
   `onerror`-Pfad; WWD behandelt zwei zentrale Startfehler nicht.
9. **Buzzer und WWD bieten einen Hostlink zur Teilnehmerlobby.** Der Link ist
   kein Zustandsreset und kann den Host aus dem vorgesehenen Monitorflow fuehren.

Klassifikation dieser Punkte: wahrscheinlich unbeabsichtigt, ausser die
unterschiedliche Offline-Queue bei WWM (unklar).

### P3 - strukturell oder kosmetisch

1. Die neueren Monitore verwenden drei unterschiedliche Architekturstile:
   kompaktes WebSocket-Template (Buzzer/HP), WebSocket plus Polling (WWD) und
   HTTP-State-Machine plus zwei WebSockets (WWM). Klassifikation: unklar.
2. Buttontexte, Statusbadges, Live-Response-Tabellen und Reviewbereiche sind
   nicht vereinheitlicht. Ein grosser Teil ist fachlich gewollt.
3. Viele aeltere Monitore duplizieren ebenfalls Hubsocket-, Backbutton- und
   State-Handling. Die alte Gruppe ist deshalb keine einheitliche Referenz.

## Empfohlene spaetere Vereinheitlichungsstrategie

Ohne in dieser Analyse Code zu aendern, ist der kleinste sinnvolle Folgepatch:

1. Den gemeinsamen Startguard an ein semantisches Attribut wie
   `[data-host-start-game]` statt an eine einzelne historische ID binden.
2. Alle 13 Startbuttons explizit an dieses Attribut anbinden und einen
   parametrisierten Browserregressionstest pro registriertem Spieltyp ergaenzen.
3. Server-Race-Antworten zentral wieder an `openLobbyReturnModal()` leiten.
4. In einem getrennten Patch Owner-/Staffpruefung fuer WebSocket-Adminaktionen
   und Hub-Mutationsendpunkte vereinheitlichen.
5. Danach das `window.adminGameMonitor`-Interface und den Backbutton fuer Buzzer,
   Host Points und WWD angleichen, ohne deren Spielmechanik zu aendern.

Diese Reihenfolge trennt den konkreten P1-Startdialogfehler von den groesseren
Berechtigungs- und Hostarchitekturthemen.

## Verifikation

Ausgefuehrte bestehende Tests:

```text
games_hub.test_lobby_return_flow.LobbyReturnFlowTests.
  test_session_lobby_presence_endpoint_reports_players_still_in_game
games_hub.test_lobby_return_flow.LobbyReturnFlowTests.
  test_consumer_start_is_blocked_when_players_are_not_in_lobby
games_hub.test_lobby_return_flow.LobbyReturnFlowTests.
  test_game_monitor_renders_lobby_return_guard_modal
host_points.tests.HostPointsFlowTests.
  test_host_monitor_uses_common_start_guard_and_consistent_controls
```

Ergebnis: 4 Tests, alle erfolgreich; Django-Systemcheck ohne Befund.

Die Analyse hat keine produktiven Dateien, Templates, JavaScriptdateien,
Modelle, Migrationen oder Tests veraendert.
