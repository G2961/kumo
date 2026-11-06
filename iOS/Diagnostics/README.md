# Native provider verification

## Verified 2026-11-06

- App/core CI: run `42252406667`, commit `c8aee50`.
- Standalone device diagnostic build: run `42252408460`.
- **166 XCTest passed**, including five `NativeProviderIntegrationTests`.
- **8 Python/Node UI staging tests passed**.
- iOS app build passed; Android sources unchanged.

The integration tests exercise the real native URLSession transport against an
isolated URLProtocol HTTP fixture, not a mock `NativeModelClient`:

1. Responses and Chat Completions each run model tool call → pending permission
   → approval → actual file write → tool result replay → actual read → final.
2. Stored messages, ordered parts, usage, request auth and patch history are
   checked; reopening the store must not repeat execution.
3. A truncated stream cannot execute a preview, even when arguments appear complete.
4. Stop cancels the held HTTP request and its preview cannot mutate a file.
5. A real-shaped HTTP 401 error is classified and persisted without its raw body.

These fixtures prove transport/agent integration, **not a live model answer**.

## Physical iPad result and resolved cause

The first native requests used ordinary Bearer authorization and were rejected
with HTTP 401 / `unauthorized_client_error` for model discovery and both generation
protocols. That result was initially treated as an external blocker; it was not
sufficient to explain why the user's desktop connection worked.

A later inspection of the **working desktop configuration** found that its apparent
Chat Completions provider points to a local gateway at `127.0.0.1:9123`. That gateway
actually translates the request to `/v1/responses` and sets a Codex-compatible
client header profile. The localhost gateway is not required by the iOS app.

On the same iPad6,11 / iOS 16.7.12, with the same Keychain credential and unchanged
model, an explicit A/B diagnostic reproduced the difference:

| Request | Ordinary native headers | Desktop-compatible headers |
| --- | --- | --- |
| `/v1/models` | HTTP 401 | HTTP 200, 3 models |
| `/v1/responses` | HTTP 401 | Successful complete stream, `OK` (2 UTF-8 bytes) |
| `/v1/chat/completions` | HTTP 401 | HTTP 400 for the tested request/model |

The key was never returned to JavaScript, stdout, files, SSH or logs. Only connection
diagnoses and counts were printed. No request used automatic retry or followed a
redirect. This proves a live Responses answer and header-profile dependency, **not**
full Chat Completions compatibility or a live file-agent turn.

`NativeProviderCompatibility.apply(to:)` centralizes the tested profile for HTTPS
on the **exact** `agentrouter.org` host (default port/443) only:

```text
User-Agent: codex_cli_rs/0.46.0 (Ubuntu 22.4.0; x86_64) xterm-256color
originator: codex_cli_rs
version: 0.46.0
session_id: one generated UUID per app process
```

This explicitly advertises a Codex-compatible client identity to this gateway;
it is not the actual Kumo/platform identity. It mirrors the owner's already
working adapter and is documented rather than silently applied to every provider.
Authorization, request body, and the selected protocol are not overwritten.
Free Inference, subdomains, different ports and all other providers are unchanged.
The provider settings explain that Responses is verified for the tested GPT model;
Chat is not automatically switched by the request builder.

The rejection's internal policy and minimal required subset of headers remain
unknown. A future officially supported Kumo profile should replace this workaround
when the gateway provides one.

## Latest compatibility validation

- Production-profile build: `42269499412`, commit `1330adb`.
- **175 XCTest and 9 UI staging tests passed**; iOS build passed.
- Standalone diagnostic `42269501776` used the common production helper without
  the `desktop-profile` switch: `/models` returned 3 models, Responses finished
  successfully with 2 UTF-8 text bytes.
- Signed IPA installed on the target iPad; its binary SHA-256 was verified.
- All six existing sessions and both providers were preserved with a pre-install
  backup. Only the existing AgentRouter config was explicitly switched to
  Responses; Free Inference config was byte-for-byte equivalent after migration.
- The iPad was locked during final app launch, so the post-install UI and a live
  file-editing turn in the app are not yet physically verified.

## Diagnostic helper

`ProviderDiagnostic.swift` is **not bundled in Kumo**. Its explicit
`ios-diagnostics.yml` workflow builds a device executable only on manual dispatch.
It accepts a native state path, provider ID and one operation (`models`, `probe`,
`responses`). Only a fixed diagnosis/status and counts are printed. On stock iOS
it is not a generally runnable app; the physical test used the owner's existing
jailbreak signing environment and a temporary Kumo-only Keychain grant.

The temporary executable and its entitlement file were removed after testing.
Do not ship this helper, its grant, or real credentials in an IPA/repository.
