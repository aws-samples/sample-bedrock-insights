from __future__ import annotations

import time

from bedrock_insights.storage import FactStore


def _fact(t, model="anthropic.claude-x", inp=100, cost=0.001, err="", region="us-east-1"):
    return {
        "t": t, "model": model, "is_global": False,
        "ident_key": "user/alice", "ident_label": "alice", "region": region,
        "err": err, "inp": inp, "out": 0, "cw": 0, "cr": 0,
        "cost": cost, "known": True, "display": "Claude X",
    }


def _store(tmp_path):
    return FactStore(tmp_path / "facts.db")


def test_add_and_load_roundtrip(tmp_path):
    s = _store(tmp_path)
    now = int(time.time() * 1000)
    s.add_many([("us-east-1:e1", _fact(now, inp=100))])
    facts, keys = s.load(0)
    assert keys == ["us-east-1:e1"]
    assert facts[0]["inp"] == 100
    assert facts[0]["is_global"] is False
    assert facts[0]["known"] is True
    s.close()


def test_insert_is_idempotent(tmp_path):
    s = _store(tmp_path)
    now = int(time.time() * 1000)
    s.add_many([("us-east-1:e1", _fact(now, inp=100))])
    s.add_many([("us-east-1:e1", _fact(now, inp=999))])  # same key → ignored
    facts, _ = s.load(0)
    assert len(facts) == 1
    assert facts[0]["inp"] == 100  # original kept
    assert s.count() == 1
    s.close()


def test_load_filters_by_time(tmp_path):
    s = _store(tmp_path)
    now = int(time.time() * 1000)
    old = now - 10 * 86_400_000
    s.add_many([("r:e_old", _fact(old)), ("r:e_new", _fact(now))])
    facts, _ = s.load(now - 5 * 86_400_000)
    assert len(facts) == 1
    assert facts[0]["t"] == now
    s.close()


def test_prune_removes_old(tmp_path):
    s = _store(tmp_path)
    now = int(time.time() * 1000)
    old = now - 100 * 86_400_000
    s.add_many([("r:e_old", _fact(old)), ("r:e_new", _fact(now))])
    removed = s.prune(now - 90 * 86_400_000)
    assert removed == 1
    assert s.count() == 1
    s.close()


def test_persistence_across_reopen(tmp_path):
    now = int(time.time() * 1000)
    s1 = _store(tmp_path)
    s1.add_many([("r:e1", _fact(now)), ("r:e2", _fact(now))])
    s1.close()

    s2 = _store(tmp_path)  # reopen same file
    facts, keys = s2.load(0)
    assert len(facts) == 2
    assert set(keys) == {"r:e1", "r:e2"}
    s2.close()
