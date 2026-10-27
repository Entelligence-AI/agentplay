import AgentPlayCore
import AppKit
import CoreServices
import Foundation
import UserNotifications

/// FSEvents on the agents' transcript folders. Coalesced by the OS (2 s latency); the model debounces again.
final class FileWatcher: @unchecked Sendable {
    private var stream: FSEventStreamRef?
    private let paths: [String]
    private let onChange: @Sendable () -> Void
    private let queue = DispatchQueue(label: "ai.entelligence.agentplay.companion.fsevents", qos: .utility)

    private let latency: CFTimeInterval

    init(paths: [String], latency: CFTimeInterval = 2.0, onChange: @escaping @Sendable () -> Void) {
        self.paths = paths
        self.latency = latency
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
                                          FSEventStreamEventId(kFSEventStreamEventIdSinceNow), latency,
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
            intentIdentifiers: []),
            UNNotificationCategory(
                identifier: finishedCategory,
                actions: [UNNotificationAction(identifier: goAction, title: "Go to Terminal", options: []),
                          UNNotificationAction(identifier: replayAction, title: "Open Replay", options: [])],
                intentIdentifiers: []),
            UNNotificationCategory(
                identifier: needsYouCategory,
                actions: [UNNotificationAction(identifier: goAction, title: "Go to Terminal", options: []),
                          UNNotificationAction(identifier: snoozeAction, title: "Snooze 15 Minutes", options: []),
                          UNNotificationAction(identifier: muteSessionAction, title: "Mute This Session", options: [])],
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
        let pose: PipPose = switch a.preset { case .allowanceLow: .sweat; case .repeatedFailure, .unresolvedEnd: .oops; case .finished: .cheer; default: .wave }
        if let att = pipAttachment(pose) { content.attachments = [att] }
        // Ids and seqs only: enough to open the moment, nothing about what happened in it.
        var info: [String: Any] = ["patternKey": a.patternKey]
        if let s = a.sessionId { info["sessionId"] = s }
        if let q = a.seq { info["seq"] = q }
        content.userInfo = info
        let req = UNNotificationRequest(identifier: a.id, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }

    /// One notification on request, so people can see what one looks like before any alert is on.
    /// Pip, in the pose that matches the alert, as the notification's picture: the face people know from the bar.
    static func pipAttachment(_ pose: PipPose) -> UNNotificationAttachment? {
        let scale: CGFloat = 12
        let img = PipIcon.image(pose, session: 0.45, weekly: 0.6)
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(20 * scale), pixelsHigh: Int(18 * scale), bitsPerSample: 8,
                                   samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        let ctx = NSGraphicsContext(bitmapImageRep: rep)!
        NSGraphicsContext.current = ctx
        ctx.imageInterpolation = .none
        let dark = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        img.draw(in: NSRect(x: 0, y: 0, width: 20 * scale, height: 18 * scale), from: .zero, operation: .sourceOver, fraction: 1)
        (dark ? NSColor.white : NSColor.black).setFill()
        NSRect(x: 0, y: 0, width: 20 * scale, height: 18 * scale).fill(using: .sourceIn)
        NSGraphicsContext.restoreGraphicsState()
        guard let png = rep.representation(using: .png, properties: [:]) else { return nil }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("pip-\(pose.rawValue)-\(dark ? "dark" : "light").png")
        try? png.write(to: url)
        return try? UNNotificationAttachment(identifier: "pip", url: url, options: [UNNotificationAttachmentOptionsThumbnailClippingRectKey: CGRect(x: 0, y: 0, width: 1, height: 1).dictionaryRepresentation])
    }

    /// A sample of one alert, sent when the person asks to see it during onboarding or in Settings.
    static func postSample(title: String, body: String, pose: PipPose = .wave) {
        guard available else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        if let a = pipAttachment(pose) { content.attachments = [a] }
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "sample-\(UUID().uuidString)", content: content, trigger: nil))
    }

    static func postTest() {
        guard available else { return }
        let content = UNMutableNotificationContent()
        content.title = "AgentPlay Companion"
        content.body = "This is how I'll reach you: a session waiting on you, or a plan window nearly spent."
        content.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "test|\(Int(Date().timeIntervalSince1970))",
                                                                     content: content, trigger: nil))
    }

    static let needsYouThread = "needsYou"
    static let needsYouCategory = "agentplay.needsYou"
    static let muteSessionAction = "agentplay.muteSession"
    static let goAction = "agentplay.go"
    static let replayAction = "agentplay.replay"
    static let finishedCategory = "agentplay.finished"
    static let snoozeAction = "agentplay.snooze15"

    /// The agent, and the project only if the user allows repo names; nothing from the conversation.
    static func postNeedsYou(_ l: LiveSession, includeRepo: Bool) {
        guard available else { return }
        let note = Redacted.needsYou(l, includeRepo: includeRepo)
        let content = UNMutableNotificationContent()
        content.title = note.title
        content.body = note.body
        content.threadIdentifier = needsYouThread
        content.categoryIdentifier = needsYouCategory
        content.sound = .default
        if let att = pipAttachment(.wave) { content.attachments = [att] }
        var info: [String: Any] = ["sessionId": l.sessionId, "kind": "needsYou"]
        if let pid = l.pid { info["pid"] = Int(pid) }
        content.userInfo = info
        let id = "needs|\(l.sessionId)|\(Int(l.statusSince?.timeIntervalSince1970 ?? Date().timeIntervalSince1970))"
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id, content: content, trigger: nil))
    }

    static func postFinished(_ note: Redacted.Note, session l: LiveSession, seq: Int?) {
        guard available else { return }
        let content = UNMutableNotificationContent()
        content.title = note.title
        content.body = note.body
        content.threadIdentifier = "finished"
        content.categoryIdentifier = finishedCategory
        var info: [String: Any] = ["sessionId": l.sessionId, "kind": "finished"]
        if let pid = l.pid { info["pid"] = Int(pid) }
        if let seq { info["seq"] = seq }
        content.userInfo = info
        let id = "done|\(l.sessionId)|\(Int(Date().timeIntervalSince1970))"
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id, content: content, trigger: nil))
    }

    static func clearNeedsYou(sessionId: String) {
        guard available else { return }
        let c = UNUserNotificationCenter.current()
        c.getDeliveredNotifications { delivered in
            let ids = delivered.map(\.request).filter {
                $0.content.threadIdentifier == needsYouThread && ($0.content.userInfo["sessionId"] as? String) == sessionId
            }.map(\.identifier)
            if !ids.isEmpty { c.removeDeliveredNotifications(withIdentifiers: ids) }
        }
    }

    static func clearDelivered() {
        guard available else { return }
        UNUserNotificationCenter.current().removeAllDeliveredNotifications()
    }
}
