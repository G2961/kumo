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
29 Playwright scenarios and patch-boundary guard. macOS CI `42377309975`
(`e09b245`) passed all **234 XCTest** and built the app; the UI job passed.

Physical iPad6,11 / iOS 16.7.12 proof (`ios-diagnostics` run `42377309804`):
one live AgentRouter summary plus one live continuation both completed HTTP 200.
The continuation recovered the exact synthetic non-secret fact. Request budget
fell from **17894 to 9240**, the final estimate was 12388 (including opaque
provider replay), original transcript/UI rows were preserved, patch history and
user state stayed unchanged, and reopening made zero model requests.
This run used a temporary harness-local transport observer sharing the production
request builder/parser/decoder; the final harness removes that duplicate and
calls `URLSessionNativeModelClient` directly. A final production-transport run
and installed IPA verification are recorded separately below.

Earlier gates were deliberately treated as failures: invalid JSON in the budget
needed `isValidJSONObject` to avoid Foundation exceptions; one provider summary
failed/incompleted without committing; another completed summary retained the
recall goal but omitted its factual value. The summary instructions now explicitly
preserve exact non-secret task values. Summaries are still lossy model output,
not a guarantee of perfect recall; the full original transcript remains available.

Standalone jailbreak CLI signing requires platform/no-container rights on this
device. These temporary grants are **not** added to the UI host/IPA; keep its
minimal WebKit-compatible entitlements. Remove the helper/grant after validation.

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
8. Ran macOS compilation/XCTest, browser CI and physical iPad checks; rejected
   failed model/recall gates, refined exact-fact retention and removed temporary
   transport duplication before final production validation.
