import Foundation

/// Foundation.Process is unavailable in the iOS SDK. On a jailbroken device fork/exec
/// works fine, so this wraps posix_spawn with pipes and waitpid to mimic the API used
/// across KumoCore (executableURL, arguments, currentDirectoryURL, Pipe handles,
/// terminationHandler, waitUntilExit, terminationStatus).
public final class KumoProcess {
    public var executableURL: URL?
    public var arguments: [String] = []
    public var currentDirectoryURL: URL?
    public var environment: [String: String]?
    public let standardInput = Pipe()
    public let standardOutput = Pipe()
    public let standardError = Pipe()

    public private(set) var terminationStatus: Int32 = -1
    public private(set) var processIdentifier: pid_t = 0

    /// Fired once, on a background queue, after the process exits (like Foundation.Process).
    public var terminationHandler: ((KumoProcess) -> Void)?

    private var waited = false
    private let lock = NSLock()

    public init() {}

    public func run() throws {
        guard let execPath = executableURL?.path else {
            throw NSError(domain: "KumoProcess", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "no executable set"])
        }

        // stdout/stderr pipes: child gets the write ends, we keep the read ends.
        let inReadFD = dup(standardInput.fileHandleForReading.fileDescriptor)
        let outWriteFD = dup(standardOutput.fileHandleForWriting.fileDescriptor)
        let errWriteFD = dup(standardError.fileHandleForWriting.fileDescriptor)
        defer { close(inReadFD) }
        defer { close(outWriteFD); close(errWriteFD) }

        var fileActions = posix_spawn_file_actions_t()
        posix_spawn_file_actions_init(&fileActions)
        posix_spawn_file_actions_adddup2(&fileActions, inReadFD, STDIN_FILENO)
        posix_spawn_file_actions_adddup2(&fileActions, outWriteFD, STDOUT_FILENO)
        posix_spawn_file_actions_adddup2(&fileActions, errWriteFD, STDERR_FILENO)
        // Close the parent-side read ends in the child.
        posix_spawn_file_actions_addclose(&fileActions, standardOutput.fileHandleForReading.fileDescriptor)
        if let dir = currentDirectoryURL?.path {
            posix_spawn_file_actions_addchdir_np(&fileActions, dir)
        }

        var argv = [execPath] + arguments
        var argvPtr: [UnsafeMutablePointer<CChar>?] = argv.map { strdup($0)! }
        argvPtr.append(nil)

        var envp: [UnsafeMutablePointer<CChar>?] = []
        for (k, v) in environment ?? ProcessInfo.processInfo.environment {
            envp.append(strdup("\(k)=\(v)"))
        }
        envp.append(nil)

        var attrs = posix_spawnattr_t()
        posix_spawnattr_init(&attrs)

        var pid: pid_t = 0
        let rc = posix_spawn(&pid, execPath, &fileActions, &attrs, &argvPtr, &envp)
        posix_spawnattr_destroy(&attrs)
        posix_spawn_file_actions_destroy(&fileActions)
        argvPtr.forEach { free($0) }
        envp.forEach { free($0) }

        guard rc == 0 else {
            throw NSError(domain: "KumoProcess", code: Int(rc),
                          userInfo: [NSLocalizedDescriptionKey: "posix_spawn failed: \(rc)"])
        }
        processIdentifier = pid

        // Reap asynchronously so terminationHandler fires like Foundation.Process does.
        DispatchQueue.global().async { [weak self] in
            self?.reap()
        }
    }

    public func terminate() {
        guard processIdentifier != 0, !waited else { return }
        kill(processIdentifier, SIGTERM)
    }

    public var isRunning: Bool {
        lock.lock()
        defer { lock.unlock() }
        guard processIdentifier != 0, !waited else { return false }
        var status: Int32 = 0
        let r = waitpid(processIdentifier, &status, WNOHANG)
        if r == processIdentifier {
            waited = true
            if WIFEXITED(status) { terminationStatus = WEXITSTATUS(status) }
            else if WIFSIGNALED(status) { terminationStatus = 128 + WTERMSIG(status) }
            DispatchQueue.global().async { [weak self] in self?.terminationHandler?(self) }
            return false
        }
        return true
    }

    public func waitUntilExit() {
        lock.lock()
        if !waited { reap() }
        lock.unlock()
    }

    private func reap() {
        lock.lock()
        defer { lock.unlock() }
        guard processIdentifier != 0, !waited else { return }
        var status: Int32 = 0
        while waitpid(processIdentifier, &status, 0) < 0 && errno == EINTR {}
        waited = true
        if WIFEXITED(status) {
            terminationStatus = WEXITSTATUS(status)
        } else if WIFSIGNALED(status) {
            terminationStatus = 128 + WTERMSIG(status)
        }
        terminationHandler?(self)
    }
}
