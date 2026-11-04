# Thread design preview (isolated prototype)

## Entry point

Settings → Diagnostics → **Thread design preview**. Back in the preview header closes it and restores the underlying UI. This is an opt-in example, not a replacement for the live conversation renderer.

- `app/src/main/assets/thread-preview.html`: interactive offline fixtures with three scenarios: short task, long work and a manually started execution/error simulation. Same typography/geometry in collapsed and expanded states, quiet reasoning rows, independent commands with environment/status, limited terminal output, neutral dismissed question, copy/more actions and a composer that does not send.
- `app/src/main/assets/codex.html`: Settings entry and `openDesignPreview()` overlay. Parent app becomes inert while the frame is visible; closing restores focus and the previous inert state. Close messages require the actual iframe window as source.
- `app/src/main/java/space/g2961/opencode/MainActivity.kt`: serves only the bundled preview asset at `/_ui/thread-preview.html` on the runtime auth port.

## Design tokens

System Roboto / platform sans-serif; answer 16 px, event rows 14 px, model 13 px, lower options/secondary text 12 px, terminal 12 px. White `#ffffff`, text `#17191d`, muted `#62656d`, surface `#f5f5f7`, line `#e7e7eb`, interactive accent `#126bd6`. Dark preview follows the existing charcoal direction. Within-phase gap is zero; between phases it is 12 px (previously 18). The composer uses 4 px vertical padding instead of 10/8 while keeping the textarea and buttons at least 44 px. Command summary padding stays 8 px in both states, including narrow layouts; labels clamp to two lines without hiding the full command in the body.

No fake Android status/navigation bars are rendered: the native device supplies them. The preview is visibly labelled as sample data. Long titles wrap instead of shrinking the entire screen.

## Safety / scope

The fixture performs no API, SSH, native bridge or persistent-storage operations. Commands and model names are examples; send/model/environment/attachment controls explain that they are a demo. Theme toggles are preview-only. The document has a restrictive CSP (`connect-src 'none'`, no external resources) and runs in an `allow-scripts` sandbox without same-origin access to parent state/storage.

Android's JavaScript interface is injected into all frames; the claim is that this audited bundled fixture never invokes it, not that iframe sandboxing removes the Android interface.

No live renderer, permissions, session data, model selection or SSH behavior was changed for this prototype.

## Scenarios

Choose a scenario from the preview's top-right menu:

- **Короткая задача**: the original example, completed and collapsed by default.
- **Длинная работа**: four phases with long paths, raw reasoning, read/search actions, a long shell chain and 36 test output rows. Reasoning titles use an explicit heading in the source or the neutral `Рассуждение` fallback, never a generated summary. Long raw text has one expansion control, no nested reasoning details; hostile-looking HTML remains literal text.
- **Выполнение и ошибка**: idle until **Запустить пример**. Local tests stream partial output and fail with a concise visible database error, followed by separate reasoning and a remote approval. Approve runs simulated SSH output; cancel produces a neutral cancelled event. No approval is accepted automatically. The timed stages take 9.4 seconds, excluding user approval wait.

Completed auto-open events collapse; explicit manual opening/closing survives ticks. Expanding output also claims manual control and retains focus on completion. Updates follow the bottom only when already less than 48 px away. Stop/replay/scenario changes/exit cancel timers. At viewport heights <=400 px the preview allows outer scrolling rather than clipping a multiline composer or shrinking controls.

## Verification

- `../.local/api-test-venv/Scripts/python.exe -B tests/thread_preview_scenarios.py --browser-executable 'C:/Program Files/Google/Chrome/Application/chrome.exe'`: **2518/2518 PASS** in the final full run. Includes the original `thread_preview_proof.py` integration assertions. Playwright's cached browser was unavailable, so the installed Chrome was used without adding dependencies.
- Light/dark × 320×800, 412×915, 412×412; extra 412×360 / 320×300 cases with a five-line draft.
- Checks font/composer stability, actual gap measurements, fixed header X positions, two-line labels, 44 px controls, stream/approval/deny/stop/replay, timer cleanup, manual expansion and focus retention, scroll thresholds, source escaping, no network/native/storage writes and preservation of parent state.
- A previous run had one click-action timeout in the short-height layout test; the final full rerun did not reproduce it.
- Fixture-only screenshots and report: `../.local/thread-preview-scenarios/`.
- `gradle assembleRelease`: **BUILD SUCCESSFUL**. Existing Android deprecated-API warnings remain.
- APK v2 signature verified; both packaged HTML assets match source byte-for-byte.
- Pixel 6 serial verified, `pm install -r`: **Success**; no app data cleared, temporary APK removed.
- Latest updated APK installed on verified Pixel 6 with `pm install -r`: **Success**; `am start -W`: **Status: ok**, cold launch 226 ms. No data was cleared. No physical-device screenshot/visual verification is claimed for this revision; open Settings → Thread design preview and choose a scenario in the top-right menu.
- No real conversation screenshots or content collected.
