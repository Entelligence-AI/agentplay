import AgentPlayCore
import AppKit
import SwiftUI
import UserNotifications

/// The AgentPlay mark: four map dots around one moment. Drawn as a template image so macOS tints it.
enum MenuBarMark {
    static func image(pulse: Bool, size: CGFloat = 18, alert: Bool = false) -> NSImage {
        let img = NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
            let c = NSPoint(x: rect.midX, y: rect.midY)
            let spread = size * (pulse ? 0.34 : 0.30)
            let dot = size * 0.16
            for k in 0..<4 {
                let a = Double(k) * .pi / 2 + .pi / 4
                let p = NSPoint(x: c.x + cos(a) * spread, y: c.y + sin(a) * spread)
                NSBezierPath(ovalIn: NSRect(x: p.x - dot / 2, y: p.y - dot / 2, width: dot, height: dot)).fill()
            }
            let center = size * (alert ? 0.34 : 0.26)
            NSBezierPath(ovalIn: NSRect(x: c.x - center / 2, y: c.y - center / 2, width: center, height: center)).fill()
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = "AgentPlay"
        return img
    }
}

/// The status item: two thin template bars (session on top, weekly below) for the most constrained provider,
/// like CodexBar. States are shapes, since template images carry no colour: dimmed when stale, a dot when a
/// session needs you. Falls back to the AgentPlay mark when no provider reports limits.
enum StatusIcon {
    static func image(session: Double?, weekly: Double?, stale: Bool, waiting: Bool, pulse: Bool) -> NSImage {
        guard session != nil || weekly != nil else {
            let mark = MenuBarMark.image(pulse: pulse, size: 18, alert: waiting)
            return mark
        }
        let size = NSSize(width: 20, height: 16)
        let img = NSImage(size: size, flipped: false) { _ in
            let alpha: CGFloat = stale ? 0.5 : 1
            func bar(_ y: CGFloat, _ fraction: Double?) {
                let track = NSRect(x: 1, y: y, width: 14, height: 3)
                NSColor.black.withAlphaComponent(0.3 * alpha).setFill()
                NSBezierPath(roundedRect: track, xRadius: 1.5, yRadius: 1.5).fill()
                guard let f = fraction else { return }
                let w = max(1.5, 14 * CGFloat(min(1, max(0, f))))
                NSColor.black.withAlphaComponent(alpha).setFill()
                NSBezierPath(roundedRect: NSRect(x: 1, y: y, width: w, height: 3), xRadius: 1.5, yRadius: 1.5).fill()
            }
            bar(9, session)
            bar(4, weekly)
            if waiting {
                NSColor.black.setFill()
                NSBezierPath(ovalIn: NSRect(x: 16, y: 11, width: 4, height: 4)).fill()
            }
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = "AgentPlay usage"
        return img
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    var model: CompanionModel?
    private var status: StatusItemController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        Notifier.register(delegate: self)
        MainActor.assumeIsolated {
            let m = CompanionModel(isDevBuild: LaunchOptions.isDev)
            model = m
            Windows.model = m
            status = StatusItemController(model: m)
            m.start(fixture: LaunchOptions.fixture)
            if !m.prefs.onboardingDone, LaunchOptions.fixture == nil { Windows.show(.welcome) }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        // Stops only the viewer and watchers this app started.
        MainActor.assumeIsolated { model?.shutdown() }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let info = response.notification.request.content.userInfo
        let sid = info["sessionId"] as? String
        let seq = info["seq"] as? Int
        let key = info["patternKey"] as? String
        let pid = (info["pid"] as? Int).map { Int32(truncatingIfNeeded: $0) }
        let action = response.actionIdentifier
        await MainActor.run { model?.handleNotificationTap(sessionId: sid, seq: seq, action: action, patternKey: key, pid: pid) }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async
        -> UNNotificationPresentationOptions { [.banner, .list] }
}

/// Settings and the welcome window, opened from AppKit (the menu, onboarding, notifications).
@MainActor
enum Windows {
    enum Kind { case settings, welcome }
    static var model: CompanionModel?
    private static var open: [Kind: NSWindow] = [:]

    static func show(_ kind: Kind) {
        guard let model else { return }
        NSApp.activate()
        if let w = open[kind] {
            if kind == .settings { SettingsWindow.select(model.settingsPane, in: w) }
            w.makeKeyAndOrderFront(nil)
            return
        }
        let w: NSWindow
        switch kind {
        case .settings:
            model.refreshBridgeState()
            w = SettingsWindow.make(model: model)
        case .welcome:
            w = NSWindow(contentViewController: NSHostingController(rootView: AnyView(OnboardingView().environmentObject(model))))
            w.title = "Welcome to AgentPlay"
            w.styleMask = [.titled, .closable, .fullSizeContentView]
            w.titleVisibility = .hidden
            w.titlebarAppearsTransparent = true
        }
        w.isReleasedWhenClosed = false
        w.center()
        open[kind] = w
        w.makeKeyAndOrderFront(nil)
    }

    static func close(_ kind: Kind) { open[kind]?.close() }
}

enum LaunchOptions {
    static let args = ProcessInfo.processInfo.arguments
    static func value(_ flag: String) -> String? {
        guard let i = args.firstIndex(of: flag), i + 1 < args.count else { return nil }
        return args[i + 1]
    }
    #if COMPANION_DEV
    static let isDev = true
    #else
    static let isDev = false
    #endif
    /// `--fixture two-active` (dev builds only): run on a synthetic snapshot, labelled Sample data.
    static var fixture: String? { isDev ? value("--fixture") : nil }
    static var renderDir: String? { isDev ? value("--render") : nil }
}

@main
struct CompanionApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    init() {
        if let dir = LaunchOptions.isDev ? LaunchOptions.value("--render-icon") : nil {
            MainActor.assumeIsolated { Renderer.renderIcon(to: dir) }
            exit(0)
        }
        if LaunchOptions.isDev && LaunchOptions.args.contains("--selftest") {
            let sem = DispatchSemaphore(value: 0)
            var code: Int32 = 1
            Task.detached { code = await SelfTest.run(); sem.signal() }
            sem.wait()
            exit(code)
        }
        if let dir = LaunchOptions.isDev ? LaunchOptions.value("--render-live") : nil {
            let sem = DispatchSemaphore(value: 0)
            Task.detached { await Renderer.renderLive(to: dir); sem.signal() }
            while sem.wait(timeout: .now()) == .timedOut { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
            exit(0)
        }
        if let dir = LaunchOptions.renderDir {
            // Dev only: draw every state to PNG and exit (docs/companion/screens).
            MainActor.assumeIsolated { Renderer.renderAll(to: dir) }
            exit(0)
        }
    }

    // The status item and windows are AppKit (AppDelegate); SwiftUI needs one scene.
    var body: some Scene {
        Settings { EmptyView() }
            .commands {
                // Without this, ⌘, in our own windows opens SwiftUI's empty Settings scene.
                CommandGroup(replacing: .appSettings) {
                    Button("Settings…") { Windows.show(.settings) }.keyboardShortcut(",")
                }
            }
    }
}
