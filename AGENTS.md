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

## Architecture notes

- The opencode binary is executed through proot inside an Alpine minirootfs. Executables and
  PROT_EXEC-mapped libraries must physically live in `nativeLibraryDir` (Android blocks exec
  from app data for targetSdk >= 29); they are packed as `lib*.so` in jniLibs.
- Server auth: runtime password in `OPENCODE_SERVER_PASSWORD`; the WebView gets an HMAC
  session cookie (scheme in `SessionToken.kt`, mirrors `packages/server/src/auth.ts` of
  anomalyco/opencode).
- CI pins all runtime versions in `.github/workflows/build.yml` `env:` block.
