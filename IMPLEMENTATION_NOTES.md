# Implementation Notes

Date: 2026-03-24

Session note: This implementation session, including the code changes, verification steps, and review passes summarized here, was carried out with Codex.

## Mobile Mission Control Fixes

### Scope

This update focused on the remote mobile Mission Control dashboard, especially live updates, mobile auth consistency, story-choice flow, and host routing for quick actions.

### Files Changed

- `mobile/index.html`
- `server.js`
- `tests/test_mobile_dashboard_contracts.py`

## What Changed

### `mobile/index.html`

- Added `_mobileEventStreamUrl(endpoint)` so mobile SSE endpoints consistently append the mobile token as a query parameter.
- Updated the gamification SSE connection to use the tokenized stream URL.
- Consolidated gamification realtime handling through `_handleGamifRealtimeEvent(evt)`.
- Fixed WebSocket gamification fallback so WS events are normalized into the same `type: 'gamif'` shape expected by the shared handler.
- Updated quick-run actions to use the existing `api('/api/control', ...)` wrapper instead of raw `fetch('/api/control', ...)`, so custom `MC_SERVER` targets work correctly.
- Retired the dead legacy story modal execution path that still referenced `/mobile/api/story/choose` and `choice_index`.
- Left the active theater choice flow as the live mobile story-choice submission path.

### `server.js`

- Tightened `/mobile/ws` auth so non-localhost WebSocket clients are rejected when `MOBILE_TOKEN` is missing.
- Kept localhost exempt, matching the existing desktop trust model.
- Preserved invalid-token rejection for non-localhost WebSocket clients.

### `tests/test_mobile_dashboard_contracts.py`

- Added focused regression checks for:
  - tokenized mobile gamification SSE wiring
  - removal of the obsolete mobile story-choice route/payload
  - `MC_SERVER`-aware quick-run behavior
  - WebSocket gamification fallback normalization
  - `/mobile/ws` token-policy enforcement shape

## Why These Changes Were Needed

The mobile dashboard had several drifted paths:

- remote phones could not subscribe to gamification SSE because the mobile token was not being passed
- quick-run buttons ignored `MC_SERVER` and posted to the page origin
- an older story-choice modal still referenced a removed API contract
- WebSocket fallback handling for gamification events was incomplete and then needed one follow-up fix to normalize events correctly
- `/mobile/ws` did not fully match the `/mobile/api/*` auth policy when `MOBILE_TOKEN` was unset

## Verification Performed

- `pytest -p no:cacheprovider tests/test_mobile_dashboard_contracts.py`
  - result: `5 passed`
- `node --check server.js`
  - result: passed

## Review Process

Parallel agents were used for:

- initial bug review
- implementation slices
- per-update review
- end-to-end integration review

The final post-fix review passes reported no material findings for the mobile Mission Control path.

## Remaining Manual Validation

Still recommended on a real mobile client:

1. Confirm remote mobile login/access works with `MOBILE_TOKEN` set.
2. Confirm live gamification updates appear without needing refresh.
3. Confirm quick-run buttons queue work against the intended server.
4. Confirm theater/story choices still submit correctly.
