"""Tests for QVAC training manifest lineage tracking."""
import json
import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch


@pytest.fixture
def manifest_dir(tmp_path):
    """Create a temp state dir and patch MANIFEST_PATH to use it."""
    manifest_file = tmp_path / "training-manifest.json"
    with patch("runtime.tools.training_manifest.MANIFEST_PATH", manifest_file):
        yield tmp_path, manifest_file


class TestRecordCaptureReviewTrainingChain:
    """Test that record_capture + record_review + record_training chain works."""

    def test_full_pipeline_chain(self, manifest_dir):
        tmp_path, manifest_file = manifest_dir
        from runtime.tools.training_manifest import (
            record_capture, record_review, record_training, get_training_stats,
        )

        # Step 1: Capture
        record_capture("hash-abc123", "trading", "2026-03-28T10:00:00+00:00")

        stats = get_training_stats()
        assert stats["total_captured"] == 1
        assert stats["total_reviewed"] == 0
        assert stats["total_approved"] == 0
        assert stats["total_trained"] == 0
        assert stats["by_domain"]["trading"]["captured"] == 1

        # Step 2: Review (approve)
        record_review("hash-abc123", approved=True, reviewer="kai")

        stats = get_training_stats()
        assert stats["total_reviewed"] == 1
        assert stats["total_approved"] == 1
        assert stats["total_trained"] == 0
        assert stats["by_domain"]["trading"]["approved"] == 1

        # Step 3: Train
        record_training("hash-abc123", "trading", "run-20260328-trading-v1")

        stats = get_training_stats()
        assert stats["total_trained"] == 1
        assert stats["by_domain"]["trading"]["trained"] == 1

    def test_multiple_domains(self, manifest_dir):
        tmp_path, manifest_file = manifest_dir
        from runtime.tools.training_manifest import (
            record_capture, record_review, get_training_stats,
        )

        record_capture("hash-001", "trading", "2026-03-28T10:00:00+00:00")
        record_capture("hash-002", "coding", "2026-03-28T10:01:00+00:00")
        record_capture("hash-003", "trading", "2026-03-28T10:02:00+00:00")

        record_review("hash-001", approved=True, reviewer="kai")
        record_review("hash-002", approved=False, reviewer="kai")
        record_review("hash-003", approved=True, reviewer="kai")

        stats = get_training_stats()
        assert stats["total_captured"] == 3
        assert stats["total_reviewed"] == 3
        assert stats["total_approved"] == 2
        assert stats["by_domain"]["trading"]["captured"] == 2
        assert stats["by_domain"]["trading"]["approved"] == 2
        assert stats["by_domain"]["coding"]["captured"] == 1
        assert stats["by_domain"]["coding"]["approved"] == 0

    def test_record_training_rejects_unapproved(self, manifest_dir):
        tmp_path, manifest_file = manifest_dir
        from runtime.tools.training_manifest import (
            record_capture, record_review, record_training, get_training_stats,
        )

        record_capture("hash-rejected", "coding", "2026-03-28T10:00:00+00:00")
        record_review("hash-rejected", approved=False, reviewer="kai")
        record_training("hash-rejected", "coding", "run-test")

        stats = get_training_stats()
        assert stats["total_trained"] == 0

    def test_record_training_missing_hash_is_noop(self, manifest_dir):
        tmp_path, manifest_file = manifest_dir
        from runtime.tools.training_manifest import record_training, get_training_stats

        # Should not raise
        record_training("nonexistent-hash", "trading", "run-test")
        stats = get_training_stats()
        assert stats["total_trained"] == 0


class TestIsDuplicate:
    """Test that is_duplicate catches repeated hashes."""

    def test_new_hash_is_not_duplicate(self, manifest_dir):
        tmp_path, manifest_file = manifest_dir
        from runtime.tools.training_manifest import is_duplicate

        assert is_duplicate("brand-new-hash") is False

    def test_recorded_hash_is_duplicate(self, manifest_dir):
        tmp_path, manifest_file = manifest_dir
        from runtime.tools.training_manifest import record_capture, is_duplicate

        record_capture("existing-hash", "trading", "2026-03-28T10:00:00+00:00")
        assert is_duplicate("existing-hash") is True

    def test_duplicate_capture_is_idempotent(self, manifest_dir):
        tmp_path, manifest_file = manifest_dir
        from runtime.tools.training_manifest import record_capture, get_training_stats

        record_capture("dup-hash", "trading", "2026-03-28T10:00:00+00:00")
        record_capture("dup-hash", "trading", "2026-03-28T10:01:00+00:00")  # same hash

        stats = get_training_stats()
        assert stats["total_captured"] == 1  # only counted once


class TestGetTrainingStats:
    """Test that get_training_stats returns correct per-domain counts."""

    def test_empty_manifest_returns_zeros(self, manifest_dir):
        tmp_path, manifest_file = manifest_dir
        from runtime.tools.training_manifest import get_training_stats

        stats = get_training_stats()
        assert stats["total_captured"] == 0
        assert stats["total_reviewed"] == 0
        assert stats["total_approved"] == 0
        assert stats["total_trained"] == 0
        assert stats["by_domain"] == {}

    def test_stats_reflect_all_operations(self, manifest_dir):
        tmp_path, manifest_file = manifest_dir
        from runtime.tools.training_manifest import (
            record_capture, record_review, record_training, get_training_stats,
        )

        for i in range(5):
            record_capture(f"hash-{i}", "personal", f"2026-03-28T1{i}:00:00+00:00")
        for i in range(3):
            record_review(f"hash-{i}", approved=True, reviewer="auto")
        record_review("hash-3", approved=False, reviewer="auto")
        record_training("hash-0", "personal", "run-test")

        stats = get_training_stats()
        assert stats["total_captured"] == 5
        assert stats["total_reviewed"] == 4
        assert stats["total_approved"] == 3
        assert stats["total_trained"] == 1
        assert stats["by_domain"]["personal"]["captured"] == 5
        assert stats["by_domain"]["personal"]["approved"] == 3
        assert stats["by_domain"]["personal"]["trained"] == 1


class TestComputeCaptureHash:
    """Test that compute_capture_hash is deterministic."""

    def test_same_input_same_hash(self):
        from runtime.tools.domain_map import compute_capture_hash

        messages = [{"role": "user", "content": "hello"}]
        response = "world"

        h1 = compute_capture_hash(messages, response)
        h2 = compute_capture_hash(messages, response)
        assert h1 == h2

    def test_different_input_different_hash(self):
        from runtime.tools.domain_map import compute_capture_hash

        messages = [{"role": "user", "content": "hello"}]
        h1 = compute_capture_hash(messages, "world")
        h2 = compute_capture_hash(messages, "different")
        assert h1 != h2

    def test_hash_is_sha256_hex(self):
        from runtime.tools.domain_map import compute_capture_hash

        h = compute_capture_hash([], "test")
        assert len(h) == 64  # SHA-256 hex length
        assert all(c in "0123456789abcdef" for c in h)

    def test_message_order_matters(self):
        from runtime.tools.domain_map import compute_capture_hash

        msgs_a = [{"role": "system", "content": "a"}, {"role": "user", "content": "b"}]
        msgs_b = [{"role": "user", "content": "b"}, {"role": "system", "content": "a"}]
        h1 = compute_capture_hash(msgs_a, "resp")
        h2 = compute_capture_hash(msgs_b, "resp")
        # json.dumps with sort_keys sorts dict keys, but list order is preserved
        assert h1 != h2
