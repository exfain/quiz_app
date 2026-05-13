# Codex working rules for this repository

## Source of truth
- If `PLANS.md` exists, follow it.
- Work only on the next unchecked step in `PLANS.md`.
- Never bundle multiple plan steps into one patch.

## Execution style
For every step:
1. inspect the actual runtime path
2. identify the concrete root cause / implementation gap
3. implement the smallest safe patch
4. add regression tests and safeguards
5. stop

## Output format
Only output:
- Patch
- Tests
- Safeguards

Do not output:
- manual test instructions
- changed-file lists
- high-level summaries unless strictly needed for the patch itself

## Scope discipline
- No refactors unless strictly required.
- Do not change unrelated game types or flows.
- Prefer minimal diffs.
- Preserve backward compatibility where possible.
- If a requested change affects shared templates, shared session logic, or shared score logic, keep the patch as narrow as possible and avoid incidental behavior changes in unrelated games.

## When something is unclear
- Do not guess if product behavior is unclear.
- If a plan step explicitly says to analyze first, do analysis only.
- If a step depends on unclear product behavior, stop after the analysis findings and do not continue automatically.
- If multiple UI paths or templates exist for the same feature, do not silently choose one unless the plan explicitly decides it; otherwise stop after identifying the active path(s).

## State and sync safety
- Be careful with session state, active/inactive transitions, timers, host/player sync, and scoring.
- Add tests for regressions whenever touching those areas.

## Migrations and schema changes
- Only add schema changes when truly necessary.
- Prefer the smallest compatible migration path.
- Keep old data working whenever possible.
- Never add a migration unless the current plan step actually requires a schema change.

## Analysis-only steps
- If the current step is analysis-only, do not modify code, templates, migrations, tests, or plan files except for allowed progress notes in `PLANS.md`.

## Progress tracking
- After completing a plan step successfully, update `PLANS.md`:
  - change `[ ]` to `[x]`
  - add a short result note directly below the step
- If a step is only partially done or blocked, do not mark it as completed.
- If blocked, add a short `Blocked:` note under that step.
- Always stop after one plan step.
- When updating `PLANS.md`, keep edits minimal and do not rewrite unrelated sections.