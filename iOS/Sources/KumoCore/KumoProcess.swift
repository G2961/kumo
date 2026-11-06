import Foundation

/// Foundation.Process is unavailable in the iOS SDK. On a jailbroken device fork/exec
/// works fine, so this wraps posix_spawn with pipes and waitpid to mimic the API used
/// across KumoCore (executableURL, arguments, currentDirectoryURL, Pipe handles,
/// terminationHandler, waitUntilExit, terminationStatus, terminate, isRunning).
public final class KumoProcess {
    public var executableURL: URL?
    public var arguments: [String] = []
    public var currentDirectoryURL: URL?
    public var environment: [String: String]?
    public var standardInput = Pipe()
    public var standardOutput = Pipe()
    public var standardError = Pipe()

    public private(set) var terminationStatus: Int32 = -1
    public private(set) var processIdentifier: pid_t = 0

    /// Fired once, on a background queue, after the process exits (like Foundation.Process).
    public var terminationHandler: ((KumoProcess) -> Void)?

    private var waited = false
    private let lock = NSLock()

    public init() {}

    public func run() throws {
        guard let originalPath = executableURL?.path else {
            throw NSError(domain: "KumoProcess", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "no executable set"])
        }

        // posix_spawn_file_actions_addchdir_np is macOS-only, so when a working directory
        // is set the child is spawned through sh: cd into it, then exec the real binary.
        var execPath = originalPath
        var args = arguments
        var env = environment ?? ProcessInfo.processInfo.environment
        if let dir = currentDirectoryURL?.path {
            env["KUMO_CWD"] = dir
            env["KUMO_BIN"] = originalPath
            args = ["-c", "cd \"$KUMO_CWD\" && exec \"$KUMO_BIN\" \"$@\"", originalPath] + args
            execPath = "/bin/sh"
        }

        // stdout/stderr pipes: child gets the write ends, we keep the read ends.
        let inReadFD = dup(standardInput.fileHandleForReading.fileDescriptor)
        let outWriteFD = dup(standardOutput.fileHandleForWriting.fileDescriptor)
        let errWriteFD = dup(standardError.fileHandleForWriting.fileDescriptor)
        defer { close(inReadFD); close(outWriteFD); close(errWriteFD) }

        let fa = UnsafeMutablePointer<posix_spawn_file_actions_t?>.allocate(capacity: 1)
        posix_spawn_file_actions_init(fa)
        defer { posix_spawn_file_actions_destroy(fa); fa.deallocate() }
        posix_spawn_file_actions_adddup2(fa, inReadFD, STDIN_FILENO)
        posix_spawn_file_actions_adddup2(fa, outWriteFD, STDOUT_FILENO)
        posix_spawn_file_actions_adddup2(fa, errWriteFD, STDERR_FILENO)
        // Close the parent-side read ends in the child.
        posix_spawn_file_actions_addclose(fa, standardInput.fileHandleForWriting.fileDescriptor)
        posix_spawn_file_actions_addclose(fa, standardOutput.fileHandleForReading.fileDescriptor)
        posix_spawn_file_actions_addclose(fa, standardError.fileHandleForReading.fileDescriptor)

        var argv = [execPath] + args
        var argvPtr: [UnsafeMutablePointer<CChar>?] = argv.map { strdup($0)! }
        argvPtr.append(nil)

        var envp: [UnsafeMutablePointer<CChar>?] = []
        for (k, v) in env {
            envp.append(strdup("\(k)=\(v)"))
        }
        envp.append(nil)

        let sa = UnsafeMutablePointer<posix_spawnattr_t?>.allocate(capacity: 1)
        posix_spawnattr_init(sa)
        defer { posix_spawnattr_destroy(sa); sa.deallocate() }

        var pid: pid_t = 0
        let rc = posix_spawn(&pid, execPath, fa, sa, &argvPtr, &envp)
        argvPtr.forEach { free($0) }
        envp.forEach { free($0) }

        guard rc == 0 else {
            throw NSError(domain: "KumoProcess", code: Int(rc),
                          userInfo: [NSLocalizedDescriptionKey: "posix_spawn failed: \(rc)"])
        }
        processIdentifier = pid

        // Reap asynchronously so terminationHandler fires like Foundation.Process does.
        DispatchQueue.global().async { [weak self] in
            guard let self else { return }
            self.waitUntilExit()
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
            applyExit(status)
            let strong = self
            DispatchQueue.global().async { strong.terminationHandler?(strong) }
            return false
        }
        return true
    }

    public func waitUntilExit() {
        lock.lock()
        if !waited { reapLocked() }
        lock.unlock()
    }

    private func reapLocked() {
        guard processIdentifier != 0, !waited else { return }
        var status: Int32 = 0
        while waitpid(processIdentifier, &status, 0) < 0 && errno == EINTR {}
        waited = true
        applyExit(status)
        terminationHandler?(self)
    }

    private func applyExit(_ status: Int32) {
        // Darwin wait macros are not exposed to Swift; do the bit math inline.
        if (status & 0x7f) == 0 {
            terminationStatus = (status >> 8) & 0xff
        } else if ((status & 0x7f) + 1) >> 1 > 0 {
            terminationStatus = 128 + (status & 0x7f)
        }
    }
}
