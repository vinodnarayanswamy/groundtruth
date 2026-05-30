# Contributing

Thanks for your interest in contributing!

## Development setup

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

## Guidelines

- Keep the core dependency-light (tree-sitter + grammars). Heavier resolution
  backends (LSP, Jedi) should be optional extras.
- Add a test for any new extraction or resolution behavior.
- One source of truth for node IDs: use `_qualified_id` in the indexer.

## Good first issues

See the roadmap in the README — type-aware resolution and additional language
grammars are the highest-impact areas.
