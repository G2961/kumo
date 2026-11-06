import Foundation
import WebKit

/// Bridges the web UI (mirrors the Android `CodexNative` / `CodexSplash` interfaces) with
/// native Kumo services. The page posts JSON action requests through
/// `window.webkit.messageHandlers.codexNative.postMessage(json)` and receives replies via
/// `window.codexNativeResult(id, json)`.
public final class JSBridge: NSObject, WKScriptMessageHandler {

    public var evaluate: ((String) -> Void)?
    public var log: ((String) -> Void) = { _ in }
    public var uiMediator: UIMediator?

    private let queue = DispatchQueue(label: "kumo.bridge", qos: .userInitiated)
    private let runtime: LocalRuntime
    private let sshStore: SshConnectionStore
    private let changeHistory: ChangeHistory
    private let shell = LocalShell()
    private let stt = STTEngine()

    public init(runtime: LocalRuntime, sshStore: SshConnectionStore) {
        self.runtime = runtime
        self.sshStore = sshStore
        self.changeHistory = ChangeHistory {
            ProjectManager.list().map { $0.path }
        }
        super.init()

        // React to STT results by pushing them into the page.
        stt.onResult = { [weak self] text in
            self?.evaluateJavaScript("__voiceResult", args: [text])
        }
        stt.onError = { [weak self] message in
            self?.evaluateJavaScript("__voiceError", args: [message])
        }
    }

    public func evaluateJavaScript(_ fn: String, args: [Any]) {
        let serialized = args.map { arg -> String in
            (try? JavaScriptJSON.encode(arg)) ?? "null"
        }.joined(separator: ", ")
        let js = "window.\(fn) && window.\(fn)(\(serialized));"
        DispatchQueue.main.async { self.evaluate?(js) }
    }

    private func escaped(_ s: String) -> String {
        (try? JavaScriptJSON.encode(s)) ?? "\"\""
    }

    public func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard message.name == "codexNative",
              let body = message.body as? [String: Any] else { return }
        queue.async { [weak self] in
            self?.handle(body)
        }
    }

    private func handle(_ request: [String: Any]) {
        let id = request["id"] as? String ?? ""
        let action = request["action"] as? String ?? ""
        let data = (request["data"] as? [String: Any]) ?? [:]

        do {
            let result = try dispatch(action: action, data: data)
            sendResult(id: id, result: result)
        } catch {
            sendResult(id: id, result: ["error": errorMessage(error)])
        }
    }

    private func errorMessage(_ e: Error) -> String {
        if let bridge = e as? BridgeError { return bridge.message }
        if let ssh = e as? SshClient.SshError { return ssh.errorDescription ?? "SSH request failed." }
        return String(describing: e)
    }

    private func dispatch(action: String, data: [String: Any]) throws -> [String: Any] {
        switch action {

        case "projects":
            return ["projects": ProjectManager.list().map { $0.path }]

        case "createProject":
            let name = data["name"] as? String ?? "project"
            let dir = ProjectManager.createNew(name: name)
            runtime.register(dir.path)
            return ["directory": dir.path]

        case "selectProject":
            guard let dir = data["directory"] as? String else { throw BridgeError("Missing directory") }
            let url = URL(fileURLWithPath: dir)
            guard ProjectManager.isDirectory(url) else { throw BridgeError("Not a directory") }
            ProjectManager.setCurrent(url)
            runtime.register(url.path)
            return ["directory": url.path]

        case "appearance":
            let dark = (data["theme"] as? String) == "dark"
            let prefs = UserDefaults.standard
            prefs.set(dark ? "dark" : "light", forKey: "uiTheme")
            return ["saved": true]

        case "appearanceGet":
            let dark = (UserDefaults.standard.string(forKey: "uiTheme") ?? "light") == "dark"
            return ["theme": dark ? "dark" : "light"]

        case "appearanceSave":
            let dark = (data["theme"] as? String) == "dark"
            UserDefaults.standard.set(dark ? "dark" : "light", forKey: "uiTheme")
            return ["saved": true]

        case "toast":
            let msg = data["msg"] as? String ?? ""
            uiMediator?.showToast(msg)
            return ["shown": true]

        case "shareText":
            let text = data["text"] as? String ?? ""
            let title = data["title"] as? String ?? "Kumo thread"
            uiMediator?.shareText(text, title: title)
            return ["shared": true]

        case "pickPhotos":
            let id = UUID().uuidString
            uiMediator?.pickPhotos { files in
                self.sendResult(id: id, result: ["files": files])
            }
            return ["picking": true]

        case "openFolder":
            let id = UUID().uuidString
            uiMediator?.openFolder { url in
                if let url {
                    ProjectManager.setCurrent(url)
                    self.runtime.register(url.path)
                    self.sendResult(id: id, result: ["directory": url.path, "cancelled": false])
                } else {
                    self.sendResult(id: id, result: ["cancelled": true])
                }
            }
            return ["opening": true]

        case "githubToken":
            let token = (data["token"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let home = ProjectManager.homeDir()
            let file = home.appendingPathComponent(".github-token")
            if token.isEmpty {
                try? FileManager.default.removeItem(at: file)
            } else {
                try? token.write(to: file, atomically: true, encoding: .utf8)
            }
            log("github: token \(token.isEmpty ? "cleared" : "saved")")
            return ["saved": true]

        case "logs":
            return [
                "log": KumoLog.shared.snapshot,
                "serverPort": runtime.serverPort,
                "authPort": runtime.authPort,
                "proxyPort": runtime.proxy.port,
                "sshPort": runtime.sshMcp?.port ?? 0
            ]

        case "restartServer":
            DispatchQueue.main.async { self.runtime.restart() }
            return ["restarting": true]

        case "turnDone":
            let failed = (data["error"] as? Bool) ?? false
            let title = (data["title"] as? String ?? "").trimmingCharacters(in: .whitespaces)
            let summary = (data["summary"] as? String ?? "").trimmingCharacters(in: .whitespaces)
            AgentNotifier.shared.notifyTurnDone(AgentNotifier.TurnSummary(title: title,
                                                                           summary: summary, failed: failed))
            AgentNotifier.shared.requestAuthorizationIfNeeded()
            return ["ok": true]

        // ---- Filesystem ----
        case "fsList":
            let path = data["path"] as? String ?? ProjectManager.currentDir().path
            let url = URL(fileURLWithPath: path)
            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) else {
                throw BridgeError("Папка недоступна: \(path)")
            }
            let fm = FileManager.default
            let entries = (try? fm.contentsOfDirectory(at: url, includingPropertiesForKeys: [.isDirectoryKey])) ?? []
            var dirs: [String] = []
            var files: [[String: Any]] = []
            for e in entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
                var d: ObjCBool = false
                _ = fm.fileExists(atPath: e.path, isDirectory: &d)
                if d.boolValue {
                    dirs.append(e.lastPathComponent)
                } else {
                    let size = (try? fm.attributesOfItem(atPath: e.path)[.size]) as? Int ?? 0
                    files.append(["name": e.lastPathComponent, "size": size])
                }
            }
            return ["path": url.path, "dirs": dirs, "files": files]

        case "fsRead":
            let path = data["path"] as? String ?? ""
            let url = URL(fileURLWithPath: path)
            guard let dataRead = try? Data(contentsOf: url) else { throw BridgeError("Файл не найден") }
            guard dataRead.count <= 512 * 1024 else { throw BridgeError("Файл больше 512 КБ") }
            guard !dataRead.contains(0) else { throw BridgeError("Бинарный файл — вьюер не для него") }
            return ["content": String(data: dataRead, encoding: .utf8) ?? "", "size": dataRead.count]

        case "fsWrite":
            let path = data["path"] as? String ?? ""
            let content = data["content"] as? String ?? ""
            let url = URL(fileURLWithPath: path)
            try? content.write(to: url, atomically: true, encoding: .utf8)
            let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size]) as? Int ?? 0
            log("files: wrote \(url.lastPathComponent)")
            return ["saved": true, "size": size]

        case "fsMkdir":
            let path = data["path"] as? String ?? ""
            guard let _ = try? FileManager.default.createDirectory(
                at: URL(fileURLWithPath: path), withIntermediateDirectories: true) else {
                throw BridgeError("Не создать папку")
            }
            return ["made": true]

        case "fsDelete":
            let path = data["path"] as? String ?? ""
            let url = URL(fileURLWithPath: path)
            do {
                if FileManager.default.fileExists(atPath: url.path) {
                    try FileManager.default.removeItem(at: url)
                }
            } catch {
                throw BridgeError("Не удалилось")
            }
            log("files: deleted \(url.lastPathComponent)")
            return ["deleted": true]

        case "saveExport":
            let name = data["name"] as? String ?? "export.md"
            let content = data["content"] as? String ?? ""
            let exportsDir = ProjectManager.documents().appendingPathComponent("Kumo/exports", isDirectory: true)
            try? FileManager.default.createDirectory(at: exportsDir, withIntermediateDirectories: true)
            let f = exportsDir.appendingPathComponent(name.isEmpty ? "export.md" : name)
            try? content.write(to: f, atomically: true, encoding: .utf8)
            log("export: \(f.path)")
            return ["path": f.path]

        // ---- Terminal ----
        case "termExec":
            let cwd = data["cwd"] as? String ?? ProjectManager.currentDir().path
            let cmd = data["cmd"] as? String ?? ""
            guard !cmd.isEmpty else { return ["run": ""] }
            let run = shell.exec(cwd: cwd, command: cmd,
                                 onDelta: { self.termDelta(run: $0, delta: $1) },
                                 onEnd: { self.termEnd(run: $0, code: $1, cwd: $2) })
            return ["run": run]

        case "termStop":
            let run = data["run"] as? String ?? ""
            if run.isEmpty {
                shell.stopAll()
                return ["stopped": 1]
            }
            shell.stop(run)
            return ["stopped": 1]

        // ---- SSH (MCP-external helpers, reachable via SshMcpServer) ----
        case "sessionContext", "connections", "revealSecrets", "removeConnection",
             "saveConnection", "cancelRemote", "tunnelOpen", "tunnelClose",
             "tunnelStatus", "remoteHistory":
            return runtime.sshMcp?.action(["action": action, "data": data]) ?? ["error": "SSH server not ready"]

        // ---- History (ChangeHistory) ----
        case "historyRegister":
            let directory = data["directory"] as? String ?? ProjectManager.currentDir().path
            try changeHistory.register(directory)
            return ["registered": true]

        case "historyHint":
            let directory = data["directory"] as? String ?? ProjectManager.currentDir().path
            let sid = data["sessionID"] as? String ?? ""
            let key = data["key"] as? String ?? ""
            let patch = data["patch"] as? String ?? ""
            try changeHistory.hint(directory: directory, sessionID: sid, key: key, patch: patch)
            return ["saved": true]

        case "historyRecord":
            let directory = data["directory"] as? String ?? ProjectManager.currentDir().path
            let sid = data["sessionID"] as? String ?? ""
            let turnsJSON = (data["turns"] as? [[String: Any]]) ?? []
            let hintsData = (data["hints"] as? [String: String]) ?? [:]
            let turns = turnsJSON.map { rawTurn(raw: $0) }
            try changeHistory.record(directory: directory, sessionID: sid, turns: turns, hints: hintsData)
            return ["saved": true]

        case "historyList":
            let directory = data["directory"] as? String ?? ProjectManager.currentDir().path
            let sid = data["sessionID"] as? String ?? ""
            return ["turns": try changeHistory.list(directory: directory, sessionID: sid)]

        case "historyShow":
            let directory = data["directory"] as? String ?? ProjectManager.currentDir().path
            let sid = data["sessionID"] as? String ?? ""
            let commit = data["commit"] as? String ?? ""
            return try changeHistory.show(directory: directory, sessionID: sid, commit: commit)

        // ---- STT ----
        case "sttSave":
            var cfg = stt.config
            cfg.mode = STTEngine.Mode(rawValue: data["mode"] as? String ?? "native") ?? .native
            let key = (data["key"] as? String ?? "").trimmingCharacters(in: .whitespaces)
            if !key.isEmpty { cfg.key = key }
            let model = (data["model"] as? String ?? "").trimmingCharacters(in: .whitespaces)
            if !model.isEmpty { cfg.model = model }
            cfg.baseURL = ((data["base"] as? String ?? "").trimmingCharacters(in: .whitespaces))
            stt.config = cfg
            log("voice: stt mode=\(cfg.mode.rawValue)")
            return ["saved": true]

        case "sttGet":
            let k = stt.config.key
            let mask = k.count > 10 ? "\(k.prefix(6))…\(k.suffix(4))" : ""
            return ["mode": stt.config.mode.rawValue, "model": stt.config.model,
                    "base": stt.config.baseURL, "hasKey": !k.isEmpty, "keyMask": mask]

        case "sttRecord":
            let on = (data["on"] as? Bool) ?? true
            if on { stt.start() } else { stt.stop() }
            return ["recording": on]

        case "voiceInput":
            stt.start()
            return ["started": true]

        case "exposePort":
            // Not applicable on iOS sandbox (no external bind); report the loopback port.
            let port = data["port"] as? Int ?? 0
            return ["ip": "", "port": port > 0 ? port + 10000 : 0]

        default:
            throw BridgeError("Unknown action: \(action)")
        }
    }

    // MARK: - Terminal JS bridges

    private func termDelta(run: String, delta: String) {
        let b64 = Data(delta.utf8).base64EncodedString()
        evaluateJavaScript("__termData", args: ["\(run)", b64])
    }

    private func termEnd(run: String, code: Int32, cwd: String) {
        let b64 = Data(cwd.utf8).base64EncodedString()
        let js = "window.__termEnd && window.__termEnd('\(run)',\(code),'\(b64)');"
        DispatchQueue.main.async { self.evaluate?(js) }
    }

    // MARK: - Helpers

    private func rawTurn(raw: [String: Any]) -> ChangeHistory.Turn {
        let files = (raw["files"] as? [[String: Any]]) ?? []
        return ChangeHistory.Turn(id: raw["id"] as? String ?? "",
                                  title: raw["title"] as? String ?? "",
                                  time: Int64(truncating: raw["time"] as? NSNumber ?? 0),
                                  files: files)
    }

    public func nativeEvent(_ name: String, detail: [String: Any]) {
        guard let s = try? JavaScriptJSON.encode(detail) else { return }
        let js = "window.dispatchEvent(new CustomEvent('\(name)',{detail:\(s)}));"
        DispatchQueue.main.async { self.evaluate?(js) }
    }

    private func sendResult(id: String, result: [String: Any]) {
        guard !id.isEmpty else { return }
        guard let jsonString = try? JavaScriptJSON.encode(result) else { return }
        let js = "window.codexNativeResult && window.codexNativeResult(\(escaped(id)), \(jsonString));"
        DispatchQueue.main.async { self.evaluate?(js) }
    }
}

public struct BridgeError: Error {
    public let message: String
    public init(_ message: String) { self.message = message }
}