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

struct MenuBarLabel: View {
    @ObservedObject var model: CompanionModel
    var onFirstAppear: () -> Void = {}
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        HStack(spacing: 4) {
            Image(nsImage: MenuBarMark.image(pulse: model.heartbeat && !reduceMotion,
                                             alert: model.menuBarText?.contains("look") == true))
            if let t = model.menuBarText { Text(t) }
        }
        .accessibilityLabel(model.menuBarText.map { "AgentPlay, \($0)" } ?? "AgentPlay")
        .onAppear {
            WelcomeOpener.action = openWindow
            onFirstAppear()
        }
    }
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
    static var direction: Direction? { isDev ? value("--direction").flatMap(Direction.init(rawValue:)) : nil }
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
            PopoverView()
                .environmentObject(model)
                .onAppear(perform: bootstrap)
                .onAppear {
                    // Option-click on the mark opens the full viewer (the footer button is the discoverable route).
                    if NSEvent.modifierFlags.contains(.option) {
                        model.openViewer()
                        NSApp.keyWindow?.close()
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
        if let d = LaunchOptions.direction { model.direction = d }
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
