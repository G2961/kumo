# Reasoning timeline and separate tool activity

## Change

`app/src/main/assets/codex.html` no longer passes tool parts to the Thought renderer.

- Separate Thought blocks for contiguous reasoning segments: reasoning → tools → new reasoning → answer, including within one assistant message. Stable keys use the message ID and first part ID; later deltas never append to earlier segments.
- Live reasoning uses a quiet full-width row, a title from an explicit Markdown heading or `Reasoning`, and a separate duration. No brain pill, checklist, sentence summary or nested `Full reasoning`.
- Original native/tag reasoning is displayed once, preserving whitespace. A single layout-measured `Show more` expands long text; no fabricated titles or additional model calls.
- Native reasoning and provider `<think>`/`<thought>` tags are supported. Literal tags inside Markdown code remain in the answer; leading interrupted reasoning remains separate after abort/reload.
- Duration uses the union of recorded reasoning intervals, excluding command execution and gaps. Missing timestamps do not produce an invented duration.
- Every local/SSH tool owns one flat event row with environment and status; consecutive reads are separate, not hidden under `Commands 2`. No nested cards, duplicate inner headers, character counters or literal `&middot;`.
- Full outputs are preserved instead of slicing at 2,000/4,000 characters. Reads/searches/unknown tools and multi-file numbered diffs remain inspectable. SSH stdout/stderr have independent fallback; exact run IDs are reserved before heuristic command/path matching.
- Only actually running tools auto-open while the session runs. Pending approval and failures are readable while collapsed; errors have a short visible cause. Explicit opening/closing and expanded text survive rerenders. Expanding output keeps manual control and keyboard focus. The exact dismissed-question outcome is a neutral `Question skipped`.
- When native SSH events precede model tool parts, manual state transfers by run ID. Completed tools outrank stale approval; nonzero exit codes show failure.
- Reasoning's live label ends at its reasoning end timestamp or when a tool/text boundary arrives, even while the session remains busy. Idle/error/abort also stops it.
- Polling preserves append-only live text extensions when an incomplete server snapshot lags SSE; completed/changed snapshots remain authoritative.
- Keyboard disclosure controls, `aria-expanded`/`aria-controls`, inert collapsed content and reduced-motion support are included.

## Optional SSH

- The selected local project remains the primary workspace, even when an SSH connection is selected. The prompt distinguishes local and remote directories and does not assume the project is mirrored on the server.
- SSH is available when the task needs server access, not mandated for every command. Local tools keep their normal permissions; stale generated SSH-only session rules are replaced on the next send.
- Default SSH exec/write requires approval, plan mode denies them, and no selected connection denies SSH tools. Auto-approve allows local writes while preserving explicit denies and sensitive-read protection.
- The environment control now says `Local · SSH available`; each action independently shows its actual environment. The compact composer no longer has an unexplained context ring or fade over the feed; context statistics remain under Settings → Context usage.

No extra model/summarization API or backend changes were added. Android shell and sidebar are unchanged.

## Validation

Using `../.local/api-test-venv/Scripts/python.exe` from the repository:

- `-B tests/live_feed_proof.py`: **80/80 PASS**, installed Chrome, 320/412 px, both themes and 100/140% font scale; live renderer fixtures, actual event handlers, original text/output, runtime font changes, native→SSE handoff, state/focus/scroll, errors and context UI. Final report: `../.local/live-feed-proof/report.json`.
- `-B tests/reasoning_activity_proof.py`: **44/44 PASS**, chronology, interruption, polling and reload.
- `-B tests/ssh_optional_proof.py`: **21 scenarios / 31 actual submits PASS**, intercepted requests, no real SSH/model calls.
- `-B tests/reasoning_effort_proof.py --sequence-only`: **PASS**, fake provider → real OpenCode → real EventSource, polling, reload and fresh page.
- `-B tests/probe_think.py`: **PASS**, light/dark visual probes.
- `-B tests/ui_round3.py`: **218/218 PASS**, real local OpenCode and isolated configuration. Existing permissions, composer, sidebar and message actions retained.
- Browser: existing `C:/Program Files/Google/Chrome/Application/chrome.exe`. Final direct run required `PYTHONHOME=C:/Users/G2961/AppData/Local/Programs/Python/Python314` for the existing test venv.
- `git diff --check`: **PASS**.

The legacy `reasoning_effort_proof.py` was attempted but is **not a passing full-suite result**: stale positional model selectors can select Cloudflare instead of the fake provider (`CLOUDFLARE_GATEWAY_ID missing`); a diagnostic run with corrected model selection passed the reasoning cases and then hit the existing `Reasoning` vs `Default` label expectation. Those unrelated baseline issues were not changed.

## Local build and installation

- Gradle 8.9 / JDK 21: `assembleRelease` **BUILD SUCCESSFUL**.
- `app/build/outputs/apk/release/app-release.apk`: approximately 69 MiB, v2 signature verified, packaged `codex.html` matches source byte-for-byte.
- This revision is the **live UI**, not only `thread-preview.html`. Installed with `pm install -r` on the user's verified Pixel 6: **Success**. `am start -W`: **Status: ok**, cold launch 800 ms. No app data cleared. No claim of physical-device visual validation; browser screenshots use synthetic messages only.
- Live model behavior on the phone still depends on the provider; optional-SSH routing was verified at the request/policy level, not by running user server commands.
