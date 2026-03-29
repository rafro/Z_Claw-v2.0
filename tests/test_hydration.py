"""Tests for artifact hydration utilities."""
import json
import os
import tempfile
import zipfile
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch


@pytest.fixture
def divisions_dir(tmp_path):
    """Create a temp divisions directory and patch DIVISIONS_DIR."""
    with patch("runtime.tools.artifact_hydration.DIVISIONS_DIR", tmp_path):
        yield tmp_path


def _write_packet(divisions_dir: Path, division: str, skill: str, data: dict) -> Path:
    """Helper: write a packet JSON to the expected location."""
    pkt_dir = divisions_dir / division / "packets"
    pkt_dir.mkdir(parents=True, exist_ok=True)
    pkt_path = pkt_dir / f"{skill}.json"
    with open(pkt_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return pkt_path


def _make_fresh_ts() -> str:
    """Return an ISO timestamp from 5 minutes ago (well within any typical window)."""
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


def _make_stale_ts(age_minutes: int = 120) -> str:
    """Return an ISO timestamp from age_minutes ago."""
    return (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat()


# ── read_fresh tests ──────────────────────────────────────────────────────────

class TestReadFresh:
    def test_returns_none_for_stale_packet(self, divisions_dir):
        from runtime.tools.artifact_hydration import read_fresh

        _write_packet(divisions_dir, "trading", "market-scan", {
            "division": "trading",
            "skill": "market-scan",
            "generated_at": _make_stale_ts(180),  # 3 hours old
            "summary": "Old scan",
        })

        result = read_fresh("trading", "market-scan", max_age_minutes=60)
        assert result is None

    def test_returns_data_for_fresh_packet(self, divisions_dir):
        from runtime.tools.artifact_hydration import read_fresh

        _write_packet(divisions_dir, "trading", "market-scan", {
            "division": "trading",
            "skill": "market-scan",
            "generated_at": _make_fresh_ts(),
            "summary": "Fresh scan",
        })

        result = read_fresh("trading", "market-scan", max_age_minutes=60)
        assert result is not None
        assert result["summary"] == "Fresh scan"

    def test_returns_none_for_missing_packet(self, divisions_dir):
        from runtime.tools.artifact_hydration import read_fresh

        result = read_fresh("nonexistent", "no-skill", max_age_minutes=60)
        assert result is None

    def test_treats_no_timestamp_as_fresh(self, divisions_dir):
        from runtime.tools.artifact_hydration import read_fresh

        _write_packet(divisions_dir, "personal", "health-logger", {
            "division": "personal",
            "skill": "health-logger",
            "summary": "No timestamp packet",
        })

        result = read_fresh("personal", "health-logger", max_age_minutes=5)
        assert result is not None
        assert result["summary"] == "No timestamp packet"

    def test_respects_custom_max_age(self, divisions_dir):
        from runtime.tools.artifact_hydration import read_fresh

        # 30 minutes old
        ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        _write_packet(divisions_dir, "op-sec", "device-posture", {
            "generated_at": ts,
            "summary": "Recent enough",
        })

        # 60-minute window: should be fresh
        assert read_fresh("op-sec", "device-posture", max_age_minutes=60) is not None
        # 15-minute window: should be stale
        assert read_fresh("op-sec", "device-posture", max_age_minutes=15) is None

    def test_handles_corrupt_json(self, divisions_dir):
        from runtime.tools.artifact_hydration import read_fresh

        pkt_dir = divisions_dir / "trading" / "packets"
        pkt_dir.mkdir(parents=True, exist_ok=True)
        pkt_path = pkt_dir / "market-scan.json"
        pkt_path.write_text("{invalid json", encoding="utf-8")

        result = read_fresh("trading", "market-scan", max_age_minutes=60)
        assert result is None


# ── cold_manifest tests ───────────────────────────────────────────────────────

class TestColdManifest:
    def test_reads_sidecar_manifests(self, divisions_dir):
        from runtime.tools.artifact_hydration import cold_manifest

        cold_dir = divisions_dir / "trading" / "cold"
        cold_dir.mkdir(parents=True, exist_ok=True)

        # Create a zip file
        zip_path = cold_dir / "trade-session-2026-03-01.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("data.json", '{"trades": []}')

        # Create sidecar manifest
        sidecar = zip_path.with_suffix(".zip.manifest.json")
        sidecar_data = {"files": ["data.json"], "created_by": "artifact-manager"}
        with open(sidecar, "w") as f:
            json.dump(sidecar_data, f)

        results = cold_manifest("trading")
        assert len(results) == 1
        assert results[0]["bundle_id"] == "trade-session-2026-03-01"
        assert results[0]["manifest"] is not None
        assert results[0]["file_count"] == 1

    def test_falls_back_without_sidecars(self, divisions_dir):
        from runtime.tools.artifact_hydration import cold_manifest

        cold_dir = divisions_dir / "personal" / "cold"
        cold_dir.mkdir(parents=True, exist_ok=True)

        # Create a zip without sidecar
        zip_path = cold_dir / "health-2026-03-01.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("log.txt", "sleep: 7.5h")

        results = cold_manifest("personal")
        assert len(results) == 1
        assert results[0]["bundle_id"] == "health-2026-03-01"
        assert results[0]["manifest"] is None
        assert results[0]["file_count"] is None
        assert results[0]["size_bytes"] > 0

    def test_empty_cold_returns_empty(self, divisions_dir):
        from runtime.tools.artifact_hydration import cold_manifest

        # Division exists but no cold directory
        results = cold_manifest("nonexistent")
        assert results == []


# ── hydrate_from_cold tests ───────────────────────────────────────────────────

class TestHydrateFromCold:
    def test_extracts_specific_files(self, divisions_dir):
        from runtime.tools.artifact_hydration import hydrate_from_cold

        cold_dir = divisions_dir / "dev-automation" / "cold"
        cold_dir.mkdir(parents=True, exist_ok=True)

        # Create a zip with multiple files
        zip_path = cold_dir / "repo-scan-2026-03-01.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("report.json", '{"flags": 3}')
            zf.writestr("raw_output.txt", "detailed scan output...")
            zf.writestr("metadata.json", '{"timestamp": "2026-03-01"}')

        # Extract only report.json
        extracted = hydrate_from_cold("dev-automation", "repo-scan-2026-03-01", files=["report.json"])
        assert len(extracted) == 1
        assert extracted[0].name == "report.json"
        assert extracted[0].exists()
        content = json.loads(extracted[0].read_text())
        assert content["flags"] == 3

    def test_extracts_all_when_no_filter(self, divisions_dir):
        from runtime.tools.artifact_hydration import hydrate_from_cold

        cold_dir = divisions_dir / "trading" / "cold"
        cold_dir.mkdir(parents=True, exist_ok=True)

        zip_path = cold_dir / "session-bundle.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.txt", "file a")
            zf.writestr("b.txt", "file b")

        extracted = hydrate_from_cold("trading", "session-bundle")
        assert len(extracted) == 2

    def test_raises_for_missing_archive(self, divisions_dir):
        from runtime.tools.artifact_hydration import hydrate_from_cold

        cold_dir = divisions_dir / "trading" / "cold"
        cold_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(FileNotFoundError):
            hydrate_from_cold("trading", "nonexistent-bundle")


# ── enforce_budget tests ──────────────────────────────────────────────────────

class TestEnforceBudget:
    def test_warns_when_over_limit(self, divisions_dir):
        from runtime.tools.artifact_hydration import enforce_budget

        division = "dev-automation"
        hot_dir = divisions_dir / division / "hot"
        hot_dir.mkdir(parents=True, exist_ok=True)

        # Create a config with a tiny budget
        config_dir = divisions_dir / division
        config_path = config_dir / "config.json"
        with open(config_path, "w") as f:
            json.dump({"artifact_policy": {"max_hot_mb": 0.001}}, f)  # ~1KB budget

        # Create a file that exceeds the budget
        big_file = hot_dir / "large.bin"
        big_file.write_bytes(b"x" * 10_000)  # ~10KB

        result = enforce_budget(division)
        assert result["over_budget"] is True
        assert result["current_mb"] > 0
        assert result["max_hot_mb"] == 0.001

    def test_under_budget_returns_false(self, divisions_dir):
        from runtime.tools.artifact_hydration import enforce_budget

        division = "trading"
        hot_dir = divisions_dir / division / "hot"
        hot_dir.mkdir(parents=True, exist_ok=True)

        config_dir = divisions_dir / division
        config_path = config_dir / "config.json"
        with open(config_path, "w") as f:
            json.dump({"artifact_policy": {"max_hot_mb": 100}}, f)  # 100MB budget

        # Small file
        (hot_dir / "tiny.txt").write_text("hello")

        result = enforce_budget(division)
        assert result["over_budget"] is False

    def test_missing_hot_dir_not_over_budget(self, divisions_dir):
        from runtime.tools.artifact_hydration import enforce_budget

        result = enforce_budget("nonexistent-division")
        assert result["over_budget"] is False
        assert result["current_mb"] == 0
