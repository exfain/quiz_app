# Assign Round-Based Elimination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repariere den rundenbasierten Assign-Modus so dass falsche Antworten zur Eliminierung führen, Antworten korrekt in der DB gespeichert werden, und Auto-Advance nur aktive (nicht eliminierte) Teilnehmer zählt.

**Architecture:** Drei Bugs werden behoben: (1) fehlender `participant_submit_answer`-Handler im Backend → Scores werden nie gespeichert; (2) `get_active_participant_count` zählt auch eliminierte Teilnehmer → Auto-Advance hängt; (3) `onTimeUp` sendet keine Antwort wenn kein Item gezogen wurde → Teilnehmer bleibt hängen. Fix: neues class-level Dict `_eliminated_participants` trackt ausgeschiedene Channels, `save_participant_answer` übernimmt die bewährte DB-Logik des Kollegen.

**Tech Stack:** Django Channels (AsyncWebsocketConsumer), Django ORM (database_sync_to_async), Vanilla JS

---

## Betroffene Dateien

- **Modify:** `Assign/consumers.py` — Bug-Fixes für Eliminierung, Auto-Advance und DB-Speicherung
- **Modify:** `templates/assign/play.html` — `onTimeUp` sendet leere Antwort bei kein Drop
- **Test:** `Assign/tests.py` — Unit-Tests für `check_round_answer` und `save_participant_answer`-Logik

---

## Task 1: Eliminierungs-Tracking und `participant_submit_answer`-Handler

**Files:**
- Modify: `Assign/consumers.py`

### Schritt 1: `AssignAnswer` zum Import hinzufügen

- [ ] In `Assign/consumers.py` Zeile 6 ändern:

```python
from .models import AssignQuiz, AssignParticipant, AssignQuestion, AssignAnswer
```

### Schritt 2: `_eliminated_participants` class-variable hinzufügen

- [ ] In `Assign/consumers.py` nach Zeile 20 (nach `_effective_time_limits`) einfügen:

```python
    # Tracks eliminated participant channels per room
    _eliminated_participants: dict[str, set] = {}
```

### Schritt 3: `participant_submit_answer` in `receive()` registrieren

- [ ] In `Assign/consumers.py` in `receive()`, nach der Zeile `elif message_type == 'participant_check_round':` (Zeile 74) einfügen:

```python
            elif message_type == 'participant_submit_answer':
                await self.handle_participant_submit_answer(text_data_json)
```

### Schritt 4: `disconnect()` um Eliminierungs-Cleanup erweitern

- [ ] In `Assign/consumers.py` in `disconnect()`, nach dem Block für `_channel_participants.pop` (nach Zeile 53) hinzufügen:

```python
        # Eliminated-Tracking bereinigen
        if self.room_code in self.__class__._eliminated_participants:
            self.__class__._eliminated_participants[self.room_code].discard(self.channel_name)
```

### Schritt 5: Reset von `_eliminated_participants` bei neuer Frage

- [ ] In `Assign/consumers.py` in `handle_admin_send_question()`, nach dem Block der `_round_submissions`/`_auto_advancing` resets (nach Zeile 147) einfügen:

```python
        # Eliminierte Teilnehmer für neue Frage zurücksetzen
        self.__class__._eliminated_participants[self.room_code] = set()
```

### Schritt 6: `handle_participant_check_round` durch korrigierte Version ersetzen

- [ ] Die gesamte Methode `handle_participant_check_round` (Zeilen 277–331) ersetzen:

```python
    async def handle_participant_check_round(self, data):
        """Prüft die Zuordnung für eine einzelne Runde. Eliminiert Teilnehmer bei falscher Antwort."""
        # Bereits ausgeschiedene Teilnehmer ignorieren
        if self.channel_name in self.__class__._eliminated_participants.get(self.room_code, set()):
            return

        round_index = data.get('round_index', 0)
        user_match = data.get('user_match', {})  # {str(left_idx): shuffled_right_pos}

        quiz = await self.get_quiz()
        if not quiz or not quiz.current_question:
            return

        is_correct = await self.check_round_answer(quiz.current_question, round_index, user_match)

        if not is_correct:
            # Teilnehmer als ausgeschieden markieren
            if self.room_code not in self.__class__._eliminated_participants:
                self.__class__._eliminated_participants[self.room_code] = set()
            self.__class__._eliminated_participants[self.room_code].add(self.channel_name)

        await self.send(text_data=json.dumps({
            'type': 'round_checked',
            'is_correct': is_correct,
            'round_index': round_index,
            'eliminated': not is_correct,
        }))

        # Live-Response an Admin broadcasten
        participant_name = self.__class__._channel_participants.get(self.channel_name, '?')
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'participant_answered',
                'answer': {
                    'participant_name': participant_name,
                    'correct_matches': 1 if is_correct else 0,
                    'total_matches': 1,
                    'accuracy': 100 if is_correct else 0,
                    'points_earned': 0,
                    'time_taken': None,
                }
            }
        )

        if not is_correct:
            return  # Ausgeschieden — kein Auto-Advance-Tracking

        # Auto-advance: nur auslösen wenn round_index mit aktuellem Serverstand übereinstimmt
        current_server_round = await self.get_current_round_index(quiz.id)
        if current_server_round != round_index:
            return

        key = (self.room_code, round_index)
        if key not in self.__class__._round_submissions:
            self.__class__._round_submissions[key] = set()
        self.__class__._round_submissions[key].add(self.channel_name)

        active_count = await self.get_active_participant_count()
        submitted_count = len(self.__class__._round_submissions[key])
        advance_key = f'{self.room_code}_{round_index}'
        if active_count > 0 and submitted_count >= active_count and advance_key not in self.__class__._auto_advancing:
            self.__class__._auto_advancing.add(advance_key)
            del self.__class__._round_submissions[key]
            await asyncio.sleep(2)
            self.__class__._auto_advancing.discard(advance_key)
            await self.handle_admin_next_round({})
```

### Schritt 7: `handle_participant_submit_answer` Methode hinzufügen

- [ ] Nach `handle_participant_check_round` (vor `handle_participant_join`) einfügen:

```python
    async def handle_participant_submit_answer(self, data):
        """Speichert alle gesammelten Runden-Antworten als AssignAnswer in der DB."""
        participant_name = data.get('participant_name')
        hub_session = data.get('hub_session')
        user_matches = data.get('user_matches', {})
        time_taken = data.get('time_taken', 0)

        answer = await self.save_participant_answer(
            participant_name, hub_session, user_matches, time_taken
        )

        if answer:
            await self.send(text_data=json.dumps({
                'type': 'answer_submitted',
                'message': 'Answer submitted successfully',
                'points_earned': answer['points_earned'],
                'correct_matches': answer['correct_matches'],
                'total_matches': answer['total_matches'],
                'accuracy': answer['accuracy']
            }))

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'participant_answered',
                    'answer': {
                        'participant_name': participant_name,
                        'points_earned': answer['points_earned'],
                        'correct_matches': answer['correct_matches'],
                        'total_matches': answer['total_matches'],
                        'time_taken': time_taken,
                        'accuracy': answer['accuracy']
                    }
                }
            )
```

### Schritt 8: `get_active_participant_count` korrigieren

- [ ] Die Methode `get_active_participant_count` (Zeilen 712–715) ersetzen:

```python
    async def get_active_participant_count(self):
        """Anzahl aktiver Teilnehmer-Channels: verbunden UND nicht ausgeschieden."""
        channels = self.__class__._participant_channels.get(self.room_code, set())
        eliminated = self.__class__._eliminated_participants.get(self.room_code, set())
        return len(channels - eliminated)
```

### Schritt 9: `save_participant_answer` DB-Methode hinzufügen

- [ ] Am Ende der Klasse (nach `get_final_scores`) hinzufügen:

```python
    @database_sync_to_async
    def save_participant_answer(self, participant_name, hub_session, user_matches, time_taken):
        """Konvertiert shuffled Positionen → Original-Indizes und speichert AssignAnswer."""
        try:
            quiz = AssignQuiz.objects.select_related('current_question').get(room_code=self.room_code)
            participant = quiz.participants.get(name=participant_name, hub_session_code=hub_session)

            if not quiz.current_question:
                return None

            # Doppeltes Speichern verhindern
            existing = AssignAnswer.objects.filter(
                quiz=quiz, participant=participant, question=quiz.current_question
            ).first()
            if existing:
                return None

            # Shuffled Positionen → Original-Indizes umrechnen
            randomized_data = quiz.current_question.get_randomized_items(room_code=self.room_code)
            position_to_original = randomized_data['position_to_original']

            original_user_matches = {}
            for left_idx, shuffled_right_pos in user_matches.items():
                original_right_idx = position_to_original.get(int(shuffled_right_pos))
                if original_right_idx is not None:
                    original_user_matches[left_idx] = original_right_idx

            answer = AssignAnswer.objects.create(
                quiz=quiz,
                participant=participant,
                question=quiz.current_question,
                user_matches=original_user_matches,
                time_taken=time_taken
            )

            return {
                'points_earned': answer.points_earned,
                'correct_matches': answer.get_correct_matches_count(),
                'total_matches': answer.get_total_matches_count(),
                'accuracy': answer.get_accuracy_percentage()
            }

        except (AssignQuiz.DoesNotExist, AssignParticipant.DoesNotExist):
            return None
```

### Schritt 10: Commit

- [ ] ```bash
git add Assign/consumers.py
git commit -m "fix: add elimination tracking, participant_submit_answer handler, fix auto-advance count"
```

---

## Task 2: `onTimeUp` sendet leere Antwort bei kein Drop

**Files:**
- Modify: `templates/assign/play.html`

### Schritt 1: `onTimeUp` Methode ersetzen

- [ ] In `play.html` die Methode `onTimeUp` (Zeilen 1707–1713) ersetzen:

```javascript
                onTimeUp() {
                    if (this.hasAnswered || this.roundSubmitted) return;
                    if (Object.keys(this.userMatches).length > 0) {
                        // Vorhandene Zuordnung einreichen
                        this.submitAnswer();
                    } else {
                        // Keine Zuordnung getroffen → leere Antwort → Eliminierung
                        if (this.websocket && this.websocket.readyState === WebSocket.OPEN) {
                            this.websocket.send(JSON.stringify({
                                type: 'participant_check_round',
                                round_index: this.currentRoundIndex,
                                user_match: {},
                            }));
                            this.roundSubmitted = true;
                        }
                    }
                }
```

### Schritt 2: Commit

- [ ] ```bash
git add templates/assign/play.html
git commit -m "fix: send empty match on timer expiry to trigger elimination"
```

---

## Task 3: Unit-Tests

**Files:**
- Modify: `Assign/tests.py`

### Schritt 1: Tests schreiben

- [ ] `Assign/tests.py` ersetzen:

```python
from django.test import TestCase
from django.contrib.auth.models import User
from .models import AssignQuiz, AssignQuestion, AssignParticipant, AssignAnswer


class CheckRoundAnswerTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='pass')
        self.quiz = AssignQuiz.objects.create(
            title='Test Quiz',
            creator=self.user,
            room_code='1234'
        )
        # Frage: 3 linke Items, 3 rechte Items, correct_matches = {"0": 2, "1": 0, "2": 1}
        self.question = AssignQuestion.objects.create(
            question_text='Match items',
            points=10,
            time_limit=60,
            left_items=['A', 'B', 'C'],
            right_items=['X', 'Y', 'Z'],
            correct_matches={'0': 2, '1': 0, '2': 1},
            created_by=self.user,
        )
        self.participant = AssignParticipant.objects.create(
            quiz=self.quiz,
            name='Alice',
            hub_session_code='sess1'
        )

    def _get_shuffled_pos_for_original(self, original_idx):
        """Hilfsmethode: ermittelt shuffled Position für einen Original-Index."""
        randomized = self.question.get_randomized_items(room_code='1234')
        for shuffled_pos, orig in randomized['position_to_original'].items():
            if orig == original_idx:
                return shuffled_pos
        return None

    def test_correct_answer_round_0(self):
        """Richtige Zuordnung für Runde 0 ergibt True."""
        shuffled_pos = self._get_shuffled_pos_for_original(2)  # correct: right[2] = 'Z'
        randomized = self.question.get_randomized_items(room_code='1234')
        position_to_original = randomized['position_to_original']

        user_match = {'0': shuffled_pos}
        original_right_idx = position_to_original.get(int(shuffled_pos))
        correct_original_idx = self.question.correct_matches.get('0')
        self.assertEqual(int(original_right_idx), int(correct_original_idx))

    def test_wrong_answer_round_0(self):
        """Falsche Zuordnung für Runde 0 ergibt False."""
        # correct für Runde 0 ist original_idx 2, wir nehmen original_idx 0
        shuffled_pos = self._get_shuffled_pos_for_original(0)
        randomized = self.question.get_randomized_items(room_code='1234')
        position_to_original = randomized['position_to_original']

        original_right_idx = position_to_original.get(int(shuffled_pos))
        correct_original_idx = self.question.correct_matches.get('0')
        self.assertNotEqual(int(original_right_idx), int(correct_original_idx))

    def test_empty_match_is_wrong(self):
        """Leeres user_match (kein Drop) zählt als falsch."""
        user_match = {}
        shuffled_right_pos = user_match.get('0')
        self.assertIsNone(shuffled_right_pos)

    def test_save_participant_answer_correct_conversion(self):
        """save_participant_answer rechnet shuffled → original korrekt um."""
        randomized = self.question.get_randomized_items(room_code='1234')
        position_to_original = randomized['position_to_original']

        # Simuliere accumulatedMatches: {left_idx: shuffled_pos} für alle 3 korrekten Runden
        shuffled_matches = {}
        for left_idx_str, correct_orig in self.question.correct_matches.items():
            shuffled_pos = self._get_shuffled_pos_for_original(correct_orig)
            shuffled_matches[left_idx_str] = shuffled_pos

        # Konvertierung simulieren (wie in save_participant_answer)
        original_user_matches = {}
        for left_idx, shuffled_right_pos in shuffled_matches.items():
            original_right_idx = position_to_original.get(int(shuffled_right_pos))
            if original_right_idx is not None:
                original_user_matches[left_idx] = original_right_idx

        self.assertEqual(original_user_matches, {'0': 2, '1': 0, '2': 1})

    def test_assign_answer_score_calculation(self):
        """AssignAnswer berechnet Punkte korrekt: 3 richtige × 10 Punkte = 30."""
        answer = AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.question,
            user_matches={'0': 2, '1': 0, '2': 1},  # alle korrekt (original indices)
            time_taken=15.0
        )
        self.assertEqual(answer.points_earned, 30)
        self.assertEqual(answer.get_correct_matches_count(), 3)
        self.assertEqual(answer.get_accuracy_percentage(), 100.0)

    def test_assign_answer_partial_score(self):
        """AssignAnswer berechnet Teilpunkte: 1 richtig × 10 Punkte = 10."""
        answer = AssignAnswer.objects.create(
            quiz=self.quiz,
            participant=self.participant,
            question=self.question,
            user_matches={'0': 2},  # nur Runde 0 korrekt
            time_taken=5.0
        )
        self.assertEqual(answer.points_earned, 10)
        self.assertEqual(answer.get_correct_matches_count(), 1)
```

### Schritt 2: Tests ausführen

- [ ] ```bash
python manage.py test Assign.tests -v 2
```

Expected: alle 6 Tests PASS

### Schritt 3: Commit

- [ ] ```bash
git add Assign/tests.py
git commit -m "test: add unit tests for round answer checking and score calculation"
```

---

## Manueller Smoke-Test

Nach der Implementierung folgendes manuell testen:

1. **Richtiger Ablauf**: Admin startet Quiz → Frage senden → Teilnehmer zieht korrektes Item → bestätigt → `round_checked {is_correct: true}` → wartet auf nächste Runde → Admin advance → Runde 2 startet
2. **Eliminierung via Submit**: Teilnehmer zieht falsches Item → bestätigt → `round_checked {is_correct: false, eliminated: true}` → `eliminatedState` wird angezeigt
3. **Eliminierung via Timer**: Teilnehmer zieht nichts → Timer läuft ab → leere Antwort wird gesendet → `eliminatedState`
4. **DB-Speicherung**: Admin beendet Frage → Teilnehmer sendet `participant_submit_answer` → `AssignAnswer` in DB vorhanden → Ergebnisseite zeigt korrekten Score
5. **Auto-Advance mit Eliminierung**: 2 Teilnehmer, einer eliminiert in Runde 1 → der andere gibt korrekte Antwort → Auto-Advance feuert sofort (nicht warten auf eliminierten)
