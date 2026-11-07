# Native context compaction

## Contract

The native backend automatically compacts at 80% of its conservative context
budget. This is **UTF-8 JSON bytes + 64 bytes/message + output reserve**, not a
model tokenizer or provider token count. Model context/output limits still come
from saved metadata; no larger provider window is guessed.

- `state.json` remains version 1. Original `transcripts`, visible `messages`,
  tool replay and `patches.json` are retained. Old stores need no migration.
- `session.compaction` holds `{boundary, summary, created, count}`. The boundary
  is an original transcript index immediately before a safe user turn. Invalid
  checkpoints fail loading rather than silently dropping history.
- Requests use a plain-text historical summary followed by the unchanged suffix.
  The latest user turn, including all of its tool calls/results, is retained.
  Summary text is untrusted reference data, never permission or a tool request.
- Summary requests use the selected provider/protocol/key/model, no callable
  tools, at most `min(model output, 2048)` output tokens, and no automatic retry.
  They are additional billable provider requests. Long histories are processed
  in bounded batches ending at complete user-turn boundaries, never clipped.
- A candidate checkpoint is committed atomically only after every required
  batch finishes and the resulting agent request is smaller and within budget.
  Empty/truncated/failed/secret-containing/tool-producing summaries, Stop and
  background cancellation leave the previous checkpoint unchanged.
- A current turn or individual historical turn that cannot fit is an explicit
  error. Compaction does not silently clip file contents or tool results.
- Summary generation does not execute files or write patch history. Opening a
  compacted session makes no provider request.
- UI session metadata `nativeContext` contains conservative `input`, `reserve`,
  `limit`, `estimated: true`, and `compacting`. The context popover labels the
  estimate, the active compaction and its saved count. Provider usage remains
  distinct. Crash recovery clears the active compaction flag.

## Verification

- `NativeContextCompactionTests`: pure budget, safe call/result boundaries,
  portable textual summary inputs, repeated checkpoints and suffix preservation.
- `NativeAgentCompactionTests`: both protocols, between-step compaction, full
  persistence/reopen, failed/empty/truncated/tool summaries, cancellation,
  oversized current turns, bounded batching, crash recovery and invalid stores.
- `test-native-file-agent-ui.py`: native estimate updates, in-progress/saved
  compaction, reopen hydration, stale replies/sessions, fallback and layout.
- `CompactionAcceptance.swift`: isolated physical-iPad harness with one scripted
  9000-byte seed followed by exactly one live summary and one live continuation.
  Checks a remembered fact, preserved original rows, smaller budgets and
  no-generation reopen. Reads existing config/Keychain only; never changes user
  sessions, credentials or files. Uses a fresh test root and a 175-second deadline.
  Output is fixed check categories/counts only; model text and credentials are
  not logged. Build with `ios-diagnostics.yml`, artifact `KumoCompactionAcceptance`.

Local Windows checks passed: Swift tree-sitter syntax, 10 staging/Node tests,
29 Playwright scenarios and patch-boundary guard. Swift compilation/XCTest and
physical-device proof must be recorded from their actual runs, not inferred from
these checks.

## Work log

1. Inspected actor lifecycle, persistence, provider replay and existing context UI.
2. Added pure boundary/budget/summary helpers so policy has one implementation.
3. Added behavioral core tests before wiring auto-compaction into the actor.
4. Integrated bounded summarization and atomic checkpoint commit, preserving
   original history and cancellation ownership.
5. Added native-only UI metadata and fail-closed staging/browser regressions;
   Android canonical HTML was not modified.
6. Reviewed cancellation/recovery and fixed batch scanning, crash flags,
   completed/error counters, replay-only estimates and counter overflow.
7. Added isolated live-device acceptance and its CI build target.
