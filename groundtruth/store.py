"""SQLite store for code-graph nodes and edges.

Supports incremental re-indexing: nodes whose content_hash is unchanged
are left untouched; changed or new nodes are upserted and their outgoing
edges rebuilt.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .indexer import Edge, IndexResult, Node

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    name          TEXT NOT NULL,
    file          TEXT NOT NULL,
    start_line    INTEGER,
    end_line      INTEGER,
    signature     TEXT,
    docstring     TEXT,
    content_hash  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    src       TEXT NOT NULL,
    dst       TEXT NOT NULL,
    rel       TEXT NOT NULL,
    resolved  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(rel);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file);
"""


class GraphStore:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def close(self):
        self.conn.close()

    # --- writes --------------------------------------------------------------
    def upsert(self, result: IndexResult) -> dict:
        """Incrementally apply an IndexResult. Returns change stats."""
        cur = self.conn.cursor()
        stats = {"inserted": 0, "updated": 0, "unchanged": 0}

        for n in result.nodes:
            row = cur.execute(
                "SELECT content_hash FROM nodes WHERE id = ?", (n.id,)
            ).fetchone()
            if row is None:
                self._insert_node(cur, n)
                stats["inserted"] += 1
            elif row["content_hash"] != n.content_hash:
                self._insert_node(cur, n)  # REPLACE
                cur.execute("DELETE FROM edges WHERE src = ?", (n.id,))
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1

        for e in result.edges:
            cur.execute(
                "INSERT OR REPLACE INTO edges(src, dst, rel, resolved) "
                "VALUES (?, ?, ?, ?)",
                (e.src, e.dst, e.rel, int(e.resolved)),
            )
        self.conn.commit()
        return stats

    def _insert_node(self, cur, n: Node):
        cur.execute(
            "INSERT OR REPLACE INTO nodes"
            "(id, kind, name, file, start_line, end_line, signature,"
            " docstring, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (n.id, n.kind, n.name, n.file, n.start_line, n.end_line,
             n.signature, n.docstring, n.content_hash),
        )

    # --- resolution ----------------------------------------------------------
    def resolve_calls(self) -> int:
        """Best-effort: rewrite bare callee names to node ids by name match.

        Same-file matches win; otherwise a unique global match is used.
        Ambiguous names are left unresolved. Returns count resolved.
        """
        cur = self.conn.cursor()
        unresolved = cur.execute(
            "SELECT rowid, src, dst, rel FROM edges "
            "WHERE rel IN ('CALLS','INHERITS') AND resolved = 0"
        ).fetchall()

        resolved = 0
        for row in unresolved:
            src_file = row["src"].split("::", 1)[0]
            name = row["dst"]
            # prefer same-file definition
            cand = cur.execute(
                "SELECT id FROM nodes WHERE name = ? AND file = ?",
                (name, src_file),
            ).fetchall()
            if not cand:
                cand = cur.execute(
                    "SELECT id FROM nodes WHERE name = ?", (name,)
                ).fetchall()
            if len(cand) == 1:
                cur.execute(
                    "UPDATE edges SET dst = ?, resolved = 1 "
                    "WHERE rowid = ?", (cand[0]["id"], row["rowid"]),
                )
                resolved += 1
        self.conn.commit()
        return resolved

    # --- reads ---------------------------------------------------------------
    def get_node(self, node_id: str):
        return self.conn.execute(
            "SELECT * FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()

    def neighbors(self, node_id: str, rels=None):
        """Outgoing + incoming resolved neighbors of a node."""
        rels = rels or ["CALLS", "INHERITS", "DEFINED_IN", "REFERENCES"]
        ph = ",".join("?" * len(rels))
        out = self.conn.execute(
            f"SELECT dst AS other, rel, 'out' AS dir FROM edges "
            f"WHERE src = ? AND resolved = 1 AND rel IN ({ph})",
            (node_id, *rels),
        ).fetchall()
        inc = self.conn.execute(
            f"SELECT src AS other, rel, 'in' AS dir FROM edges "
            f"WHERE dst = ? AND resolved = 1 AND rel IN ({ph})",
            (node_id, *rels),
        ).fetchall()
        return list(out) + list(inc)

    def counts(self) -> dict:
        n = self.conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
        e = self.conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
        r = self.conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE resolved = 1"
        ).fetchone()["c"]
        return {"nodes": n, "edges": e, "resolved_edges": r}
