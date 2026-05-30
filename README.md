# groundtruth

A tree-sitter code knowledge graph for **token-efficient code retrieval and generation**.

Instead of stuffing whole files into an LLM prompt so the model can re-derive
how your code fits together, `groundtruth` parses your source into a graph of
definitions and relationships, then retrieves only the *relevant* slice for a
given task — the target symbol plus its ranked dependency neighborhood, packed
under a token budget.

> Status: early prototype (v0.1.0). Python only. See the roadmap for what's next.

## Why

LLM code generation often pays for context twice: once to send the surrounding
code, and again for the model to infer relationships it can't see. A code
knowledge graph makes those relationships explicit and queryable, so retrieval
becomes *deterministic* and *compact*:

- **Nodes** — functions, methods, classes (with signatures, docstrings, spans)
- **Edges** — `CALLS`, `INHERITS`, `DEFINED_IN` (typed, not just proximity)
- **Retrieval** — N-hop neighborhood, ranked by hop distance and edge relevance,
  trimmed to a token budget with graceful degradation to signatures.

## Install

```bash
pip install -e ".[dev]"
```

## Quickstart

```bash
python -m examples.demo examples/sample_app.py
```

Or from Python:

```python
from groundtruth import index_path, GraphStore, context_pack

result = index_path("your_package/")      # parse files into nodes + edges
store = GraphStore("graph.db")            # SQLite store (or ":memory:")
store.upsert(result)                      # incremental: unchanged nodes skipped
store.resolve_calls()                     # best-effort name -> node resolution

pack = context_pack(store, "your_package/mod.py::ClassName.method",
                    max_tokens=2200, hops=2)
print(pack["prompt"])                     # feed this to your model
```

## How it works

```
 source files
      │  tree-sitter parse + query
      ▼
   nodes + raw edges            (indexer.py)
      │  incremental upsert (content-hash)
      ▼
   SQLite graph store           (store.py)
      │  best-effort call/inherit resolution
      ▼
   resolved graph
      │  BFS neighborhood → rank → budget-pack
      ▼
   context pack (prompt)        (retrieval.py)
```

Indexing is incremental: each node stores a content hash, so re-indexing only
touches changed nodes and rebuilds their outgoing edges.

## Limitations (and the resolution gap)

tree-sitter resolves *definitions* and *same-file calls* exactly, but
*cross-file call resolution* is best-effort by name. Ambiguous names (e.g. two
methods both called `save`) are intentionally left unresolved rather than
guessed. Closing this gap with type information (an LSP, or Jedi for Python) is
the main accuracy lever — see the roadmap.

## Roadmap

- [ ] Type-aware cross-file resolution (LSP / Jedi) for exact call edges
- [ ] PageRank-style salience ranking layered on typed edges
- [ ] Real tokenizer for budget accuracy (currently a ~4 chars/token heuristic)
- [x] File-span body extraction (prototype reconstructs stubs from metadata)
- [ ] Additional language grammars (JS/TS, Java, Go)
- [ ] Incremental watch mode

## License

MIT — see [LICENSE](LICENSE).
