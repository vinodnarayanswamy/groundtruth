"""SQLite store for code-graph nodes and edges.

Supports incremental re-indexing: nodes whose content_hash is unchanged
are left untouched; changed or new nodes are upserted and their outgoing
edges rebuilt.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .indexer import Edge, Import, IndexResult, Node

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
    content_hash  TEXT NOT NULL,
    source_path   TEXT
);
CREATE TABLE IF NOT EXISTS edges (
    src         TEXT NOT NULL,
    dst         TEXT NOT NULL,
    rel         TEXT NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0,
    confidence  TEXT,
    PRIMARY KEY (src, dst, rel)
);
CREATE TABLE IF NOT EXISTS imports (
    file    TEXT NOT NULL,
    module  TEXT,
    symbol  TEXT,
    PRIMARY KEY (file, module, symbol)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(rel);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file);
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
                "INSERT OR REPLACE INTO edges(src, dst, rel, resolved, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (e.src, e.dst, e.rel, int(e.resolved), e.confidence or None),
            )

        # Rebuild import records for every file present in this result.
        for f in {imp.file for imp in result.imports}:
            cur.execute("DELETE FROM imports WHERE file = ?", (f,))
        for imp in result.imports:
            cur.execute(
                "INSERT OR REPLACE INTO imports(file, module, symbol) "
                "VALUES (?, ?, ?)",
                (imp.file, imp.module, imp.symbol),
            )
        self.conn.commit()
        return stats

    def _insert_node(self, cur, n: Node):
        cur.execute(
            "INSERT OR REPLACE INTO nodes"
            "(id, kind, name, file, start_line, end_line, signature,"
            " docstring, content_hash, source_path) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (n.id, n.kind, n.name, n.file, n.start_line, n.end_line,
             n.signature, n.docstring, n.content_hash, n.source_path or n.file),
        )

    # --- resolution ----------------------------------------------------------
    def _imports_for(self, file: str):
        """Return (direct_symbol_modules, module_stems) for an importing file.

        - direct_symbol_modules: {callee_name: {module, ...}} from
          ``from module import callee`` bindings (the strongest signal).
        - module_stems: set of last module components from any import, used to
          match a candidate definition's file stem.
        """
        rows = self.conn.execute(
            "SELECT module, symbol FROM imports WHERE file = ?", (file,)
        ).fetchall()
        direct: dict[str, set] = {}
        stems: set = set()
        for r in rows:
            module = r["module"] or ""
            last = module.split(".")[-1] if module else ""
            if last:
                stems.add(last)
            if r["symbol"]:
                direct.setdefault(r["symbol"], set()).add(module)
        return direct, stems

    @staticmethod
    def _stem(path: str) -> str:
        base = path.rsplit("/", 1)[-1]
        return base[:-3] if base.endswith(".py") else base

    def resolve_calls(self) -> int:
        """Best-effort: rewrite bare callee names to node ids by name match.

        Resolution tiers, most trustworthy first:
          1. same_file — a unique definition of the name in the caller's file
          2. import   — a unique candidate reachable via the file's imports
          3. global   — a single definition of the name anywhere in the graph
        Ambiguous names are left unresolved. The chosen tier is recorded in
        ``edges.confidence``. Returns count resolved.
        """
        cur = self.conn.cursor()
        unresolved = cur.execute(
            "SELECT rowid, src, dst, rel FROM edges "
            "WHERE rel IN ('CALLS','INHERITS') AND resolved = 0"
        ).fetchall()

        imports_cache: dict = {}
        resolved = 0
        for row in unresolved:
            src_file = row["src"].split("::", 1)[0]
            name = row["dst"]

            same = cur.execute(
                "SELECT id, file, source_path FROM nodes WHERE name = ? AND file = ?",
                (name, src_file),
            ).fetchall()
            choice = confidence = None
            if len(same) == 1:
                choice, confidence = same[0]["id"], "same_file"
            elif len(same) == 0:
                allc = cur.execute(
                    "SELECT id, file, source_path FROM nodes WHERE name = ?",
                    (name,),
                ).fetchall()
                if len(allc) == 1:
                    choice, confidence = allc[0]["id"], "global"
                elif len(allc) > 1:
                    # Disambiguate ambiguous names via the caller's imports.
                    if src_file not in imports_cache:
                        imports_cache[src_file] = self._imports_for(src_file)
                    direct, stems = imports_cache[src_file]
                    direct_mods = direct.get(name, set())
                    reachable = []
                    for c in allc:
                        cand_stem = self._stem(c["file"])
                        # symbol-import of this exact name from a matching module
                        hit = any(
                            (m.split(".")[-1] if m else "") == cand_stem
                            or cand_stem in m.split(".")
                            for m in direct_mods
                        )
                        # or any import whose module stem matches the cand file
                        if hit or cand_stem in stems:
                            reachable.append(c)
                    if len(reachable) == 1:
                        choice, confidence = reachable[0]["id"], "import"

            if choice is not None:
                cur.execute(
                    "UPDATE edges SET dst = ?, resolved = 1, confidence = ? "
                    "WHERE rowid = ?", (choice, confidence, row["rowid"]),
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
