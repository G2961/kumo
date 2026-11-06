import Foundation
import UIKit
import PhotosUI
import UniformTypeIdentifiers

/// Concrete `UIMediator` for the SwiftUI host: toast, share, photo picker via
/// `PHPickerViewController`, and an in-app folder importer.
public final class DefaultMediator: NSObject, UIMediator {
    private var photoCompletion: (([[String: Any]]) -> Void)?
    private var folderCompletion: ((URL?) -> Void)?

    public override init() {}

    public func showToast(_ message: String) {
        UIKitBridge.toast(message)
    }

    public func shareText(_ text: String, title: String) {
        UIKitBridge.share(text, title: title)
    }

    public func pickPhotos(completion: @escaping ([[String: Any]]) -> Void) {
        photoCompletion = completion
        var config = PHPickerConfiguration()
        config.filter = .images
        config.selectionLimit = 8
        let picker = PHPickerViewController(configuration: config)
        picker.delegate = self
        presentModal(picker)
    }

    public func openFolder(completion: @escaping (URL?) -> Void) {
        folderCompletion = completion
        let docPicker = UIDocumentPickerViewController(forOpeningContentTypes: [.folder])
        docPicker.delegate = self
        presentModal(docPicker)
    }

    private func presentModal(_ vc: UIViewController) {
        DispatchQueue.main.async {
            let scenes = UIApplication.shared.connectedScenes
            guard let scene = scenes.compactMap({ $0 as? UIWindowScene }).first,
                  let root = scene.windows.first?.rootViewController else { return }
            var visible = root
            while let presented = visible.presentedViewController { visible = presented }
            visible.present(vc, animated: true)
        }
    }
}

extension DefaultMediator: PHPickerViewControllerDelegate {
    public func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult]) {
        picker.dismiss(animated: true)
        var files: [[String: Any]] = []
        let group = DispatchGroup()
        for item in results {
            group.enter()
            let provider = item.itemProvider
            if provider.hasItemConformingToTypeIdentifier(UTType.image.identifier) {
                provider.loadDataRepresentation(forTypeIdentifier: UTType.image.identifier) { data, error in
                    defer { group.leave() }
                    guard let data, error == nil else { return }
                    let mime = "image/jpeg"
                    let b64 = data.base64EncodedString()
                    files.append([
                        "type": "file", "mime": mime,
                        "filename": "photo-\(UUID().uuidString.prefix(6)).jpg",
                        "url": "data:\(mime);base64,\(b64)", "size": data.count
                    ])
                }
            } else {
                group.leave()
            }
        }
        group.notify(queue: .main) { [weak self] in
            self?.photoCompletion?(files)
            self?.photoCompletion = nil
        }
    }
}

extension DefaultMediator: UIDocumentPickerDelegate {
    public func documentPicker(_ controller: UIDocumentPickerViewController,
                               didPickDocumentsAt urls: [URL]) {
        controller.dismiss(animated: true)
        folderCompletion?(urls.first)
        folderCompletion = nil
    }

    public func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
        controller.dismiss(animated: true)
        folderCompletion?(nil)
        folderCompletion = nil
    }
}