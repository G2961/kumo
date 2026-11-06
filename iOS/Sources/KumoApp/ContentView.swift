import SwiftUI
import WebKit

/// Main container: shows a native loading/status screen while the opencode server boots,
/// then transitions to an edge-to-edge WKWebView hosting the opencode web UI.
public struct ContentView: View {
    @StateObject private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase

    public init() {
        _model = StateObject(wrappedValue: AppModel())
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
        .onAppear { model.start() }
        .onChange(of: scenePhase) { phase in
            if phase == .background { model.interruptForBackground() }
        }
    }

    private var statusView: some View {
        VStack {
            Text("Kumo")
                .font(.system(size: 42, weight: .black))
                .foregroundColor(Color(white: 0.2))
            Spacer().frame(height: 16)
            if model.webLoadState == .loading {
                ProgressView()
                    .padding(.bottom, 8)
            }
            Text(model.statusMessage.isEmpty ? "Opening Kumo…" : model.statusMessage)
                .font(.subheadline)
                .foregroundColor(.white.opacity(0.7))
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)
                .padding(.bottom, 8)

            if case .failed = model.webLoadState {
                Button(model.currentURL == nil ? "Retry" : "Reload interface") {
                    model.reloadInterface()
                }
                .padding(.top, 8)
            }
        }
    }

    private func bridgeBinding(_ bridge: JSBridge) -> Binding<JSBridge> {
        Binding(get: { bridge }, set: { _ in })
    }
}