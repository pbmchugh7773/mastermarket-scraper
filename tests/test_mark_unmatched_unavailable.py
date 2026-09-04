"""
Tests for mark_unmatched_unavailable.py — the follow-up to repair_lidl_aliases
that flags aliases the repair tool could not match as unavailable.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import mark_unmatched_unavailable as mod  # noqa: E402

PROPOSAL = {
    "generated_at": "2026-09-04T10:00:00+00:00",
    "repairs": [{"alias_id": 9, "product_name": "Repaired", "new_url": "https://www.lidl.ie/p/x/p1"}],
    "unmatched": [
        {"alias_id": 1, "product_name": "Potato Gratin", "reason": "no_match"},
        {"alias_id": 2, "product_name": "Pizza", "reason": "ambiguous"},
        {"alias_id": 3, "product_name": "Sourdough", "reason": "no_match"},
        {"alias_id": 4, "product_name": "Hummus", "reason": "size_mismatch"},
    ],
}


def test_select_unmatched_keeps_only_requested_reasons():
    got = mod.select_unmatched(PROPOSAL, {"no_match"})
    assert [r["alias_id"] for r in got] == [1, 3]


def test_select_unmatched_accepts_several_reasons():
    got = mod.select_unmatched(PROPOSAL, {"no_match", "size_mismatch"})
    assert [r["alias_id"] for r in got] == [1, 3, 4]


def test_mark_unavailable_sends_persistent_404_reason():
    seen = []

    def patch_fn(alias_id, reason, token):
        seen.append((alias_id, reason, token))

    done, failed = mod.mark_unavailable([{"alias_id": 7}], "tok", patch_fn=patch_fn)
    assert seen == [(7, "persistent_404", "tok")]
    assert (done, failed) == (1, 0)


def test_mark_unavailable_continues_after_one_failure():
    calls = []

    def patch_fn(alias_id, reason, token):
        calls.append(alias_id)
        if alias_id == 3:
            raise RuntimeError("boom")

    done, failed = mod.mark_unavailable(
        [{"alias_id": 1}, {"alias_id": 3}, {"alias_id": 5}], "tok", patch_fn=patch_fn
    )
    assert calls == [1, 3, 5]
    assert (done, failed) == (2, 1)


def test_main_without_apply_never_calls_api(tmp_path, monkeypatch):
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(PROPOSAL))

    def explode(*_a, **_k):
        raise AssertionError("API must not be called in proposal-only mode")

    monkeypatch.setattr(mod, "_api_login", explode)
    monkeypatch.setattr(mod, "_patch_mark_unavailable", explode)

    assert mod.main([str(path)]) == 0


def test_main_with_apply_marks_selected_and_exits_nonzero_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(PROPOSAL))
    marked = []

    def patch_fn(alias_id, reason, token):
        marked.append(alias_id)
        if alias_id == 3:
            raise RuntimeError("500")

    monkeypatch.setattr(mod, "_api_login", lambda: "tok")
    monkeypatch.setattr(mod, "_patch_mark_unavailable", patch_fn)

    assert mod.main([str(path), "--apply"]) == 1
    assert marked == [1, 3]
