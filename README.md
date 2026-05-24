# csclient — ConductorScore client

Python skill that extracts structural metrics from local Claude Code transcripts and uploads numeric-only results to [conductorscore.com](https://conductorscore.com).

**Privacy-load-bearing:** only numbers, hashes, and known categoricals cross the wire. No prompts, no code, no file contents. The privacy-invariant test lives in this repo so anyone can clone and verify.

## Install

```
Claude, install conductorscore from https://conductorscore.com/install.md
```

## Local development

```bash
pip install -e ".[dev]"
pytest -v
```

See `WIRE_FORMAT.md` for the schema the client emits.

## License

Apache License 2.0 — see `LICENSE`.
