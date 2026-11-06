import Foundation
import Combine

/// Owns the in-process agent independently of the WebView's lifetime.
public final class AppModel: ObservableObject {
    @Published public var statusMessage = "Opening Kumo…"
    @Published public var currentURL: URL?
    @Published public var bridge: JSBridge?
    @Published public var webLoadState: WebLoadState = .loading
    @Published public var webViewAttempt = 0

    private var backend: NativeAgentBackend?
    private var started = false

    public init() {}

    public func start() {
        guard !started else { return }
        started = true
        Task {
            do {
                let root = ProjectManager.applicationSupport()
                ProjectManager.restorePickedFolders()
                let backend = try NativeAgentBackend(root: root)
                let bridge = JSBridge(backend: backend, sshStore: SshConnectionStore())
                bridge.uiMediator = DefaultMediator()
                bridge.log = { KumoLog.shared.append($0) }
                self.backend = backend
                self.bridge = bridge
                let dir = ProjectManager.currentDir().path
                let hash = Data(dir.utf8).base64EncodedString()
                    .replacingOccurrences(of: "+", with: "-")
                    .replacingOccurrences(of: "/", with: "_")
                    .replacingOccurrences(of: "=", with: "")
                self.currentURL = URL(string: "kumo://app/_ui/index.html#d=\(hash)")
                KumoLog.shared.append("Native backend ready; no OpenCode process or HTTP listener.")
            } catch {
                self.started = false
                self.webLoadState = .failed(error.localizedDescription)
                self.statusMessage = "Kumo could not open its native storage: \(error.localizedDescription)"
            }
        }
    }

    public func webViewDidChange(_ state: WebLoadState) {
        webLoadState = state
        switch state {
        case .loading: statusMessage = "Loading interface…"
        case .loaded: statusMessage = ""
        case .failed(let message): statusMessage = "Interface failed: \(message)"
        }
    }

    public func reloadInterface() {
        if currentURL == nil { start(); return }
        webLoadState = .loading
        statusMessage = "Loading interface…"
        webViewAttempt += 1
    }

    public func interruptForBackground() {
        guard let backend else { return }
        Task { await backend.interruptActiveRuns() }
    }
}
