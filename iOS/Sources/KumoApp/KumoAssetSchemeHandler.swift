import Foundation
import WebKit

/// Loads trusted bundled UI assets without listening on a socket or proxying API traffic.
final class KumoAssetSchemeHandler: NSObject, WKURLSchemeHandler {
    func webView(_ webView: WKWebView, start urlSchemeTask: WKURLSchemeTask) {
        guard let url = urlSchemeTask.request.url, url.scheme == "kumo", url.host == "app" else {
            urlSchemeTask.didFailWithError(NSError(domain: "KumoAssets", code: 1))
            return
        }
        let path = url.path
        let name: String
        if path == "/_ui/index.html" { name = "codex.html" }
        else if path.hasPrefix("/_ui/") { name = String(path.dropFirst(5)) }
        else if path.hasPrefix("/kumo_") { name = String(path.dropFirst()) }
        else { urlSchemeTask.didFailWithError(NSError(domain: "KumoAssets", code: 404)); return }
        // Native bridge is installed in this origin; never serve Documents overrides or
        // arbitrary filesystem content as executable UI.
        guard !name.isEmpty, !name.contains("/"), !name.contains("\\"), name != ".", name != "..",
              let resource = Bundle.main.resourceURL?.appendingPathComponent("AppHarness-Resources/").appendingPathComponent(name),
              let data = try? Data(contentsOf: resource) else {
            urlSchemeTask.didFailWithError(NSError(domain: "KumoAssets", code: 404,
                userInfo: [NSLocalizedDescriptionKey: "Bundled UI asset missing: \(name)"]))
            return
        }
        let response = URLResponse(url: url, mimeType: Self.mime(name), expectedContentLength: data.count,
                                   textEncodingName: name.hasSuffix(".html") || name.hasSuffix(".js") || name.hasSuffix(".css") ? "utf-8" : nil)
        urlSchemeTask.didReceive(response)
        urlSchemeTask.didReceive(data)
        urlSchemeTask.didFinish()
    }

    func webView(_ webView: WKWebView, stop urlSchemeTask: WKURLSchemeTask) {
        // Bundle reads and delivery complete synchronously; there is no pending task.
    }

    private static func mime(_ name: String) -> String {
        switch (name as NSString).pathExtension.lowercased() {
        case "html": return "text/html"
        case "js": return "application/javascript"
        case "css": return "text/css"
        case "png": return "image/png"
        case "svg": return "image/svg+xml"
        default: return "application/octet-stream"
        }
    }
}
