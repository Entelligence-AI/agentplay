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

struct MenuBarLabel: View {
    @ObservedObject var model: CompanionModel
    var onFirstAppear: () -> Void = {}
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let bars = model.statusBars
        HStack(spacing: 4) {
            Image(nsImage: StatusIcon.image(session: bars.session, weekly: bars.weekly, stale: bars.stale,
                                            waiting: model.hasWaiting, pulse: model.heartbeat && !reduceMotion))
            if model.prefs.showActivityInMenuBar, let pct = bars.session {
                Text(Format.percent((pct * 100).rounded())).monospacedDigit()
            }
        }
        .accessibilityLabel(accessibility(bars))
        .onAppear {
            WelcomeOpener.action = openWindow
            MenuBarControl.install(model: model, openWindow: openWindow)
            onFirstAppear()
        }
    }

    func accessibility(_ b: (session: Double?, weekly: Double?, stale: Bool)) -> String {
        var parts = ["AgentPlay"]
        if let s = b.session { parts.append("session \(Int(s * 100)) percent used") }
        if let w = b.weekly { parts.append("weekly \(Int(w * 100)) percent used") }
        if model.hasWaiting { parts.append("a session needs you") }
        return parts.joined(separator: ", ")
    }
}

/// The underlying status item, for what MenuBarExtra doesn't expose: closing the window after an action
/// and a native right-click menu.
@MainActor
enum MenuBarControl {
    private static var monitor: Any?
    private static weak var model: CompanionModel?
    private static var openWindow: OpenWindowAction?

    static var statusButton: NSStatusBarButton? {
        for w in NSApp.windows where w.className.contains("NSStatusBarWindow") {
            if let b = find(in: w.contentView) { return b }
        }
        return nil
    }

    private static func find(in view: NSView?) -> NSStatusBarButton? {
        guard let view else { return nil }
        if let b = view as? NSStatusBarButton { return b }
        for v in view.subviews { if let b = find(in: v) { return b } }
        return nil
    }

    static var menuWindow: NSWindow? {
        NSApp.windows.first { $0.isVisible && $0.className.contains("MenuBarExtraWindow") }
    }

    /// Close the menu the way a click on the status item would, so MenuBarExtra keeps its state right.
    static func close() {
        guard menuWindow != nil else { return }
        if let b = statusButton { b.performClick(nil) } else { menuWindow?.orderOut(nil) }
    }

    static func install(model m: CompanionModel, openWindow o: OpenWindowAction) {
        model = m
        openWindow = o
        guard monitor == nil else { return }
        monitor = NSEvent.addLocalMonitorForEvents(matching: [.rightMouseDown]) { event in
            guard let w = event.window, w.className.contains("NSStatusBarWindow") else { return event }
            showContextMenu(at: event)
            return nil
        }
    }

    private static func showContextMenu(at event: NSEvent) {
        let menu = NSMenu()
        func item(_ title: String, _ key: String, _ action: @escaping () -> Void) {
            let i = ClosureMenuItem(title: title, keyEquivalent: key, action: action)
            menu.addItem(i)
        }
        item("Refresh", "r") { model?.scheduleRefresh(reason: .manual) }
        item("Open AgentPlay", "o") { model?.openViewer() }
        menu.addItem(.separator())
        item("Settings…", ",") { NSApp.activate(ignoringOtherApps: true); openWindow?(id: "settings") }
        item("Quit AgentPlay", "q") { NSApp.terminate(nil) }
        if let b = statusButton { menu.popUp(positioning: nil, at: NSPoint(x: 0, y: b.bounds.height + 4), in: b) }
    }
}

final class ClosureMenuItem: NSMenuItem {
    private let closure: () -> Void
    init(title: String, keyEquivalent: String, action: @escaping () -> Void) {
        closure = action
        super.init(title: title, action: #selector(run), keyEquivalent: keyEquivalent)
        target = self
    }
    required init(coder: NSCoder) { fatalError("unused") }
    @objc private func run() { closure() }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    var model: CompanionModel?

    func applicationDidFinishLaunching(_ notification: Notification) {
        Notifier.register(delegate: self)
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
        let action = response.actionIdentifier
        await MainActor.run { model?.handleNotificationTap(sessionId: sid, seq: seq, action: action, patternKey: key) }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async
        -> UNNotificationPresentationOptions { [.banner, .list] }
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
    @StateObject private var model: CompanionModel

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
        let m = MainActor.assumeIsolated { CompanionModel(isDevBuild: LaunchOptions.isDev) }
        _model = StateObject(wrappedValue: m)
    }

    var body: some Scene {
        MenuBarExtra {
            MenuView()
                .environmentObject(model)
                .onAppear(perform: bootstrap)
                .onAppear {
                    // Option-click on the mark opens the full viewer (the footer button is the discoverable route).
                    if NSEvent.modifierFlags.contains(.option) {
                        model.openViewer()
                        MenuBarControl.close()
                    }
                }
        } label: {
            MenuBarLabel(model: model, onFirstAppear: bootstrap)
        }
        .menuBarExtraStyle(.window)

        Window("AgentPlay Settings", id: "settings") {
            SettingsView().environmentObject(model)
        }
        .windowResizability(.contentSize)
        .defaultPosition(.center)

        Window("Welcome to AgentPlay", id: "welcome") {
            OnboardingView().environmentObject(model)
        }
        .windowResizability(.contentSize)
        .windowStyle(.hiddenTitleBar)
        .defaultPosition(.center)
    }

    private func bootstrap() {
        delegate.model = model
        model.start(fixture: LaunchOptions.fixture)
        if !model.prefs.onboardingDone, LaunchOptions.fixture == nil {
            DispatchQueue.main.async { WelcomeOpener.open() }
        }
    }
}

/// Opens the welcome window once, from outside a view (first launch has no popover open yet).
enum WelcomeOpener {
    @MainActor static var opened = false
    @MainActor static var action: OpenWindowAction?
    @MainActor static func open() {
        guard !opened else { return }
        opened = true
        NSApp.activate(ignoringOtherApps: true)
        action?(id: "welcome")
    }
}
