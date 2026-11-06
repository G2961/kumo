import Foundation

/// Wraps project storage in a sandboxed-iOS-friendly way, mirroring `ProjectManager.kt`.
/// On iOS there is no global external storage; projects live inside the app sandbox under
/// `<Documents>/Kumo/projects`, with the user's own files selected via security-scoped bookmarks.
public final class ProjectManager {
    public struct ProjectInfo {
        public let path: String
        public let name: String

        public init(path: String, name: String) {
            self.path = path
            self.name = name
        }
    }

    private static let prefsKeyCurrent = "pm.current"
    private static let prefsKeyDirectoryList = "pm.directories"
    private static let defaults = UserDefaults.standard

    // MARK: - Root folders

    public static func applicationSupport() -> URL {
        let url = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    public static func documents() -> URL {
        let url = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    public static func homeDir() -> URL {
        let url = applicationSupport().appendingPathComponent("home", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    public static func projectsRoot() -> URL {
        let url = documents().appendingPathComponent("Kumo/projects", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    // MARK: - Current directory

    public static func currentDir() -> URL {
        let saved = defaults.string(forKey: prefsKeyCurrent)
        if let saved {
            let u = URL(fileURLWithPath: saved)
            if isDirectory(u) { return u }
        }
        let def = projectsRoot().appendingPathComponent("default", isDirectory: true)
        try? FileManager.default.createDirectory(at: def, withIntermediateDirectories: true)
        defaults.set(def.path, forKey: prefsKeyCurrent)
        return def
    }

    public static func setCurrent(_ dir: URL) {
        remember(dir)
        defaults.set(dir.path, forKey: prefsKeyCurrent)
    }

    public static func remember(_ dir: URL) {
        var list = storedDirectories()
        if !list.contains(dir.path) {
            list.append(dir.path)
        }
        defaults.set(list, forKey: prefsKeyDirectoryList)
    }

    private static func storedDirectories() -> [String] {
        defaults.stringArray(forKey: prefsKeyDirectoryList) ?? []
    }

    public static func list() -> [ProjectInfo] {
        var paths = storedDirectories()
        paths.append(currentDir().path)
        // Recover any sub-folders from the projects root that were created before bookkeeping.
        if let entries = try? FileManager.default.contentsOfDirectory(
            at: projectsRoot(),
            includingPropertiesForKeys: nil
        ) {
            for e in entries {
                var isDir: ObjCBool = false
                if FileManager.default.fileExists(atPath: e.path, isDirectory: &isDir), isDir.boolValue,
                   !e.lastPathComponent.hasPrefix(".") {
                    paths.append(e.path)
                }
            }
        }
        let distinct = Array(Set(paths)).sorted()
        let valid = distinct.filter { isDirectory(URL(fileURLWithPath: $0)) }
        defaults.set(valid, forKey: prefsKeyDirectoryList)
        return valid.map {
            ProjectInfo(path: $0, name: URL(fileURLWithPath: $0).lastPathComponent)
        }
    }

    public static func createNew(name: String) -> URL {
        let safe = sanitize(name) ?? "project"
        let target = uniqueDirectory(base: projectsRoot(), name: safe)
        try? FileManager.default.createDirectory(at: target, withIntermediateDirectories: true)
        setCurrent(target)
        return target
    }

    @discardableResult
    public static func copyDirectory(from src: URL, to dest: URL) -> Bool {
        do {
            try FileManager.default.copyItem(at: src, to: dest)
            return true
        } catch {
            return false
        }
    }

    // MARK: - Helpers

    static func sanitize(_ name: String) -> String? {
        let cleaned = name.trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "[^A-Za-z0-9. _-]", with: "", options: .regularExpression)
        return cleaned.isEmpty ? nil : cleaned
    }

    public static func uniqueDirectory(base: URL, name: String) -> URL {
        var candidate = base.appendingPathComponent(name, isDirectory: true)
        var i = 1
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = base.appendingPathComponent("\(name)-\(i)", isDirectory: true)
            i += 1
        }
        return candidate
    }

    public static func isDirectory(_ url: URL) -> Bool {
        var isDir: ObjCBool = false
        return FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir) && isDir.boolValue
    }

    /// Write-probe, like the Android `canWrite` which is unreliable on FUSE paths.
    public static func canWrite(_ url: URL) -> Bool {
        let probe = url.appendingPathComponent(".kumo-write-test-\(UUID().uuidString)")
        do {
            try Data().write(to: probe)
            try? FileManager.default.removeItem(at: probe)
            return true
        } catch {
            return false
        }
    }
}