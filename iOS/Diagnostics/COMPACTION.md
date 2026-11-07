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

## Manual compaction

The native context popover and Context settings sheet offer **Сжать сейчас**.
It uses the selected provider/model/effort and build/plan mode, ignoring the
80% trigger but otherwise sharing automatic compaction's safety and budget
checks. A short cost hint explains that summarization is a provider request.

`POST /session/{sessionID}/compact` accepts optional `model` (providerID/modelID),
`variant`, and `agent`. It returns `{started:true}` immediately and publishes
normal busy/context/idle events. It does not append user/assistant messages or
run a tool-capable continuation. The whole latest completed user turn remains.
An empty/single-turn/already-compacted history returns
`{started:false,reason:"nothing_to_compact"}` with no writes or model calls.
Busy sessions and mismatched directories are rejected before starting.

Manual runs have a separate active `kind:"compaction"` marker and no owned chat
turn: error, abort, backgrounding and crash recovery must not mark any older
message/tool as interrupted. `session.compactionError` reports errors separately
and clears to JSON null on a new manual start. The existing Stop button cancels
summarization. Immediate cancellation is checked before any deferred task write.
UI double-click protection is session-scoped, and delayed acknowledgements do
not overwrite another thread or undo Stop. Rejected admission feedback survives
switching away/back; successful retry clears only its own previous chat error.

Regression coverage adds 12 backend tests and 9 browser scenarios for manual
requests, repeated no-op, selection, untouched transcript/UI, failure/cancellation,
crash/reopen, immediate-abort scheduling, busy guards and stale feedback.
The standalone acceptance harness accepts a final `manual` argument: two exact
scripted turns below 80%, one live summary, a zero-call no-op, and only then an
explicit live recall request. It uses the unchanged production model transport.

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
calls `URLSessionNativeModelClient` directly.

Final production-transport device gate: `ios-diagnostics` run `42377565503`,
revision `0aef1be`. Exactly two live streams passed, recovered the synthetic
fact, and reduced request budget **17894 → 9181** (final estimate 12117).
Original rows, patch history and user state were unchanged; zero-generation
reopen passed. Safe count-only evidence is kept locally in
`.local/compaction-proof-0aef1be/device.log`.

Installed IPA: app build `42377309975` (`e09b245`; subsequent commits changed
only the harness/docs), signed binary SHA256
`d7dae399bf77219ed55711a74cfd363fdc13690379965cf432b941f4d27bc83e`.
Installation and path migration preserved **10 existing sessions** and both
provider configurations. Physical WKWebView launch passed `didFinish`, DOM
probe and native bridge smoke (two providers, project files available); no
JavaScript error appeared in the captured launch log. Context-indicator behavior
is covered by Chromium scenarios, not by a physical WKWebView interaction probe.
Screenshots were unavailable because the device screenshot/developer service
could not start; no UI taps were automated.

Final app/core/browser CI `42377565263` also passed all 234 XCTest and the UI
job. The temporary standalone helper and its signing-grant file were removed;
UI-host entitlements remain minimal.

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
7. Added isolated live-device acceptance and its CI build target.
8. Ran macOS compilation/XCTest, browser CI and physical iPad checks; rejected
   failed model/recall gates, refined exact-fact retention and removed temporary
   transport duplication before final production validation.
9. Passed the production-client device gate, installed the IPA with data backup
   and container path remapping, verified physical bridge/DOM launch, and removed
   the temporary standalone helper/grant.
10. Added manual summary-only runs and context buttons, wrote behavioral tests,
    reviewed cancellation/feedback races, and added a below-threshold device gate.
    Local manual UI validation: 12 staging/Node tests and 38 browser scenarios
    passed; Swift/device outcomes must be taken from their actual CI/live runs.
