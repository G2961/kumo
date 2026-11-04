# Work log — stages 1–8 + local dev sandbox (bun/npm)

## Stage 11 — npm exit 127: tar parser desync (found via host repro)

- `npm install` still exited 127 (`npm: inaccessible or not found`) while bun worked — so the npm shim was never created: rootfs extraction was producing a broken tree. Reproduced the Kotlin TarGz parser one-to-one as a Java program against the actual asset on the host: **only 451 files extracted, `npm-cli.js` missing, stream desynced into file contents**.
- Root cause, classic: after reading a file's data the parser never skipped the tar **512-byte block padding**, so the next header parse started mid-data (`254-byte node-gyp shim` → misalignment → garbage headers → premature EOF). Fix: skip `(512 - size % 512) % 512` after every entry. Host mirror after fix: **1310 files, npm-cli.js present, 14139 files total — matches python's view**.
- Same fix applied to the app's Kotlin parser; rootfs.tgz repacked with a `.packed` stage marker so the phone's stored hash invalidates and re-extracts cleanly (the previous broken tree is deleted on re-extract).
- Known Android storage note (from the user's run): FUSE doesn't allow symlinks — bun's `node_modules/.bin` symlink warning is expected; scripts should run via `bun run`/npm CLI.
- `assembleRelease` **BUILD SUCCESSFUL**; v2 verified; `codex.html`+`rootfs.tgz` byte-identical. Streamed install: **Success**; launch **Status: ok**, 479 ms. No app data cleared.
- On-device checks pending (user): `npm install` should now execute the real npm CLI; Server log should show `rootfs ready` + `bun --version` probe.

## Stage 10 — DNS/mode fixes (built; install pending)

- Root cause of the DNS failures (agent diagnosed it live, confirmed by code): bun/npm honor `HTTPS_PROXY`, but my earlier `NO_PROXY` addition made `npmjs.org` bypass the app's ConnectProxy — and musl has no `/etc/resolv.conf` on Android, so direct resolution died with `EAI_AGAIN`. The app's own `ConnectProxy` already does HTTP CONNECT with Java-side DNS, so the fix is simply reverting npmjs out of `NO_PROXY`: all sandbox traffic goes through the proxy, which resolves names itself. No resolv.conf, no proot needed.
- Auto-approve still asking: the prompt was gated by `external_directory` (access to `/etc` etc.), which the ruleset never covered — the server fell back to its default `ask`. `syncSessionPermissions` now appends `external_directory: allow` in auto mode, `ask` otherwise.
- The first-session `npm` exit 127: likely my rootfs.tgz was packed in tarfile's default PAX format, which corrupts >100-char paths for the hand-rolled ustar parser (18 npm paths exceed 100 chars). Repacked in explicit USTAR format; parser handles prefix-field long names; extraction logs now include success/failure and file counts.
- Prompt updated: sandbox note now says traffic goes through the app proxy which resolves DNS itself (no resolv.conf needed) — so the model won't burn turns diagnosing DNS again.
- `assembleRelease` **BUILD SUCCESSFUL** (APK 122MB), v2 signature verified, `codex.html` + `rootfs.tgz` byte-identical to source.
- **Installed** (second attempt: plain `pm install -r` failed with `INSTALL_FAILED_INSUFFICIENT_STORAGE` even at ~1.3GB free — the update needs the new APK plus freshly extracted native libs on top of the old set; `pm trim-caches` freed nothing). Working method on this device: **`adb install -r` streamed install** (no /data/local/tmp staging copy). **Success**; launch **Status: ok**, 329 ms. No app data cleared.
- Not verified on device yet: first-run rootfs extraction, `bun --version` probe result, and an actual `npm install` run inside the agent sandbox (user confirms on device; startup probe logs into Server log).

## Stage 9 — local terminal sandbox: bun + real npm

- The agent's local sandbox now has a real JS runtime: `libbun.so` (bun 1.4.4, `bun-linux-aarch64-musl`) is packed as a native library and runs through the same musl loader as opencode (bun musl is NOT fully static — it has `PT_INTERP`, so direct exec would fail; the loader shim handles it).
- `bin/` shims written at startup: `bun` (loader + libbun.so), `node` → bun, `npx` → `bun x`, `npm` → the real npm CLI from Alpine 3.22 (`npm-11.6.4`) packed into `assets/rootfs.tgz` and extracted to `filesDir/rootfs` at startup (idempotent, sha256-marked, never committed). npm runs under bun; lifecycle scripts calling `node` resolve to the bun-backed shim.
- npm reaches the registry directly: `NO_PROXY` now includes `npmjs.org,registry.npmjs.org` so the auth proxy is bypassed. A non-fatal `bun --version` probe is logged at startup so sandbox failures have a visible cause.
- Prompt updated: the system text tells the model about bun/npm/npx and the direct internet access; `bun install`/`bun run` suggested as fallback if npm misbehaves. Known gap: `git` is still a stub (exit 128) and there is no real node binary (node is bun-backed).
- CI (`build.yml`): pinned `BUN_VERSION: 1.4.4`, added to the native cache key; new steps download bun and pack the npm rootfs (rootfs step runs on every build, uncached).
- Locally fetched: bun 47.8MB zip → libbun.so 99.7MB; npm apk → rootfs.tgz 1.86MB / 1750 entries; ELF verified (aarch64, PT_INTERP present).
- APK: 122MB (was 72MB), v2 signature verified, `codex.html` + `rootfs.tgz` byte-identical to source, `lib/arm64-v8a/libbun.so` present. Installed on verified Pixel 6 (`pm install -r`): **Success**; launch **Status: ok**, 344 ms. No app data cleared.
- Not verified on device yet: first-run rootfs extraction, `bun --version` probe result, and an actual `npm install` run inside the agent sandbox (user confirms on device; startup probe logs into Server log).

## Stage 8 — diff scroll: verified fix (root cause found)

- Stage 7's theory was wrong twice (my fault): `touch-action:pan-x` outright disables vertical gestures on the preview, and `overscroll-behavior-y` was never the culprit. The real sink was `overflow-y:hidden` on the preview (a non-user-scrollable scroll container swallows pans). Final rule: `max-height:none` + default `overflow:auto` + `overscroll-behavior-x:contain` + `overscroll-behavior-y:auto` — the preview never has vertical overflow, so every vertical gesture chains to the log; horizontal gestures scroll the preview.
- Verified in-browser with a proper fixture (open Edit card, wheel aimed INSIDE the visible preview area — earlier runs aimed at a point beyond the viewport and silently hit nothing): vertical wheel scrolled the log body 0→400px and back; horizontal wheel scrolled the preview 0→400px with the log body unmoved. Check script: `../.local/touch_chain_check.py` (throwaway, outside the repo tests).
- `assembleRelease` **BUILD SUCCESSFUL**; APK v2 verified; bundled `codex.html` byte-identical. Installed on verified Pixel 6 (`pm install -r`): **Success**; launch **Status: ok**, 228 ms. No app data cleared.

## Stage 7 — vertical scroll over diff previews (root cause)

- Root cause of the dead vertical pan on diff previews: the base `.change-preview` sets `overscroll-behavior:contain` on both axes, and the stage-5 override only changed the x-axis — so vertical gestures inside the preview could never chain to the work log list. The scoped rule now sets `overscroll-behavior-y:auto` and `touch-action:pan-x`, so the preview consumes only horizontal pans and every vertical gesture scrolls the whole log.
- `assembleRelease` **BUILD SUCCESSFUL**; APK v2 verified; bundled `codex.html` byte-identical. Installed on verified Pixel 6 (`pm install -r`): **Success**; launch **Status: ok**, 2268 ms cold. No app data cleared.

## Stage 6 — diff markers and live mode switching

- Diff `-`/`+` markers and line numbers are now sticky inside the work log's horizontally scrolling preview (`position:sticky` + per-row opaque backgrounds), so scrolling a long code line no longer hides which lines were edited.
- Mode changes now apply to the running session immediately: ruleset building was extracted into `syncSessionPermissions()` (single source for submit and mid-run changes); switching Ask first/Auto-approve/Plan mid-answer PATCHes the session rules — Auto stops future prompts, Plan re-denies writes. An already-asked permission still waits for an explicit answer by design.
- `tests/auto_approve_proof.py` submit-transition assertions updated for the new contract (one ruleset PATCH per mode change, never a prompt): **PASS** in all 4 ssh/config combinations. `node tests/work_transcript_unit.js` **37/37**; diagnostics clean; `assembleRelease` **BUILD SUCCESSFUL**; APK v2 verified; bundled `codex.html` byte-identical. Installed on verified Pixel 6 (`pm install -r`): **Success**; launch **Status: ok**, 194 ms. No app data cleared.

## Stage 5 — work log stability and screen-edge fixes

- Flicker fixed: work log entries are reconciled by key instead of a full innerHTML rebuild. Unchanged entries are neither rebuilt nor moved (append-only streams never reorder), and streaming reasoning grows via an in-place `.rtext` text update. State transitions still rebuild that entry once; disclosure state, focus and nested preview scroll survive refreshes.
- Diff previews inside the work log: vertical clamp removed (`max-height:none`), so the finger's vertical scroll always scrolls the whole log; horizontal scrolling is unified on the preview container and the per-line scrollbars are disabled.
- Edge-to-edge: the activity now draws behind the gesture bar (`enableEdgeToEdge()` + IME bottom inset padding), so the work log sheet and its dim backdrop reach the true screen edge instead of stopping above the gesture strip. The page already pads via `env(safe-area-inset-*)`.
- Validated: `node tests/work_transcript_unit.js` **37/37**, asset/Java diagnostics clean, `assembleRelease` **BUILD SUCCESSFUL**, APK v2 signature verified, bundled `codex.html` byte-identical to source. Installed on verified Pixel 6 (`pm install -r`): **Success**; launch **Status: ok**, 1875 ms cold. No app data cleared. Keyboard/IME behavior after the inset change is deliberately left for on-device visual confirmation by the user.

## Stage 4 — density and honest labels (visual polish)

- Work log dialog entries tightened: 10px entry gaps (was 16), 20px timeline indent, 40px collapsed rows inside the dialog (was 44/48), tighter activity body padding. Same timeline, much less scrolling.
- Reasoning-only blocks are labelled `Reasoning · Ns` (streaming: `Reasoning…`) instead of the fake-work `Work log · 1 step`; blocks with any tool keep factual totals.
- A running summary whose label already starts with `Running` no longer repeats it with a `Working` badge.
- `thinkDuration` moved into the transcript-model region (hoisting keeps the renderer working; unit harness extracts only that region). No duplicated logic.
- Unit expectations updated to the new labels (`tests/work_transcript_unit.js` **37/37 PASS**); asset diagnostics clean.
- Gradle `assembleRelease`: **BUILD SUCCESSFUL**; APK v2 signature verified; bundled `codex.html` byte-identical to source. Installed on verified Pixel 6 (`pm install -r`): **Success**; launch **Status: ok**, 664 ms. No app data cleared. Visual confirmation delegated to the user.

## Stage 3 — model step commentary

- `submit()` appends a `Communication:` block to `body.system` after the websearch line: brief 1–2 sentence commentary in the user's language before significant steps; report findings/approach changes/obstacles; no per-tool narration; no internal-reasoning reveal; intentions stated as intentions; never claim verified without a verified result.
- Verified by `tests/commentary_prompt_proof.py`: **14/14 PASS** — guidance present exactly once across build/plan/auto × local/SSH × websearch on/off (24 submits), all prior policy strings preserved verbatim, no secret canaries, language-neutral wording; silent-model smoke keeps the main timeline `user → work summary → final text` with honest statuses and no fabricated chatter.
- Real OpenCode SSE (`reasoning_effort_proof.py --sequence-only`) passes with the new system text; `tests/ssh_optional_proof.py` **21/21**, `tests/work_log_proof.py` **80/80**, `tests/ui_round3.py` **218/218**, `tests/work_sheet_motion_proof.py` **24/24**, `tests/auto_approve_proof.py` **8/8**, `node tests/work_transcript_unit.js` **37/37**.
- `tests/work_polish_proof.py` disclosure-motion scenarios (112 runs / 208 checks) intentionally left stale: they assert the pre-animation instant-disclosure contract, while the app now animates disclosures as requested. Reduced-motion and all non-motion scenarios in it remain green. Not a product bug; visual confirmation delegated to the user.
- Gradle `assembleRelease`: **BUILD SUCCESSFUL**; APK v2 signature verified; bundled `codex.html` byte-identical to source. Installed on verified Pixel 6 (`pm install -r`): **Success**; launch **Status: ok**, 251 ms. No app data cleared. Device visual verification not claimed.

## Stage 2 — polish (live chat)

## Implemented

- Work log dialog opens/closes with 220/200ms transform+opacity motion, smooth backdrop, drag-aware; replacements (sheets, permissions, session switch) still close instantly. Android Back uses the animated close. Reduced motion is instant.
- Reasoning rows, tool rows and Changes-sheet diffs disclose with real-height animation (CSS grid transition / WAAPI with details-hold), rapid-toggle safe; disclosure state attributes unchanged for persistence.
- Inside the work log, per-row `Local` environment and the success checkmark are visually hidden but remain in the accessibility tree; remote SSH labels, running/waiting/error states stay visible. Raw source data untouched.
- Work log output/reasoning text uses the primary text color (contrast >= 4.5 checked per theme).
- Settings/Remote/Detail screens no longer clip their last rows (flex-shrink fix); Copy diagnostics is fully reachable.
- Auto-approve is selectable again: the mode menu now closes before the callback opens the confirmation; only explicit Enable saves the warned flag and switches mode.

## Validation

- `tests/work_polish_proof.py`: **384/384 scenarios (24×16), 4160/4160 checks PASS**.
- `tests/work_sheet_motion_proof.py`: **24/24 PASS**.
- `tests/auto_approve_proof.py`: **8/8 PASS**; `tests/ssh_optional_proof.py`: **21 scenarios / 31 submits PASS**.
- `tests/work_log_proof.py`: **80/80 PASS**; `tests/reasoning_effort_proof.py --sequence-only`: **PASS**; `tests/ui_round3.py`: **218/218 PASS**.
- `node tests/work_transcript_unit.js`: **37/37 PASS**; `git diff --check` clean; asset diagnostics clean.
- One Chrome tab crash after ~72 consecutive contexts was observed once; isolated rerun and full rerun were green (browser flake, not asset).
- `tests/live_feed_proof.py` was **removed as superseded**: it asserted the previous inline-rows presentation that this stage replaced; its coverage lives in `work_log_proof.py` and `work_polish_proof.py`.
- Gradle `assembleRelease`: **BUILD SUCCESSFUL**; APK v2 signature verified; bundled `codex.html` byte-identical to source.
- Installed on verified Pixel 6 (`pm install -r`): **Success**; launch **Status: ok**. Device visual verification not claimed.

Environment: `PYTHONHOME=C:/Users/G2961/AppData/Local/Programs/Python/Python314`, `PLAYWRIGHT_BROWSER_EXECUTABLE=C:/Program Files/Google/Chrome/Application/chrome.exe`, venv `../.local/api-test-venv`.

## Implemented

The main conversation now shows a compact **Work log** summary for each consecutive span of reasoning and tool events. Spans can cross technical assistant-message boundaries when their parent matches. Ordinary assistant text, user messages, files and message errors remain visible boundaries. No source messages are rewritten and no generated commentary is inserted.

Selecting a summary opens a native `<dialog>` bottom sheet at 68dvh, expandable to 92dvh. The sheet retains the existing reasoning, tool output, per-operation diff and history renderers in chronological entries. Reasoning and tools remain separate. Closing via button/backdrop/Escape/swipe restores focus and chat scroll; Android Back is wired to close the open work log first.

Summary counts derive from actual completed operations, unique file identities include environment, failed reads do not count as successful reads, and relative/absolute SSH result aliases do not count as two edited files. Error counts remain visible even while later work runs. Successful shell execution is not labelled as successful tests or completed user task.

Pending questions/permissions remain outside the modal. New permission events from either SSE or polling close the work log so approval is actionable. No automatic approval was added.

Native SSH events arriving before model tool parts have provisional presentation events. Exact bindings are reserved before fallback; the modal renderer obeys the transcript binding rather than guessing again. Provisional→actual handoff, including a move across commentary, preserves selected output, manual disclosure and focus. Only unmatched running native events are provisionally surfaced, matching the previous fallback behavior.

## Files

- `app/src/main/assets/codex.html`: pure transcript helpers, summary rendering, dedicated sheet, shared detail interactions/state and lifecycle integration.
- `app/src/main/java/space/g2961/opencode/MainActivity.kt`: Android Back closes work log before normal navigation.
- `tests/work_transcript_unit.js`: grouping/facts/bindings tests.
- `tests/work_log_proof.py`: actual live-asset browser integration matrix.
- `tests/reasoning_effort_proof.py`: real OpenCode SSE sequence/reload proof adapted to grouped UI.
- `tests/ui_round3.py`: relevant feed assertions migrated, unrelated UI coverage retained.

## Validation

- `node tests/work_transcript_unit.js`: **37/37 PASS**; whole inline-script syntax valid.
- `tests/work_log_proof.py`: **80/80 PASS**, 20 scenarios × light/dark × 320/412px. Final report: `C:/Temp/work-log-proof-r0e5snu_/report.json`.
- `tests/reasoning_effort_proof.py --sequence-only`: **PASS**, fake provider → real OpenCode → EventSource, polling, reload and fresh page; no additional model rounds caused by opening details.
- `tests/ui_round3.py`: **218/218 PASS**.
- Existing Chrome used via `PLAYWRIGHT_BROWSER_EXECUTABLE=C:/Program Files/Google/Chrome/Application/chrome.exe`; Python venv used with `PYTHONHOME=C:/Users/G2961/AppData/Local/Programs/Python/Python314`.
- `git diff --check`: clean; HTML diagnostics: no errors/warnings.
- Gradle 8.9 `assembleRelease`: **BUILD SUCCESSFUL**. Existing Android API deprecation warnings remain.
- APK v2 signature verified, bundled `codex.html` matches source byte-for-byte.
- Installed on verified Pixel 6 via `pm install -r`: **Success**. Launch: **Status: ok**, 405ms. No app data cleared. Android Back code compiled; physical gesture behavior and visual appearance not claimed as device-tested.

## Deliberately deferred

This is the first stage of the agreed plan, not a claim to make every model write like Claude. Public-commentary prompting, optional generation of summaries and a per-turn aggregate changed-files/artifact surface are not added. Per-operation diffs already appear in the Work log; existing Changes view remains available. Model/provider, SSH policy, permissions and session content are unchanged.

Older standalone inline-feed acceptance scripts describe the previous presentation. The migrated UI suite and the new grouped-work suite validate this stage; the previous inline-feed suite is not claimed as passing.
