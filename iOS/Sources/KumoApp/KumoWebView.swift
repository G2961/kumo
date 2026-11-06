import SwiftUI
import WebKit

/// Wrapper that hosts the opencode web UI once the server reports ready.
public struct KumoWebView: UIViewRepresentable {
    public var url: URL?
    @Binding public var bridge: JSBridge

    public init(url: URL?, bridge: Binding<JSBridge>) {
        self.url = url
        self._bridge = bridge
    }

    public func makeCoordinator() -> Coordinator {
        Coordinator(bridge: bridge)
    }

    public func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.userContentController.add(context.coordinator.bridge, name: "codexNative")

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.isOpaque = false
        webView.backgroundColor = UIColor(white: 0.05, alpha: 1)

        context.coordinator.bridge.evaluate = { js in
            DispatchQueue.main.async {
                webView.evaluateJavaScript(js, completionHandler: nil)
            }
        }
        applySafeArea(webView)

        if let url {
            webView.load(URLRequest(url: url))
        }
        return webView
    }

    public func updateUIView(_ webView: WKWebView, context: Context) {
        context.coordinator.bridge = bridge
        bridge.evaluate = { js in
            DispatchQueue.main.async {
                webView.evaluateJavaScript(js, completionHandler: nil)
            }
        }
        if let url, webView.url != url {
            webView.load(URLRequest(url: url))
        }
    }

    private func applySafeArea(_ webView: WKWebView) {
        let script = """
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
        webView.evaluateJavaScript(script, completionHandler: nil)
    }

    public final class Coordinator: NSObject, WKNavigationDelegate {
        var bridge: JSBridge

        init(bridge: JSBridge) {
            self.bridge = bridge
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
            if url.host == "127.0.0.1" || url.host == "localhost" {
                decisionHandler(.allow)
            } else if url.scheme == "http" || url.scheme == "https" {
                #if canImport(UIKit)
                UIApplication.shared.open(url)
                #elseif canImport(AppKit)
                NSWorkspace.shared.open(url)
                #endif
                decisionHandler(.cancel)
            } else {
                decisionHandler(.allow)
            }
        }
    }
}