from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def slice_between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def test_mobile_gamif_stream_includes_token_query():
    mobile = read_text("mobile/index.html")
    helper_block = slice_between(
        mobile,
        "function _mobileEventStreamUrl(endpoint) {",
        "function _connectAlertStream() {",
    )
    gamif_block = slice_between(
        mobile,
        "function _connectGamifStream() {",
        "function triggerSpriteAnim(type) {",
    )

    assert "?token=" in helper_block
    assert "encodeURIComponent(MC_TOKEN)" in helper_block
    assert "_mobileEventStreamUrl('/mobile/api/gamif/stream')" in gamif_block
    assert "_handleGamifRealtimeEvent(evt);" in gamif_block
    assert "if (evt.event === 'prestige')" not in gamif_block


def test_mobile_story_choice_uses_current_route_and_payload():
    mobile = read_text("mobile/index.html")
    theater_choice_block = slice_between(
        mobile,
        "async function _thPlayChoice(event) {",
        "// ── Main queue player",
    )

    assert "/mobile/api/story/choose" not in mobile
    assert "choice_index" not in mobile
    assert "_checkStoryState();" not in mobile
    assert "case 'story_choice_pending':" not in mobile
    assert "/mobile/api/story/choice" in theater_choice_block
    assert "choice_id: chosen.id" in theater_choice_block
    assert "choice_text: chosen.text" in theater_choice_block


def test_mobile_quick_run_uses_api_wrapper_not_raw_origin_fetch():
    mobile = read_text("mobile/index.html")
    quick_run_block = slice_between(
        mobile,
        "async function runSkillFromMobile(skill, btnEl) {",
        "function _divQuickActions(key) {",
    )

    assert "fetch('/api/control'" not in quick_run_block
    assert "api('/api/control'" in quick_run_block


def test_mobile_ws_gamif_fallback_normalizes_type_before_dispatch():
    mobile = read_text("mobile/index.html")
    ws_block = slice_between(
        mobile,
        "function _handleWSMessage(msg) {",
        "//",
    )

    assert "_handleGamifRealtimeEvent({ ...msg, event: msg.type, type: 'gamif' })" in ws_block


def test_mobile_ws_requires_token_when_mobile_token_missing():
    server = read_text("server.js")
    ws_block = slice_between(
        server,
        "const wss = new WebSocketServer({ server, path: '/mobile/ws' });",
        "  console.log('  WebSocket : ws://localhost:' + PORT + '/mobile/ws');",
    )

    assert "mobileToken" in ws_block
    assert "if (!isLocalhost)" in ws_block
    assert "if (!mobileToken)" in ws_block
    assert "Mobile access not configured" in ws_block
    assert "timingSafeEqual" in ws_block
    assert "Unauthorized" in ws_block
