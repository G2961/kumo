# Resume failed native model requests

Native Retry now uses **Продолжить** when the latest failed assistant message has
`info.nativeResume: true`. It calls `POST /session/{sessionID}/resume` with only
`{messageID}`; it never deletes history, resubmits user text, or automatically
aborts a busy session. Without a safe checkpoint the UI explains that a new
message is required instead of silently regenerating the turn.

Before each agent model request the backend persists a session `resume`
checkpoint: original user/message IDs, transcript boundary, provider/model,
agent/variant, and cumulative step. On a recoverable network/HTTP/truncated or
provider failure it keeps that checkpoint and publishes the continuation marker.
There are no automatic billable retries. Resuming adds only a new assistant
message under the same user turn and retries the failed request, with completed
assistant/tool-call/tool-result history retained. Already executed tools are not
re-executed by the retry loop.

The checkpoint is cleared **before** any tool execution after a verified provider
terminal event. A cancelled/unknown file-operation outcome is not resumable:
inspect the file and send a new message rather than risking a duplicate write.
Stop/background/crash do not auto-resume. The 32-step limit remains cumulative;
retrying does not start a fresh tool-step allowance. Selection comes from the
saved checkpoint, not a newly selected model.

Unfinished provider text/replay remains on disk and visible in chat, marked
`excludedFromContext: true` in the original transcript. It is excluded from
subsequent model requests, compaction summaries, budget and tool-ID bookkeeping.
This also applies to nonrecoverable stream errors and cancellation/crash while a
model checkpoint is active: an incomplete tool preview cannot become execution.
Completed transcripts and native patch history are never trimmed or rolled back.

Snapshots remain version 1 with optional fields; old sessions load unchanged,
but failures recorded before this feature have no safe resume checkpoint.
New prompts/manual compaction invalidate old continuation buttons. Stale,
busy, foreign-directory and malformed resume requests fail without writes.

Validation:
- `NativeAgentResumeTests`: both provider formats; completed write followed by
  TLS failure and resume; manual file edit is not overwritten; exact retained
  tool pair; saved partial/repeated failure/reopen; selection; rejection guards;
  cancellation/truncated preview; crash; cumulative limits.
- `NativeAgentCompactionTests`: failed partials excluded from the active budget
  while the complete stored transcript/UI remains intact.
- Browser scenarios: resume payload, preserved previous messages, no prompt/delete/
  abort fallback, pending/busy/stale responses and nonresumable feedback.

Work log: inspected legacy Retry and agent persistence; added tests; shared the
existing drive loop with a persisted request checkpoint; wired native-only UI;
reviewed incomplete-provider versus uncertain-tool boundaries. Paused physical
compaction-probe work remains separate/uncommitted and is not included in this
change. No device provider requests or reboot/respring commands are used for
this task's validation.
