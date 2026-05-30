"""Smoke + behavior tests for the codegraph prototype."""

from groundtruth import GraphStore, context_pack, index_path, index_source

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


# --- Tier 0: multi-repo namespacing -----------------------------------------

def _make_repo(root, name, body):
    pkg = root / name / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "util.py").write_text(body, encoding="utf-8")
    return root / name


def test_repo_namespacing_avoids_collision(tmp_path):
    """Two repos sharing pkg/util.py must produce distinct node ids."""
    repo_a = _make_repo(tmp_path, "repo_a", "def helper():\n    return 'a'\n")
    repo_b = _make_repo(tmp_path, "repo_b", "def helper():\n    return 'b'\n")

    store = GraphStore(":memory:")
    store.upsert(index_path(str(repo_a), repo="repo_a"))
    store.upsert(index_path(str(repo_b), repo="repo_b"))

    ids = {r["id"] for r in store.conn.execute("SELECT id FROM nodes")}
    assert "repo_a/pkg/util.py::helper" in ids
    assert "repo_b/pkg/util.py::helper" in ids
    # Both definitions survive — no overwrite.
    assert store.counts()["nodes"] == 2


def test_namespaced_source_path_reads_body(tmp_path):
    """Namespaced ids still resolve to the physical file for body extraction."""
    repo = _make_repo(tmp_path, "repo_a", "def helper():\n    return 42\n")
    store = GraphStore(":memory:")
    store.upsert(index_path(str(repo), repo="repo_a"))
    pack = context_pack(store, "repo_a/pkg/util.py::helper", hops=1)
    assert "return 42" in pack["prompt"]


# --- Tier 1: import-scoped call disambiguation ------------------------------

def test_import_disambiguates_ambiguous_call(tmp_path):
    """`save` exists in two repos; the import points resolution at the right one."""
    # repo_b defines the Store.save we expect to win.
    store_b = tmp_path / "repo_b" / "pkg"
    store_b.mkdir(parents=True)
    (store_b / "store.py").write_text(
        "class Store:\n"
        "    def save(self, x):\n"
        "        return x\n",
        encoding="utf-8",
    )
    # repo_c has an unrelated, colliding `save` (a decoy).
    store_c = tmp_path / "repo_c" / "pkg"
    store_c.mkdir(parents=True)
    (store_c / "other.py").write_text(
        "def save(y):\n"
        "    return y\n",
        encoding="utf-8",
    )
    # repo_a calls save() and imports it from repo_b's store module.
    app_a = tmp_path / "repo_a" / "pkg"
    app_a.mkdir(parents=True)
    (app_a / "app.py").write_text(
        "from pkg.store import save\n"
        "def run(x):\n"
        "    return save(x)\n",
        encoding="utf-8",
    )

    store = GraphStore(":memory:")
    store.upsert(index_path(str(tmp_path / "repo_a"), repo="repo_a"))
    store.upsert(index_path(str(tmp_path / "repo_b"), repo="repo_b"))
    store.upsert(index_path(str(tmp_path / "repo_c"), repo="repo_c"))
    store.resolve_calls()

    row = store.conn.execute(
        "SELECT dst, resolved, confidence FROM edges "
        "WHERE src = 'repo_a/pkg/app.py::run' AND rel = 'CALLS'"
    ).fetchone()
    assert row["resolved"] == 1
    assert row["dst"] == "repo_b/pkg/store.py::Store.save"
    assert row["confidence"] == "import"
