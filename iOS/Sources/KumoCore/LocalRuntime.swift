import Foundation
import Network
import Security

/// Launches the native darwin/arm64 opencode server on the device and waits until it is
/// ready. This is the iOS counterpart of `LocalRuntime.kt`, without the Android musl
/// loader / rootfs / bun machinery because the iOS binary is a true Darwin executable.
///
/// The binary is expected to be bundled at `opencode` next to the app resources, or
/// overridable for testing. It is spawned with `--hostname 127.0.0.1 --port <N>`.
public final class LocalRuntime {

    public struct Settings {
        /// Path to the bundled darwin/arm64 `opencode` binary.
        public var executablePath: String
        /// Base loopback port preferred for the server.
        public var preferredPort: UInt16 = 14100
        /// Working directory for the server.
        public var cwd: URL
        /// Additional environment overrides.
        public var environment: [String: String]

        public init(executablePath: String, preferredPort: UInt16 = 14100,
                    cwd: URL, environment: [String: String] = [:]) {
            self.executablePath = executablePath
            self.preferredPort = preferredPort
            self.cwd = cwd
            self.environment = environment
        }
    }

    public weak var delegate: RuntimeDelegate?

    public func settingsPath() -> String { settings.executablePath }

    public private(set) var state: RuntimeState = .preparing
    public private(set) var serverPort: UInt16 = 0
    public private(set) var authPort: UInt16 = 0
    public private(set) var password: String = ""

    public private(set) var proxy = ConnectProxy()
    private var authProxy: AuthProxy?
    public private(set) var sshMcp: SshMcpServer?
    private var secureStore = SshConnectionStore()

    private var process: Process?
    private var settings: Settings
    private let lock = NSLock()
    private var stopping = false
    public private(set) var authUser = "opencode"

    public init(settings: Settings) {
        self.settings = settings
        self.password = Self.loadOrCreatePassword()
    }

    // MARK: - Lifecycle

    public func start() {
        lock.lock()
        guard state == .stopped || state == .failed || process == nil else {
            lock.unlock()
            return
        }
        stopping = false
        lock.unlock()

        setState(.preparing, "Preparing runtime…")

        proxy = ConnectProxy { [weak self] line in
            self?.log("[net] \(line)")
        }
        do {
            try proxy.start(preferredPort: 14150)
        } catch {
            setState(.failed, "Proxy failed: \(error.localizedDescription)")
            return
        }

        if sshMcp == nil {
            let server = SshMcpServer(store: secureStore) { [weak self] line in
                self?.log("[ssh] \(line)")
            }
            do {
                try server.start(preferredPort: 0)
                sshMcp = server
            } catch {
                log("[ssh] MCP server failed: \(error.localizedDescription)")
            }
        }

        setState(.starting, "Starting the agent server…")
        serverPort = freePort(base: settings.preferredPort)
        password = Self.loadOrCreatePassword()

        let auth = AuthProxy(targetPort: serverPort, user: authUser, password: password) { [weak self] line in
            self?.log("[web] \(line)")
        }
        do {
            try auth.start(preferredPort: 14101)
        } catch {
            setState(.failed, "Auth proxy failed: \(error.localizedDescription)")
            return
        }
        authProxy = auth
        authPort = auth.port

        launchWithRetry()
    }

    private func launchWithRetry() {
        var attempt = 0
        while true {
            attempt += 1
            if stopping { setState(.stopped, "Stopped"); return }
            guard let proc = spawn() else {
                if attempt >= 3 {
                    setState(.failed, "Server did not start")
                    return
                }
                Thread.sleep(forTimeInterval: 0.8)
                continue
            }
            process = proc
            readOutput(proc)
            if awaitReady() && !stopping && proc.isRunning {
                setState(.ready, "Ready on 127.0.0.1:\(serverPort)")
                delegate?.runtime(self, didBecomeReady: Int(authPort))
                return
            }
            if stopping { setState(.stopped, "Stopped"); return }
            if attempt >= 3 {
                setState(.failed, "Server did not become ready")
                return
            }
            log("[retry] server not ready, restarting (\(attempt)/2)")
            proc.terminate()
            process = nil
            Thread.sleep(forTimeInterval: 0.8)
        }
    }

    private func spawn() -> Process? {
        guard FileManager.default.fileExists(atPath: settings.executablePath) else {
            setState(.failed, "opencode binary not found at \(settings.executablePath)")
            return nil
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: settings.executablePath)
        p.arguments = ["serve", "--hostname", "127.0.0.1", "--port", "\(serverPort)"]
        p.currentDirectoryURL = settings.cwd
        p.standardOutput = Pipe()
        p.standardError = Pipe()

        var env = ProcessInfo.processInfo.environment
        env["HOME"] = ProjectManager.homeDir().path
        env["TMPDIR"] = NSTemporaryDirectory()
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        env["LANG"] = "en_US.UTF-8"
        env["OPENCODE_SERVER_PASSWORD"] = password
        env["HTTP_PROXY"] = "http://127.0.0.1:\(proxy.port)"
        env["HTTPS_PROXY"] = "http://127.0.0.1:\(proxy.port)"
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
        env["OPENCODE_ENABLE_EXA"] = "true"
        env["OPENCODE_CONFIG_CONTENT"] = Self.configJSON(mcpURL: sshMcp?.url, mcpToken: sshMcp?.token)
        for (k, v) in settings.environment { env[k] = v }
        p.environment = env

        do {
            try p.run()
            return p
        } catch {
            log("runtime error: \(error.localizedDescription)")
            setState(.failed, error.localizedDescription)
            return nil
        }
    }

    private func readOutput(_ p: Process) {
        if let stdout = p.standardOutput as? Pipe {
            stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                if let s = String(data: data, encoding: .utf8), !s.isEmpty {
                    self?.log(s)
                }
            }
        }
        if let stderr = p.standardError as? Pipe {
            stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                if let s = String(data: data, encoding: .utf8), !s.isEmpty {
                    self?.log(s)
                }
            }
        }
        p.terminationHandler = { [weak self] proc in
            guard let self else { return }
            self.log("[process exited] code=\(proc.terminationStatus)")
            if !self.stopping {
                self.setExpired()
            }
        }
    }

    private func awaitReady() -> Bool {
        let deadline = Date().addingTimeInterval(120)
        while Date() < deadline {
            if stopping { return false }
            if process?.isRunning == false { return false }
            if isReady() { return true }
            Thread.sleep(forTimeInterval: 0.5)
        }
        return false
    }

    private func isReady() -> Bool {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:\(serverPort)/")!)
        let combined = "\(authUser):\(password)"
        request.setValue("Basic " + Data(combined.utf8).base64EncodedString(), forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 1.5

        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: request) { _, response, _ in
            if let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) {
                ok = true
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 3)
        return ok
    }

    public func stop() {
        stopping = true
        proxy.stop()
        authProxy?.stop()
        sshMcp?.stop()
        process?.terminate()
        process = nil
        setState(.stopped, "Stopped")
    }

    public func restart() {
        stop()
        Thread.sleep(forTimeInterval: 0.5)
        state = .stopped
        start()
    }

    // MARK: - Project / files

    public func register(_ directory: String) -> Bool {
        let dir = URL(fileURLWithPath: directory)
        ProjectManager.remember(dir)
        return true
    }

    // MARK: - Wiring into the UI

    public func log(_ line: String) {
        KumoLog.shared.append(line)
        delegate?.runtime(self, didLog: line)
    }

    private func setState(_ s: RuntimeState, _ msg: String) {
        state = s
        delegate?.runtime(self, didChange: s, message: msg)
    }

    private func setExpired() {
        guard !stopping else { return }
        lock.lock()
        let shouldFail = (state == .ready || process == nil)
        process = nil
        lock.unlock()
        if shouldFail {
            setState(.failed, "opencode process exited")
        }
    }

    // MARK: - Helpers

    private func freePort(base: UInt16) -> UInt16 {
        var candidate = base
        for _ in 0..<20 {
            // Probe port availability by binding a temporary listener.
            if portIsFree(candidate) { return candidate }
            candidate &+= 1
        }
        return 0
    }

    private func portIsFree(_ p: UInt16) -> Bool {
        do {
            let listener = try NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: p) ?? .any)
            listener.cancel()
            return true
        } catch {
            return false
        }
    }

    static func loadOrCreatePassword() -> String {
        let key = "kumo.runtime.password"
        var generated: String
        if let saved = keychainGet(key), !saved.isEmpty {
            return saved
        }
        var bytes = [UInt8](repeating: 0, count: 18)
        SecRandomCopyBytes(kSecRandomDefault, 18, &bytes)
        generated = Data(bytes).base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
        keychainSet(key, generated)
        return generated
    }

    static func configJSON(mcpURL: String? = nil, mcpToken: String? = nil) -> String {
        // Minimal config passed to the server via env, mirroring Android.
        let shell = "/bin/sh"
        var dict: [String: Any] = [
            "shell": shell,
            "permission": ["bash": "ask", "write": "ask", "edit": "ask"]
        ]
        if let mcpURL, let mcpToken {
            dict["mcp"] = [
                "mobile_ssh": [
                    "type": "remote",
                    "url": mcpURL,
                    "oauth": false,
                    "timeout": 900000,
                    "headers": ["Authorization": "Bearer \(mcpToken)"]
                ]
            ]
            var permission = (dict["permission"] as? [String: Any]) ?? [:]
            permission["mobile_ssh_ssh_exec"] = "ask"
            permission["mobile_ssh_ssh_write_file"] = "ask"
            dict["permission"] = permission
        }
        if let data = try? JSONSerialization.data(withJSONObject: dict, options: []),
           let s = String(data: data, encoding: .utf8) {
            return s
        }
        return "{}"
    }

    // MARK: - Keychain

    static func keychainGet(_ key: String) -> String? {
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func keychainSet(_ key: String, _ value: String) {
        let data = Data(value.utf8)
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
            kSecValueData as String: data
        ]
        SecItemDelete(q as CFDictionary)
        SecItemAdd(q as CFDictionary, nil)
    }
}