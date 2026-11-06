# Kumo iOS: in-process native backend

Kumo opens its bundled Web UI immediately. The iOS application no longer starts
OpenCode, Bun, a loopback HTTP server, an auth proxy, or an SSH MCP server. Model
requests go directly to the configured HTTPS provider through `URLSession`.
Android continues to use its existing OpenCode runtime unchanged.

## Implemented first slice

- Provider configuration with **Responses** and **Chat Completions** adapters.
  API keys are stored in Keychain, not HTML/localStorage or the configuration JSON.
  `/models` discovery is native; manually entered model IDs are supported.
- An actor-owned agent loop: provider stream → validated tool calls → permission
  decision → actual execution → tool results → another provider request.
- Native `list`, `read`, `glob`, `grep`, `write`, and `edit` tools. No custom VM,
  project jail, or process runtime. Absolute paths are allowed wherever iOS and
  the user's permissions provide access.
- Plan / Ask / Auto permission rules enforced in the backend. Proposed write/edit
  diffs are prepared before approval; changed file preconditions reject stale edits.
- Ordered text/reasoning/tool parts and live UI events. Only provider-supplied
  reasoning is displayed. Tool previews are not executable before stream completion.
- Durable sessions, messages, provider replay metadata and native patch history.
  Relaunch restores interrupted turns without replaying file mutations.
- Stop cancels streaming and pending approvals. Backgrounding interrupts active
  turns; the same backend can accept a new turn in the foreground.
- File picker bookmarks retain security-scoped folder access across launches.
- Bundled assets served through `WKURLSchemeHandler` at `kumo://app/_ui/index.html`.
  Backend commands and events use the native bridge, not fetch/EventSource.

## Not implemented yet

This is a real file-editing agent, **not full OpenCode parity**. The first slice
has no shell/npm/compiler execution, Git clone/push, MCP, SSH agent tools, skills,
subagents, automatic compaction, OAuth provider login, Anthropic adapter or model
attachments. Unsupported features are labeled or fail explicitly; they do not
claim success. The old runtime source files remain for reference but are not
created by the native application lifecycle.

The UI and provider contracts are adapted to our existing interface. There is no
promise that arbitrary OpenCode plugins or SDK packages will run in this engine.
A future local execution helper is separate from the agent core; stock iOS does
not permit arbitrary subprocesses, and elevated signing rights must not be
bolted onto the WebKit host.

## Providers and context

Settings → Providers → add your provider, HTTPS base URL and key. Select Responses
or Chat Completions according to its actual API. The base URL should end at the
API root, usually `/v1`, not a generation endpoint.

**AgentRouter:** the exact HTTPS `agentrouter.org` host uses the Codex-compatible
header profile verified against the owner's desktop gateway. On the target iPad,
model discovery succeeded and the configured GPT model returned a complete `OK`
stream through **Responses** with the unchanged Keychain key. The tested Chat
Completions request returned HTTP 400 after this profile fixed its authentication.
Use Responses for this setup. The profile does not change the protocol silently,
add credentials, follow redirects, or affect Free Inference/other provider hosts.
Details and the A/B results are in `Diagnostics/README.md`.

Model discovery may not supply a context window. In that case, enter the actual
context size from the provider documentation in **Context window for models
without metadata**. This explicit fallback applies only to models missing saved
or discovered metadata. Do not apply one value to heterogeneous models with
different limits. Existing per-model limits are preserved.

Context admission uses a conservative UTF-8-byte estimate plus overhead and an
output reserve, not an exact tokenizer. The reserve is also sent as the provider's
generation cap. If the budget is exceeded, the turn stops with an explanation;
it does not silently truncate the conversation. Automatic compaction is a later
milestone. Provider errors and incomplete streams are not retried automatically,
which avoids duplicate charges and potentially repeated operations.

## Storage and safety

Native data lives under `Application Support/native-agent/state.json`; native
patch history has a separate atomic JSON ledger. Old OpenCode session/auth files
are not automatically imported or deleted. The origin changes from HTTP to
`kumo://`; old WebView localStorage preferences are not guaranteed to carry over.
Provider credentials should be configured for the new backend.

Native history uses `native:<id>` identifiers, **not Git commits**. Failed writes
are not recorded as successful changes. The engine serializes its own mutations,
but portable POSIX file replacement is not a cross-process compare-and-swap:
there remains a narrow race with unrelated external writers. OS sandbox limits
remain even in Auto mode; Auto changes approvals, not operating-system privileges.
File content sent to the model is shared with the selected provider. Never place
API keys in the workspace or prompt; Keychain credentials are kept out of tools.

## Signing

Use an ordinary application signature, preserving the application's identifiers.
Do not add `platform-application`, `no-container`, or skip-library-validation rights
just to launch a server. On the target jailbroken iPad those rights caused:

```text
System Policy: Kumo deny mach-lookup com.apple.WebKit.Networking
System Policy: Kumo deny mach-lookup com.apple.WebKit.WebContent
System Policy: Kumo deny iokit-open-user-client IOSurfaceRootUserClient
```

CI no longer downloads or packages the macOS OpenCode executable. The app can be
signed normally; installation on the current jailbroken device uses the existing
ldid/AppSync workflow. Do not uninstall or clear data to work around an install.

## Build and validation

The `ios-build.yml` workflow:

1. Stages canonical Android HTML through `iOS/scripts/stage-native-ui.py` without
   changing Android sources.
2. Runs behavioral Python/Node staging tests and **all KumoCore XCTest tests** in
   an isolated macOS package. Failures are not hidden with `|| echo`.
3. Builds the iOS application via `iOS/project.yml` and XcodeGen.
4. Packages an IPA with no OpenCode binary.

```sh
python iOS/scripts/test-stage-native-ui.py
python iOS/scripts/stage-native-ui.py --check
```

Core tests cover provider request/stream framing, truncation/cancellation,
chronology, real filesystem mutations, approval rejection, stale snapshots,
session persistence/recovery and bookmarks. Scripted model tests prove the agent
loop with real local files, **not a real provider response**. Live-provider and
physical-device checks must be reported separately.

Milestone 1 has now been exercised on the physical iPad with the real AgentRouter
model: read/edit/read, one-time approval, rejection, Stop, stale-file conflict and
separate-process recovery all passed. The iOS approval card's missing diff preview
was fixed and covered by browser UI tests. See
`Diagnostics/FILE-AGENT-ACCEPTANCE.md` for exact evidence and the distinction between
live-core/device checks and UI-fixture checks.

```sh
python iOS/scripts/test-native-file-agent-ui.py
```

WebView lifecycle diagnostics use `[web]`; successful `didFinish` confirms a
navigation, not the completion of an agent task. Load failures show a native
message and Reload interface instead of leaving a black screen.

## Migration roadmap

1. **Current:** no-server text/file agent, truthful events and durable state.
2. Provider breadth, attachments, tokenizer/context compaction, resumable history.
3. Subagents, skills, native Git and supported MCP transports.
4. Optional device execution/SSH integration through real supported libraries or
   a trusted jailbreak helper. No VPS is required for the agent core itself.

Every milestone must preserve the work-log chronology and prove its own full
scenario rather than fill `/doc` with unsupported endpoints.
