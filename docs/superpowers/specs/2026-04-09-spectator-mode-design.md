# Spectator-Mode Design

**Datum:** 2026-04-09  
**Status:** Approved

## Überblick

Der Spectator-Mode ermöglicht es, eine laufende Quiz-Session auf einem zweiten Bildschirm oder Beamer darzustellen. Zuschauer, die nicht aktiv teilnehmen, sehen das aktuelle Spiel und die aktuelle Frage in Echtzeit. Der Spectator meldet sich nicht als Teilnehmer an, es gibt keine Eingabefelder, keine Antwort-Buttons und keine Auswertung für den Spectator.

## Einstiegspunkt

- Im **Session Monitor** (`templates/hub/monitor.html`) kommt ein neuer Button "Spectator-View öffnen"
- Klick öffnet `/hub/spectate/<session_code>/` in einem neuen Browser-Tab (`window.open`)
- Die URL ist durch den bestehenden Admin-Login-Decorator geschützt (wie der Monitor selbst)

## Spectator Hub-Seite

**URL:** `/hub/spectate/<session_code>/`  
**View:** `spectate_session` in `games_hub/views.py` (admin-geschützt)  
**Template:** `templates/hub/spectate.html`

- Verbindet sich mit dem bestehenden Hub-WebSocket `/ws/hub/<session_code>/`
- Schickt **kein** `join`-Paket — registriert sich nicht als Teilnehmer
- Hört auf `navigate`-Events; lädt bei Spielwechsel die passende Game-Spectator-Seite in einem `<iframe>`
- Zeigt einen schmalen Info-Header: Session-Name, aktueller Spieltyp, Spieltitel
- Bei noch keinem aktiven Spiel: Warteanzeige ("Warte auf nächstes Spiel...")
- Bei `session_ended`-Event: Abschlussmeldung statt iframe
- Orientiert sich im Look & Feel an der bestehenden Lobby-Seite (dunkles Gradient-Design)

## Game-spezifische Spectator-Seiten

Für jeden der 9 Spieltypen gibt es eine eigene URL, einen eigenen View und ein eigenes Template:

| Spieltyp       | URL                               | Template                              |
|----------------|-----------------------------------|---------------------------------------|
| Quiz           | `/quiz/spectate/<room_code>/`     | `templates/quiz/spectate.html`        |
| Assign         | `/assign/spectate/<room_code>/`   | `templates/assign/spectate.html`      |
| Estimation     | `/estimation/spectate/<room_code>/` | `templates/estimation/spectate.html` |
| Where Is This  | `/where/spectate/<room_code>/`    | `templates/where_is_this/spectate.html` |
| Who Is Lying   | `/who/spectate/<room_code>/`      | `templates/who_is_lying/spectate.html` |
| Who Is That    | `/who-is-that/spectate/<room_code>/` | `templates/who_is_that/spectate.html` |
| Blackjack      | `/blackjack/spectate/<room_code>/` | `templates/black_jack_quiz/spectate.html` |
| Sorting Ladder | `/sorting-ladder/spectate/<room_code>/` | `templates/sorting_ladder/spectate.html` |
| Clue Rush      | `/clue-rush/spectate/<room_code>/` | `templates/clue_rush/spectate.html`  |

Jede Game-Spectator-Seite:
- Verbindet sich mit dem spieleigenen WebSocket des jeweiligen Spiels
- Empfängt dieselben Events wie ein Teilnehmer (read-only)
- Zeigt Frage + Optionen ohne Eingabefelder oder Antwort-Buttons
- Orientiert sich am jeweiligen `play.html`-Template als Vorlage

## Routing-Logik (Spectator Hub → iframe)

Beim Empfang eines `navigate`-Events über den Hub-WebSocket:

```javascript
const spectatorRoutes = {
  quiz: '/quiz/spectate/',
  assign: '/assign/spectate/',
  estimation: '/estimation/spectate/',
  where: '/where/spectate/',
  who: '/who/spectate/',
  who_that: '/who-is-that/spectate/',
  blackjack: '/blackjack/spectate/',
  sorting_ladder: '/sorting-ladder/spectate/',
  clue_rush: '/clue-rush/spectate/',
};
// iframe.src = spectatorRoutes[game_key] + room_code + '/'
```

Bei Spielwechsel wird nur der iframe-`src` aktualisiert — kein Neuladen der gesamten Spectator-Seite.

## Zugriffschutz

- `/hub/spectate/<session_code>/` — Admin-Login-Decorator (wie Monitor)
- Alle Game-Spectator-URLs — ebenfalls Admin-Login-Decorator
- Kein eigener Nutzer/Teilnehmer für den Spectator

## Änderungen im Überblick

| Datei | Änderung |
|---|---|
| `templates/hub/monitor.html` | Button "Spectator-View öffnen" hinzufügen |
| `games_hub/views.py` | View `spectate_session` hinzufügen |
| `games_hub/urls.py` | URL `/hub/spectate/<session_code>/` hinzufügen |
| `templates/hub/spectate.html` | Neues Template (Hub-Spectator-Seite) |
| `QuizGame/views.py` + `urls.py` | Spectator-View + URL |
| `Assign/views.py` + `urls.py` | Spectator-View + URL |
| `Estimation/views.py` + `urls.py` | Spectator-View + URL |
| `where_is_this/views.py` + `urls.py` | Spectator-View + URL |
| `who_is_lying/views.py` + `urls.py` | Spectator-View + URL |
| `who_is_that/views.py` + `urls.py` | Spectator-View + URL |
| `black_jack_quiz/views.py` + `urls.py` | Spectator-View + URL |
| `sorting_ladder/views.py` + `urls.py` | Spectator-View + URL |
| `clue_rush/views.py` + `urls.py` | Spectator-View + URL |
| 9× `templates/*/spectate.html` | Neue Templates (Game-Spectator-Seiten) |
