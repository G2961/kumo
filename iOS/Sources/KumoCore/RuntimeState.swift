import Foundation

/// Lifecycle mirror of the Android `LocalRuntime.State`.
public enum RuntimeState: Equatable {
    case preparing
    case starting
    case ready
    case stopped
    case failed
}

public protocol RuntimeDelegate: AnyObject {
    func runtime(_ runtime: LocalRuntime, didChange state: RuntimeState, message: String)
    func runtime(_ runtime: LocalRuntime, didLog line: String)
    func runtime(_ runtime: LocalRuntime, didBecomeReady port: Int)
}