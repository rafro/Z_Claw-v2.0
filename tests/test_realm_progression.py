import json
from datetime import datetime, timedelta, timezone


def _patch_realm_paths(tmp_path, monkeypatch):
    from runtime.tools import anim_queue, xp
    from runtime.realm import chronicle, events, story

    monkeypatch.setattr(xp, "STATS_FILE", tmp_path / "jclaw-stats.json")
    monkeypatch.setattr(xp, "XP_HISTORY_FILE", tmp_path / "xp-history.jsonl")
    monkeypatch.setattr(events, "EVENTS_FILE", tmp_path / "game-events.jsonl")
    monkeypatch.setattr(chronicle, "CHRONICLE_FILE", tmp_path / "realm-chronicle.jsonl")
    monkeypatch.setattr(story, "STORY_FILE", tmp_path / "story-state.json")
    monkeypatch.setattr(anim_queue, "QUEUE_FILE", tmp_path / "anim-queue.json")
    monkeypatch.setattr(anim_queue, "HISTORY_FILE", tmp_path / "anim-history.json")
    return xp, events, chronicle, anim_queue, story


def test_grant_skill_xp_emits_canonical_event_and_chronicle(tmp_path, monkeypatch):
    xp, events, chronicle, anim_queue, story = _patch_realm_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("JCLAW_RUN_ID", "test-run-skill")
    monkeypatch.setattr(
        xp.packet_io,
        "read",
        lambda division, skill: {
            "status": "partial",
            "escalate": True,
            "summary": "Needs review",
            "urgency": "high",
            "provider_used": "deterministic",
        },
    )

    result = xp.grant_skill_xp("job-intake")

    assert result["xp_granted"] == 10
    assert result["division"] == "opportunity"

    game_events = [json.loads(line) for line in events.EVENTS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    skill_event = next(evt for evt in game_events if evt["event"] == "skill_complete")
    assert skill_event["run_id"] == "test-run-skill"
    assert skill_event["status"] == "partial"
    assert skill_event["escalate"] is True
    assert skill_event["summary"] == "Needs review"

    chronicle_entries = [json.loads(line) for line in chronicle.CHRONICLE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(entry["category"] == "achievement" and entry["achievement"] == "first_hunt" for entry in chronicle_entries)

    queue = json.loads(anim_queue.QUEUE_FILE.read_text(encoding="utf-8"))
    skill_scene = next(entry for entry in queue if entry["type"] == "skill_complete")
    assert skill_scene["status"] == "partial"
    assert skill_scene["escalate"] is True
    assert any(entry["type"] == "story_scene" for entry in queue)

    story_state = json.loads(story.STORY_FILE.read_text(encoding="utf-8"))
    assert story_state["flags"]["first_activation"]["opportunity"] is True
    assert story_state["progress"]["crisis_events"] == 1


def test_force_prestige_resets_divisions_and_emits_event(tmp_path, monkeypatch):
    xp, events, chronicle, anim_queue, _story = _patch_realm_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("JCLAW_RUN_ID", "test-run-prestige")

    stats = xp._empty_stats()
    for div_key, div_stats in stats["divisions"].items():
        div_stats["xp"] = 500
        div_stats["rank"] = xp.DIVISIONS[div_key]["ranks"][-1]
    xp._save_stats(stats)

    result = xp.force_prestige()

    assert result["ok"] is True
    assert result["prestige"] == 1

    saved = json.loads(xp.STATS_FILE.read_text(encoding="utf-8"))
    assert saved["prestige"] == 1
    assert all(div["xp"] == 0 for div in saved["divisions"].values())

    game_events = [json.loads(line) for line in events.EVENTS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    prestige_event = next(evt for evt in game_events if evt["event"] == "prestige")
    assert prestige_event["auto"] is False
    assert prestige_event["run_id"] == "test-run-prestige"

    chronicle_entries = [json.loads(line) for line in chronicle.CHRONICLE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(entry["category"] == "prestige" for entry in chronicle_entries)

    queue = json.loads(anim_queue.QUEUE_FILE.read_text(encoding="utf-8"))
    assert any(entry["type"] == "prestige" for entry in queue)

    history = [json.loads(line) for line in xp.XP_HISTORY_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    prestige_history = next(entry for entry in history if entry["event"] == "prestige")
    assert prestige_history["auto"] is False


def test_story_choice_updates_arc_and_relationships(tmp_path, monkeypatch):
    _xp, _events, _chronicle, _anim_queue, story = _patch_realm_paths(tmp_path, monkeypatch)

    state = story.current_state()
    assert state["active_arc"]["id"] == "balanced"

    story.apply_choice("opportunity", "aggressive", "Push harder")
    story.apply_choice("trading", "aggressive", "Scale the position")
    updated = story.apply_choice("production", "aggressive", "Open the next run")

    assert updated["active_arc"]["id"] == "aggressive"
    assert updated["doctrine"]["aggressive"] == 3
    assert updated["relationships"]["opportunity"]["trust"] > 50
    assert any(scene["scene_key"] == "arc_shift:aggressive" for scene in updated["recent_scenes"])


def test_streak_shield_only_covers_one_missed_day(tmp_path, monkeypatch):
    xp, _events, _chronicle, _anim_queue, _story = _patch_realm_paths(tmp_path, monkeypatch)

    stats = xp._empty_stats()
    now = datetime.now(timezone.utc)
    week = xp._week_key(now)

    entry = stats["streaks"]["opportunity"]
    entry["current"] = 6
    entry["last_date"] = (now - timedelta(days=2)).date().isoformat()
    entry["week"] = week

    milestone = xp._update_streak(stats, "opportunity")

    assert milestone == 7
    assert entry["current"] == 7
    assert entry["shield_this_week"] is True

    entry["current"] = 6
    entry["last_date"] = (now - timedelta(days=3)).date().isoformat()
    entry["shield_this_week"] = False
    entry["week"] = week

    milestone = xp._update_streak(stats, "opportunity")

    assert milestone is False
    assert entry["current"] == 1
    assert entry["shield_this_week"] is False


def test_base_xp_progress_tracks_current_level_and_remaining_xp(tmp_path, monkeypatch):
    xp, _events, _chronicle, _anim_queue, _story = _patch_realm_paths(tmp_path, monkeypatch)

    result = xp.grant_base_xp(110, "progression-check")
    saved = xp.current_stats()

    assert result["level"] == 2
    assert result["base_xp"] == 110
    assert result["xp_into_level"] == 10
    assert result["xp_for_next_level"] == 180
    assert result["xp_to_next_level"] == 170
    assert saved["xp_into_level"] == 10
    assert saved["xp_for_next_level"] == 180
    assert saved["xp_to_next_level"] == 170


def test_auto_prestige_records_history_entry(tmp_path, monkeypatch):
    xp, _events, chronicle, _anim_queue, _story = _patch_realm_paths(tmp_path, monkeypatch)

    stats = xp._empty_stats()
    for div_key, div_stats in stats["divisions"].items():
        div_stats["xp"] = 500
        div_stats["rank"] = xp.DIVISIONS[div_key]["ranks"][-1]
    stats["divisions"]["opportunity"]["xp"] = 495
    xp._save_stats(stats)

    result = xp.grant_division_xp("opportunity", 5, skill_name="auto-prestige-trigger", reason="coverage")

    assert result["division"] == "opportunity"

    history = [json.loads(line) for line in xp.XP_HISTORY_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    prestige_history = next(entry for entry in history if entry["event"] == "prestige")
    assert prestige_history["auto"] is True
    assert prestige_history["prestige"] == 1

    chronicle_entries = [json.loads(line) for line in chronicle.CHRONICLE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(entry["category"] == "prestige" for entry in chronicle_entries)


def test_chronicle_migration_rebuilds_prestige_entries(tmp_path, monkeypatch):
    _xp, _events, chronicle, _anim_queue, _story = _patch_realm_paths(tmp_path, monkeypatch)

    history_path = tmp_path / "xp-history.jsonl"
    history_path.write_text(
        json.dumps({
            "ts": "2026-03-23T00:00:00+00:00",
            "event": "prestige",
            "prestige": 2,
            "multiplier": 1.1,
            "auto": True,
        }) + "\n",
        encoding="utf-8",
    )

    written = chronicle.migrate_from_history(history_path, tmp_path / "jclaw-stats.json")

    assert written == 1
    chronicle_entries = [json.loads(line) for line in chronicle.CHRONICLE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert chronicle_entries[0]["category"] == "prestige"
    assert chronicle_entries[0]["auto"] is True


def test_defeat_applies_xp_penalty(tmp_path, monkeypatch):
    xp, events, _chronicle, anim_queue, _story = _patch_realm_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("JCLAW_RUN_ID", "test-run-defeat")
    monkeypatch.setattr(
        xp.packet_io,
        "read",
        lambda division, skill: {
            "status": "failed",
            "escalate": False,
            "summary": "Job application rejected",
            "urgency": "normal",
            "provider_used": "deterministic",
        },
    )

    result = xp.grant_skill_xp("job-intake")

    # Base XP for job-intake is 10; defeat penalty halves it
    assert result["xp_granted"] <= 5
    assert result["xp_granted"] >= 1

    queue = json.loads(anim_queue.QUEUE_FILE.read_text(encoding="utf-8"))
    battle = next(e for e in queue if e["type"] == "skill_complete")
    assert battle["defeat_penalty"] is True
    assert battle["xp"] == result["xp_granted"]
