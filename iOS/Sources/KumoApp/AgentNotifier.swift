import Foundation
import UserNotifications

/// Local background notifications when an agent turn finishes while the app is in the
/// background, mirroring `MainActivity.turnDone` on iOS via `UNUserNotificationCenter`.
public final class AgentNotifier {
    public static let shared = AgentNotifier()

    public struct TurnSummary {
        public var title: String
        public var summary: String
        public var failed: Bool
    }

    private var authorized = false
    private let delegateDisamb = NotificationDelegateAdapter()

    private init() {}

    public func requestAuthorizationIfNeeded() {
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            guard settings.authorizationStatus == .notDetermined else { return }
            center.requestAuthorization(options: [.alert, .badge, .sound]) { granted, _ in
                self.authorized = granted
            }
        }
    }

    /// Posts a notification when a turn ends and the app is backgrounded.
    public func notifyTurnDone(_ summary: TurnSummary) {
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                return
            }
            let content = UNMutableNotificationContent()
            content.title = (summary.failed ? "Kumo: turn failed" : "Kumo finished") +
                (summary.title.isEmpty ? "" : " · \(summary.title)")
            content.body = summary.summary.isEmpty
                ? (summary.failed ? "The turn completed with an error" : "The turn finished")
                : summary.summary
            content.sound = .default

            let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 0.5, repeats: false)
            let id = "kumo-turn-\(UUID().uuidString)"
            let request = UNNotificationRequest(identifier: id, content: content, trigger: trigger)
            center.add(request, withCompletionHandler: nil)
            // Auto-clear after a short window like the Android 12s timeout.
            DispatchQueue.main.asyncAfter(deadline: .now() + 12) {
                center.removeDeliveredNotifications(withIdentifiers: [id])
            }
        }
    }
}

/// Keeps `UNUserNotificationCenterDelegate` alive; on iOS you assign it in the scene/app.
public final class NotificationDelegateAdapter: NSObject, UNUserNotificationCenterDelegate {
    public func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }
}