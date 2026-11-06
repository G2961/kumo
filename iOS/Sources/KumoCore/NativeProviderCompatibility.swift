import Foundation

public enum NativeProviderCompatibility {
    private static let userAgent = "codex_cli_rs/0.46.0 (Ubuntu 22.4.0; x86_64) xterm-256color"
    private static let originator = "codex_cli_rs"
    private static let version = "0.46.0"
    // One identity per app process preserves continuity across catalog fetches and model turns.
    private static let sessionID = UUID().uuidString

    public static func apply(to request: inout URLRequest) {
        guard let url = request.url,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == "https",
              components.host?.lowercased() == "agentrouter.org",
              components.user == nil, components.password == nil,
              components.port == nil || components.port == 443 else { return }

        request.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue(originator, forHTTPHeaderField: "originator")
        request.setValue(version, forHTTPHeaderField: "version")
        request.setValue(sessionID, forHTTPHeaderField: "session_id")
    }
}
