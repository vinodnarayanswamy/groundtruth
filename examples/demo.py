"""End-to-end demo: index a file, store it, resolve edges, and build a
token-budgeted context pack for a target node.

Usage:
    python -m examples.demo examples/sample_app.py
    python -m examples.demo examples/sample_app.py "examples/sample_app.py::checkout"
"""

import sys

from groundtruth import GraphStore, context_pack, index_path


def main(path: str, target: str | None = None):
    print(f"Indexing {path} ...\n")
    result = index_path(path)

    store = GraphStore(":memory:")
    stats = store.upsert(result)
    resolved = store.resolve_calls()

    print("== Index stats ==")
    print(f"  nodes:        {len(result.nodes)}")
    print(f"  edges:        {len(result.edges)}")
    print(f"  upsert:       {stats}")
    print(f"  resolved now: {resolved}")
    print(f"  store totals: {store.counts()}\n")

    print("== Nodes ==")
    for n in result.nodes:
        print(f"  [{n.kind:8}] {n.id}")
        print(f"             sig: {n.signature}")
    print()

    print("== Edges (post-resolution sample) ==")
    rows = store.conn.execute(
        "SELECT src, rel, dst, resolved, confidence FROM edges ORDER BY rel, src"
    ).fetchall()
    for r in rows:
        mark = "->" if r["resolved"] else ".."
        conf = f"  ({r['confidence']})" if r["confidence"] else ""
        print(f"  {r['src']}  {mark}[{r['rel']}]  {r['dst']}{conf}")
    print()

    # Default to a target with some neighborhood: CartRepository.add
    target = target or f"{path}::CartRepository.add"
    print(f"== Context pack for {target} (budget 400 tokens) ==")
    pack = context_pack(store, target, max_tokens=400, hops=2)
    print(f"  nodes packed: {pack['node_count']}")
    print(f"  total tokens: {pack['total_tokens']}\n")
    print(pack["prompt"])


if __name__ == "__main__":
    main(
        sys.argv[1] if len(sys.argv) > 1 else "examples/sample_app.py",
        sys.argv[2] if len(sys.argv) > 2 else None,
    )
