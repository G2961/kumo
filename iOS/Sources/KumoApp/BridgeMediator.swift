import Foundation
import UIKit

/// UI-facing callbacks the web UI can request from the native app. Implemented by the
/// SwiftUI hosting layer and injected into the bridge so JS actions (toast, share,
/// photo picker) run on the main thread through proper UI components.
public protocol UIMediator: AnyObject {
    func showToast(_ message: String)
    func shareText(_ text: String, title: String)
    func pickPhotos(completion: @escaping ([[String: Any]]) -> Void)
    func openFolder(completion: @escaping (URL?) -> Void)
}

/// Reusable UIKit helpers so `UIMediator` implementations stay thin.
public enum UIKitBridge {
    public static func toast(_ message: String) {
        DispatchQueue.main.async {
            let scenes = UIApplication.shared.connectedScenes
            guard let scene = scenes.compactMap({ $0 as? UIWindowScene }).first,
                  let root = scene.windows.first?.rootViewController else { return }
            var visible = root
            while let presented = visible.presentedViewController { visible = presented }
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default))
            visible.present(alert, animated: true)
        }
    }

    public static func share(_ text: String, title: String) {
        DispatchQueue.main.async {
            let scenes = UIApplication.shared.connectedScenes
            guard let scene = scenes.compactMap({ $0 as? UIWindowScene }).first,
                  let root = scene.windows.first?.rootViewController else { return }
            var visible = root
            while let presented = visible.presentedViewController { visible = presented }
            let items: [Any] = [text]
            let ac = UIActivityViewController(activityItems: items, applicationActivities: nil)
            ac.popoverPresentationController?.sourceView = visible.view
            visible.present(ac, animated: true)
        }
    }
}