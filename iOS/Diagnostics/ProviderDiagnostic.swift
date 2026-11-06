import Foundation
import Security

/// Device-only diagnostic helper. Never bundled into Kumo; prints no credentials,
/// request headers, provider bodies, model output, or user transcript.
@main
struct ProviderDiagnostic {
    static func main() async {
        do {
            let args = CommandLine.arguments
            guard args.count >= 4 else {
                print("Usage: KumoProviderDiagnostic <state.json> <providerID> <models|probe|responses>")
                return
            }
            let stateURL = URL(fileURLWithPath: args[1])
            let root = stateURL.deletingLastPathComponent().deletingLastPathComponent()
            let state = try JSONSerialization.jsonObject(with: Data(contentsOf: stateURL)) as? [String: Any]
            guard let config = state?["config"] as? [String: Any],
                  let provider = (config["provider"] as? [String: [String: Any]])?[args[2]],
                  let base = (provider["options"] as? [String: Any])?["baseURL"] as? String,
                  let url = URL(string: base), url.scheme == "https", url.user == nil, url.password == nil,
                  let models = provider["models"] as? [String: [String: Any]], let model = models.keys.sorted().first else {
                print("DIAGNOSTIC_CONFIG_UNAVAILABLE")
                return
            }
            let key = try readKey(root: root, id: args[2])
            guard let key, !key.isEmpty else { print("DIAGNOSTIC_KEY_NOT_FOUND"); return }
            guard !key.unicodeScalars.contains(where: { $0.value == 10 || $0.value == 13 }) else {
                print("DIAGNOSTIC_STORED_KEY_HAS_LINE_BREAK"); return
            }
            print("DIAGNOSTIC_KEY_LOADED (value withheld)")
            if args[3] == "models" {
                let session = URLSession(configuration: .ephemeral, delegate: NoRedirect(), delegateQueue: nil)
                defer { session.invalidateAndCancel() }
                var request = URLRequest(url: url.appendingPathComponent("models"))
                request.timeoutInterval = 20
                request.setValue("Bearer " + key, forHTTPHeaderField: "Authorization")
                request.setValue("application/json", forHTTPHeaderField: "Accept")
                let (bytes, response) = try await session.bytes(for: request)
                guard let http = response as? HTTPURLResponse else { print("DIAGNOSTIC_INVALID_HTTP"); return }
                if !(200..<300).contains(http.statusCode) {
                    let mime = http.mimeType?.lowercased() ?? ""
                    let data = mime == "application/json" || mime.hasSuffix("+json")
                        ? await NativeProviderHTTPDiagnostic.readBody(bytes, expectedLength: http.expectedContentLength, session: session) : nil
                    print("DIAGNOSTIC_MODELS " + NativeProviderHTTPDiagnostic.description(status: http.statusCode, data: data))
                    return
                }
                var data = Data()
                for try await byte in bytes {
                    guard data.count < 2 * 1024 * 1024 else { print("DIAGNOSTIC_CATALOG_TOO_LARGE"); return }
                    data.append(byte)
                }
                let json = try JSONSerialization.jsonObject(with: data)
                let object = json as? [String: Any]
                let list = json as? [Any] ?? object?["data"] as? [Any] ?? object?["models"] as? [Any] ?? []
                print("DIAGNOSTIC_MODELS_OK count=\(list.count)")
            } else if args[3] == "probe" || args[3] == "responses" {
                let format: NativeProviderFormat = args[3] == "responses" || provider["npm"] as? String == "@ai-sdk/openai" ? .responses : .chatCompletions
                let request = NativeModelRequest(baseURL: url, apiKey: key, format: format, model: model,
                    system: "You are a coding assistant. This is a one-time developer connectivity test.",
                    messages: [NativeModelMessage(role: "user", content: "Reply with OK to confirm the coding application's provider connection.")],
                    tools: [], maxOutputTokens: 512)
                var textBytes = 0
                var finished = false
                for try await event in URLSessionNativeModelClient().stream(request: request) {
                    switch event {
                    case .text(let delta): textBytes += delta.utf8.count
                    case .finished: finished = true
                    default: break
                    }
                }
                print("DIAGNOSTIC_PROBE format=\(format.rawValue) finished=\(finished) textBytes=\(textBytes)")
            } else { print("DIAGNOSTIC_INVALID_MODE") }
        } catch let error as NativeModelClientError {
            print("DIAGNOSTIC_ERROR " + error.localizedDescription)
        } catch {
            let ns = error as NSError
            // Do not print NSError descriptions; they can embed the URL or request.
            print("DIAGNOSTIC_ERROR domain=\(ns.domain) code=\(ns.code) details withheld")
        }
    }

    private static func readKey(root: URL, id: String) throws -> String? {
        var accounts = [id, root.standardizedFileURL.path + ":" + id]
        let marker = root.appendingPathComponent("native-agent/legacy-keychain-root.txt")
        if let data = try? Data(contentsOf: marker), data.count <= 4096,
           let oldRoot = String(data: data, encoding: .utf8), oldRoot.hasPrefix("/"),
           !data.contains(where: { $0 == 0 || $0 == 10 || $0 == 13 }) {
            accounts.append(oldRoot + ":" + id)
        }
        for account in accounts {
            let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: "kumo.native.provider", kSecAttrAccount as String: account,
                kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
            var result: CFTypeRef?
            let status = SecItemCopyMatching(query as CFDictionary, &result)
            if status == errSecItemNotFound { continue }
            guard status == errSecSuccess, let data = result as? Data else {
                throw NSError(domain: "Keychain", code: Int(status))
            }
            return String(data: data, encoding: .utf8)
        }
        return nil
    }
}

private final class NoRedirect: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
