# AGENTS.md

## Git workflow

- After completing a task, always commit and push without asking.
  Commit messages: English, imperative, concise summary line + body if needed.
- One commit per task: bundle all related changes (code, docs, config) into a single commit
  so each push triggers only one CI build.
- Before pushing, `git pull --rebase` if the remote is ahead.
- If the working directory is not a git repo, initialize one, create a PRIVATE GitHub repo
  (`gh repo create <name> --private --source . --push`) and push there. Never publish the
  user's code publicly.
- `app/src/main/jniLibs/` and `app/src/main/assets/rootfs.tgz` are CI-downloaded artifacts:
  never commit them.

## Local build and device verification

- Default workflow requested by the user: build locally, install the release APK on their
  connected phone, and launch the app without asking for installation again.
- Use Gradle 8.9 (`../.local/gradle-8.9/bin/gradle assembleRelease`) and preserve the existing
  signing key. The APK is `app/build/outputs/apk/release/app-release.apk`.
- Identify the user's Pixel 6 by `ro.serialno` = `24301FDF600063`, including when connected
  through wireless ADB. Do not install on other devices.
- Over wireless ADB, prefer pushing the APK followed by `pm install -r` (preserve app data),
  then launch `space.g2961.opencode/.MainActivity`. Verify installation success before
  restarting. Do not uninstall or clear app data to work around installation failures.
- `build_local.sh` implements the local build/install workflow. If the phone is unavailable,
  report the built APK path and the connection issue instead of claiming installation.

## Conversation UI and SSH behavior

- Preserve chronology: reasoning → tools → new reasoning → answer. Never aggregate a
  whole assistant turn into one Thought block; tool calls and visible text end a reasoning
  segment, including within one assistant message. Keep completed segments stable while
  streaming the next one, and never nest tool output inside Thought.
- The main chat groups consecutive reasoning/tool events into a Work log summary, even
  across assistant messages with the same parent. Public text, user messages, files and
  message errors end the group. Details open in a dedicated bottom sheet; raw reasoning,
  tools and per-operation diffs stay chronological and separate inside it.
- Summaries count confirmed actions from events, never inferred success or fabricated
  model commentary. Permissions/questions must stay actionable outside the work log.
- Selected SSH is optional access, not a replacement for the selected local project.
  Keep local tools available under their usual permissions; use SSH when the task needs
  server access. Never force searches for the local project on the remote server.

## Architecture notes

- The opencode binary is executed through proot inside an Alpine minirootfs. Executables and
  PROT_EXEC-mapped libraries must physically live in `nativeLibraryDir` (Android blocks exec
  from app data for targetSdk >= 29); they are packed as `lib*.so` in jniLibs.
- Local sandbox runtime: `libbun.so` (bun, musl-linked like opencode) runs through the same
  musl loader; `bin/bun` is a loader shim, `bin/node` -> bun, `bin/npm` -> the real npm CLI
  from `assets/rootfs.tgz` (Alpine npm, extracted to filesDir/rootfs at startup),
  `bin/npx` -> `bun x`. Outbound sandbox traffic uses the app's local ConnectProxy (HTTP
  CONNECT with Java-side DNS) — musl has no /etc/resolv.conf on Android, so npmjs.org must
  stay proxied and must NOT be added to NO_PROXY. `libbun.so` and `rootfs.tgz` are
  CI-downloaded artifacts like jniLibs: never commit them. There is no real node binary;
  node is bun-backed.
- Server auth: runtime password in `OPENCODE_SERVER_PASSWORD`; the WebView gets an HMAC
  session cookie (scheme in `SessionToken.kt`, mirrors `packages/server/src/auth.ts` of
  anomalyco/opencode).
- CI pins all runtime versions in `.github/workflows/build.yml` `env:` block.
