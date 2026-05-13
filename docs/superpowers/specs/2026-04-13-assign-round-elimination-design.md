# Assign — Rundenbasierter Modus mit Eliminierung

**Datum:** 2026-04-13  
**Status:** Genehmigt

## Ziel

Der Assign-Spieltyp läuft rundenbasiert: Pro Runde sieht der Teilnehmer ein linkes Item und ordnet es einem rechten Item per Drag & Drop zu. Ist die Zuordnung richtig, darf er in der nächsten Runde weiterspielen. Ist sie falsch, ist er für diese Frage ausgeschieden. Bisher wurden Antworten nicht korrekt in der DB gespeichert — dieser Fehler wird mit dem Redesign behoben.

---

## Spielfluss (Teilnehmer)

```
Runde N startet
    ↓
Teilnehmer zieht linkes Item auf rechtes Feld
    ↓ (Submit-Button ODER Timer läuft ab → Auto-Submit)
Server prüft Korrektheit
    ├── Richtig → "Warte auf nächste Runde"-State → weiter mit Runde N+1
    └── Falsch  → "Ausgeschieden"-State → fertig für diese Frage
```

Alle Runden erfolgreich abgeschlossen → "Alle Runden geschafft"-State (wartet auf Admin-Auflösung)

---

## Backend — `Assign/consumers.py`

### Neue class-level Tracking-Dicts

```python
_round_answers: dict[str, dict[str, dict]] = {}
# Struktur: {room_code: {channel_name: {round_idx: user_match}}}

_eliminated_participants: dict[str, set] = {}
# Struktur: {room_code: set(channel_names)}
```

Beide werden beim Verbindungsabbau (disconnect) und beim Quiz-Ende bereinigt.

### `handle_participant_check_round` — Änderungen

1. **Frühabbruch für Eliminierte**: Wenn `channel_name` in `_eliminated_participants[room_code]` → sofort return, keine Verarbeitung
2. **Bei richtig**:
   - Antwort in `_round_answers[room_code][channel_name][round_index] = user_match` speichern
   - `round_checked {is_correct: True}` an den Teilnehmer senden
3. **Bei falsch**:
   - Channel in `_eliminated_participants[room_code]` eintragen
   - `round_checked {is_correct: False, eliminated: True}` an den Teilnehmer senden
4. **Auto-Advance-Logik**: Zählt nur aktive (nicht eliminierte) Teilnehmer. Auto-Advance wird ausgelöst, wenn alle aktiven Teilnehmer für die aktuelle Runde eingereicht haben.

### `_save_all_answers()` — neue Hilfsmethode

Wird von `handle_admin_end_question` aufgerufen, bevor die Frage beendet wird. Ausserdem von `handle_admin_next_round`, wenn `new_round_index >= total_rounds` (alle Runden abgeschlossen).

- Iteriert über alle `_round_answers[room_code]`
- Erstellt für jeden Teilnehmer einen `AssignAnswer`-Eintrag (analog zur Kollegen-Implementierung)
- Konvertiert shuffled Positions → original Indices (bestehende Logik aus `check_round_answer`)
- Setzt `user_matches = {round_idx: original_right_idx, ...}`
- Eliminierte Teilnehmer erhalten `AssignAnswer` mit ihren bisherigen (teilweisen) Antworten

### Cleanup

- In `disconnect()`: Channel aus `_round_answers` und `_eliminated_participants` entfernen
- In `handle_start_question()`: Tracking für diesen Raum zurücksetzen

---

## Frontend — `templates/assign/play.html`

### WebSocket-Nachrichtenverarbeitung

Bestehende `round_checked`-Verarbeitung wird erweitert:

- `is_correct: True` → bestehenden `roundSubmittedMessage`-State anzeigen ("Antwort gespeichert! Warte auf nächste Runde...")
- `is_correct: False, eliminated: True` → `eliminatedState`-Div einblenden (div bereits im Template vorhanden)
- Spielbereich wird in beiden Fällen gesperrt (Drag & Drop deaktiviert), bis die nächste Runde startet

### Auto-Submit via Timer

Beim Timer-Ablauf: aktuellen Drop-Zone-Status auslesen und `participant_check_round`-Nachricht senden — identisch zum Submit-Button-Handler. Wenn kein Item gedroppt wurde, wird eine leere Zuordnung gesendet (→ automatisch falsch → Eliminierung).

### Reset-Button

"Zuordnung zurücksetzen": Hebt die aktuelle Drop-Zone zurück auf die linke Seite. Nur aktiv solange Spieler nicht eingereicht hat und Timer noch läuft.

---

## Admin-Monitor — `templates/admin_dashboard/assign_monitor.html`

Keine Änderungen an der Runden-Navigation. Optional (nice-to-have, nicht Pflicht):
- Anzeige: "X aktiv / Y ausgeschieden" im Live-Status

---

## Datenbank — `AssignAnswer`

Kein Modeländerung erforderlich. Das bestehende Modell speichert `user_matches` als JSON-Dict `{round_idx: original_right_idx}`. Teilweise ausgefüllte Antworten (eliminierte Teilnehmer) sind damit abbildbar.

---

## Nicht im Scope

- Änderungen an `games_website/settings.py`, `urls.py`, `routing.py` (betreffen andere Spiele)
- Änderungen am `AssignBundle`-Modell oder an `assign_management.html`
- Änderungen am `result.html` (bestehende Auswertungslogik reicht)
