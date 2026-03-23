# Codex Change Note

Date: 2026-03-23

These game-engine refactor changes were made by Codex, not Claude Code.

Scope:
- Added a canonical `state/game-events.jsonl` event stream for progression events.
- Wired live chronicle writes into progression and prestige flows.
- Enriched theater events so battle outcomes can reflect real skill status and escalation data.
- Moved manual ruler XP, mobile/manual division XP, and manual prestige onto the Python progression engine.
- Added regression coverage for canonical event emission and prestige behavior.
- Added a canonical story engine with chapter progression, doctrine tracking, commander relationship state, and milestone story scenes tied to real progression events.
- Upgraded the mobile theater presentation with improved chapter cards, battle hit effects, outcome states, and richer story-scene staging.
