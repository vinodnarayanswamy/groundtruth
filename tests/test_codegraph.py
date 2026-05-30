"""Smoke + behavior tests for the codegraph prototype."""

from groundtruth import GraphStore, context_pack, index_source

SRC = b'''
def helper(x):
    return x + 1

class Service:
    def run(self, x):
        return helper(x)
'''


def _build():
    res = index_source("m.py", SRC)
    store = GraphStore(":memory:")
    store.upsert(res)
    store.resolve_calls()
    return res, store


def test_nodes_extracted():
    res, _ = _build()
    ids = {n.id for n in res.nodes}
    assert "m.py::helper" in ids
    assert "m.py::Service" in ids
    assert "m.py::Service.run" in ids


def test_method_kind():
    res, _ = _build()
    kinds = {n.id: n.kind for n in res.nodes}
    assert kinds["m.py::Service.run"] == "method"
    assert kinds["m.py::helper"] == "function"


def test_call_edge_resolved():
    _, store = _build()
    rows = store.conn.execute(
        "SELECT src, dst, resolved FROM edges WHERE rel='CALLS'"
    ).fetchall()
    edge = {(r["src"], r["dst"]): r["resolved"] for r in rows}
    assert edge[("m.py::Service.run", "m.py::helper")] == 1


def test_incremental_unchanged():
    res, store = _build()
    stats = store.upsert(res)  # re-apply identical
    assert stats["inserted"] == 0
    assert stats["unchanged"] == len(res.nodes)


def test_context_pack_budget():
    _, store = _build()
    pack = context_pack(store, "m.py::Service.run", max_tokens=200, hops=2)
    assert pack["total_tokens"] <= 200
    assert pack["node_count"] >= 1
    assert pack["nodes"][0].id == "m.py::Service.run"
