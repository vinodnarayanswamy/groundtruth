"""Context-pack retrieval.

Given a target node, gather its N-hop neighborhood, rank by hop distance
and edge relevance, and pack under a token budget. Distant nodes degrade
to signature-only so the pack stays near the budget.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .store import GraphStore

# Relevance weights per edge type. Tune per task.
REL_WEIGHT = {
    "CALLS": 3,
    "IMPLEMENTS": 3,
    "INHERITS": 2,
    "DEFINED_IN": 2,
    "REFERENCES": 1,
    "IMPORTS": 1,
}


@dataclass
class PackedNode:
    id: str
    distance: int
    via_rel: str
    mode: str  # "full" | "signature"
    text: str
    tokens: int


def estimate_tokens(text: str) -> int:
    """Cheap heuristic: ~4 chars per token. Replace with a real
    tokenizer (tiktoken/anthropic) in production."""
    return max(1, len(text) // 4)


def _full_body(store: GraphStore, node_id: str) -> str:
    row = store.get_node(node_id)
    if row is None:
        return ""
    # In a real build you'd slice the file by span; here we reconstruct
    # a faithful stub from stored metadata to keep the prototype self-contained.
    doc = f'    """{row["docstring"]}"""\n' if row["docstring"] else ""
    return f'{row["signature"]}:\n{doc}    ...  # lines {row["start_line"]}-{row["end_line"]}'


def _signature_only(store: GraphStore, node_id: str) -> str:
    row = store.get_node(node_id)
    if row is None:
        return ""
    return f'{row["signature"]}:  # {row["kind"]}'


def bfs_neighborhood(store: GraphStore, target: str, hops: int):
    """Return list of (node_id, distance, via_rel) reachable within `hops`."""
    seen = {target}
    out = []
    q = deque([(target, 0, "SELF")])
    while q:
        nid, dist, via = q.popleft()
        if dist > 0:
            out.append((nid, dist, via))
        if dist == hops:
            continue
        for nb in store.neighbors(nid):
            if nb["other"] not in seen:
                seen.add(nb["other"])
                q.append((nb["other"], dist + 1, nb["rel"]))
    return out


def context_pack(store: GraphStore, target: str,
                 max_tokens: int = 2200, hops: int = 2) -> dict:
    """Assemble a token-budgeted context pack for `target`."""
    target_row = store.get_node(target)
    if target_row is None:
        raise KeyError(f"unknown node: {target}")

    neighborhood = bfs_neighborhood(store, target, hops)

    ranked = sorted(
        neighborhood,
        key=lambda t: (t[1], -REL_WEIGHT.get(t[2], 0)),
    )

    packed: list[PackedNode] = []
    # Target always included at full fidelity, off-budget.
    tgt_text = _full_body(store, target)
    packed.append(PackedNode(target, 0, "SELF", "full",
                             tgt_text, estimate_tokens(tgt_text)))
    used = packed[0].tokens

    for nid, dist, via in ranked:
        if dist <= 1:
            text = _full_body(store, nid)
            mode = "full"
        else:
            text = _signature_only(store, nid)
            mode = "signature"
        cost = estimate_tokens(text)

        if used + cost > max_tokens:
            # degrade to signature before giving up
            text = _signature_only(store, nid)
            mode = "signature"
            cost = estimate_tokens(text)
        if used + cost > max_tokens:
            break

        packed.append(PackedNode(nid, dist, via, mode, text, cost))
        used += cost

    return {
        "target": target,
        "total_tokens": used,
        "node_count": len(packed),
        "nodes": packed,
        "prompt": _assemble_prompt(packed),
    }


def _assemble_prompt(packed: list[PackedNode]) -> str:
    lines = ["# Context pack (graph-retrieved)\n"]
    for p in packed:
        tag = "TARGET" if p.distance == 0 else f"hop {p.distance} via {p.via_rel}"
        lines.append(f"# --- {p.id}  [{tag}, {p.mode}] ---")
        lines.append(p.text)
        lines.append("")
    return "\n".join(lines)
