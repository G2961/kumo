# File-agent acceptance — milestone 1

Verified on **iPad6,11, iOS 16.7.12**, 2026-11-06, with the user's configured
**AgentRouter / gpt-6.1-sol / Responses** and the actual native core. No OpenCode
process, localhost backend or scripted model was used in the device scenarios.

## Results

| Scenario | Device result | Live generation requests |
| --- | --- | --- |
| Allow | Read seed → proposed actual diff → one `once` approval → edit → reread after-value → final answer; one patch record | 4 |
| Reject | One rejected approval; real tool error returned to the model; final answer; original seed and empty history preserved | 3 |
| Stop | Abort during pending approval; pending tool marked error, approval removed, stale Allow rejected; no late write/history | 2 |
| Concurrent user edit | Changed seed to user-value while approval waited; Allow refused the stale mutation, preserved user-value; no patch record | 3 |
| Reopen | Separate process restores completed messages and patch byte-identically; disk unchanged; no model request or edit replay | 0 |
| Interrupted process | Exit with real active marker and pending tool; new process marks interrupted, no write/replay; second reopen idempotent | 2 to prepare, 0 to reopen |

14 live generation requests in total. These were bounded developer acceptance
calls and may consume provider quota. Keys were read only inside Keychain; no
credential was sent through SSH, CLI arguments, logs, a test report or JavaScript.
The test backend had its own unique storage and projects under Documents, outside
the user app's state. User state bytes were checked unchanged after each scenario.
Test sessions were never added to the user's conversation list.

The concurrent-edit case deliberately stops the test turn after observing the
actual conflict tool error; it does **not** claim the model naturally completed.
Stop likewise remains an interrupted turn, not a successful assistant answer.
Crash preparation uses explicit process exit without backend shutdown, not an
OS-induced crash; recovery reads the genuine persisted active marker in another
process. Successful-message history is verified by its exact serialized content,
not by checking merely that a file exists.

## UI bug fixed

A real failing browser test found that `permCard()` did not render native
`metadata.diff`. Backend approval preparation was correct, but the user could
approve without seeing the change.

The iOS staging transform now adds an open **Proposed changes** preview for actual
prepared write/edit/apply_patch diffs. It uses the existing escaped diff renderer,
shows the file path and preserves all actual lines inside a bounded scroll area.
No preview is manufactured from tool inputs or before/after snapshots when no
prepared diff exists. Canonical Android assets are unchanged.

`test-native-file-agent-ui.py` runs the entire staged page and its actual bridge
transport/event handlers/renderers with a strict UI-only backend fixture:

- Prepared diff and old/new lines are visible **before** Approve.
- HTML/script-like file names and diff content render as text, not executable HTML.
- Missing, malformed and partial diff data do not invent a preview.
- Long diffs scroll without dropping actual lines.
- Approve sends exactly one `once`; Deny sends `reject`; duplicate click is ignored.
- Stop targets the current session and reconciles its snapshot.
- Native ledger IDs/patches and restored reasoning/tool/text chronology are retained.
- Foreign-session events do not change the current permission/state.

**11 UI scenarios passed**, plus **9 existing staging tests** and the existing
**44 chronology checks**. UI fixtures are not a live model or a physical
WKWebView test. The corrected HTML was installed on the iPad with all seven user
sessions and both provider configurations preserved; the device was locked for
post-install launch, so the new preview was not clicked in WKWebView itself.

## Reproduce

```sh
python iOS/scripts/test-stage-native-ui.py
python iOS/scripts/test-native-file-agent-ui.py
```

The latter uses the repository's pinned Playwright dependency and installed
Chromium/Chrome. Its static asset server and browser are finite and shut down on
completion. CI runs it in a separate Linux job and uploads screenshots/report.
Core tests and the iOS build run separately on macOS; no errors are suppressed.

The explicit `ios-diagnostics.yml` workflow builds `KumoFileAgentAcceptance`.
It is **not bundled into Kumo**. Invoke with the native state path, provider ID,
a new empty test root and one of `allow`, `reject`, `stop`, `conflict`, `reopen`,
`crashPrepare`, `crashReopen`. `reopen` uses the allow root; crash modes share a
separate root. Each live scenario has a generation cap, 120-second scenario
budget and 175-second process watchdog. Recovery must perform zero generations.
Only the owner's existing jailbreak signing environment can run the standalone
helper with a temporary Kumo-only Keychain grant.

On Windows Git Bash, set `MSYS_NO_PATHCONV=1` when passing literal iOS absolute
paths to Python; otherwise MSYS rewrites `/var/mobile/...` before the SSH command.
The harness rejects empty execution/reports: zero live generations or missing
scenario records cannot produce a PASS. The tested harness used `-Onone`; earlier
optimized diagnostic execution produced an empty false report and was discarded.
The exact cause of that optimizer/runtime behavior is not established; product
Release code is separately compiled and covered by core tests.

## Scope and remaining limits

The device probe shares the app's source-level native core and real provider, but
runs as a separately signed diagnostic process, not under the UI host's normal
sandbox. The actual app can use app-owned projects and security-scoped folders;
this probe does not prove access to every external iOS file/provider.
Read/edit preparation can read a snapshot before the current permission decision;
external data is not delivered/executed after a deny, but this is not an OS-level
read sandbox. Portable POSIX replacement still has a narrow race with unrelated
writers after the last precondition check. Neither limitation was concealed.

No Git undo, compaction, shell, npm, MCP, subagents or unrelated features are
claimed by this gate. Milestone 1 verifies native file tools, approvals, Stop,
truthful patch history and recovery; context compaction remains the next milestone.
