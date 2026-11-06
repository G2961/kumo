# Kumo for iPad / iOS (native port)

Портированная версия Android-клиента Kumo (opencode on-device) под **iOS 16+**,
сфокусированная на локальном запуске **opencode server** на jailbroken iPad (palera1n).

## Что это

Это Swift Package (SPM) с двумя библиотеками и host-шаблоном:

```
iOS/
├── Package.swift                 # SPM манифест (iOS 16, KumoCore + KumoApp + тесты)
├── xcodegen.yml                  # (опционально) генерация .xcodeproj через XcodeGen
├── AppHarness/
│   └── KumoApp.swift             # @main entry point — скопируй в свой Xcode host-app
├── Sources/
│   ├── KumoCore/
│   │   ├── KumoLog.swift           # bounded ring log (аналог LocalRuntime.logs)
│   │   ├── RuntimeState.swift      # lifecycle enum + delegate
│   │   ├── ProjectManager.swift    # проекты внутри песочницы (было ProjectManager.kt)
│   │   ├── ConnectProxy.swift      # loopback CONNECT-прокси (было ConnectProxy.kt)
│   │   ├── AuthProxy.swift         # Basic-auth прокси (было AuthProxy.kt)
│   │   ├── LocalRuntime.swift      # запуск darwin/arm64 opencode server (было LocalRuntime.kt)
│   │   ├── LocalShell.swift        # /bin/sh для termExec (было termExec в MainActivity)
│   │   ├── SshConnectionStore.swift # шифрованное хранилище SSH (было SshConnectionStore.kt)
│   │   ├── SshClient.swift         # SSH/SFTP через системный ssh (было RemoteSsh.java)
│   │   ├── SshMcpServer.swift      # локальный MCP-сервер (было SshToolsServer.java)
│   │   └── ChangeHistory.swift     # git-история патчей (было ChangeHistory.kt, JGit→git)
│   └── KumoApp/
│       ├── AppModel.swift          # ObservableObject состояние + runtime bridge
│       ├── JSBridge.swift          # Native↔Web UI мост (было CodexNative/SplashBridge)
│       ├── KumoWebView.swift       # WKWebView-обёртка
│       ├── ContentView.swift       # loading-экран → WebView UI
│       ├── STTEngine.swift         # голос: запись WAV → HTTP-транскрибация
│       ├── AgentNotifier.swift     # локальные уведомления о финише хода
│       ├── BridgeMediator.swift    # протокол UI (toast/share) + UIKit-хелперы
│       └── DefaultMediator.swift   # PHPicker + UIDocumentPicker реализация
└── Tests/KumoCoreTests/           # unit-тесты логики (запуск: swift test на macOS)
```

## Отличия от Android-версии (важно!)

Android-версия гоняет **Linux musl** бинарь opencode через `ld-musl-aarch64.so.1`,
rootfs/Alpine, bun/npm. Всё это **не работает и не нужно на iOS**. Здесь:

1. Нужен **нативный darwin/arm64** бинарь opencode (тот же Go-проект, собранный под
   `GOOS=darwin GOARCH=arm64`). Никакого musl-loader / rootfs / bun.
2. Процесс запускается обычным `Process()` из песочницы — на palera1n ограничения
   на fork/exec отсутствуют, поэтому обычный запуск работает.
3. Прокси (ConnectProxy), auth-прокси (AuthProxy) и файловая логика портированы
   напрямую под Foundation/Network framework.

## Сборка

### Способ A — Xcode (рекомендую)
1. Склонируй репо, открой `iOS/` в Xcode.
2. `File → New → iOS App`, назови **Kumo**, SwiftUI, min iOS 16.
3. Добавь в зависимости пакета пути `iOS` (KumoCore + KumoApp).
4. Скопируй `AppHarness/KumoApp.swift` в таргет приложения.
5. Перетащи darwin/arm64 бинарь `opencode` в таргет (пункт "Copy Bundle Resources").
6. Воткни реальное устройство (jailbroken iPad), `⌘R`. Настрой подпись на палера1н.

### Способ B — XcodeGen
```
brew install xcodegen
cd iOS
xcodegen generate
open Kumo.xcodeproj
```

## Подготовка darwin/arm64 бинаря opencode

Из исходника opencode (AnomalyCo/opencode):

```bash
GOOS=darwin GOARCH=arm64 CGO_ENABLED=0 go build -o opencode .
```

Положи получившийся файл в таргет как resource (Способ A, шаг 5). Путь к нему в
`AppHarness/KumoApp.swift` задаётся через `Bundle.main.path(forResource: "opencode",...)`.

> Пока нет бинаря — приложение покажет «opencode binary not found», это ожидаемо.

## Info.plist (локальная сеть + loopback)

Добавь в Info.plist host-app:

```xml
<key>NSAppTransportSecurity</key>
<dict>
  <key>NSAllowsLocalNetworking</key><true/>
  <key>NSAllowsArbitraryLoadsInWebContent</key><true/>
</dict>
```

WKWebView ходит на `http://127.0.0.1:<authPort>/` — loopback, поэтому
NSAllowsArbitraryLoadsInWebContent и cover для WebKit.

## Что уже перенесено

- Локальный запуск сервера + readiness-проверка
- AuthProxy (вход без промпта)
- ConnectProxy (исходящий DNS/прокси для провайдеров)
- Проекты: список/создать/выбрать/файловые операции
- Логирование (KumoLog + bridge action "logs")
- GitHub token (в sandbox home)
- Экспорт thread в Documents/Kumo/exports
- SshConnectionStore (SSH-метаданные в iOS Keychain)
- **SSH + MCP** (`SshMcpServer` на `/mcp`, `/bridge`; инструменты ssh_connections / ssh_exec / ssh_read_file / ssh_write_file; шифрованные `mobile_ssh_*` подключены к `OPENCODE_CONFIG_CONTENT`; bridge-actions connections/save/remove/reveal/session/tunnel/history)
- **ChangeHistory** (git-история патчей: register / hint / record / list / show через system git, refs под `refs/codex/changes/<session>`)
- **STT** (запись MPC → WAV через AVAudioEngine → Groq / custom OpenAI-compatible / OpenRouter)
- **Push-уведомления** (turnDone → UNUserNotificationCenter, автозакрытие 12с)
- **Terminal** (termExec / termStop через `/bin/sh`)
- **Toasts / share / photo picker** (UIAlert, UIActivityViewController, PHPicker), open folder (UIDocumentPicker)

## Важные отличия в деталях (риски, которые стоит допилить)

- SSH использует системные `/usr/bin/ssh` и `/usr/bin/sftp`. На device это работает, но
  password-auth через stdin хрупка (нужен `sshpass`-подобный подход или askpass); для
  key-auth надо завести identity в `~/.ssh`. `SshClient.readFile/writeFile` реализованы
  через batch-файл sftp — best-effort.
- `ChangeHistory` гоняет plumbing `git` под-процессами; каждая операция с чек-инддексом
  во временный index, refs обновляются вручную. Это основной кандидат на ручную доработку.
- STT "native" (on-device распознавание без сети) на iOS не реализовано — только через
  HTTP-провайдеров, как и в Android-версии.

## TODO (осталось на потом)

- Полноценный worker для SSH tunnel (tunnelOpen/tunnelClose) — сейчас возвращает
  stub через `remoteHistory`/`cancelRemote`.
- Voice "native" recognizer (Speech.framework) как альтернатива HTTP-style STT.

## Тесты

```bash
cd iOS
swift test          # на macOS; KumoCore не зависит от UIKit
```

`KumoCoreTests` (включая `KumoCoreExtraTests`) покрывают:
- ring-log (`KumoLog`): bounded snapshot + observer + clear
- пароль (`loadOrCreatePassword`): URL-safe, валидный JSON config + включение `mcp.mobile_ssh` и permissions
- project-менеджмент: create/list/unique-name/canWrite/home-dir
- `SshClient`: unified-diff патчи (identity/empty/fallback), redact секретов (пароль, Bearer, sk-токены), валидация хостов/портов/директорий

Сам модуль `KumoApp` (iOS-only: SwiftUI + WebKit) компилируется в Xcode для iPad и не
входит в эти unit-тесты.