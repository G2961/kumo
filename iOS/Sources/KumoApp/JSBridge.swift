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
    private let backend: NativeAgentBackend
    private let sshStore: SshConnectionStore
    private let stt = STTEngine()
    private var pickerPresented = false // Accessed only on the main queue.

    public init(backend: NativeAgentBackend, sshStore: SshConnectionStore) {
        self.backend = backend
        self.sshStore = sshStore
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
              message.frameInfo.isMainFrame,
              let url = message.frameInfo.request.url,
              url.scheme == "kumo", url.host == "app",
              url.user == nil, url.password == nil,
              url.path.hasPrefix("/_ui/"),
              let body = message.body as? [String: Any] else { return }
        Task { [weak self] in
            await self?.handle(body)
        }
    }

    private func handle(_ request: [String: Any]) async {
        let id = request["id"] as? String ?? ""
        let action = request["action"] as? String ?? ""
        let data = (request["data"] as? [String: Any]) ?? [:]

        do {
            guard request["data"] == nil || request["data"] is NSNull || request["data"] is [String: Any] else {
                throw BridgeError("Action data must be a JSON object.")
            }
            let result: Any
            if action.hasPrefix("history") {
                var historyData = data
                historyData["directory"] = try directory(from: data) ?? ProjectManager.currentDir().path
                result = try await backend.historyAction(action, data: historyData)
            } else {
                switch action {
                case "backendRequest":
                    result = try await backendRequest(data)
                case "backendSubscribe":
                    await backend.setEventHandler { [weak self] event in
                        self?.nativeEvent("kumoBackendEvent", detail: event)
                    }
                    result = ["subscribed": true]
                case "providerModels":
                    result = try await providerModels(data)
                case "pickPhotos", "openFolder":
                    result = try await pick(action: action)
                case "toast", "shareText", "turnDone", "sttSave", "sttGet", "sttRecord", "voiceInput":
                    result = try await MainActor.run { try self.dispatch(action: action, data: data) }
                default:
                    result = try await withCheckedThrowingContinuation { continuation in
                        queue.async {
                            do { continuation.resume(returning: try self.dispatch(action: action, data: data)) }
                            catch { continuation.resume(throwing: error) }
                        }
                    }
                }
            }
            sendResult(id: id, result: result)
        } catch {
            sendResult(id: id, result: ["error": errorMessage(error, data: data)])
        }
    }

    private func errorMessage(_ error: Error, data: [String: Any]) -> String {
        let message = (error as? BridgeError)?.message ?? (error as NSError).localizedDescription
        let redacted = redactSecrets(message, in: data)
        let sanitized = redacted.components(separatedBy: .controlCharacters).joined(separator: " ")
        return sanitized.isEmpty ? "Native request failed." : String(sanitized.prefix(512))
    }

    private func redactSecrets(_ message: String, in data: [String: Any]) -> String {
        let secretFields: Set<String> = ["key", "apikey", "token", "password", "privatekey", "passphrase", "authorization"]
        return data.reduce(message) { result, item in
            if secretFields.contains(item.key.lowercased()), let secret = item.value as? String, !secret.isEmpty {
                return result.replacingOccurrences(of: secret, with: "[REDACTED]")
            }
            if let nested = item.value as? [String: Any] { return redactSecrets(result, in: nested) }
            if let rows = item.value as? [[String: Any]] {
                return rows.reduce(result) { redactSecrets($0, in: $1) }
            }
            return result
        }
    }

    private func directory(from data: [String: Any]) throws -> String? {
        guard let value = data["directory"] else { return ProjectManager.currentDir().path }
        if value is NSNull { return nil }
        guard let directory = value as? String else { throw BridgeError("Directory must be a path string or null.") }
        if directory.isEmpty { return ProjectManager.currentDir().path }
        guard directory.hasPrefix("/"), directory.rangeOfCharacter(from: .controlCharacters) == nil else {
            throw BridgeError("Directory must be an absolute local path.")
        }
        return directory
    }

    private func backendRequest(_ data: [String: Any]) async throws -> Any {
        guard let method = data["method"] as? String,
              ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].contains(method.uppercased()) else {
            throw BridgeError("Backend request requires a valid HTTP method.")
        }
        guard let path = data["path"] as? String, path.hasPrefix("/"), !path.hasPrefix("//"),
              !path.contains("#"), !path.contains("\\"), path.rangeOfCharacter(from: .controlCharacters) == nil else {
            throw BridgeError("Backend request requires a local path, not a URL.")
        }
        let body: [String: Any]?
        if let value = data["body"], !(value is NSNull) {
            guard let object = value as? [String: Any], JSONSerialization.isValidJSONObject(object) else {
                throw BridgeError("Backend body must be a JSON object or null.")
            }
            body = object
        } else {
            body = nil
        }
        return try await backend.request(method: method.uppercased(), path: path,
                                         directory: directory(from: data), body: body)
    }

    private func pick(action: String) async throws -> [String: Any] {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.main.async {
                guard let mediator = self.uiMediator else {
                    continuation.resume(throwing: BridgeError("Native picker is unavailable."))
                    return
                }
                guard !self.pickerPresented else {
                    continuation.resume(throwing: BridgeError("Another native picker is already open."))
                    return
                }
                self.pickerPresented = true
                if action == "pickPhotos" {
                    mediator.pickPhotos { files in
                        DispatchQueue.main.async {
                            self.pickerPresented = false
                            continuation.resume(returning: ["files": files])
                        }
                    }
                } else {
                    mediator.openFolder { url in
                        DispatchQueue.main.async {
                            self.pickerPresented = false
                            guard let url else {
                                continuation.resume(returning: ["cancelled": true])
                                return
                            }
                            do {
                                // ProjectManager owns bookmark persistence and security-scoped access lifetime.
                                try ProjectManager.rememberPickedFolder(url)
                                ProjectManager.setCurrent(url)
                                continuation.resume(returning: ["directory": url.path, "cancelled": false])
                            } catch {
                                continuation.resume(throwing: error)
                            }
                        }
                    }
                }
            }
        }
    }

    private func providerModels(_ data: [String: Any]) async throws -> Any {
        guard let rawURL = data["baseURL"] as? String else { throw BridgeError("Provider base URL is required.") }
        let base = rawURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard base.rangeOfCharacter(from: .controlCharacters) == nil, !base.contains("\\"),
              let components = URLComponents(string: base), components.scheme?.lowercased() == "https",
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil,
              let baseURL = components.url else {
            throw BridgeError("Provider base URL must use HTTPS without credentials, query or fragment.")
        }
        let key: String
        if let value = data["key"], !(value is NSNull) {
            guard let text = value as? String, !text.unicodeScalars.contains(where: { $0.value == 10 || $0.value == 13 }) else {
                throw BridgeError("Provider API key must be text without CR or LF.")
            }
            key = text
        } else {
            key = ""
        }
        var request = URLRequest(url: baseURL.appendingPathComponent("models"))
        request.httpMethod = "GET"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if key.isEmpty, let providerID = data["providerID"] as? String, !providerID.isEmpty {
            let savedRequest = try await backend.modelCatalogRequest(providerID: providerID)
            // A saved key must never follow a newly edited URL before that configuration
            // is saved. The backend owns the credential and its configured destination.
            guard savedRequest.url == request.url else {
                throw BridgeError("The provider URL changed. Enter its key explicitly or save the new configuration before fetching models.")
            }
            request = savedRequest
        } else if !key.isEmpty {
            request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        }

        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 30
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.urlCredentialStorage = nil
        let session = URLSession(configuration: configuration, delegate: ProviderModelsRedirectDelegate(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let maxBytes = 2 * 1024 * 1024
        do {
            let (bytes, response) = try await session.bytes(for: request)
            guard let response = response as? HTTPURLResponse else { throw BridgeError("Provider returned an invalid HTTP response.") }
            if (300..<400).contains(response.statusCode) {
                throw BridgeError("Provider model catalog redirects are not allowed.")
            }
            guard (200..<300).contains(response.statusCode) else {
                let mime = response.mimeType?.lowercased() ?? ""
                let content = mime == "application/json" || mime.hasSuffix("+json")
                    ? await NativeProviderHTTPDiagnostic.readBody(bytes, expectedLength: response.expectedContentLength, session: session)
                    : nil
                let message = NativeProviderHTTPDiagnostic.description(status: response.statusCode, data: content)
                NSLog("[provider] model catalog: %@", message)
                throw BridgeError("Model list unavailable: \(message)")
            }
            guard response.expectedContentLength <= Int64(maxBytes) else {
                throw BridgeError("Provider model catalog exceeds the 2 MB limit.")
            }
            var content = Data()
            for try await byte in bytes {
                guard content.count < maxBytes else { throw BridgeError("Provider model catalog exceeds the 2 MB limit.") }
                content.append(byte)
            }
            let json: Any
            do { json = try JSONSerialization.jsonObject(with: content) }
            catch { throw BridgeError("Provider model catalog is not valid JSON.") }
            let object = json as? [String: Any]
            guard json is [Any] || object?["data"] is [Any] || object?["models"] is [Any] else {
                throw BridgeError("Provider model catalog must be an array or contain a data/models array.")
            }
            return json
        } catch let error as BridgeError {
            throw error
        } catch {
            let message = NativeModelClientError.transport((error as NSError).code).localizedDescription
            NSLog("[provider] model catalog: %@", message)
            throw BridgeError("Model list unavailable: \(message)")
        }
    }

    private func dispatch(action: String, data: [String: Any]) throws -> [String: Any] {
        switch action {

        case "projects":
            return ["projects": ProjectManager.list().map { $0.path }]

        case "createProject":
            let name = data["name"] as? String ?? "project"
            let dir = ProjectManager.createNew(name: name)
            return ["directory": dir.path]

        case "selectProject":
            guard let dir = data["directory"] as? String else { throw BridgeError("Missing directory") }
            let url = URL(fileURLWithPath: dir)
            guard ProjectManager.isDirectory(url) else { throw BridgeError("Not a directory") }
            ProjectManager.setCurrent(url)
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
            guard let mediator = uiMediator else { throw BridgeError("Native UI is unavailable.") }
            let msg = data["msg"] as? String ?? ""
            mediator.showToast(msg)
            return ["shown": true]

        case "shareText":
            guard let mediator = uiMediator else { throw BridgeError("Native UI is unavailable.") }
            let text = data["text"] as? String ?? ""
            let title = data["title"] as? String ?? "Kumo thread"
            mediator.shareText(text, title: title)
            return ["shared": true]

        case "githubToken":
            let token = (data["token"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let home = ProjectManager.homeDir()
            let file = home.appendingPathComponent(".github-token")
            if token.isEmpty {
                if FileManager.default.fileExists(atPath: file.path) { try FileManager.default.removeItem(at: file) }
            } else {
                try token.write(to: file, atomically: true, encoding: .utf8)
            }
            log("github: token \(token.isEmpty ? "cleared" : "saved")")
            return ["saved": true]

        case "logs":
            return ["log": KumoLog.shared.snapshot, "backend": "native"]

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
            try content.write(to: url, atomically: true, encoding: .utf8)
            log("files: wrote \(url.lastPathComponent)")
            return ["saved": true, "size": content.utf8.count]

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
            try FileManager.default.createDirectory(at: exportsDir, withIntermediateDirectories: true)
            let f = exportsDir.appendingPathComponent(name.isEmpty ? "export.md" : name)
            try content.write(to: f, atomically: true, encoding: .utf8)
            log("export: \(f.path)")
            return ["path": f.path]

        // ---- Compatibility probes (no SSH runtime) ----
        case "connections":
            return ["connections": [Any]()]
        case "remoteHistory":
            return ["events": [Any]()]
        case "cancelRemote":
            return ["cancelled": 0]

        case "termExec", "termStop", "restartServer", "exposePort", "sessionContext",
             "revealSecrets", "removeConnection", "saveConnection", "tunnelOpen", "tunnelClose", "tunnelStatus":
            throw BridgeError("This action is not supported by the native iOS backend.")

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

        default:
            throw BridgeError("Unknown native bridge action.")
        }
    }

    // MARK: - JavaScript delivery

    public func nativeEvent(_ name: String, detail: [String: Any]) {
        guard let s = try? JavaScriptJSON.encode(detail) else { return }
        let js = "window.dispatchEvent(new CustomEvent(\(escaped(name)),{detail:\(s)}));"
        DispatchQueue.main.async { self.evaluate?(js) }
    }

    private func sendResult(id: String, result: Any) {
        guard !id.isEmpty else { return }
        let jsonString = (try? JavaScriptJSON.encode(result)) ?? "{\"error\":\"Native response could not be encoded as JSON.\"}"
        let js = "window.codexNativeResult && window.codexNativeResult(\(escaped(id)), \(jsonString));"
        DispatchQueue.main.async { self.evaluate?(js) }
    }
}

private final class ProviderModelsRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

public struct BridgeError: Error {
    public let message: String
    public init(_ message: String) { self.message = message }
}