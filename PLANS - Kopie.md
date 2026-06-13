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





Weitere Punkte:
-UI
-wo liegt was?
-Optionen, ob geschickte Fragen gelocked
-Bracketmodus
-wann war das
-1v1 Modus (vieles ohne timer)
	-buzzerspiel (quick quiz - short answer, wann war das?, who is that?, clue rush)
	-estimation
	-who is lying
	-quick quiz true or false
	-sorting ladder
	-wer weiß mehr?
	-assign
	-black jack + wo liegt was? wie ranking modus
