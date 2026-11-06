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
        runtime.start()
    }

    public func restart() {
        phase = .loading
        runtime.restart()
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
            self.currentURL = URL(string: "http://127.0.0.1:\(port)/")
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