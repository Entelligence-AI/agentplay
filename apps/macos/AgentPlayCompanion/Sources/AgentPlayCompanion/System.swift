import AgentPlayCore
import CoreServices
import Foundation
import UserNotifications

/// FSEvents on the agents' transcript folders. Coalesced by the OS (2 s latency); the model debounces again.
final class FileWatcher: @unchecked Sendable {
    private var stream: FSEventStreamRef?
    private let paths: [String]
    private let onChange: @Sendable () -> Void
    private let queue = DispatchQueue(label: "ai.entelligence.agentplay.companion.fsevents", qos: .utility)

    init(paths: [String], onChange: @escaping @Sendable () -> Void) {
        self.paths = paths
        self.onChange = onChange
    }

    func start() {
        guard stream == nil, !paths.isEmpty else { return }
        var ctx = FSEventStreamContext(version: 0, info: Unmanaged.passUnretained(self).toOpaque(),
                                       retain: nil, release: nil, copyDescription: nil)
        let cb: FSEventStreamCallback = { _, info, _, _, _, _ in
            guard let info else { return }
            Unmanaged<FileWatcher>.fromOpaque(info).takeUnretainedValue().onChange()
        }
        guard let s = FSEventStreamCreate(nil, cb, &ctx, paths as CFArray,
                                          FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 2.0,
                                          FSEventStreamCreateFlags(kFSEventStreamCreateFlagNoDefer)) else { return }
        FSEventStreamSetDispatchQueue(s, queue)
        FSEventStreamStart(s)
        stream = s
    }

    func stop() {
        guard let s = stream else { return }
        FSEventStreamStop(s)
        FSEventStreamInvalidate(s)
        FSEventStreamRelease(s)
        stream = nil
    }

    deinit { stop() }
}

/// Local notifications only: UNUserNotificationCenter, no server, no push, no tracking.
enum Notifier {
    static let category = "agentplay.alert"
    static let openAction = "agentplay.open"
    static let muteAction = "agentplay.mute"

    /// UNUserNotificationCenter needs a real app bundle; a bare `swift run` binary has none.
    static var available: Bool { Bundle.main.bundleIdentifier != nil && Bundle.main.bundleURL.pathExtension == "app" }

    static func register(delegate: UNUserNotificationCenterDelegate) {
        guard available else { return }
        let c = UNUserNotificationCenter.current()
        c.delegate = delegate
        c.setNotificationCategories([UNNotificationCategory(
            identifier: category,
            actions: [UNNotificationAction(identifier: openAction, title: "See sequence", options: [.foreground]),
                      UNNotificationAction(identifier: muteAction, title: "Mute this pattern", options: [])],
            intentIdentifiers: [])])
    }

    static func status() async -> UNAuthorizationStatus {
        guard available else { return .denied }
        return await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
    }

    static func requestAuthorization() async -> Bool {
        guard available else { return false }
        return (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])) ?? false
    }

    static func post(_ a: PendingAlert) {
        guard available else { return }
        let content = UNMutableNotificationContent()
        content.title = a.note.title
        content.body = a.note.body
        content.categoryIdentifier = category
        content.threadIdentifier = a.preset.rawValue
        // Ids and seqs only: enough to open the moment, nothing about what happened in it.
        var info: [String: Any] = ["patternKey": a.patternKey]
        if let s = a.sessionId { info["sessionId"] = s }
        if let q = a.seq { info["seq"] = q }
        content.userInfo = info
        let req = UNNotificationRequest(identifier: a.id, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }

    static func clearDelivered() {
        guard available else { return }
        UNUserNotificationCenter.current().removeAllDeliveredNotifications()
    }
}
