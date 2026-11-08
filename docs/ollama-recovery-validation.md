# Ollama recovery validation

2026-11-08, MCP branch. Reused the existing Python environment and local model; no dependency installation, model download, or paid API call.

## Restored behavior

Only known local connection/runner failures retry the same generation request, at most five attempts with 2/4/6/8-second delays. Other errors remain explicit. Model checks remain single-attempt. Per-cue checkpoints and zero-cost usage recording are unchanged. See [setup](setup.md) for the error contract.

## Checks

- Before the implementation was restored, the restored regression tests produced 4 failures and 18 passes, detecting missing runner/connection recovery and retry bounds.
- After restoration, `uv run --directory backend --no-sync pytest -q tests/test_ollama_translate.py`: 22 passed. Tests cover repeated requests for the same cue, existing translation preservation, one usage record on success, retry bounds, non-retryable errors, safe error messages, and connection recovery.
- Full backend regression, `uv run --directory backend --no-sync pytest -q`: 581 passed in 204.37 seconds, with one existing duplicate-ZIP-entry warning.
- Manual local-model replay used the production request function and original translation parameters, with a limit of 60 cues. The first cue exhausted all five attempts due to continuing runner HTTP 500 errors; the expected 2/4/6/8-second waits were logged. No cue completed in this check. The source cues file SHA-256 remained unchanged.
- The live backend was not restarted: a fresh read-only task check found an active ASR job, so the restart was aborted before stopping any process. The running backend needs a restart after processing finishes to load this change.

## Limitation

The retry behavior is restored; real-model recovery was not demonstrated in this run. Persistent Ollama/Windows runner failure can still exhaust the retry budget. This change does not claim to fix that underlying failure, and no full course was rerun.
