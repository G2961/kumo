import Foundation
import Combine

/// Observable state backing ContentView. Bridges the runtime lifecycle and exposes the
/// URL the WebView should load once the server is ready.
public final class AppModel: ObservableObject {
    public enum Phase: Equatable {
        case preparing
        case loading
        case ready
        case failed
        case stopped
    }

    @Published public var phase: Phase = .preparing
    @Published public var statusMessage = "Starting server…"
    @Published public var currentURL: URL?
    @Published public var bridge: JSBridge?

    private let runtime: LocalRuntime
    private var uiServer: UIServer?
    private var started = false

    public init(runtime: LocalRuntime) {
        self.runtime = runtime
        runtime.delegate = self
    }

    public func start() {
        guard !started else { return }
        started = true
        phase = .preparing
        let sshStore = SshConnectionStore()
        let bridge = JSBridge(runtime: runtime, sshStore: sshStore)
        bridge.uiMediator = DefaultMediator()
        self.bridge = bridge
        // start() runs a blocking boot loop (spawn + awaitReady with sleeps); on the main
        // thread it froze the UI and the watchdog killed the app (crash bug_type 509).
        DispatchQueue.global(qos: .userInitiated).async { [runtime] in
            runtime.start()
        }
    }

    public func restart() {
        phase = .loading
        DispatchQueue.global(qos: .userInitiated).async { [runtime] in
            runtime.restart()
        }
    }

    /// Opens the WebView UI even when the local server failed: the bundled UI works with
    /// remote SSH servers, so the app is still usable on devices where the local runtime
    /// cannot spawn (AMFI-restricted).
    public func forceUI() {
        if uiServer == nil {
            let ui = UIServer(targetPort: 1, user: runtime.authUser, password: runtime.password)
            do { try ui.start() } catch { statusMessage = "UI server failed: \(error.localizedDescription)"; return }
            uiServer = ui
        }
        let dir = ProjectManager.currentDir().path
        let data = Data(dir.utf8).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        currentURL = URL(string: "http://127.0.0.1:\(uiServer!.port)/_ui/index.html#d=\(data)")
        phase = .ready
    }

    public var isReady: Bool { phase == .ready }
    public var isLoading: Bool { phase == .preparing || phase == .loading }
}

extension AppModel: RuntimeDelegate {
    public func runtime(_ runtime: LocalRuntime, didChange state: RuntimeState, message: String) {
        DispatchQueue.main.async {
            self.phase = state.phaseMapping
            self.statusMessage = state == .failed ? "Failed: \(message)" : message
        }
    }

    public func runtime(_ runtime: LocalRuntime, didLog line: String) {
        // Persisted via KumoLog; the UI can fetch it through the bridge.
    }

    public func runtime(_ runtime: LocalRuntime, didBecomeReady port: Int) {
        DispatchQueue.main.async {
            // Front-end: serve the bundled Kumo UI and proxy the API on one origin
            // (mirrors Android's /_ui/index.html substitution).
            do {
                let ui = UIServer(targetPort: UInt16(port),
                                  user: runtime.authUser,
                                  password: runtime.password)
                if let existing = self.uiServer {
                    existing.stop()
                }
                try ui.start()
                self.uiServer = ui
                let dir = ProjectManager.currentDir().path
                let data = Data(dir.utf8).base64EncodedString()
                    .replacingOccurrences(of: "+", with: "-")
                    .replacingOccurrences(of: "/", with: "_")
                    .replacingOccurrences(of: "=", with: "")
                self.currentURL = URL(string: "http://127.0.0.1:\(ui.port)/_ui/index.html#d=\(data)")
            } catch {
                self.statusMessage = "UI server failed: \(error.localizedDescription)"
                self.phase = .failed
                return
            }
            self.phase = .ready
        }
    }
}

extension RuntimeState {
    var phaseMapping: AppModel.Phase {
        switch self {
        case .preparing: return .preparing
        case .starting: return .loading
        case .ready: return .ready
        case .failed: return .failed
        case .stopped: return .stopped
        }
    }
}