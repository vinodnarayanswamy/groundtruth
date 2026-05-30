"""Tree-sitter based code indexer.

Parses Python source into graph nodes (definitions) and edges
(calls, imports, inheritance), with per-node content hashing for
incremental re-indexing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Parser, Query, QueryCursor

PY_LANGUAGE = Language(tspython.language())

# --- Queries -----------------------------------------------------------------
# Definitions, calls, imports and inheritance. Captures are processed by name.
_QUERY_SRC = """
(class_definition
    name: (identifier) @class.name
    superclasses: (argument_list)? @class.bases) @class.def

(function_definition
    name: (identifier) @func.name) @func.def

(call
    function: [
        (identifier) @call.name
        (attribute attribute: (identifier) @call.name)
    ]) @call.site

(import_from_statement
    module_name: [(dotted_name) (relative_import)] @import.module) @import.from

(import_statement
    name: (dotted_name) @import.name) @import.plain
"""

_QUERY = Query(PY_LANGUAGE, _QUERY_SRC)


@dataclass
class Node:
    id: str
    kind: str  # function | method | class
    name: str
    file: str
    start_line: int
    end_line: int
    signature: str
    docstring: str
    content_hash: str


@dataclass
class Edge:
    src: str
    dst: str  # may be an unresolved bare name until resolution pass
    rel: str  # CALLS | INHERITS | IMPORTS | DEFINED_IN
    resolved: bool = False


@dataclass
class IndexResult:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _enclosing_def(node):
    """Walk up to the nearest function/class definition node."""
    cur = node.parent
    while cur is not None:
        if cur.type in ("function_definition", "class_definition"):
            return cur
        cur = cur.parent
    return None


def _signature(defn, src: bytes) -> str:
    """Extract the def/class header line(s) up to the colon."""
    text = _text(defn, src)
    head = text.split(":", 1)[0]
    return " ".join(head.split())


def _docstring(defn, src: bytes) -> str:
    """First line of a docstring if the body starts with a string literal."""
    body = defn.child_by_field_name("body")
    if body is None or body.child_count == 0:
        return ""
    first = body.children[0]
    if first.type == "expression_statement" and first.child_count:
        inner = first.children[0]
        if inner.type == "string":
            raw = _text(inner, src).strip("\"'")
            return raw.strip().splitlines()[0] if raw.strip() else ""
    return ""


def _node_name_for(defn, src: bytes) -> str:
    name_node = defn.child_by_field_name("name")
    return _text(name_node, src) if name_node else "<anon>"


def _qualified_id(defn, path: str, src: bytes) -> str:
    """Fully-qualified node id for a definition, e.g.
    'file.py::Class.method' for methods, 'file.py::func' otherwise."""
    name = _node_name_for(defn, src)
    if defn.type == "function_definition":
        enc = _enclosing_def(defn)
        if enc is not None and enc.type == "class_definition":
            return f"{path}::{_node_name_for(enc, src)}.{name}"
    return f"{path}::{name}"


def index_source(path: str, src: bytes) -> IndexResult:
    """Index a single source buffer into nodes and edges."""
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(src)
    cursor = QueryCursor(_QUERY)
    caps = cursor.captures(tree.root_node)

    result = IndexResult()

    # Definitions: classes and functions/methods.
    def_nodes = caps.get("func.def", []) + caps.get("class.def", [])
    for defn in def_nodes:
        name = _node_name_for(defn, src)
        kind = "class" if defn.type == "class_definition" else "function"
        # method vs function: enclosed in a class?
        if kind == "function":
            enc = _enclosing_def(defn)
            if enc is not None and enc.type == "class_definition":
                kind = "method"
        node_id = _qualified_id(defn, path, src)

        body = src[defn.start_byte:defn.end_byte]
        result.nodes.append(
            Node(
                id=node_id,
                kind=kind,
                name=name,
                file=path,
                start_line=defn.start_point[0] + 1,
                end_line=defn.end_point[0] + 1,
                signature=_signature(defn, src),
                docstring=_docstring(defn, src),
                content_hash=hashlib.sha1(body).hexdigest(),
            )
        )

        # DEFINED_IN edge for methods.
        if kind == "method":
            enc = _enclosing_def(defn)
            result.edges.append(
                Edge(src=node_id,
                     dst=f"{path}::{_node_name_for(enc, src)}",
                     rel="DEFINED_IN", resolved=True)
            )

        # INHERITS edges for classes.
        if kind == "class":
            bases = defn.child_by_field_name("superclasses")
            if bases is not None:
                for child in bases.named_children:
                    base_name = _text(child, src)
                    result.edges.append(
                        Edge(src=node_id, dst=base_name, rel="INHERITS")
                    )

    # Call edges: src is the enclosing definition, dst is the bare callee name.
    for call in caps.get("call.site", []):
        enc = _enclosing_def(call)
        if enc is None:
            continue  # module-level call; skip for now
        enc_id = _qualified_id(enc, path, src)
        # find the captured callee name within this call
        fn_field = call.child_by_field_name("function")
        if fn_field is None:
            continue
        if fn_field.type == "attribute":
            callee = _text(fn_field.child_by_field_name("attribute"), src)
        else:
            callee = _text(fn_field, src)
        result.edges.append(Edge(src=enc_id, dst=callee, rel="CALLS"))

    return result


def index_path(root: str) -> IndexResult:
    """Index a file or recursively index a directory of .py files."""
    p = Path(root)
    merged = IndexResult()
    files = [p] if p.is_file() else sorted(p.rglob("*.py"))
    for fp in files:
        src = fp.read_bytes()
        res = index_source(fp.as_posix(), src)
        merged.nodes.extend(res.nodes)
        merged.edges.extend(res.edges)
    return merged
