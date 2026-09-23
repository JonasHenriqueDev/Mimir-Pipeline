import pytest

from mimir_pipeline.models import Issue, Snapshot
from mimir_pipeline.tracking import IssueTracker


def issue(key, line=1, **changes):
    values = dict(
        key=key,
        rule="python:rule",
        path="source.py",
        line=line,
        message="unused",
        anchor="unused = 1",
    )
    return Issue(**(values | changes))


def test_identical_lines_in_different_locations_are_independent():
    tracker = IssueTracker()
    baseline = tracker.register(Snapshot(issues=[issue("a", 4), issue("b", 12)]))
    first, second = baseline.issues
    assert first.identity != second.identity
    after = tracker.register(Snapshot(issues=[issue("b", 10)]))
    assert after.issues[0].identity == second.identity


def test_baseline_identity_is_shared_across_different_sonar_projects():
    left = IssueTracker().register(Snapshot(issues=[issue("random-left-key", 4)]))
    right = IssueTracker().register(Snapshot(issues=[issue("random-right-key", 4)]))
    assert left.issues[0].identity == right.issues[0].identity


def test_changed_anchor_does_not_erase_an_unresolved_diagnostic():
    tracker = IssueTracker()
    baseline = tracker.register(Snapshot(issues=[issue("a")]))
    after = tracker.register(Snapshot(issues=[issue("a", anchor="unused = 2")]))
    assert baseline.issues[0].identity == after.issues[0].identity


def test_new_key_at_identical_location_is_not_credited_as_baseline():
    tracker = IssueTracker()
    baseline = tracker.register(Snapshot(issues=[issue("a")]))
    after = tracker.register(Snapshot(issues=[issue("new-key")]))
    assert baseline.issues[0].identity != after.issues[0].identity
    assert after.issues[0].identity.startswith("new:")


def test_reopened_diagnostic_restores_original_identity():
    tracker = IssueTracker()
    baseline = tracker.register(Snapshot(issues=[issue("a")]))
    tracker.register(Snapshot(issues=[]))
    restored = tracker.register(Snapshot(issues=[issue("a")]))
    assert restored.issues[0].identity == baseline.issues[0].identity


def test_same_line_different_messages_are_independent():
    snapshot = IssueTracker().register(
        Snapshot(issues=[issue("a"), issue("b", message="different")])
    )
    assert snapshot.issues[0].identity != snapshot.issues[1].identity


def test_duplicate_server_keys_rejected():
    with pytest.raises(ValueError, match="duplicadas"):
        IssueTracker().register(Snapshot(issues=[issue("a"), issue("a")]))
