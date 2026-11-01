# OpenCode Android

Native Android client for [opencode](https://github.com/anomalyco/opencode) that runs the
**opencode server locally on the device**: the official single-binary release (arm64 musl)
is shipped inside the APK and served to an embedded WebView with the official opencode web UI.
no proot, no Termux, no remote server, no NDK build.

## How it works

```
┌─ APK ───────────────────────────────────────────────────────────┐
│  MainActivity (Kotlin)                                          │
│    └─ WebView ── http://127.0.0.1:<port>/ (session cookie)      │
│  ConnectProxy (127.0.0.1:<p2>) — CONNECT proxy, resolves DNS    │
│    through the OS so Bun/c-ares never needs /etc/resolv.conf    │
│  LocalRuntime                                                   │
│    └─ ld-musl-aarch64.so.1 --library-path <staged libs>         │
│         libopencode.so serve  (direct exec, no proot, no ptrace)│
│    env: HTTPS_PROXY→proxy, HOME/TMPDIR→app dirs,                │
│         PATH=<files>/bin:/system/bin (toybox shell for agent),  │
│         OPENCODE_CONFIG_CONTENT={"shell":"/system/bin/sh"}      │
└─────────────────────────────────────────────────────────────────┘
```

- The **opencode binary** (from `opencode-linux-arm64-musl` on npm) embeds the full web UI
  and serves it at `/` on a loopback port.
- The binary is dynamically linked against **musl libc + libstdc++/libgcc** (Alpine packages),
- **Auth**: the runtime generates a password once, starts the server with
  `OPENCODE_SERVER_PASSWORD`, then mints an HMAC session token (same scheme as
  `packages/server/src/auth.ts`) and installs it as the WebView session cookie — the UI opens
  signed-in, no password prompt.
- **DNS** inside the rootfs comes from `/etc/resolv.conf` generated from the active network's
  DNS servers (fallback: 1.1.1.1 / 8.8.8.8).
- **Agent shell**: the Bash tool runs busybox (static, Alpine) inside the rootfs;
  `rg` is a **static musl ripgrep built in CI** (cargo-zigbuild), exposed on `PATH` through `filesDir/bin`.

## Project structure

```
.
├── app/
│   ├── build.gradle.kts            # SDK 26–35, arm64-v8a, legacy jniLib packaging
│   ├── opencode.jks                # release keystore (test credentials)
│   └── src/main/
│       ├── AndroidManifest.xml
│       ├── java/space/g2961/opencode/
│       │   ├── MainActivity.kt     # status screen + WebView
│       │   ├── LocalRuntime.kt     # loader exec, env wiring, readiness, logs
│       │   └── SessionToken.kt     # HMAC session cookie minting (mirror of server auth)
│       ├── ConnectProxy.kt         # loopback CONNECT proxy (DNS via OS)
│       └── jniLibs/arm64-v8a/      # downloaded by CI (gitignored)
├── .github/workflows/build.yml     # downloads runtime pieces, builds debug+release APKs
└── …
```

## Build

CI only (runtime artifacts are downloaded there):

```bash
gradle assembleDebug        # requires app/src/main/jniLibs/arm64-v8a/*.so + assets/rootfs.tgz
```

The workflow pins: opencode `1.19.3`, Alpine libstdc++/libgcc `3.22.1`, ripgrep `15.1.0` (static musl).
Bump the `env:` block in `.github/workflows/build.yml` to update.

## Requirements

- arm64-v8a device, Android 8.0+
- An LLM API key (configure the provider in the web UI once it is running)

## Status / roadmap

- [x] Local opencode server (direct exec via musl loader), official web UI in WebView
- [x] Cookie-based silent sign-in
- [ ] First-run wizard (provider/API key setup)
- [ ] Background service (keep server alive, notifications)
- [ ] Workspace picker / SAF bind-ins, git (JGit) bridge
- [ ] Remote mode (connect to code.g2961.space or any opencode server over SSH)
