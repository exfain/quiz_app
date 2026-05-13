# Quiz App Work Plan

## How to use this file
- Work only on the next unchecked step.
- Mark a finished step by changing `[ ]` to `[x]`.
- If a step is blocked, do not check it off. Add a short `Blocked:` note.
- After finishing a step, update its `Status:` section with short bullet points.
- Stop after one completed step.

---

## Shared scoring rules for later leaderboard work
These rules are product requirements for the later overall-score system:

- Every participant takes part in every game ranking.
- A participant with 0 in-game points is still ranked and will therefore usually end up near or at the bottom.
- Equal in-game score = equal rank.
- If ranks are tied, the following rank is skipped.
- Overall points for a game are calculated from the final rank of that game, not from raw in-game points.
- Planned formula for overall points:

  overall_points = (number_of_players - rank + 1) * (game_number ^ A)

- `game_number` starts at 1.
- `A` is a session-level configurable exponent.
- `A = 0` means no game-order weighting.
- `0 < A < 1` means softened weighting.
- `A = 1` means linear weighting.
- For later implementation, keep exact calculation internally and define display rounding explicitly when the leaderboard work begins.

This formula is not part of the early implementation phases below unless a step explicitly says so.

---

## Shared tutorial rule for later game-start flow
This is a cross-game product requirement for later implementation:

- A game can optionally have a tutorial before the first scored question/set.
- The tutorial must never count toward scoring.
- The tutorial can be sent on the fly before the first real scored question/set, or skipped entirely.
- The tutorial should only ever be available before the first scored question/set of the game.
- Do not implement tutorial behavior implicitly inside unrelated steps unless a step explicitly targets it.

---

## Phase 1 — Session / game state hardening

### [ ] Step 01 — Re-activate inactive games
Status:
- not started

Scope:
- Inactive games currently seem not to be re-activatable, at least the button is missing.

Tasks:
- Verify whether the problem is UI-only or also server-side.
- Add the missing re-activate action for inactive games in active sessions.
- Preserve game progress.
- Do not allow re-activation if the session itself is inactive.
- Preserve the rule that only one game can be active at a time.

Checks:
- Inactive game in active session can be re-activated manually.
- Progress is preserved.
- Inactive session cannot re-activate a game.
- No second game becomes active at the same time.

### [ ] Step 02 — Add “Set inactive” action for active games
Status:
- not started

Scope:
- In addition to “End Quiz”, unfinished games should be settable to inactive.

Tasks:
- Add a visible “Set inactive” action while not all sets/questions of the game are completed.
- Do not show that option once the game is fully completed.
- Setting inactive must preserve progress.

Checks:
- Active unfinished game can be set inactive.
- Finished games do not show the inactive option.
- Progress is preserved after re-activation.

### [ ] Step 03 — Leaving active game view / overview popup
Status:
- not started

Scope:
- When the host wants to leave an active game to the overview, show a decision popup.

Tasks:
- Add popup with:
  - End Quiz
  - Set inactive (only if unfinished)
  - Back to overview
  - Zurück
- “End Quiz” should warn additionally if not all sets/questions were played.
- “Back to overview” keeps the game active.
- “Zurück” changes nothing.

Checks:
- Popup appears when leaving an active game.
- Each button performs the correct action.
- Active/inactive/end states remain consistent.

### [ ] Step 04 — Verify session-active -> game-inactive propagation
Status:
- not started

Scope:
- When a session becomes inactive because another session starts, active games inside it must become inactive too.

Tasks:
- Verify and fix session/game state propagation.
- Ensure that when the session becomes active again, the game stays inactive and can be manually re-activated later.
- Do not auto-reactivate the game with the session.

Checks:
- Active session with active game becomes inactive -> game also becomes inactive.
- Session later becomes active again -> game stays inactive.
- Game can then be manually re-activated.

### [ ] Step 05 — Enforce single active game per session
Status:
- not started

Scope:
- Starting a second game while another is active must be blocked by a dialog and server-side guard.

Tasks:
- Add / verify popup:
  - “Ein anderes Spiel ist noch aktiv: (Name des Spiels)”
  - [Aktives Spiel beenden]
  - [Aktives Spiel auf inaktiv setzen]
  - [Zurück]
- Add or verify robust server-side enforcement.

Checks:
- No session can end up with two active games.
- Popup behavior matches product rules.
- Direct requests cannot bypass the guard.

### [ ] Step 06 — Persist custom game order in session
Status:
- not started

Scope:
- Reordering games in a session should persist.

Tasks:
- Verify where current order is stored.
- Persist manual order changes.
- Ensure order remains after playing a game / reloading the session view.

Checks:
- Manual order survives navigation and gameplay.
- Order is not reverted after playing a game.

### [ ] Step 07 — Warn when skipping planned order
Status:
- not started

Scope:
- Starting a game out of the planned order should show a warning popup.

Tasks:
- Detect whether the selected game is not the next game in order.
- Add confirmation popup before starting out-of-order.
- Do not force the order, only warn.

Checks:
- No popup for the next planned game.
- Popup appears for out-of-order start.
- Confirming allows start; canceling prevents start.

---

## Phase 2 — Quick Quiz

### [ ] Step 08 — Evaluate non-logged answers in Quick Quiz
Status:
- not started

Scope:
- Answers that were entered but not explicitly logged in must still be evaluated.

Tasks:
- Verify pending answer handling at timer end and manual question end.
- Fix only the missing evaluation path.

Checks:
- Typed but not logged answer is still evaluated on timer end.
- Typed but not logged answer is still evaluated when host ends the question.
- No regression for logged answers.

### [ ] Step 09 — Replace Quick Quiz waiting screen with reveal state
Status:
- not started

Scope:
- Remove “Waiting for next question” screen for Quick Quiz.

Tasks:
- After question end, keep showing:
  - the question
  - the participant’s given answer
  - the correct answer
- Keep this visible until the next question appears.

Checks:
- No waiting screen appears after question end.
- Participant sees own answer and correct answer until the next question.

---

## Phase 3 — Estimation

### [ ] Step 10 — Fix Estimation mode dropdown layout
Status:
- not started

Scope:
- The mode dropdown should not overlap other text and should be positioned better.

Tasks:
- Adjust host/session UI layout only.
- Do not touch scoring logic.

Checks:
- Dropdown no longer overlaps labels/text.
- Layout stays stable on normal viewport sizes.

### [ ] Step 11 — Prefill default time in Estimation host question field
Status:
- not started

Scope:
- Host-side per-question time field is missing the configured default time.

Tasks:
- Verify where default time is stored and why it does not prefill.
- Fix host-side prefill only.

Checks:
- Existing configured default time appears in the host field.
- Time remains editable.

### [ ] Step 12 — Show zone explanation in Estimation reveal
Status:
- not started

Scope:
- In zone mode, reveal should explain why the participant got the shown points.

Tasks:
- Show zone ranges and corresponding points in the reveal.
- Keep explanation clear and compact.

Checks:
- Reveal shows zone boundaries and points mapping.
- Participant can understand why the awarded score was given.

---

## Phase 4 — Who is lying

### [ ] Step 13 — Improve end-of-set reveal details
Status:
- not started

Scope:
- End-of-set reveal should show the truth for every name and whether the participant reacted correctly.

Tasks:
- Replace current limited reveal with a full per-name result list.
- For every name show:
  - whether the person lies / does not lie
  - whether the participant behaved correctly
- Use a short and clear display form.

Checks:
- Every name in the set appears in the reveal.
- Reveal shows truth + participant correctness for each entry.
- Output is compact and easy to scan.

### [ ] Step 14 — Remove obsolete host points badge in Who is lying
Status:
- not started

Scope:
- Host-side question badge like “10 pts per ID” is obsolete.

Tasks:
- Remove that marker from host-side UI only.

Checks:
- Badge is gone.
- No regression in host flow.

---

## Phase 5 — Who is that

### [ ] Step 15 — Fix timer in Who is that
Status:
- not started

Scope:
- Timer currently does not work correctly.

Tasks:
- Run a narrow timer-flow analysis.
- Fix timer start, countdown, sync, and end behavior.

Checks:
- Timer starts correctly.
- Host/player timer remains consistent.
- Timer end transitions correctly.

### [ ] Step 16 — Remove accidental question-status table in Who is that
Status:
- not started

Scope:
- A previously added question-status table should be removed again.

Tasks:
- Remove the table and only the table.
- Preserve the rest of the flow.

Checks:
- No question-status table remains.
- Other UI stays intact.

---

## Phase 6 — Sorting Ladder

### [ ] Step 17 — Filter live responses to current session only
Status:
- not started

Scope:
- “Live responses” currently shows participants from old sessions.

Tasks:
- Compare current live-responses data source with the participants tab.
- Restrict live responses to the current session / current active participant set.
- Fix root cause, not just hide rows superficially.

Checks:
- Old-session participants no longer appear in live responses.
- Current-session participants still appear correctly.

### [ ] Step 18 — Improve Sorting Ladder reveal presentation
Status:
- not started

Scope:
- End-of-set reveal should present the correct result more clearly.

Tasks:
- Show the correct ordered list vertically.
- Mark items the participant placed wrongly or did not place.
- Keep reveal readable.

Checks:
- Correct solution is shown as a vertical list.
- Wrong/missing participant placements are visually marked.
- Reveal remains stable for longer lists.

---

## Phase 7 — Black Jack rescue

### [ ] Step 19 — Soll-Ist analysis for Black Jack core flow
Status:
- not started

Scope:
- Black Jack is currently not reliably functional.

Tasks:
- Inspect the actual runtime flow before broad fixes.
- Verify:
  - question loading
  - sending first question
  - explicit set handling
  - star accumulation
  - bust (>21) handling
  - simple vs ranking mode application
  - mode lock timing
  - whether sets are explicitly modeled or only implied from a pool
- Only patch a trivially obvious blocker if absolutely necessary; otherwise stop after analysis findings inside tests/safeguards.

Checks:
- A clear minimal-fix order is established for Black Jack.
- No speculative broad refactor.

### [ ] Step 20 — Fix missing host-side questions in Black Jack
Status:
- not started

Scope:
- In testing, no questions could be sent although the set reportedly contained questions.

Tasks:
- Fix the first concrete blocker in the question-loading / question-sending path.

Checks:
- Host can see questions to send.
- First question can be sent.
- No regression in set state.

### [ ] Step 21 — Lock Black Jack mode when first question is sent
Status:
- not started

Scope:
- The mode must become immutable when the first question of the game/set is sent, not only after the first set ends.

Tasks:
- Move the lock boundary to first question send.
- Preserve current mode before that point.

Checks:
- Mode is editable before first sent question.
- Mode is locked immediately after first question is sent.

### [ ] Step 22 — Analyze explicit set model for Black Jack
Status:
- not started

Scope:
- Desired target state is explicit sets, not just slicing a flat question pool.

Tasks:
- Analyze current Black Jack data model and runtime flow to determine the minimal path toward explicit sets.
- Verify whether per-set question counts can be represented cleanly.
- Do not implement speculative schema changes in this step.

Checks:
- Clear minimal implementation path exists for explicit sets.
- Clear implementation path exists for per-set question count.

### [ ] Step 23 — Implement explicit sets and configurable question count in Black Jack
Status:
- not started

Scope:
- After Step 22 confirms the structure, implement it.

Tasks:
- Ensure Black Jack uses explicit sets.
- Add configurable question count for Black Jack creation.
- Support per-set configurable value.
- Preserve backward compatibility with current/default behavior where possible.

Checks:
- Black Jack uses explicit sets.
- Question count is configurable.
- Per-set value is supported.
- Existing Black Jack games still run or are safely migrated.

---

## Phase 8 — Cross-cutting moderation / scoring UI

### [ ] Step 24 — Manual host correction for free-text answers
Status:
- not started

Scope:
- For free-text games, the host must be able to mark a participant answer as correct if automatic matching rejected it.

Targets:
- Quick Quiz (Short Answer)
- Who is that

Tasks:
- In host view, show participant free-text answers.
- Add a per-answer action to mark a response as correct manually.
- Host can only promote to correct, not downgrade correct answers to wrong.
- Preserve auditability and avoid double-scoring.

Checks:
- Host sees participant free-text answers.
- Host can promote an auto-false answer to correct.
- Scores update consistently.
- No duplicate scoring.

### [ ] Step 25 — Analyze score box consistency across all games
Status:
- not started

Scope:
- Score box should be aligned across games, but this is broad.

Tasks:
- Audit each game’s score box:
  - presence
  - correctness
  - timing
  - update behavior
- Do not broadly redesign all at once in this step.
- If a small, obvious bug is found in one game, fix only that one.

Checks:
- A concrete follow-up list exists for per-game score-box fixes.
- No risky global redesign yet.

### [ ] Step 26 — Analyze leaderboard architecture
Status:
- not started

Scope:
- There is both an in-game scoreboard and an overall scoreboard.
- Game points do not directly become overall points; overall points are assigned from final game ranking.

Tasks:
- Map the existing data flow for:
  - in-game points
  - end-of-game ranking
  - total evening points
- Incorporate the planned overall-points formula:
  - overall_points = (number_of_players - rank + 1) * (game_number ^ A)
- Incorporate ranking rules:
  - equal score = equal rank
  - next rank skipped
  - everyone participates in every game ranking
- Define minimal implementation order:
  1. host can show game leaderboard at game end
  2. host can show overall interim leaderboard between games
  3. session-level exponent A can be configured
- Do not implement both boards in this step.

Checks:
- Clear architecture and implementation split exists.
- No premature mixed patch.

### [ ] Step 27 — Spectator view analysis
Status:
- not started

Scope:
- Spectator view is needed, but is too large to implement blindly.

Tasks:
- Analyze whether a shared spectator shell or per-game spectator screens are the minimal safe path.
- Verify what “always shows correct reveal at the end” means per game type.
- Do not implement in this step.

Checks:
- Clear minimal spectator implementation path exists.
- No speculative cross-game UI rewrite.

### [ ] Step 28 — Analyze optional tutorial-before-first-question flow
Status:
- not started

Scope:
- Games may optionally have a tutorial before the first scored question/set.

Tasks:
- Analyze how tutorial delivery fits into the existing game-start flow.
- Verify how to keep tutorial state fully separate from scored progression.
- Verify host controls:
  - tutorial available before first scored question/set
  - host may send tutorial on the fly
  - host may skip tutorial
- Do not implement in this step.

Checks:
- Clear minimal implementation path exists.
- Tutorial can be kept outside scoring and outside real question/set progression.

---

## Phase 9 — New game modes (analysis first only)

### [ ] Step 29 — Analyze manual host-scored external game mode
Status:
- not started

Scope:
- Some games happen outside the app; host manually awards points.

Tasks:
- Analyze minimal viable mode:
  - participant list only
  - plus/minus or direct point assignment
  - per-round or direct total entry
- Do not implement yet.

Checks:
- A minimal product shape exists for later implementation.

### [ ] Step 30 — Analyze “Wer weiß mehr”
Status:
- not started

Scope:
- Round-based free-text elimination mode with fixed answer pool.

Tasks:
- Analyze unanswered rule details:
  - duplicates across participants
  - round timing
  - whether everyone answers simultaneously or sequentially
  - reveal state per round
- Do not implement yet.

Checks:
- Open product questions are identified before coding starts.

### [ ] Step 31 — Analyze “Wann war das”
Status:
- not started

Scope:
- Number-answer game with growing tolerance over time and ranking of correct answers by when they answered.

Tasks:
- Analyze unanswered rule details:
  - single answer vs multiple attempts
  - whether early wrong answers lock the player out
  - how ranking interacts with minimum points
- Do not implement yet.

Checks:
- Scoring and timing model is clear enough for later implementation.

