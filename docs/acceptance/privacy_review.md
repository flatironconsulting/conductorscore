# Privacy review (Task 11.6)

The privacy claim is "Numbers, hashes, and known categoricals only. Never prompts, code, file contents, or paths."

## Tier 1 test (the canonical contract)

`pytest tests/test_extractor_integration.py::test_extracted_json_contains_no_session_content`

This test:
1. Builds a fake `~/.claude/projects/-tmp-foo/abc-secret-session.jsonl` with the literal string `TOPSECRET_PHRASE_DO_NOT_LEAK_42`
2. Calls `extract(...)` and `to_json()`
3. Asserts the secret phrase is absent from the JSON
4. Asserts the raw session id is absent (only its sha256[:16] appears)
5. Asserts the project path is absent (only its sha256[:16] appears)

**Status: passing on current main.** Re-run after every extractor change.

## Tier 3 manual checks (post-launch)

### --dry-run audit (Task 11.6 Step 2)
On author's machine:
```bash
python3 -m scripts.run --dry-run > payload.json
grep -E "[a-zA-Z]{10,}" payload.json
```
Manually verify every long non-hash string is a known categorical (model id, tool name, signal enum). Tabulate findings below.

### tcpdump comparison (Task 11.6 Step 3 — requires sudo)
```bash
sudo tcpdump -i any -w net.pcap host conductorscore.com &
python3 -m scripts.run
# Decode pcap and compare bytes against the --dry-run JSON
```
This step requires sudo and is not part of CI. Document findings here once run.

## Findings (post-launch sweep)

(Empty until first manual run.)
