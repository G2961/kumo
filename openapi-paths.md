# Пути настоящего opencode API 1.19.3

Источник: GET /doc работающего opencode-ai@1.19.3 на Windows. Схема сохранена без преобразования в openapi.json; группы соответствуют первому сегменту пути.

Версия установки закреплена явной командой: npm install --global --save-exact opencode-ai@1.19.3. Пакет установлен вне репозитория.

Проверка:

```text
opencode --version: 1.19.3
Server: http://127.0.0.1:58074
GET /doc: 200; OpenAPI: 3.1.0; info.version: 1.0.0
GET /global/health: 200; healthy: true; version: 1.19.3
Paths: 162; operations: 188; groups: 26
openapi.json bytes: 479667
openapi.json SHA-256: eebb56042880e07c354e0ad0a31f70aab45f536adf5b40b6b1fd5abf4663417b
```

## /agent (1 путей)

- `GET /agent`

## /api (51 путей)

- `GET /api/agent`
- `GET /api/command`
- `PATCH, DELETE /api/credential/{credentialID}`
- `GET /api/event`
- `GET /api/fs/find`
- `GET /api/fs/list`
- `GET /api/fs/read/*`
- `GET /api/health`
- `GET /api/integration`
- `GET /api/integration/{integrationID}`
- `POST /api/integration/{integrationID}/connect/key`
- `POST /api/integration/{integrationID}/connect/oauth`
- `GET, DELETE /api/integration/attempt/{attemptID}`
- `POST /api/integration/attempt/{attemptID}/complete`
- `GET /api/location`
- `GET /api/model`
- `GET /api/permission/request`
- `GET /api/permission/saved`
- `DELETE /api/permission/saved/{id}`
- `GET /api/provider`
- `GET /api/provider/{providerID}`
- `GET, POST /api/pty`
- `GET, PUT, DELETE /api/pty/{ptyID}`
- `GET /api/pty/{ptyID}/connect`
- `POST /api/pty/{ptyID}/connect-token`
- `GET /api/question/request`
- `GET /api/reference`
- `GET, POST /api/session`
- `GET /api/session/{sessionID}`
- `POST /api/session/{sessionID}/agent`
- `POST /api/session/{sessionID}/compact`
- `GET /api/session/{sessionID}/context`
- `GET /api/session/{sessionID}/event`
- `GET /api/session/{sessionID}/history`
- `POST /api/session/{sessionID}/interrupt`
- `GET /api/session/{sessionID}/message`
- `GET /api/session/{sessionID}/message/{messageID}`
- `POST /api/session/{sessionID}/model`
- `POST, GET /api/session/{sessionID}/permission`
- `GET /api/session/{sessionID}/permission/{requestID}`
- `POST /api/session/{sessionID}/permission/{requestID}/reply`
- `POST /api/session/{sessionID}/prompt`
- `GET /api/session/{sessionID}/question`
- `POST /api/session/{sessionID}/question/{requestID}/reject`
- `POST /api/session/{sessionID}/question/{requestID}/reply`
- `POST /api/session/{sessionID}/revert/clear`
- `POST /api/session/{sessionID}/revert/commit`
- `POST /api/session/{sessionID}/revert/stage`
- `POST /api/session/{sessionID}/wait`
- `GET /api/session/active`
- `GET /api/skill`

## /auth (1 путей)

- `PUT, DELETE /auth/{providerID}`

## /command (1 путей)

- `GET /command`

## /config (2 путей)

- `GET, PATCH /config`
- `GET /config/providers`

## /event (1 путей)

- `GET /event`

## /experimental (21 путей)

- `GET /experimental/capabilities`
- `GET /experimental/console`
- `GET /experimental/console/orgs`
- `POST /experimental/console/switch`
- `POST /experimental/control-plane/move-session`
- `POST, DELETE /experimental/project/{projectID}/copy`
- `POST /experimental/project/{projectID}/copy/generate-name`
- `POST /experimental/project/{projectID}/copy/refresh`
- `GET /experimental/resource`
- `GET /experimental/session`
- `POST /experimental/session/{sessionID}/background`
- `GET /experimental/tool`
- `GET /experimental/tool/ids`
- `GET, POST /experimental/workspace`
- `DELETE /experimental/workspace/{id}`
- `GET /experimental/workspace/adapter`
- `GET /experimental/workspace/status`
- `POST /experimental/workspace/sync-list`
- `POST /experimental/workspace/warp`
- `GET, POST, DELETE /experimental/worktree`
- `POST /experimental/worktree/reset`

## /file (3 путей)

- `GET /file`
- `GET /file/content`
- `GET /file/status`

## /find (3 путей)

- `GET /find`
- `GET /find/file`
- `GET /find/symbol`

## /formatter (1 путей)

- `GET /formatter`

## /global (5 путей)

- `GET, PATCH /global/config`
- `POST /global/dispose`
- `GET /global/event`
- `GET /global/health`
- `POST /global/upgrade`

## /instance (1 путей)

- `POST /instance/dispose`

## /log (1 путей)

- `POST /log`

## /lsp (1 путей)

- `GET /lsp`

## /mcp (6 путей)

- `GET, POST /mcp`
- `POST, DELETE /mcp/{name}/auth`
- `POST /mcp/{name}/auth/authenticate`
- `POST /mcp/{name}/auth/callback`
- `POST /mcp/{name}/connect`
- `POST /mcp/{name}/disconnect`

## /path (1 путей)

- `GET /path`

## /permission (2 путей)

- `GET /permission`
- `POST /permission/{requestID}/reply`

## /project (5 путей)

- `GET /project`
- `PATCH /project/{projectID}`
- `GET /project/{projectID}/directories`
- `GET /project/current`
- `POST /project/git/init`

## /provider (4 путей)

- `GET /provider`
- `POST /provider/{providerID}/oauth/authorize`
- `POST /provider/{providerID}/oauth/callback`
- `GET /provider/auth`

## /pty (5 путей)

- `GET, POST /pty`
- `GET, PUT, DELETE /pty/{ptyID}`
- `GET /pty/{ptyID}/connect`
- `POST /pty/{ptyID}/connect-token`
- `GET /pty/shells`

## /question (3 путей)

- `GET /question`
- `POST /question/{requestID}/reject`
- `POST /question/{requestID}/reply`

## /session (20 путей)

- `GET, POST /session`
- `GET, DELETE, PATCH /session/{sessionID}`
- `POST /session/{sessionID}/abort`
- `GET /session/{sessionID}/children`
- `POST /session/{sessionID}/command`
- `GET /session/{sessionID}/diff`
- `POST /session/{sessionID}/fork`
- `POST /session/{sessionID}/init`
- `GET, POST /session/{sessionID}/message`
- `GET, DELETE /session/{sessionID}/message/{messageID}`
- `DELETE, PATCH /session/{sessionID}/message/{messageID}/part/{partID}`
- `POST /session/{sessionID}/permissions/{permissionID}`
- `POST /session/{sessionID}/prompt_async`
- `POST /session/{sessionID}/revert`
- `POST, DELETE /session/{sessionID}/share`
- `POST /session/{sessionID}/shell`
- `POST /session/{sessionID}/summarize`
- `GET /session/{sessionID}/todo`
- `POST /session/{sessionID}/unrevert`
- `GET /session/status`

## /skill (1 путей)

- `GET /skill`

## /sync (4 путей)

- `POST /sync/history`
- `POST /sync/replay`
- `POST /sync/start`
- `POST /sync/steal`

## /tui (13 путей)

- `POST /tui/append-prompt`
- `POST /tui/clear-prompt`
- `GET /tui/control/next`
- `POST /tui/control/response`
- `POST /tui/execute-command`
- `POST /tui/open-help`
- `POST /tui/open-models`
- `POST /tui/open-sessions`
- `POST /tui/open-themes`
- `POST /tui/publish`
- `POST /tui/select-session`
- `POST /tui/show-toast`
- `POST /tui/submit-prompt`

## /vcs (5 путей)

- `GET /vcs`
- `POST /vcs/apply`
- `GET /vcs/diff`
- `GET /vcs/diff/raw`
- `GET /vcs/status`
