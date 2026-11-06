import SwiftUI
import WebKit

public enum WebLoadState: Equatable {
    case loading
    case loaded
    case failed(String)
}

/// Wrapper that hosts the opencode web UI once the server reports ready.
public struct KumoWebView: UIViewRepresentable {
    public var url: URL?
    @Binding public var bridge: JSBridge
    public var onLoadStateChange: (WebLoadState) -> Void

    public init(
        url: URL?,
        bridge: Binding<JSBridge>,
        onLoadStateChange: @escaping (WebLoadState) -> Void = { _ in }
    ) {
        self.url = url
        self._bridge = bridge
        self.onLoadStateChange = onLoadStateChange
    }

    public func makeCoordinator() -> Coordinator {
        Coordinator(bridge: bridge, onLoadStateChange: onLoadStateChange)
    }

    public func makeUIView(context: Context) -> WKWebView {
        NSLog("[web] creation begin nativePID=%d", ProcessInfo.processInfo.processIdentifier)
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.setURLSchemeHandler(KumoAssetSchemeHandler(), forURLScheme: "kumo")
        config.userContentController.addUserScript(WKUserScript(
            source: "window.KumoNativeBackend = true;",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))
        config.userContentController.add(context.coordinator.bridge, name: "codexNative")
        config.userContentController.add(context.coordinator, name: "kumoDiagnostics")
        config.userContentController.addUserScript(WKUserScript(
            source: Self.diagnosticsScript,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: false
        ))
        config.userContentController.addUserScript(WKUserScript(
            source: Self.safeAreaScript,
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: true
        ))

        let webView = WKWebView(frame: .zero, configuration: config)
        NSLog("[web] creation complete nativePID=%d", ProcessInfo.processInfo.processIdentifier)
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.isOpaque = false
        webView.backgroundColor = UIColor(white: 0.05, alpha: 1)
        context.coordinator.updateBridge(bridge, webView: webView)

        if let url {
            context.coordinator.load(url, in: webView)
        }
        return webView
    }

    public func updateUIView(_ webView: WKWebView, context: Context) {
        context.coordinator.onLoadStateChange = onLoadStateChange
        context.coordinator.updateBridge(bridge, webView: webView)
        if let url, context.coordinator.requestedURL != url {
            context.coordinator.load(url, in: webView)
        }
    }

    public static func dismantleUIView(_ webView: WKWebView, coordinator: Coordinator) {
        coordinator.invalidate()
        webView.navigationDelegate = nil
        webView.stopLoading()
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "kumoDiagnostics")
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "codexNative")
    }

    private static let diagnosticsScript = """
    (function(){
      function report(kind, line, column){
        try {
          window.webkit.messageHandlers.kumoDiagnostics.postMessage({
            kind: kind, line: line || 0, column: column || 0
          });
        } catch (_) {}
      }
      window.addEventListener('error', function(event){
        report('error', event.lineno, event.colno);
      });
      window.addEventListener('unhandledrejection', function(){
        report('unhandledrejection', 0, 0);
      });
    })();
    """

    private static let safeAreaScript = """
    (function(){
      function inset(){
        var top = window.visualViewport ? visualViewport.offsetTop : 0;
        var bottom = window.innerHeight - (window.visualViewport ? visualViewport.height : 0) - (window.visualViewport ? visualViewport.offsetTop : 0);
        document.documentElement.style.setProperty('--sat', Math.max(0, top) + 'px');
        document.documentElement.style.setProperty('--sab', Math.max(0, bottom) + 'px');
      }
      window.addEventListener('resize', inset);
      window.addEventListener('scroll', inset, true);
      inset();
    })();
    """

    public final class Coordinator: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
        var bridge: JSBridge
        var onLoadStateChange: (WebLoadState) -> Void
        private(set) var requestedURL: URL?
        private var activeNavigation: WKNavigation?
        private var isLoading = false
        private var generation: UInt64 = 0
        private var timeoutWorkItem: DispatchWorkItem?

        init(bridge: JSBridge, onLoadStateChange: @escaping (WebLoadState) -> Void) {
            self.bridge = bridge
            self.onLoadStateChange = onLoadStateChange
        }

        func updateBridge(_ bridge: JSBridge, webView: WKWebView) {
            self.bridge = bridge
            bridge.evaluate = { [weak webView] js in
                DispatchQueue.main.async { [weak webView] in
                    webView?.evaluateJavaScript(js, completionHandler: nil)
                }
            }
        }

        func load(_ url: URL, in webView: WKWebView) {
            guard requestedURL != url else { return }
            requestedURL = url
            generation += 1
            let loadGeneration = generation
            cancelTimeout()
            isLoading = true
            activeNavigation = nil

            NSLog("[web] load begin generation=%llu", loadGeneration)
            activeNavigation = webView.load(URLRequest(url: url))
            NSLog("[web] load returned generation=%llu navigation=%@", loadGeneration,
                  activeNavigation == nil ? "none" : "created")

            // SwiftUI may initiate a load during make/updateUIView; publish on the next turn.
            DispatchQueue.main.async { [weak self] in
                guard let self, self.generation == loadGeneration, self.isLoading else { return }
                self.onLoadStateChange(.loading)
            }

            let timeout = DispatchWorkItem { [weak self, weak webView] in
                guard let self, self.generation == loadGeneration, self.isLoading else { return }
                self.fail(NSError(
                    domain: NSURLErrorDomain,
                    code: NSURLErrorTimedOut,
                    userInfo: [NSLocalizedDescriptionKey: "Web view load timed out after 15 seconds."]
                ), event: "load timeout")
                webView?.stopLoading()
            }
            timeoutWorkItem = timeout
            DispatchQueue.main.asyncAfter(deadline: .now() + 15, execute: timeout)
        }

        private func cancelTimeout() {
            timeoutWorkItem?.cancel()
            timeoutWorkItem = nil
        }

        func invalidate() {
            generation += 1
            cancelTimeout()
            isLoading = false
            activeNavigation = nil
            onLoadStateChange = { _ in }
        }

        private func isCurrent(_ navigation: WKNavigation?) -> Bool {
            guard let navigation else { return isLoading }
            guard let activeNavigation else { return false }
            return navigation === activeNavigation
        }

        private func fail(_ error: NSError, event: String) {
            cancelTimeout()
            isLoading = false
            // Descriptions may contain URLs or credentials; expose them to the UI, not NSLog.
            NSLog("[web] %@ domain=%@ code=%ld", event, error.domain, error.code)
            onLoadStateChange(.failed(
                "\(event): domain=\(error.domain) code=\(error.code) description=\(error.localizedDescription)"
            ))
        }

        public func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            NSLog("[web] didStartProvisionalNavigation current=%@", isCurrent(navigation) ? "yes" : "no")
        }

        public func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
            NSLog("[web] didCommit current=%@", isCurrent(navigation) ? "yes" : "no")
        }

        public func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            NSLog("[web] didFinish current=%@", isCurrent(navigation) ? "yes" : "no")
            guard isCurrent(navigation), isLoading else { return }
            cancelTimeout()
            isLoading = false
            onLoadStateChange(.loaded)
            webView.callAsyncJavaScript("""
            const health = await api('/global/health');
            const sessions = await api('/session');
            const providers = await api('/config/providers');
            const listing = await nativeCall('fsList', {path: S.dir});
            return {backend: health.backend, sessions: sessions.length,
              providers: providers.providers.length, mainChildren: document.getElementById('main').children.length,
              files: listing.files.length};
            """, arguments: [:], in: nil, in: .page) { result in
                switch result {
                case .success(let value):
                    guard let info = value as? [String: Any], info["backend"] as? String == "native" else {
                        NSLog("[web] native bridge smoke returned unexpected data")
                        return
                    }
                    NSLog("[web] native bridge smoke OK sessions=%@ providers=%@ mainChildren=%@ files=%@",
                          String(describing: info["sessions"] ?? 0),
                          String(describing: info["providers"] ?? 0),
                          String(describing: info["mainChildren"] ?? 0),
                          String(describing: info["files"] ?? 0))
                case .failure(let error):
                    NSLog("[web] native bridge smoke failed domain=%@ code=%ld", (error as NSError).domain, (error as NSError).code)
                }
            }
            webView.evaluateJavaScript("""
            (function(){
              var app = document.getElementById('app');
              var feed = document.getElementById('feed');
              return {width: app ? app.clientWidth : 0, height: app ? app.clientHeight : 0,
                feedChildren: feed ? feed.children.length : 0,
                renderAvailable: typeof render === 'function'};
            })();
            """) { result, error in
                guard let metrics = result as? [String: Any], error == nil else {
                    NSLog("[web] DOM probe failed code=%ld", (error as NSError?)?.code ?? 0)
                    return
                }
                NSLog("[web] DOM width=%@ height=%@ feedChildren=%@ renderAvailable=%@",
                      String(describing: metrics["width"] ?? 0),
                      String(describing: metrics["height"] ?? 0),
                      String(describing: metrics["feedChildren"] ?? 0),
                      String(describing: metrics["renderAvailable"] ?? false))
            }
        }

        public func webView(
            _ webView: WKWebView,
            didFailProvisionalNavigation navigation: WKNavigation!,
            withError error: Error
        ) {
            navigationFailed(navigation, error: error, event: "didFailProvisionalNavigation")
        }

        public func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            navigationFailed(navigation, error: error, event: "didFail")
        }

        private func navigationFailed(_ navigation: WKNavigation?, error: Error, event: String) {
            let nsError = error as NSError
            guard isCurrent(navigation), isLoading else {
                NSLog("[web] %@ ignored domain=%@ code=%ld", event, nsError.domain, nsError.code)
                return
            }
            fail(nsError, event: event)
        }

        public func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            fail(NSError(
                domain: WKErrorDomain,
                code: WKError.Code.webContentProcessTerminated.rawValue,
                userInfo: [NSLocalizedDescriptionKey: "Web content process terminated."]
            ), event: "webViewWebContentProcessDidTerminate")
        }

        public func userContentController(
            _ userContentController: WKUserContentController,
            didReceive message: WKScriptMessage
        ) {
            guard message.name == "kumoDiagnostics",
                  let body = message.body as? [String: Any],
                  let kind = body["kind"] as? String,
                  kind == "error" || kind == "unhandledrejection" else { return }
            let line = (body["line"] as? Int).flatMap { $0 >= 0 ? $0 : nil } ?? 0
            let column = (body["column"] as? Int).flatMap { $0 >= 0 ? $0 : nil } ?? 0
            NSLog("[web] JavaScript %@ mainFrame=%@ line=%ld column=%ld", kind,
                  message.frameInfo.isMainFrame ? "yes" : "no", line, column)
        }

        public func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url else {
                decisionHandler(.cancel)
                return
            }
            if url.scheme == "kumo", url.host == "app" {
                decisionHandler(.allow)
            } else if url.scheme == "http" || url.scheme == "https" {
                #if canImport(UIKit)
                UIApplication.shared.open(url)
                #elseif canImport(AppKit)
                NSWorkspace.shared.open(url)
                #endif
                decisionHandler(.cancel)
            } else {
                decisionHandler(.cancel)
            }
        }
    }
}