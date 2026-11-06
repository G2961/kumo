// MARK: - SwiftUI app entry point (template)
//
// This file is NOT part of the SPM package (KumoCore / KumoApp are libraries). Copy it into
// your local Xcode iOS-application target (the one that links KumoCore + KumoApp packages)
// and it becomes the `@main` entry point. It resolves the bundled darwin/arm64 `opencode`
// binary and boots the UI.
//
// Steps:
// 1. Create a new iOS App project ("Kumo", interface: SwiftUI, minimum iOS 16).
// 2. Link the KumoCore and KumoApp SPM packages.
// 3. Drag the darwin/arm64 `opencode` binary into the app target so it is copied to the
//    bundle (uncheck "Include only for app extension"); expose its path below.
// 4. Copy this file into the app target.
// 5. Add an ATS exception for local networking in Info.plist (see README-iOS.md).

import SwiftUI
import KumoCore
import KumoApp

@main
struct KumoMain: App {
    var body: some Scene {
        WindowGroup {
            ContentView(executablePath: KumoMain.binaryPath())
        }
    }

    /// Resolves the bundled opencode binary from the main bundle's resources.
    static func binaryPath() -> String {
        // Adjust `opencode` to the actual name of the copied file (e.g. "opencode").
        if let p = Bundle.main.path(forResource: "opencode", ofType: nil) {
            return p
        }
        // Fallback: application-support copy path for manual staging.
        let docs = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return docs.appendingPathComponent("opencode").path
    }
}