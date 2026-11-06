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

## Physical iPad result

On iPad6,11 / iOS 16.7.12, a temporary native diagnostic executable read the
configured AgentRouter credential through Kumo's Keychain access group. The key
was never returned to JavaScript, stdout, a file, SSH or logs. The helper used the
same `NativeModelClient.swift` as the app, no provider-specific header spoofing,
no redirects and no automatic retry. No application transcript was sent; the
single generation request asked only for a connection-test confirmation.

Observed separately:

```text
/models:          HTTP 401, unauthorized_client_error
Chat Completions: HTTP 401, unauthorized_client_error
Responses:        HTTP 401, unauthorized_client_error
```

The credential was found, TLS connected, and the gateway explicitly rejected the
client. This does **not** establish whether the account/key itself is valid or
what internal gateway policy caused the rejection. No live answer or file edit
from AgentRouter has been claimed. Do not try to cure this by changing model IDs,
repeated retry, or pretending Kumo is another client.

Provider support needs to confirm that ordinary Bearer requests from Kumo are
allowed for this account. If a different provider is configured by the user, it
can be tested without changing the agent core. Do not invent model/context limits
or migrate credentials to an unrelated endpoint.

## Diagnostic helper

`ProviderDiagnostic.swift` is **not bundled in Kumo**. Its explicit
`ios-diagnostics.yml` workflow builds a device executable only on manual dispatch.
It accepts a native state path, provider ID and one operation (`models`, `probe`,
`responses`). Only a fixed diagnosis/status and counts are printed. On stock iOS
it is not a generally runnable app; the physical test used the owner's existing
jailbreak signing environment and a temporary Kumo-only Keychain grant.

The temporary executable and its entitlement file were removed after testing.
Do not ship this helper, its grant, or real credentials in an IPA/repository.
