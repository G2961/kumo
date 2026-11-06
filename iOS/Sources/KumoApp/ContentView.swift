import SwiftUI
import WebKit

/// Main container: shows a native loading/status screen while the opencode server boots,
/// then transitions to an edge-to-edge WKWebView hosting the opencode web UI.
public struct ContentView: View {
    @StateObject private var model: AppModel

    public init(executablePath: String) {
        let cwd = ProjectManager.projectsRoot()
        let settings = LocalRuntime.Settings(executablePath: executablePath, cwd: cwd)
        let runtime = LocalRuntime(settings: settings)
        _model = StateObject(wrappedValue: AppModel(runtime: runtime))
    }

    public var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            if let url = model.currentURL, let bridge = model.bridge {
                KumoWebView(url: url, bridge: bridgeBinding(bridge),
                            onLoadStateChange: model.webViewDidChange)
                    .id(model.webViewAttempt)
                    .ignoresSafeArea(.all)
            }
            if model.currentURL == nil || model.webLoadState != .loaded {
                Color.black.ignoresSafeArea()
                statusView
            }
        }
        .onAppear {
            model.start()
        }
    }

    private var statusView: some View {
        VStack {
            Text("Kumo")
                .font(.system(size: 42, weight: .black))
                .foregroundColor(Color(white: 0.2))
            Spacer().frame(height: 16)
            if model.isLoading || (model.currentURL != nil && model.webLoadState == .loading) {
                ProgressView()
                    .padding(.bottom, 8)
            }
            Text(model.statusMessage.isEmpty ? "Starting server…" : model.statusMessage)
                .font(.subheadline)
                .foregroundColor(.white.opacity(0.7))
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)
                .padding(.bottom, 8)

            if model.currentURL != nil {
                if case .failed = model.webLoadState {
                    Button("Reload interface") {
                        model.reloadInterface()
                    }
                    .padding(.top, 8)
                }
            } else if case .failed = model.phase {
                Button("Retry") {
                    model.restart()
                }
                .padding(.top, 8)
                Button("Open interface anyway") {
                    model.forceUI()
                }
                .padding(.top, 4)
            }
        }
    }

    private func bridgeBinding(_ bridge: JSBridge) -> Binding<JSBridge> {
        Binding(get: { bridge }, set: { _ in })
    }
}