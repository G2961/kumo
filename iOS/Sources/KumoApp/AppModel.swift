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
    private var openingUI = false
    private let runtimeQueue = DispatchQueue(label: "kumo.runtime", qos: .userInitiated)
    private let uiQueue = DispatchQueue(label: "kumo.ui-startup", qos: .userInitiated)

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
        runtimeQueue.async { [runtime] in
            runtime.start()
        }
    }

    public func restart() {
        guard !isLoading else { return }
        phase = .loading
        statusMessage = "Restarting server…"
        runtimeQueue.async { [runtime] in
            runtime.restart()
        }
    }

    /// Opens the WebView UI even when the local server failed: the bundled UI works with
    /// remote SSH servers, so the app is still usable on devices where the local runtime
    /// cannot spawn (AMFI-restricted).
    public func forceUI() {
        guard !openingUI, currentURL == nil else { return }
        openUI(targetPort: 1)
    }

    private func openUI(targetPort: UInt16) {
        openingUI = true
        if currentURL == nil {
            phase = .loading
            statusMessage = "Opening interface…"
        }
        // Listener readiness is asynchronous. Wait off the main thread, and serialize
        // replacements so Retry and fallback cannot leave competing UI listeners behind.
        uiQueue.async { [self] in
            let existing = uiServer
            let ui = UIServer(targetPort: targetPort, user: runtime.authUser,
                              password: runtime.password, log: runtime.log)
            do {
                try ui.start()
                uiServer = ui
                existing?.stop()
                let dir = ProjectManager.currentDir().path
                let data = Data(dir.utf8).base64EncodedString()
                    .replacingOccurrences(of: "+", with: "-")
                    .replacingOccurrences(of: "/", with: "_")
                    .replacingOccurrences(of: "=", with: "")
                let url = URL(string: "http://127.0.0.1:\(ui.port)/_ui/index.html#d=\(data)")
                DispatchQueue.main.async {
                    self.currentURL = url
                    self.openingUI = false
                    self.phase = .ready
                }
            } catch {
                DispatchQueue.main.async {
                    self.openingUI = false
                    self.statusMessage = "UI server failed: \(error.localizedDescription)"
                    if self.currentURL == nil { self.phase = .failed }
                }
            }
        }
    }

    public var isReady: Bool { phase == .ready }
    public var isLoading: Bool { phase == .preparing || phase == .loading }
}

extension AppModel: RuntimeDelegate {
    public func runtime(_ runtime: LocalRuntime, didChange state: RuntimeState, message: String) {
        DispatchQueue.main.async {
            // A runtime retry may still report failures after fallback opened the UI.
            // Keep the interface mounted; runtime readiness alone is not UI readiness.
            guard self.currentURL == nil, !self.openingUI else { return }
            if state != .ready { self.phase = state.phaseMapping }
            self.statusMessage = state == .failed ? "Failed: \(message)" : message
        }
    }

    public func runtime(_ runtime: LocalRuntime, didLog line: String) {
        // Persisted via KumoLog; the UI can fetch it through the bridge.
    }

    public func runtime(_ runtime: LocalRuntime, didBecomeReady port: Int) {
        DispatchQueue.main.async {
            self.openUI(targetPort: UInt16(port))
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