# Spectator-Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only Spectator-Mode that the admin opens from the Session Monitor, which automatically mirrors the active game (question + options) in real-time via an iframe — no participant registration required.

**Architecture:** The Hub Spectator page (`/hub/spectate/<session_code>/`) connects to the existing Hub WebSocket without sending a `join` message, listens for `navigate` events, and renders the active game as a game-specific Spectator page inside an `<iframe>`. Each of the 9 game types gets its own `/spectate/<room_code>/` URL+View+Template that connects to the game's existing WebSocket and shows the current question read-only.

**Tech Stack:** Django (views, urls), Django Channels WebSocket (existing consumers, no changes), HTML/CSS/JS (dark lobby-style templates)

---

## File Map

### Modified files
| File | Change |
|---|---|
| `templates/hub/monitor.html` | Add "Spectator-View öffnen" button |
| `games_hub/views.py` | Add `spectate_session` view |
| `games_hub/urls.py` | Add `spectate/<session_code>/` URL |
| `games_website/routing.py` | Add Clue Rush WebSocket to routing |
| `QuizGame/views.py` | Add `quiz_spectate` view |
| `QuizGame/urls.py` | Add `spectate/<room_code>/` URL |
| `Assign/views.py` | Add `assign_spectate` view |
| `Assign/urls.py` | Add `spectate/<room_code>/` URL |
| `Estimation/views.py` | Add `estimation_spectate` view |
| `Estimation/urls.py` | Add `spectate/<room_code>/` URL |
| `where_is_this/views.py` | Add `where_spectate` view |
| `where_is_this/urls.py` | Add `spectate/<room_code>/` URL |
| `who_is_lying/views.py` | Add `who_spectate` view |
| `who_is_lying/urls.py` | Add `spectate/<room_code>/` URL |
| `who_is_that/views.py` | Add `who_that_spectate` view |
| `who_is_that/urls.py` | Add `spectate/<room_code>/` URL |
| `black_jack_quiz/views.py` | Add `blackjack_spectate` view |
| `black_jack_quiz/urls.py` | Add `spectate/<room_code>/` URL |
| `sorting_ladder/views.py` | Add `sorting_ladder_spectate` view |
| `sorting_ladder/urls.py` | Add `spectate/<room_code>/` URL |
| `clue_rush/views.py` | Add `clue_rush_spectate` view |
| `clue_rush/urls.py` | Add `spectate/<room_code>/` URL |

### New files
| File | Purpose |
|---|---|
| `templates/hub/spectate.html` | Hub-level spectator page (iframe container) |
| `templates/quiz/spectate.html` | Quiz spectator (MC questions read-only) |
| `templates/assign/spectate.html` | Assign spectator (drag-assign read-only) |
| `templates/estimation/spectate.html` | Estimation spectator (estimation read-only) |
| `templates/where_is_this/spectate.html` | Where spectator (image + text) |
| `templates/who_is_lying/spectate.html` | Who Is Lying spectator (statements) |
| `templates/who_is_that/spectate.html` | Who Is That spectator (image + name) |
| `templates/black_jack_quiz/spectate.html` | Blackjack spectator (MC read-only) |
| `templates/sorting_ladder/spectate.html` | Sorting Ladder spectator (items read-only) |
| `templates/clue_rush/spectate.html` | Clue Rush spectator (clues display) |

---

## Task 1: Hub-Infrastruktur — Button, View, URL

**Files:**
- Modify: `templates/hub/monitor.html:43-47`
- Modify: `games_hub/views.py` (after `monitor` function)
- Modify: `games_hub/urls.py`

- [ ] **Step 1: Button in monitor.html einfügen**

In `templates/hub/monitor.html`, nach dem `recallLobbyBtn`-Button (Zeile 43-45), neuen Button einfügen:

```html
                    <button id="spectateBtn" class="btn btn-outline-info" onclick="window.open('/hub/spectate/{{ session.code }}/', '_blank')">
                        <i data-lucide="monitor" class="me-1"></i> Spectator-View öffnen
                    </button>
```

Der vollständige Button-Block sieht danach so aus:
```html
                <div class="d-flex align-items-center gap-2 flex-wrap">
                    <div id="wsStatus" ...>...</div>
                    <a href="..." class="btn btn-outline-secondary">...</a>
                    {% if not session.started_at %}
                    <button id="startSessionBtn" class="btn btn-success">...</button>
                    {% endif %}
                    <button id="spectateBtn" class="btn btn-outline-info" onclick="window.open('/hub/spectate/{{ session.code }}/', '_blank')">
                        <i data-lucide="monitor" class="me-1"></i> Spectator-View öffnen
                    </button>
                    <button id="recallLobbyBtn" class="btn btn-outline-warning">...</button>
                    <button id="endSessionBtn" class="btn btn-outline-danger">...</button>
                </div>
```

- [ ] **Step 2: View in games_hub/views.py hinzufügen**

Nach der `monitor`-Funktion (ca. Zeile 393) einfügen:

```python
def spectate_session(request, session_code: str):
    """Read-only spectator view for a hub session. No participant registration."""
    session = get_object_or_404(HubSession, code=session_code)
    return render(request, 'hub/spectate.html', {
        'session': session,
        'session_code': session_code,
    })
```

- [ ] **Step 3: URL in games_hub/urls.py hinzufügen**

```python
path('spectate/<str:session_code>/', views.spectate_session, name='spectate_session'),
```

Die Datei sieht danach so aus:
```python
urlpatterns = [
    path('create/', views.create_session, name='create_session'),
    path('join/', views.join_session, name='join_session'),
    path('lobby/<str:session_code>/', views.lobby, name='lobby'),
    path('monitor/<str:session_code>/', views.monitor, name='monitor'),
    path('spectate/<str:session_code>/', views.spectate_session, name='spectate_session'),
    path('session/<str:session_code>/leaderboard/', views.session_leaderboard, name='session_leaderboard'),
    # API endpoints
    path('api/session/<str:session_code>/leaderboard/', views.session_leaderboard_api, name='session_leaderboard_api'),
    path('api/session/<str:session_code>/add-step/', views.add_step_to_session, name='add_step_to_session'),
    path('api/participant/score/', views.set_hub_participant_score, name='set_hub_participant_score'),
    path('api/games/<str:game_key>/questions/', views.get_available_questions, name='get_available_questions'),
    path('api/games/<str:game_key>/instances/', views.get_game_instances, name='get_game_instances'),
    path('api/session/<str:session_code>/reorder-steps/', views.reorder_steps, name='reorder_steps'),
    path('api/session/<str:session_code>/delete-step/<int:step_id>/', views.delete_step, name='delete_step'),
    path('api/session/<str:session_code>/vote/', views.submit_vote, name='submit_vote'),
    path('api/session/<str:session_code>/votes/', views.get_votes, name='get_votes'),
]
```

- [ ] **Step 4: Manuell testen**

Server starten: `python manage.py runserver` (oder Daphne)  
Monitor öffnen: `http://localhost:8000/hub/monitor/<session_code>/`  
Prüfen: Button "Spectator-View öffnen" sichtbar, Klick öffnet neuen Tab mit `/hub/spectate/<session_code>/` (404 normal, Template fehlt noch)

- [ ] **Step 5: Commit**

```bash
git add templates/hub/monitor.html games_hub/views.py games_hub/urls.py
git commit -m "feat: add spectator session button, view, and URL to hub"
```

---

## Task 2: Hub-Spectator Template

**Files:**
- Create: `templates/hub/spectate.html`

- [ ] **Step 1: Template erstellen**

```html
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Spectator · {{ session.name|default:session.code }}</title>
  <style>
    :root {
      --bg: #0b0f17;
      --panel: #0f172a;
      --text: #e5e7eb;
      --muted: #94a3b8;
      --primary: #22d3ee;
      --accent: #10b981;
    }
    @keyframes gradientBG {
      0% { background-position: 0% 50%; }
      50% { background-position: 100% 50%; }
      100% { background-position: 0% 50%; }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      min-height: 100vh;
      color: var(--text);
      background: linear-gradient(-45deg, #050816, #0b1026, #1a1f4a, #2b2f77);
      background-size: 400% 400%;
      animation: gradientBG 15s ease infinite;
      display: flex;
      flex-direction: column;
    }
    header {
      background: rgba(15, 23, 42, 0.85);
      border-bottom: 1px solid rgba(255,255,255,0.1);
      padding: 0.6rem 1.5rem;
      display: flex;
      align-items: center;
      gap: 1rem;
      flex-shrink: 0;
    }
    .session-name {
      font-size: 1rem;
      font-weight: 700;
      color: var(--primary);
    }
    .game-badge {
      font-size: 0.8rem;
      color: var(--muted);
      border: 1px solid rgba(148,163,184,.25);
      background: rgba(15,23,42,.5);
      padding: .25rem .6rem;
      border-radius: 999px;
    }
    .ws-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: #6b7280; display: inline-block;
      margin-left: auto; flex-shrink: 0;
    }
    .ws-dot.connected { background: var(--accent); }
    #waiting {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-direction: column;
      gap: 1rem;
      color: var(--muted);
    }
    .pulse {
      width: 60px; height: 60px; border-radius: 50%;
      border: 2px solid var(--primary);
      animation: pulse 2s ease-in-out infinite;
    }
    @keyframes pulse {
      0%, 100% { transform: scale(1); opacity: 1; }
      50% { transform: scale(1.15); opacity: 0.5; }
    }
    #gameFrame {
      flex: 1;
      border: none;
      width: 100%;
      display: none;
    }
    #endedMsg {
      flex: 1;
      display: none;
      align-items: center;
      justify-content: center;
      flex-direction: column;
      gap: 1rem;
      color: var(--muted);
    }
  </style>
</head>
<body>
  <header>
    <span class="session-name">{{ session.name|default:session.code }}</span>
    <span class="game-badge" id="gameBadge">Warte auf Spiel…</span>
    <span class="ws-dot" id="wsDot"></span>
  </header>

  <div id="waiting">
    <div class="pulse"></div>
    <p>Warte auf nächstes Spiel…</p>
  </div>

  <iframe id="gameFrame" src="" allowfullscreen></iframe>

  <div id="endedMsg">
    <p style="font-size:1.5rem;">🏁</p>
    <p>Session beendet.</p>
  </div>

  <script>
    const code = '{{ session_code }}';
    const wsDot = document.getElementById('wsDot');
    const gameBadge = document.getElementById('gameBadge');
    const waiting = document.getElementById('waiting');
    const gameFrame = document.getElementById('gameFrame');
    const endedMsg = document.getElementById('endedMsg');

    const spectatorRoutes = {
      quiz:           '/quiz/spectate/',
      assign:         '/assign/spectate/',
      estimation:     '/estimation/spectate/',
      where:          '/where/spectate/',
      who:            '/who/spectate/',
      who_that:       '/who-is-that/spectate/',
      blackjack:      '/blackjack/spectate/',
      sorting_ladder: '/sorting-ladder/spectate/',
      clue_rush:      '/clue-rush/spectate/',
    };

    const gameNames = {
      quiz: 'Quiz', assign: 'Assign', estimation: 'Estimation',
      where: 'Where Is This', who: 'Who Is Lying', who_that: 'Who Is That',
      blackjack: 'Black Jack', sorting_ladder: 'Sorting Ladder', clue_rush: 'Clue Rush',
    };

    const ws = new WebSocket(
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
      location.host + '/ws/hub/' + code + '/'
    );

    ws.onopen = () => {
      wsDot.classList.add('connected');
      // Do NOT send join — spectator only listens
    };

    ws.onclose = () => {
      wsDot.classList.remove('connected');
      setTimeout(() => location.reload(), 3000);
    };

    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);

      if (data.type === 'navigate' && data.step) {
        const step = data.step;
        const route = spectatorRoutes[step.game_key];
        if (route && step.room_code) {
          gameFrame.src = route + step.room_code + '/';
          gameFrame.style.display = 'block';
          waiting.style.display = 'none';
          const name = gameNames[step.game_key] || step.game_key;
          const title = step.title ? `${name} · ${step.title}` : name;
          gameBadge.textContent = title;
        }
      }

      if (data.type === 'session_ended') {
        gameFrame.style.display = 'none';
        waiting.style.display = 'none';
        endedMsg.style.display = 'flex';
        gameBadge.textContent = 'Session beendet';
      }

      if (data.type === 'recall_to_lobby') {
        waiting.style.display = 'flex';
        gameFrame.style.display = 'none';
        gameFrame.src = '';
        gameBadge.textContent = 'Warte auf Spiel…';
      }
    };
  </script>
</body>
</html>
```

- [ ] **Step 2: Manuell testen**

`http://localhost:8000/hub/spectate/<session_code>/` aufrufen.  
Prüfen: Seite lädt, WS verbindet (grüner Punkt), "Warte auf Spiel…" sichtbar.  
Im Admin-Monitor ein Spiel starten → iframe erscheint mit Game-Spectator-URL (404, Templates folgen).

- [ ] **Step 3: Commit**

```bash
git add templates/hub/spectate.html
git commit -m "feat: add hub spectator template with hub WS listener and iframe routing"
```

---

## Task 3: Clue Rush WebSocket Routing Fix

**Files:**
- Modify: `games_website/routing.py`

- [ ] **Step 1: Clue Rush Consumer zu Routing hinzufügen**

In `games_website/routing.py` Clue Rush importieren und einbinden:

```python
import os
from django.core.asgi import get_asgi_application
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'games_website.settings')

django_asgi_app = get_asgi_application()

from QuizGame.routing import websocket_urlpatterns as quiz_ws
from Estimation.routing import websocket_urlpatterns as estimation_ws
from Assign.routing import websocket_urlpatterns as assign_ws
from where_is_this.routing import websocket_urlpatterns as where_ws
from who_is_lying.routing import websocket_urlpatterns as who_ws
from who_is_that.routing import websocket_urlpatterns as who_that_ws
from black_jack_quiz.routing import websocket_urlpatterns as blackjack_ws
from games_hub.routing import websocket_urlpatterns as hub_ws
from sorting_ladder.routing import websocket_urlpatterns as sorting_ladder_ws
from clue_rush.routing import websocket_urlpatterns as clue_rush_ws

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(
                quiz_ws
                + estimation_ws
                + assign_ws
                + where_ws
                + who_ws
                + who_that_ws
                + blackjack_ws
                + hub_ws
                + sorting_ladder_ws
                + clue_rush_ws
            )
        )
    ),
})
```

- [ ] **Step 2: Commit**

```bash
git add games_website/routing.py
git commit -m "fix: add Clue Rush WebSocket consumer to routing"
```

---

## Task 4: Quiz Spectator

**Files:**
- Modify: `QuizGame/urls.py`
- Modify: `QuizGame/views.py`
- Create: `templates/quiz/spectate.html`

- [ ] **Step 1: URL in QuizGame/urls.py einfügen**

```python
path('spectate/<str:room_code>/', views.quiz_spectate, name='spectate'),
```

Die vollständige urls.py:
```python
from django.urls import path
from . import views

app_name = 'quiz'

urlpatterns = [
    path('join/', views.quiz_join_view, name='join'),
    path('check-room/<str:room_code>/', views.check_room_code, name='check_room'),
    path('play/<str:room_code>/<str:participant_name>/', views.quiz_play, name='play'),
    path('result/<str:room_code>/<str:participant_name>/', views.quiz_result, name='result'),
    path('spectate/<str:room_code>/', views.quiz_spectate, name='spectate'),
    path('submit-answer/<str:room_code>/<str:participant_name>/', views.submit_answer, name='submit_answer'),
    path('status/<str:room_code>/<str:participant_name>/', views.get_quiz_status, name='quiz_status'),
    path('leave/<str:room_code>/<str:participant_name>/', views.leave_quiz, name='leave_quiz'),
    path('api/<str:room_code>/participants/', views.api_quiz_participants, name='api_participants'),
    path('api/<str:room_code>/leaderboard/', views.api_quiz_leaderboard, name='api_leaderboard'),
]
```

- [ ] **Step 2: View in QuizGame/views.py einfügen**

Nach der `quiz_play`-Funktion (ca. Zeile 287):

```python
def quiz_spectate(request, room_code):
    """Read-only spectator view for a quiz game."""
    quiz = get_object_or_404(Quiz, room_code=room_code)
    return render(request, 'quiz/spectate.html', {'quiz': quiz})
```

- [ ] **Step 3: Template erstellen — templates/quiz/spectate.html**

```html
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Quiz Spectator</title>
  <style>
    :root {
      --bg: #0b0f17; --panel: #0f172a; --text: #e5e7eb;
      --muted: #94a3b8; --primary: #22d3ee; --accent: #10b981;
    }
    @keyframes gradientBG {
      0% { background-position: 0% 50%; }
      50% { background-position: 100% 50%; }
      100% { background-position: 0% 50%; }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      min-height: 100vh; color: var(--text);
      background: linear-gradient(-45deg, #050816, #0b1026, #1a1f4a, #2b2f77);
      background-size: 400% 400%;
      animation: gradientBG 15s ease infinite;
      display: flex; flex-direction: column; align-items: center;
      padding: 2rem 1rem;
    }
    .card {
      background: linear-gradient(180deg, rgba(17,24,39,.85), rgba(15,23,42,.85));
      border: 2px solid rgba(255,255,255,0.15);
      border-radius: 14px;
      padding: 2rem;
      max-width: 720px;
      width: 100%;
      box-shadow: 0 10px 30px rgba(0,0,0,.35);
    }
    .title {
      font-size: 1.1rem; font-weight: 700; color: var(--primary);
      margin-bottom: 1.5rem; text-align: center;
    }
    .waiting {
      text-align: center; color: var(--muted); padding: 3rem 0;
    }
    .question-text {
      font-size: clamp(1.1rem, 2.5vw, 1.6rem);
      font-weight: 600; line-height: 1.4;
      margin-bottom: 1.5rem;
    }
    .timer {
      font-size: 2.5rem; font-weight: 700;
      color: var(--primary); text-align: center;
      margin-bottom: 1.5rem;
    }
    .options { display: grid; grid-template-columns: 1fr 1fr; gap: .75rem; }
    .option {
      padding: 1rem 1.25rem;
      border-radius: 10px;
      border: 1px solid rgba(148,163,184,.25);
      background: rgba(15,23,42,.5);
      font-size: .95rem; font-weight: 500;
    }
    .option.correct {
      border-color: var(--accent);
      background: rgba(16,185,129,.15);
      color: var(--accent);
    }
    .answer-reveal {
      text-align: center; padding: 1rem;
      color: var(--accent); font-size: 1.1rem; font-weight: 600;
      border: 1px solid var(--accent);
      border-radius: 10px; margin-top: 1rem;
      display: none;
    }
    .ended { text-align: center; color: var(--muted); padding: 2rem 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="title">{{ quiz.title }}</div>

    <div id="waitingState" class="waiting">
      <p>Warte auf nächste Frage…</p>
    </div>

    <div id="questionState" style="display:none;">
      <div class="timer"><span id="timerDisplay">—</span></div>
      <div class="question-text" id="questionText"></div>
      <div class="options" id="optionsContainer"></div>
      <div class="answer-reveal" id="answerReveal"></div>
    </div>

    <div id="endedState" class="ended" style="display:none;">
      <p>Quiz beendet.</p>
    </div>
  </div>

  <script>
    const roomCode = '{{ quiz.room_code }}';
    const ws = new WebSocket(
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
      location.host + '/ws/quiz/' + roomCode + '/'
    );

    const waitingState = document.getElementById('waitingState');
    const questionState = document.getElementById('questionState');
    const endedState = document.getElementById('endedState');
    const questionText = document.getElementById('questionText');
    const optionsContainer = document.getElementById('optionsContainer');
    const timerDisplay = document.getElementById('timerDisplay');
    const answerReveal = document.getElementById('answerReveal');

    let timerInterval = null;

    function showWaiting() {
      waitingState.style.display = '';
      questionState.style.display = 'none';
      endedState.style.display = 'none';
    }
    function showQuestion() {
      waitingState.style.display = 'none';
      questionState.style.display = '';
      endedState.style.display = 'none';
    }
    function showEnded() {
      waitingState.style.display = 'none';
      questionState.style.display = 'none';
      endedState.style.display = '';
    }

    function startTimer(seconds) {
      if (timerInterval) clearInterval(timerInterval);
      let remaining = seconds;
      timerDisplay.textContent = remaining;
      timerInterval = setInterval(() => {
        remaining--;
        timerDisplay.textContent = Math.max(0, remaining);
        if (remaining <= 0) clearInterval(timerInterval);
      }, 1000);
    }

    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);

      if (data.type === 'quiz_started') {
        showWaiting();
      }

      if (data.type === 'question_started') {
        const q = data.question;
        answerReveal.style.display = 'none';
        questionText.textContent = q.question_text;

        // Render options
        optionsContainer.innerHTML = '';
        (q.options || []).forEach(opt => {
          const div = document.createElement('div');
          div.className = 'option';
          div.textContent = opt.text !== undefined ? opt.text : opt;
          optionsContainer.appendChild(div);
        });

        startTimer(q.time_limit || 30);
        showQuestion();
      }

      if (data.type === 'question_ended') {
        if (timerInterval) clearInterval(timerInterval);
        timerDisplay.textContent = '—';
        // Highlight correct answer if provided
        const correct = data.correct_answer;
        if (correct) {
          const correctText = correct.text !== undefined ? correct.text : correct;
          document.querySelectorAll('.option').forEach(opt => {
            if (opt.textContent === correctText) opt.classList.add('correct');
          });
          answerReveal.textContent = 'Richtige Antwort: ' + correctText;
          answerReveal.style.display = 'block';
        }
      }

      if (data.type === 'quiz_ended') {
        if (timerInterval) clearInterval(timerInterval);
        showEnded();
      }
    };

    ws.onclose = () => setTimeout(() => location.reload(), 3000);
  </script>
</body>
</html>
```

- [ ] **Step 4: Manuell testen**

`http://localhost:8000/quiz/spectate/<room_code>/` aufrufen.  
Prüfen: Seite lädt, WS verbindet.  
Im Monitor eine Quiz-Frage senden → Frage + Optionen erscheinen, Timer läuft.  
Frage beenden → korrekte Antwort wird grün hervorgehoben.

- [ ] **Step 5: Commit**

```bash
git add QuizGame/urls.py QuizGame/views.py templates/quiz/spectate.html
git commit -m "feat: add Quiz spectator view and template"
```

---

## Task 5: Assign Spectator

**Files:**
- Modify: `Assign/urls.py`
- Modify: `Assign/views.py`
- Create: `templates/assign/spectate.html`

WebSocket-Events: `quiz_started`, `question_started` (question_text, left_items, right_items, current_left_item, round_index, total_rounds), `round_advanced` (current_left_item, right_items, round_index), `question_rounds_complete`, `question_ended`, `quiz_ended`

- [ ] **Step 1: URL in Assign/urls.py einfügen**

```python
path('spectate/<str:room_code>/', views.assign_spectate, name='spectate'),
```

Die vollständige urls.py:
```python
from django.urls import path
from . import views

app_name = 'assign'

urlpatterns = [
    path('join/', views.assign_join_view, name='join'),
    path('check-room/<str:room_code>/', views.check_room_code, name='check_room'),
    path('play/<str:room_code>/<str:participant_name>/', views.assign_play, name='play'),
    path('result/<str:room_code>/<str:participant_name>/', views.assign_result, name='result'),
    path('spectate/<str:room_code>/', views.assign_spectate, name='spectate'),
    path('submit-answer/<str:room_code>/<str:participant_name>/', views.submit_answer, name='submit_answer'),
    path('status/<str:room_code>/<str:participant_name>/', views.get_quiz_status, name='quiz_status'),
    path('leave/<str:room_code>/<str:participant_name>/', views.leave_quiz, name='leave_quiz'),
    path('api/<str:room_code>/participants/', views.api_quiz_participants, name='api_participants'),
    path('api/<str:room_code>/leaderboard/', views.api_quiz_leaderboard, name='api_leaderboard'),
]
```

- [ ] **Step 2: View in Assign/views.py einfügen**

Nach der `assign_play`-Funktion:

```python
def assign_spectate(request, room_code):
    """Read-only spectator view for an Assign game."""
    from .models import AssignQuiz
    quiz = get_object_or_404(AssignQuiz, room_code=room_code)
    return render(request, 'assign/spectate.html', {'quiz': quiz})
```

- [ ] **Step 3: Template erstellen — templates/assign/spectate.html**

```html
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Assign Spectator</title>
  <style>
    :root {
      --bg: #0b0f17; --text: #e5e7eb; --muted: #94a3b8;
      --primary: #22d3ee; --accent: #10b981; --purple: #a78bfa;
    }
    @keyframes gradientBG {
      0% { background-position: 0% 50%; }
      50% { background-position: 100% 50%; }
      100% { background-position: 0% 50%; }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, sans-serif;
      min-height: 100vh; color: var(--text);
      background: linear-gradient(-45deg, #050816, #0b1026, #1a1f4a, #2b2f77);
      background-size: 400% 400%;
      animation: gradientBG 15s ease infinite;
      display: flex; flex-direction: column; align-items: center; padding: 2rem 1rem;
    }
    .card {
      background: linear-gradient(180deg, rgba(17,24,39,.85), rgba(15,23,42,.85));
      border: 2px solid rgba(255,255,255,0.15); border-radius: 14px;
      padding: 2rem; max-width: 860px; width: 100%;
    }
    .title { font-size: 1.1rem; font-weight: 700; color: var(--primary); margin-bottom: 1.5rem; text-align: center; }
    .waiting { text-align: center; color: var(--muted); padding: 3rem 0; }
    .question-text { font-size: clamp(1rem, 2vw, 1.4rem); font-weight: 600; margin-bottom: 0.5rem; }
    .round-info { font-size: .85rem; color: var(--muted); margin-bottom: 1.5rem; }
    .match-area { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-top: 1rem; }
    .match-col-title { font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: .6rem; }
    .left-item {
      padding: 1rem 1.25rem; border-radius: 10px;
      background: rgba(167,139,250,.15); border: 1px solid rgba(167,139,250,.4);
      font-weight: 600; font-size: 1.1rem; text-align: center;
    }
    .right-items { display: flex; flex-direction: column; gap: .6rem; }
    .right-item {
      padding: .75rem 1rem; border-radius: 8px;
      background: rgba(15,23,42,.5); border: 1px solid rgba(148,163,184,.2);
    }
    .ended { text-align: center; color: var(--muted); padding: 2rem 0; }
    .timer { font-size: 2rem; font-weight: 700; color: var(--primary); text-align: center; margin-bottom: 1rem; }
  </style>
</head>
<body>
  <div class="card">
    <div class="title">{{ quiz.title }}</div>
    <div id="waitingState" class="waiting"><p>Warte auf nächste Frage…</p></div>
    <div id="questionState" style="display:none;">
      <div class="timer"><span id="timerDisplay">—</span></div>
      <div class="question-text" id="questionText"></div>
      <div class="round-info" id="roundInfo"></div>
      <div class="match-area">
        <div>
          <div class="match-col-title">Zu zuordnen</div>
          <div class="left-item" id="leftItem">—</div>
        </div>
        <div>
          <div class="match-col-title">Optionen</div>
          <div class="right-items" id="rightItems"></div>
        </div>
      </div>
    </div>
    <div id="endedState" class="ended" style="display:none;"><p>Spiel beendet.</p></div>
  </div>

  <script>
    const roomCode = '{{ quiz.room_code }}';
    const ws = new WebSocket(
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
      location.host + '/ws/assign/' + roomCode + '/'
    );

    const waitingState = document.getElementById('waitingState');
    const questionState = document.getElementById('questionState');
    const endedState = document.getElementById('endedState');
    const questionText = document.getElementById('questionText');
    const roundInfo = document.getElementById('roundInfo');
    const leftItem = document.getElementById('leftItem');
    const rightItems = document.getElementById('rightItems');
    const timerDisplay = document.getElementById('timerDisplay');
    let timerInterval = null;

    function show(state) {
      waitingState.style.display = state === 'waiting' ? '' : 'none';
      questionState.style.display = state === 'question' ? '' : 'none';
      endedState.style.display = state === 'ended' ? '' : 'none';
    }

    function startTimer(seconds) {
      if (timerInterval) clearInterval(timerInterval);
      let remaining = seconds;
      timerDisplay.textContent = remaining;
      timerInterval = setInterval(() => {
        remaining--;
        timerDisplay.textContent = Math.max(0, remaining);
        if (remaining <= 0) clearInterval(timerInterval);
      }, 1000);
    }

    function renderRound(q) {
      questionText.textContent = q.question_text || '';
      roundInfo.textContent = `Runde ${(q.round_index ?? 0) + 1} von ${q.total_rounds ?? '?'}`;
      leftItem.textContent = q.current_left_item || '—';
      rightItems.innerHTML = '';
      (q.right_items || []).forEach(item => {
        const div = document.createElement('div');
        div.className = 'right-item';
        div.textContent = typeof item === 'string' ? item : (item.text || JSON.stringify(item));
        rightItems.appendChild(div);
      });
    }

    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'quiz_started') show('waiting');
      if (data.type === 'question_started') {
        renderRound(data.question);
        startTimer(data.question.time_limit || 60);
        show('question');
      }
      if (data.type === 'round_advanced') {
        leftItem.textContent = data.current_left_item || '—';
        rightItems.innerHTML = '';
        (data.right_items || []).forEach(item => {
          const div = document.createElement('div');
          div.className = 'right-item';
          div.textContent = typeof item === 'string' ? item : (item.text || JSON.stringify(item));
          rightItems.appendChild(div);
        });
        roundInfo.textContent = `Runde ${(data.round_index ?? 0) + 1}`;
      }
      if (data.type === 'question_rounds_complete' || data.type === 'question_ended') {
        if (timerInterval) clearInterval(timerInterval);
        timerDisplay.textContent = '—';
      }
      if (data.type === 'quiz_ended') {
        if (timerInterval) clearInterval(timerInterval);
        show('ended');
      }
    };

    ws.onclose = () => setTimeout(() => location.reload(), 3000);
    show('waiting');
  </script>
</body>
</html>
```

- [ ] **Step 4: Manuell testen**

`http://localhost:8000/assign/spectate/<room_code>/` aufrufen.  
Im Admin-Monitor eine Assign-Frage senden → linkes Item + rechte Optionen erscheinen.  
Nächste Runde → linkes Item wechselt.

- [ ] **Step 5: Commit**

```bash
git add Assign/urls.py Assign/views.py templates/assign/spectate.html
git commit -m "feat: add Assign spectator view and template"
```

---

## Task 6: Estimation Spectator

**Files:**
- Modify: `Estimation/urls.py`
- Modify: `Estimation/views.py`
- Create: `templates/estimation/spectate.html`

WebSocket: `ws/estimation/{room_code}/`  
Events: `question_started` (question_text, unit, unit_display, hint_text, time_limit), `question_ended`, `quiz_ended`

- [ ] **Step 1: URL in Estimation/urls.py einfügen**

```python
path('spectate/<str:room_code>/', views.estimation_spectate, name='spectate'),
```

- [ ] **Step 2: View in Estimation/views.py einfügen**

Nach der `estimation_play`-Funktion:

```python
def estimation_spectate(request, room_code):
    """Read-only spectator view for an Estimation game."""
    from .models import EstimationQuiz
    quiz = get_object_or_404(EstimationQuiz, room_code=room_code)
    return render(request, 'estimation/spectate.html', {'quiz': quiz})
```

- [ ] **Step 3: Template erstellen — templates/estimation/spectate.html**

```html
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Estimation Spectator</title>
  <style>
    :root { --text: #e5e7eb; --muted: #94a3b8; --primary: #22d3ee; --accent: #10b981; }
    @keyframes gradientBG {
      0% { background-position: 0% 50%; }
      50% { background-position: 100% 50%; }
      100% { background-position: 0% 50%; }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, sans-serif; min-height: 100vh; color: var(--text);
      background: linear-gradient(-45deg, #050816, #0b1026, #1a1f4a, #2b2f77);
      background-size: 400% 400%; animation: gradientBG 15s ease infinite;
      display: flex; flex-direction: column; align-items: center; padding: 2rem 1rem;
    }
    .card {
      background: linear-gradient(180deg, rgba(17,24,39,.85), rgba(15,23,42,.85));
      border: 2px solid rgba(255,255,255,0.15); border-radius: 14px;
      padding: 2rem; max-width: 720px; width: 100%;
    }
    .title { font-size: 1.1rem; font-weight: 700; color: var(--primary); margin-bottom: 1.5rem; text-align: center; }
    .waiting { text-align: center; color: var(--muted); padding: 3rem 0; }
    .question-text { font-size: clamp(1.1rem, 2.5vw, 1.8rem); font-weight: 600; line-height: 1.4; margin-bottom: 1rem; }
    .hint { font-size: .9rem; color: var(--muted); font-style: italic; margin-bottom: 1rem; }
    .unit-badge {
      display: inline-block; padding: .35rem .8rem; border-radius: 999px;
      background: rgba(34,211,238,.15); border: 1px solid rgba(34,211,238,.3);
      color: var(--primary); font-size: .85rem; font-weight: 600; margin-bottom: 1.5rem;
    }
    .timer { font-size: 2.5rem; font-weight: 700; color: var(--primary); text-align: center; margin-bottom: 1.5rem; }
    .ended { text-align: center; color: var(--muted); padding: 2rem 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="title">{{ quiz.title }}</div>
    <div id="waitingState" class="waiting"><p>Warte auf nächste Frage…</p></div>
    <div id="questionState" style="display:none;">
      <div class="timer"><span id="timerDisplay">—</span></div>
      <div class="question-text" id="questionText"></div>
      <div class="hint" id="hintText"></div>
      <div class="unit-badge" id="unitBadge"></div>
    </div>
    <div id="endedState" class="ended" style="display:none;"><p>Spiel beendet.</p></div>
  </div>

  <script>
    const roomCode = '{{ quiz.room_code }}';
    const ws = new WebSocket(
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
      location.host + '/ws/estimation/' + roomCode + '/'
    );

    const waitingState = document.getElementById('waitingState');
    const questionState = document.getElementById('questionState');
    const endedState = document.getElementById('endedState');
    let timerInterval = null;

    function show(state) {
      waitingState.style.display = state === 'waiting' ? '' : 'none';
      questionState.style.display = state === 'question' ? '' : 'none';
      endedState.style.display = state === 'ended' ? '' : 'none';
    }

    function startTimer(seconds) {
      if (timerInterval) clearInterval(timerInterval);
      let remaining = seconds;
      document.getElementById('timerDisplay').textContent = remaining;
      timerInterval = setInterval(() => {
        remaining--;
        document.getElementById('timerDisplay').textContent = Math.max(0, remaining);
        if (remaining <= 0) clearInterval(timerInterval);
      }, 1000);
    }

    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'quiz_started') show('waiting');
      if (data.type === 'question_started') {
        const q = data.question;
        document.getElementById('questionText').textContent = q.question_text;
        document.getElementById('hintText').textContent = q.hint_text ? 'Hinweis: ' + q.hint_text : '';
        document.getElementById('unitBadge').textContent = 'Einheit: ' + (q.unit_display || q.unit || '?');
        startTimer(q.time_limit || 90);
        show('question');
      }
      if (data.type === 'question_ended') {
        if (timerInterval) clearInterval(timerInterval);
        document.getElementById('timerDisplay').textContent = '—';
      }
      if (data.type === 'quiz_ended') { if (timerInterval) clearInterval(timerInterval); show('ended'); }
    };

    ws.onclose = () => setTimeout(() => location.reload(), 3000);
    show('waiting');
  </script>
</body>
</html>
```

- [ ] **Step 4: Manuell testen**

`http://localhost:8000/estimation/spectate/<room_code>/` aufrufen.  
Frage senden → Fragetext, Hinweis und Einheit erscheinen, Timer läuft.

- [ ] **Step 5: Commit**

```bash
git add Estimation/urls.py Estimation/views.py templates/estimation/spectate.html
git commit -m "feat: add Estimation spectator view and template"
```

---

## Task 7: Where Is This Spectator

**Files:**
- Modify: `where_is_this/urls.py`
- Modify: `where_is_this/views.py`
- Create: `templates/where_is_this/spectate.html`

WebSocket: `ws/where/{room_code}/`  
Events: `question_started` (question_text, image_url, hint_text, difficulty, points, time_limit), `question_ended`, `quiz_ended`

- [ ] **Step 1: URL in where_is_this/urls.py einfügen**

```python
path('spectate/<str:room_code>/', views.where_spectate, name='spectate'),
```

- [ ] **Step 2: View in where_is_this/views.py einfügen**

Nach der `where_play`-Funktion:

```python
def where_spectate(request, room_code):
    """Read-only spectator view for a Where Is This game."""
    from .models import WhereQuiz
    quiz = get_object_or_404(WhereQuiz, room_code=room_code)
    return render(request, 'where_is_this/spectate.html', {'quiz': quiz})
```

- [ ] **Step 3: Template erstellen — templates/where_is_this/spectate.html**

```html
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Where Is This Spectator</title>
  <style>
    :root { --text: #e5e7eb; --muted: #94a3b8; --primary: #22d3ee; }
    @keyframes gradientBG {
      0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, sans-serif; min-height: 100vh; color: var(--text);
      background: linear-gradient(-45deg, #050816, #0b1026, #1a1f4a, #2b2f77);
      background-size: 400% 400%; animation: gradientBG 15s ease infinite;
      display: flex; flex-direction: column; align-items: center; padding: 2rem 1rem;
    }
    .card {
      background: linear-gradient(180deg, rgba(17,24,39,.85), rgba(15,23,42,.85));
      border: 2px solid rgba(255,255,255,0.15); border-radius: 14px;
      padding: 2rem; max-width: 860px; width: 100%;
    }
    .title { font-size: 1.1rem; font-weight: 700; color: var(--primary); margin-bottom: 1.5rem; text-align: center; }
    .waiting { text-align: center; color: var(--muted); padding: 3rem 0; }
    .timer { font-size: 2rem; font-weight: 700; color: var(--primary); text-align: center; margin-bottom: 1rem; }
    .image-wrap { text-align: center; margin-bottom: 1.5rem; }
    .image-wrap img { max-width: 100%; max-height: 400px; border-radius: 10px; border: 1px solid rgba(255,255,255,.1); }
    .question-text { font-size: 1.3rem; font-weight: 600; text-align: center; margin-bottom: .75rem; }
    .hint { font-size: .9rem; color: var(--muted); font-style: italic; text-align: center; }
    .ended { text-align: center; color: var(--muted); padding: 2rem 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="title">{{ quiz.title }}</div>
    <div id="waitingState" class="waiting"><p>Warte auf nächste Frage…</p></div>
    <div id="questionState" style="display:none;">
      <div class="timer"><span id="timerDisplay">—</span></div>
      <div class="image-wrap"><img id="questionImage" src="" alt="" style="display:none;" /></div>
      <div class="question-text" id="questionText"></div>
      <div class="hint" id="hintText"></div>
    </div>
    <div id="endedState" class="ended" style="display:none;"><p>Spiel beendet.</p></div>
  </div>

  <script>
    const roomCode = '{{ quiz.room_code }}';
    const ws = new WebSocket(
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
      location.host + '/ws/where/' + roomCode + '/'
    );
    let timerInterval = null;

    function show(state) {
      document.getElementById('waitingState').style.display = state === 'waiting' ? '' : 'none';
      document.getElementById('questionState').style.display = state === 'question' ? '' : 'none';
      document.getElementById('endedState').style.display = state === 'ended' ? '' : 'none';
    }

    function startTimer(seconds) {
      if (timerInterval) clearInterval(timerInterval);
      let remaining = seconds;
      document.getElementById('timerDisplay').textContent = remaining;
      timerInterval = setInterval(() => {
        remaining--;
        document.getElementById('timerDisplay').textContent = Math.max(0, remaining);
        if (remaining <= 0) clearInterval(timerInterval);
      }, 1000);
    }

    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'quiz_started') show('waiting');
      if (data.type === 'question_started') {
        const q = data.question;
        document.getElementById('questionText').textContent = q.question_text || 'Wo ist das?';
        document.getElementById('hintText').textContent = q.hint_text ? 'Hinweis: ' + q.hint_text : '';
        const img = document.getElementById('questionImage');
        if (q.image_url) { img.src = q.image_url; img.style.display = ''; }
        else { img.style.display = 'none'; }
        startTimer(q.time_limit || 60);
        show('question');
      }
      if (data.type === 'question_ended') { if (timerInterval) clearInterval(timerInterval); document.getElementById('timerDisplay').textContent = '—'; }
      if (data.type === 'quiz_ended') { if (timerInterval) clearInterval(timerInterval); show('ended'); }
    };
    ws.onclose = () => setTimeout(() => location.reload(), 3000);
    show('waiting');
  </script>
</body>
</html>
```

- [ ] **Step 4: Manuell testen**

`http://localhost:8000/where/spectate/<room_code>/` aufrufen.  
Frage senden → Bild + Fragetext erscheinen.

- [ ] **Step 5: Commit**

```bash
git add where_is_this/urls.py where_is_this/views.py templates/where_is_this/spectate.html
git commit -m "feat: add Where Is This spectator view and template"
```

---

## Task 8: Who Is Lying Spectator

**Files:**
- Modify: `who_is_lying/urls.py`
- Modify: `who_is_lying/views.py`
- Create: `templates/who_is_lying/spectate.html`

WebSocket: `ws/who/{room_code}/`  
Events: `question_started` (statement, people: [{name, statement}], time_limit), `question_ended`, `quiz_ended`

- [ ] **Step 1: URL in who_is_lying/urls.py einfügen**

```python
path('spectate/<str:room_code>/', views.who_spectate, name='spectate'),
```

- [ ] **Step 2: View in who_is_lying/views.py einfügen**

Nach der `who_play`-Funktion:

```python
def who_spectate(request, room_code):
    """Read-only spectator view for a Who Is Lying game."""
    from .models import WhoQuiz
    quiz = get_object_or_404(WhoQuiz, room_code=room_code)
    return render(request, 'who_is_lying/spectate.html', {'quiz': quiz})
```

- [ ] **Step 3: Template erstellen — templates/who_is_lying/spectate.html**

```html
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Who Is Lying Spectator</title>
  <style>
    :root { --text: #e5e7eb; --muted: #94a3b8; --primary: #22d3ee; }
    @keyframes gradientBG {
      0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, sans-serif; min-height: 100vh; color: var(--text);
      background: linear-gradient(-45deg, #050816, #0b1026, #1a1f4a, #2b2f77);
      background-size: 400% 400%; animation: gradientBG 15s ease infinite;
      display: flex; flex-direction: column; align-items: center; padding: 2rem 1rem;
    }
    .card {
      background: linear-gradient(180deg, rgba(17,24,39,.85), rgba(15,23,42,.85));
      border: 2px solid rgba(255,255,255,0.15); border-radius: 14px;
      padding: 2rem; max-width: 720px; width: 100%;
    }
    .title { font-size: 1.1rem; font-weight: 700; color: var(--primary); margin-bottom: 1.5rem; text-align: center; }
    .waiting { text-align: center; color: var(--muted); padding: 3rem 0; }
    .timer { font-size: 2rem; font-weight: 700; color: var(--primary); text-align: center; margin-bottom: 1rem; }
    .topic { font-size: 1.2rem; font-weight: 600; text-align: center; margin-bottom: 1.5rem; }
    .people-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: .9rem; }
    .person-card {
      padding: 1rem; border-radius: 10px;
      background: rgba(15,23,42,.5); border: 1px solid rgba(148,163,184,.2);
    }
    .person-name { font-weight: 700; font-size: .9rem; color: var(--primary); margin-bottom: .4rem; }
    .person-statement { font-size: .85rem; color: var(--text); line-height: 1.4; }
    .ended { text-align: center; color: var(--muted); padding: 2rem 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="title">{{ quiz.title }}</div>
    <div id="waitingState" class="waiting"><p>Warte auf nächste Frage…</p></div>
    <div id="questionState" style="display:none;">
      <div class="timer"><span id="timerDisplay">—</span></div>
      <div class="topic" id="topicText"></div>
      <div class="people-grid" id="peopleGrid"></div>
    </div>
    <div id="endedState" class="ended" style="display:none;"><p>Spiel beendet.</p></div>
  </div>

  <script>
    const roomCode = '{{ quiz.room_code }}';
    const ws = new WebSocket(
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
      location.host + '/ws/who/' + roomCode + '/'
    );
    let timerInterval = null;

    function show(state) {
      document.getElementById('waitingState').style.display = state === 'waiting' ? '' : 'none';
      document.getElementById('questionState').style.display = state === 'question' ? '' : 'none';
      document.getElementById('endedState').style.display = state === 'ended' ? '' : 'none';
    }

    function startTimer(seconds) {
      if (timerInterval) clearInterval(timerInterval);
      let remaining = seconds;
      document.getElementById('timerDisplay').textContent = remaining;
      timerInterval = setInterval(() => {
        remaining--;
        document.getElementById('timerDisplay').textContent = Math.max(0, remaining);
        if (remaining <= 0) clearInterval(timerInterval);
      }, 1000);
    }

    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'quiz_started') show('waiting');
      if (data.type === 'question_started') {
        const q = data.question;
        document.getElementById('topicText').textContent = q.statement || '';
        const grid = document.getElementById('peopleGrid');
        grid.innerHTML = '';
        (q.people || []).forEach(p => {
          const card = document.createElement('div');
          card.className = 'person-card';
          card.innerHTML = `<div class="person-name">${p.name}</div><div class="person-statement">${p.statement}</div>`;
          grid.appendChild(card);
        });
        startTimer(q.time_limit || 60);
        show('question');
      }
      if (data.type === 'question_ended') { if (timerInterval) clearInterval(timerInterval); document.getElementById('timerDisplay').textContent = '—'; }
      if (data.type === 'quiz_ended') { if (timerInterval) clearInterval(timerInterval); show('ended'); }
    };
    ws.onclose = () => setTimeout(() => location.reload(), 3000);
    show('waiting');
  </script>
</body>
</html>
```

- [ ] **Step 4: Manuell testen**

`http://localhost:8000/who/spectate/<room_code>/` aufrufen.  
Frage senden → Thema + Personen-Karten mit Aussagen erscheinen.

- [ ] **Step 5: Commit**

```bash
git add who_is_lying/urls.py who_is_lying/views.py templates/who_is_lying/spectate.html
git commit -m "feat: add Who Is Lying spectator view and template"
```

---

## Task 9: Who Is That Spectator

**Files:**
- Modify: `who_is_that/urls.py`
- Modify: `who_is_that/views.py`
- Create: `templates/who_is_that/spectate.html`

WebSocket: `ws/who_that/{room_code}/`  
Events: `question_started` (question_text, image_url, hint_text, category, time_limit), `question_ended`, `quiz_ended`

- [ ] **Step 1: URL in who_is_that/urls.py einfügen**

```python
path('spectate/<str:room_code>/', views.who_that_spectate, name='spectate'),
```

- [ ] **Step 2: View in who_is_that/views.py einfügen**

Nach der `who_that_play`-Funktion:

```python
def who_that_spectate(request, room_code):
    """Read-only spectator view for a Who Is That game."""
    from .models import WhoThatQuiz
    quiz = get_object_or_404(WhoThatQuiz, room_code=room_code)
    return render(request, 'who_is_that/spectate.html', {'quiz': quiz})
```

- [ ] **Step 3: Template erstellen — templates/who_is_that/spectate.html**

```html
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Who Is That Spectator</title>
  <style>
    :root { --text: #e5e7eb; --muted: #94a3b8; --primary: #22d3ee; }
    @keyframes gradientBG {
      0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, sans-serif; min-height: 100vh; color: var(--text);
      background: linear-gradient(-45deg, #050816, #0b1026, #1a1f4a, #2b2f77);
      background-size: 400% 400%; animation: gradientBG 15s ease infinite;
      display: flex; flex-direction: column; align-items: center; padding: 2rem 1rem;
    }
    .card {
      background: linear-gradient(180deg, rgba(17,24,39,.85), rgba(15,23,42,.85));
      border: 2px solid rgba(255,255,255,0.15); border-radius: 14px;
      padding: 2rem; max-width: 720px; width: 100%; text-align: center;
    }
    .title { font-size: 1.1rem; font-weight: 700; color: var(--primary); margin-bottom: 1.5rem; }
    .waiting { color: var(--muted); padding: 3rem 0; }
    .timer { font-size: 2rem; font-weight: 700; color: var(--primary); margin-bottom: 1rem; }
    .image-wrap img { max-width: 100%; max-height: 400px; border-radius: 10px; border: 1px solid rgba(255,255,255,.1); margin-bottom: 1rem; }
    .question-text { font-size: 1.3rem; font-weight: 600; margin-bottom: .5rem; }
    .category { font-size: .8rem; color: var(--muted); }
    .hint { font-size: .9rem; color: var(--muted); font-style: italic; margin-top: .5rem; }
    .ended { color: var(--muted); padding: 2rem 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="title">{{ quiz.title }}</div>
    <div id="waitingState" class="waiting"><p>Warte auf nächste Frage…</p></div>
    <div id="questionState" style="display:none;">
      <div class="timer"><span id="timerDisplay">—</span></div>
      <div class="image-wrap"><img id="questionImage" src="" alt="" style="display:none;" /></div>
      <div class="category" id="categoryText"></div>
      <div class="question-text" id="questionText"></div>
      <div class="hint" id="hintText"></div>
    </div>
    <div id="endedState" class="ended" style="display:none;"><p>Spiel beendet.</p></div>
  </div>

  <script>
    const roomCode = '{{ quiz.room_code }}';
    const ws = new WebSocket(
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
      location.host + '/ws/who_that/' + roomCode + '/'
    );
    let timerInterval = null;

    function show(state) {
      document.getElementById('waitingState').style.display = state === 'waiting' ? '' : 'none';
      document.getElementById('questionState').style.display = state === 'question' ? '' : 'none';
      document.getElementById('endedState').style.display = state === 'ended' ? '' : 'none';
    }

    function startTimer(seconds) {
      if (timerInterval) clearInterval(timerInterval);
      let remaining = seconds;
      document.getElementById('timerDisplay').textContent = remaining;
      timerInterval = setInterval(() => {
        remaining--;
        document.getElementById('timerDisplay').textContent = Math.max(0, remaining);
        if (remaining <= 0) clearInterval(timerInterval);
      }, 1000);
    }

    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'quiz_started') show('waiting');
      if (data.type === 'question_started') {
        const q = data.question;
        document.getElementById('questionText').textContent = q.question_text || 'Wer ist das?';
        document.getElementById('categoryText').textContent = q.category ? 'Kategorie: ' + q.category : '';
        document.getElementById('hintText').textContent = q.hint_text ? 'Hinweis: ' + q.hint_text : '';
        const img = document.getElementById('questionImage');
        if (q.image_url) { img.src = q.image_url; img.style.display = ''; } else { img.style.display = 'none'; }
        startTimer(q.time_limit || 60);
        show('question');
      }
      if (data.type === 'question_ended') { if (timerInterval) clearInterval(timerInterval); document.getElementById('timerDisplay').textContent = '—'; }
      if (data.type === 'quiz_ended') { if (timerInterval) clearInterval(timerInterval); show('ended'); }
    };
    ws.onclose = () => setTimeout(() => location.reload(), 3000);
    show('waiting');
  </script>
</body>
</html>
```

- [ ] **Step 4: Manuell testen**

`http://localhost:8000/who-is-that/spectate/<room_code>/` aufrufen.  
Frage senden → Bild + Name-Frage erscheinen.

- [ ] **Step 5: Commit**

```bash
git add who_is_that/urls.py who_is_that/views.py templates/who_is_that/spectate.html
git commit -m "feat: add Who Is That spectator view and template"
```

---

## Task 10: Blackjack Spectator

**Files:**
- Modify: `black_jack_quiz/urls.py`
- Modify: `black_jack_quiz/views.py`
- Create: `templates/black_jack_quiz/spectate.html`

WebSocket: `ws/blackjack/{room_code}/`  
Events: `question_started` (question_text, time_limit, question_number), `question_ended`, `quiz_ended`

Note: Blackjack-Fragen haben keine Optionen im `question_started` Event (Antwort wird per Freitext eingegeben). Die Spectator-Ansicht zeigt nur den Fragetext + Timer.

- [ ] **Step 1: URL in black_jack_quiz/urls.py einfügen**

```python
path('spectate/<str:room_code>/', views.blackjack_spectate, name='spectate'),
```

- [ ] **Step 2: View in black_jack_quiz/views.py einfügen**

Nach der `blackjack_play`-Funktion:

```python
def blackjack_spectate(request, room_code):
    """Read-only spectator view for a Blackjack game."""
    from .models import BlackJackQuiz
    quiz = get_object_or_404(BlackJackQuiz, room_code=room_code)
    return render(request, 'black_jack_quiz/spectate.html', {'quiz': quiz})
```

- [ ] **Step 3: Template erstellen — templates/black_jack_quiz/spectate.html**

```html
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blackjack Spectator</title>
  <style>
    :root { --text: #e5e7eb; --muted: #94a3b8; --primary: #22d3ee; }
    @keyframes gradientBG {
      0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, sans-serif; min-height: 100vh; color: var(--text);
      background: linear-gradient(-45deg, #050816, #0b1026, #1a1f4a, #2b2f77);
      background-size: 400% 400%; animation: gradientBG 15s ease infinite;
      display: flex; flex-direction: column; align-items: center; padding: 2rem 1rem;
    }
    .card {
      background: linear-gradient(180deg, rgba(17,24,39,.85), rgba(15,23,42,.85));
      border: 2px solid rgba(255,255,255,0.15); border-radius: 14px;
      padding: 2rem; max-width: 720px; width: 100%;
    }
    .title { font-size: 1.1rem; font-weight: 700; color: var(--primary); margin-bottom: 1.5rem; text-align: center; }
    .waiting { text-align: center; color: var(--muted); padding: 3rem 0; }
    .timer { font-size: 2.5rem; font-weight: 700; color: var(--primary); text-align: center; margin-bottom: 1.5rem; }
    .q-num { font-size: .85rem; color: var(--muted); text-align: center; margin-bottom: .5rem; }
    .question-text { font-size: clamp(1.1rem, 2.5vw, 1.6rem); font-weight: 600; line-height: 1.4; text-align: center; }
    .ended { text-align: center; color: var(--muted); padding: 2rem 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="title">{{ quiz.title }}</div>
    <div id="waitingState" class="waiting"><p>Warte auf nächste Frage…</p></div>
    <div id="questionState" style="display:none;">
      <div class="timer"><span id="timerDisplay">—</span></div>
      <div class="q-num" id="qNum"></div>
      <div class="question-text" id="questionText"></div>
    </div>
    <div id="endedState" class="ended" style="display:none;"><p>Spiel beendet.</p></div>
  </div>

  <script>
    const roomCode = '{{ quiz.room_code }}';
    const ws = new WebSocket(
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
      location.host + '/ws/blackjack/' + roomCode + '/'
    );
    let timerInterval = null;

    function show(state) {
      document.getElementById('waitingState').style.display = state === 'waiting' ? '' : 'none';
      document.getElementById('questionState').style.display = state === 'question' ? '' : 'none';
      document.getElementById('endedState').style.display = state === 'ended' ? '' : 'none';
    }

    function startTimer(seconds) {
      if (timerInterval) clearInterval(timerInterval);
      let remaining = seconds;
      document.getElementById('timerDisplay').textContent = remaining;
      timerInterval = setInterval(() => {
        remaining--;
        document.getElementById('timerDisplay').textContent = Math.max(0, remaining);
        if (remaining <= 0) clearInterval(timerInterval);
      }, 1000);
    }

    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'quiz_started') show('waiting');
      if (data.type === 'question_started') {
        const q = data.question;
        document.getElementById('questionText').textContent = q.question_text;
        document.getElementById('qNum').textContent = q.question_number ? 'Frage ' + q.question_number : '';
        startTimer(q.time_limit || 30);
        show('question');
      }
      if (data.type === 'question_ended') { if (timerInterval) clearInterval(timerInterval); document.getElementById('timerDisplay').textContent = '—'; }
      if (data.type === 'quiz_ended') { if (timerInterval) clearInterval(timerInterval); show('ended'); }
    };
    ws.onclose = () => setTimeout(() => location.reload(), 3000);
    show('waiting');
  </script>
</body>
</html>
```

- [ ] **Step 4: Manuell testen**

`http://localhost:8000/blackjack/spectate/<room_code>/` aufrufen.  
Frage senden → Fragetext + Timer erscheinen.

- [ ] **Step 5: Commit**

```bash
git add black_jack_quiz/urls.py black_jack_quiz/views.py templates/black_jack_quiz/spectate.html
git commit -m "feat: add Blackjack spectator view and template"
```

---

## Task 11: Sorting Ladder Spectator

**Files:**
- Modify: `sorting_ladder/urls.py`
- Modify: `sorting_ladder/views.py`
- Create: `templates/sorting_ladder/spectate.html`

WebSocket: `ws/sorting-ladder/{room_code}/`  
Events: `question_started` (payload direkt im event: placed_elements [{id, text}], active_element, current_round, time_limit_seconds), `question_ended`, `quiz_ended`

Note: Das `question_started`-Event hat hier eine andere Struktur — die Felder kommen direkt im Event (via `**payload`), nicht unter einem `question`-Key.

- [ ] **Step 1: URL in sorting_ladder/urls.py einfügen**

```python
path('spectate/<str:room_code>/', views.sorting_ladder_spectate, name='spectate'),
```

- [ ] **Step 2: View in sorting_ladder/views.py einfügen**

Nach der `play`-Funktion:

```python
def sorting_ladder_spectate(request, room_code):
    """Read-only spectator view for a Sorting Ladder game."""
    from .models import SortingLadderGame
    quiz = get_object_or_404(SortingLadderGame, room_code=room_code)
    return render(request, 'sorting_ladder/spectate.html', {'quiz': quiz})
```

- [ ] **Step 3: Template erstellen — templates/sorting_ladder/spectate.html**

```html
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Sorting Ladder Spectator</title>
  <style>
    :root { --text: #e5e7eb; --muted: #94a3b8; --primary: #22d3ee; --purple: #a78bfa; }
    @keyframes gradientBG {
      0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, sans-serif; min-height: 100vh; color: var(--text);
      background: linear-gradient(-45deg, #050816, #0b1026, #1a1f4a, #2b2f77);
      background-size: 400% 400%; animation: gradientBG 15s ease infinite;
      display: flex; flex-direction: column; align-items: center; padding: 2rem 1rem;
    }
    .card {
      background: linear-gradient(180deg, rgba(17,24,39,.85), rgba(15,23,42,.85));
      border: 2px solid rgba(255,255,255,0.15); border-radius: 14px;
      padding: 2rem; max-width: 720px; width: 100%;
    }
    .title { font-size: 1.1rem; font-weight: 700; color: var(--primary); margin-bottom: 1.5rem; text-align: center; }
    .waiting { text-align: center; color: var(--muted); padding: 3rem 0; }
    .timer { font-size: 2rem; font-weight: 700; color: var(--primary); text-align: center; margin-bottom: 1rem; }
    .section-label { font-size: .8rem; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: .5rem; margin-top: 1rem; }
    .ladder-list { list-style: none; display: flex; flex-direction: column; gap: .5rem; }
    .ladder-item {
      padding: .75rem 1rem; border-radius: 8px;
      background: rgba(15,23,42,.5); border: 1px solid rgba(148,163,184,.2);
      display: flex; align-items: center; gap: .75rem;
    }
    .ladder-rank { font-weight: 700; color: var(--muted); min-width: 1.5rem; text-align: center; }
    .ended { text-align: center; color: var(--muted); padding: 2rem 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="title">{{ quiz.title }}</div>
    <div id="waitingState" class="waiting"><p>Warte auf nächste Frage…</p></div>
    <div id="questionState" style="display:none;">
      <div class="timer"><span id="timerDisplay">—</span></div>
      <div class="section-label">Aktuelle Leiter (Runde <span id="roundNum">1</span>)</div>
      <ul class="ladder-list" id="ladderList"></ul>
    </div>
    <div id="endedState" class="ended" style="display:none;"><p>Spiel beendet.</p></div>
  </div>

  <script>
    const roomCode = '{{ quiz.room_code }}';
    const ws = new WebSocket(
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
      location.host + '/ws/sorting-ladder/' + roomCode + '/'
    );
    let timerInterval = null;

    function show(state) {
      document.getElementById('waitingState').style.display = state === 'waiting' ? '' : 'none';
      document.getElementById('questionState').style.display = state === 'question' ? '' : 'none';
      document.getElementById('endedState').style.display = state === 'ended' ? '' : 'none';
    }

    function startTimer(seconds) {
      if (timerInterval) clearInterval(timerInterval);
      let remaining = seconds;
      document.getElementById('timerDisplay').textContent = remaining;
      timerInterval = setInterval(() => {
        remaining--;
        document.getElementById('timerDisplay').textContent = Math.max(0, remaining);
        if (remaining <= 0) clearInterval(timerInterval);
      }, 1000);
    }

    function renderLadder(placed) {
      const list = document.getElementById('ladderList');
      list.innerHTML = '';
      (placed || []).forEach((item, i) => {
        const li = document.createElement('li');
        li.className = 'ladder-item';
        li.innerHTML = `<span class="ladder-rank">${i === 0 ? '▲' : i === placed.length - 1 ? '▼' : '·'}</span>${item.text}`;
        list.appendChild(li);
      });
    }

    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'quiz_started') show('waiting');
      if (data.type === 'question_started') {
        // Sorting Ladder: payload fields are at top level (placed_elements, current_round, time_limit_seconds)
        renderLadder(data.placed_elements);
        document.getElementById('roundNum').textContent = data.current_round || 1;
        startTimer(data.time_limit_seconds || 30);
        show('question');
      }
      if (data.type === 'round_result' || data.type === 'round_started') {
        // Update ladder if new placed_elements provided
        if (data.placed_elements) renderLadder(data.placed_elements);
        if (data.current_round) document.getElementById('roundNum').textContent = data.current_round;
      }
      if (data.type === 'question_ended') { if (timerInterval) clearInterval(timerInterval); document.getElementById('timerDisplay').textContent = '—'; }
      if (data.type === 'quiz_ended') { if (timerInterval) clearInterval(timerInterval); show('ended'); }
    };
    ws.onclose = () => setTimeout(() => location.reload(), 3000);
    show('waiting');
  </script>
</body>
</html>
```

- [ ] **Step 4: Manuell testen**

`http://localhost:8000/sorting-ladder/spectate/<room_code>/` aufrufen.  
Frage senden → Leiter mit Elementen erscheint, Timer läuft.

- [ ] **Step 5: Commit**

```bash
git add sorting_ladder/urls.py sorting_ladder/views.py templates/sorting_ladder/spectate.html
git commit -m "feat: add Sorting Ladder spectator view and template"
```

---

## Task 12: Clue Rush Spectator

**Files:**
- Modify: `clue_rush/urls.py`
- Modify: `clue_rush/views.py`
- Create: `templates/clue_rush/spectate.html`

WebSocket: `ws/clue-rush/{room_code}/`  
Events: `question_started` (question_text, time_limit, points), `clue_started` (clue: {text, order, ...}), `clue_sequence_completed`, `question_ended`, `quiz_ended`

- [ ] **Step 1: URL in clue_rush/urls.py einfügen**

```python
path('spectate/<str:room_code>/', views.clue_rush_spectate, name='spectate'),
```

- [ ] **Step 2: View in clue_rush/views.py einfügen**

Nach der `play`-Funktion:

```python
def clue_rush_spectate(request, room_code):
    """Read-only spectator view for a Clue Rush game."""
    from .models import ClueRushGame
    quiz = get_object_or_404(ClueRushGame, room_code=room_code)
    return render(request, 'clue_rush/spectate.html', {'quiz': quiz})
```

- [ ] **Step 3: Template erstellen — templates/clue_rush/spectate.html**

```html
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Clue Rush Spectator</title>
  <style>
    :root { --text: #e5e7eb; --muted: #94a3b8; --primary: #22d3ee; --accent: #10b981; }
    @keyframes gradientBG {
      0% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } 100% { background-position: 0% 50%; }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, sans-serif; min-height: 100vh; color: var(--text);
      background: linear-gradient(-45deg, #050816, #0b1026, #1a1f4a, #2b2f77);
      background-size: 400% 400%; animation: gradientBG 15s ease infinite;
      display: flex; flex-direction: column; align-items: center; padding: 2rem 1rem;
    }
    .card {
      background: linear-gradient(180deg, rgba(17,24,39,.85), rgba(15,23,42,.85));
      border: 2px solid rgba(255,255,255,0.15); border-radius: 14px;
      padding: 2rem; max-width: 720px; width: 100%;
    }
    .title { font-size: 1.1rem; font-weight: 700; color: var(--primary); margin-bottom: 1.5rem; text-align: center; }
    .waiting { text-align: center; color: var(--muted); padding: 3rem 0; }
    .timer { font-size: 2rem; font-weight: 700; color: var(--primary); text-align: center; margin-bottom: 1rem; }
    .question-text { font-size: 1.3rem; font-weight: 600; margin-bottom: 1.5rem; text-align: center; }
    .clues-label { font-size: .8rem; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: .6rem; }
    .clues-list { list-style: none; display: flex; flex-direction: column; gap: .5rem; }
    .clue-item {
      padding: .75rem 1rem; border-radius: 8px;
      background: rgba(34,211,238,.08); border: 1px solid rgba(34,211,238,.2);
      display: flex; gap: .75rem; align-items: flex-start;
    }
    .clue-num { font-size: .75rem; color: var(--primary); font-weight: 700; min-width: 1.5rem; }
    .clue-text { font-size: .95rem; }
    .ended { text-align: center; color: var(--muted); padding: 2rem 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="title">{{ quiz.title }}</div>
    <div id="waitingState" class="waiting"><p>Warte auf nächste Frage…</p></div>
    <div id="questionState" style="display:none;">
      <div class="timer"><span id="timerDisplay">—</span></div>
      <div class="question-text" id="questionText"></div>
      <div class="clues-label">Hinweise bisher</div>
      <ul class="clues-list" id="cluesList"></ul>
    </div>
    <div id="endedState" class="ended" style="display:none;"><p>Spiel beendet.</p></div>
  </div>

  <script>
    const roomCode = '{{ quiz.room_code }}';
    const ws = new WebSocket(
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
      location.host + '/ws/clue-rush/' + roomCode + '/'
    );
    let timerInterval = null;
    let clueCount = 0;

    function show(state) {
      document.getElementById('waitingState').style.display = state === 'waiting' ? '' : 'none';
      document.getElementById('questionState').style.display = state === 'question' ? '' : 'none';
      document.getElementById('endedState').style.display = state === 'ended' ? '' : 'none';
    }

    function startTimer(seconds) {
      if (timerInterval) clearInterval(timerInterval);
      let remaining = seconds;
      document.getElementById('timerDisplay').textContent = remaining;
      timerInterval = setInterval(() => {
        remaining--;
        document.getElementById('timerDisplay').textContent = Math.max(0, remaining);
        if (remaining <= 0) clearInterval(timerInterval);
      }, 1000);
    }

    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'quiz_started') show('waiting');
      if (data.type === 'question_started') {
        const q = data.question;
        document.getElementById('questionText').textContent = q.question_text;
        document.getElementById('cluesList').innerHTML = '';
        clueCount = 0;
        startTimer(q.time_limit || 60);
        show('question');
      }
      if (data.type === 'clue_started') {
        clueCount++;
        const clue = data.clue;
        const li = document.createElement('li');
        li.className = 'clue-item';
        const clueText = typeof clue === 'string' ? clue : (clue.text || JSON.stringify(clue));
        li.innerHTML = `<span class="clue-num">#${clueCount}</span><span class="clue-text">${clueText}</span>`;
        document.getElementById('cluesList').appendChild(li);
      }
      if (data.type === 'question_ended' || data.type === 'clue_sequence_completed') {
        if (timerInterval) clearInterval(timerInterval);
        document.getElementById('timerDisplay').textContent = '—';
      }
      if (data.type === 'quiz_ended') { if (timerInterval) clearInterval(timerInterval); show('ended'); }
    };
    ws.onclose = () => setTimeout(() => location.reload(), 3000);
    show('waiting');
  </script>
</body>
</html>
```

- [ ] **Step 4: Manuell testen**

`http://localhost:8000/clue-rush/spectate/<room_code>/` aufrufen.  
Frage senden → Fragetext erscheint, Hinweise werden live eingeblendet sobald der Admin sie freigibt.

- [ ] **Step 5: Commit**

```bash
git add clue_rush/urls.py clue_rush/views.py templates/clue_rush/spectate.html
git commit -m "feat: add Clue Rush spectator view and template"
```

---

## Task 13: Abschluss-Test Spectator Hub → iframe

- [ ] **Step 1: End-to-End Test**

1. Server starten: `python -m daphne -b 0.0.0.0 -p 8000 games_website.routing:application`
2. Hub-Session im Admin anlegen und Monitor öffnen
3. Im Monitor "Spectator-View öffnen" klicken → neuer Tab mit `/hub/spectate/<code>/`
4. Spectator-Seite zeigt "Warte auf nächstes Spiel…" mit WS-Verbindung (grüner Punkt)
5. Im Admin ein Quiz starten → Spectator zeigt Quiz-iframe automatisch
6. Im Admin eine Quiz-Frage senden → Frage erscheint im iframe
7. Frage beenden → korrekte Antwort grün hervorgehoben
8. Nächstes Spiel starten (z.B. Assign) → iframe wechselt auf Assign-Spectator
9. Session beenden → Spectator zeigt "Session beendet"

- [ ] **Step 2: Commit (falls ausstehende Änderungen)**

```bash
git status
# Nur wenn noch ungestaged:
git add .
git commit -m "feat: complete spectator mode implementation"
```
