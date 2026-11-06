import Foundation

public enum NativeFileBrowserError: LocalizedError {
    case invalidPath(path: String)
    case notDirectory(path: String)
    case notFile(path: String)
    case fileTooLarge(path: String)
    case binaryFile(path: String)
    case invalidUTF8(path: String)
    case fileSystem(path: String, operation: String, underlying: Error)

    public var errorDescription: String? {
        switch self {
        case .invalidPath(let path):
            return "Expected an absolute native path without NUL bytes: \(path)"
        case .notDirectory(let path):
            return "Not a directory: \(path)"
        case .notFile(let path):
            return "Cannot read a directory as a file: \(path)"
        case .fileTooLarge(let path):
            return "File exceeds the 512 KiB preview limit: \(path)"
        case .binaryFile(let path):
            return "File contains binary NUL bytes and cannot be previewed as text: \(path)"
        case .invalidUTF8(let path):
            return "File is not valid UTF-8: \(path)"
        case .fileSystem(let path, let operation, let underlying):
            return "Cannot \(operation) \(path): \(underlying.localizedDescription)"
        }
    }
}

/// Uses native paths and any security-scoped access already retained by ProjectManager.
public enum NativeFileBrowser {
    private static let byteLimit = 512 * 1024
    private static let chunkBytes = 64 * 1024

    public static func list(path: String) throws -> [String: Any] {
        let url = try nativeURL(path: path)
        return try withFileSystemError(path: url.path, operation: "list directory") {
            guard try attributes(at: url)[.type] as? FileAttributeType == .typeDirectory else {
                throw NativeFileBrowserError.notDirectory(path: url.path)
            }
            let entries = try FileManager.default.contentsOfDirectory(at: url, includingPropertiesForKeys: nil)
            var dirs: [String] = []
            var files: [[String: Any]] = []
            for entry in entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
                let metadata = try withFileSystemError(path: entry.path, operation: "inspect entry") {
                    try attributes(at: entry)
                }
                if metadata[.type] as? FileAttributeType == .typeDirectory {
                    dirs.append(entry.lastPathComponent)
                } else {
                    files.append(["name": entry.lastPathComponent, "size": try byteSize(metadata, path: entry.path)])
                }
            }
            return ["path": url.path, "dirs": dirs, "files": files]
        }
    }

    public static func read(path: String) throws -> [String: Any] {
        let url = try nativeURL(path: path)
        return try withFileSystemError(path: url.path, operation: "read file") {
            let metadata = try attributes(at: url)
            guard metadata[.type] as? FileAttributeType != .typeDirectory else {
                throw NativeFileBrowserError.notFile(path: url.path)
            }
            guard try byteSize(metadata, path: url.path) <= byteLimit else {
                throw NativeFileBrowserError.fileTooLarge(path: url.path)
            }
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            // Check the opened file too: the path may have been replaced since the stat.
            guard try handle.seekToEnd() <= UInt64(byteLimit) else {
                throw NativeFileBrowserError.fileTooLarge(path: url.path)
            }
            try handle.seek(toOffset: 0)
            let data = try boundedData(from: handle, path: url.path)
            guard !data.contains(0) else {
                throw NativeFileBrowserError.binaryFile(path: url.path)
            }
            guard let content = String(data: data, encoding: .utf8) else {
                throw NativeFileBrowserError.invalidUTF8(path: url.path)
            }
            return ["content": content, "size": data.count]
        }
    }

    private static func nativeURL(path: String) throws -> URL {
        guard path.hasPrefix("/"), !path.utf8.contains(0) else {
            throw NativeFileBrowserError.invalidPath(path: path)
        }
        return URL(fileURLWithPath: path).standardizedFileURL
    }

    private static func attributes(at url: URL) throws -> [FileAttributeKey: Any] {
        // Follow links for type/size, but keep the caller's native path in the payload.
        try FileManager.default.attributesOfItem(atPath: url.resolvingSymlinksInPath().path)
    }

    private static func byteSize(_ attributes: [FileAttributeKey: Any], path: String) throws -> Int {
        guard let number = attributes[.size] as? NSNumber,
              let size = Int(exactly: number.int64Value), size >= 0 else {
            throw NativeFileBrowserError.fileSystem(
                path: path, operation: "inspect file size", underlying: CocoaError(.fileReadUnknown)
            )
        }
        return size
    }

    private static func boundedData(from handle: FileHandle, path: String) throws -> Data {
        var data = Data()
        while true {
            // One extra byte detects growth beyond the limit without an unbounded read.
            let count = min(chunkBytes, byteLimit - data.count + 1)
            guard let chunk = try handle.read(upToCount: count), !chunk.isEmpty else { return data }
            guard chunk.count <= byteLimit - data.count else {
                throw NativeFileBrowserError.fileTooLarge(path: path)
            }
            data.append(chunk)
        }
    }

    private static func withFileSystemError<T>(path: String, operation: String, perform: () throws -> T) throws -> T {
        do {
            return try perform()
        } catch let error as NativeFileBrowserError {
            throw error
        } catch {
            throw NativeFileBrowserError.fileSystem(path: path, operation: operation, underlying: error)
        }
    }
}
