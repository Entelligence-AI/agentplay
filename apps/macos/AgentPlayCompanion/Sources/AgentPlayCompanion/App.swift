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
        MainActor.assumeIsolated { NSApp.mainMenu = Self.mainMenu() }
        MainActor.assumeIsolated {
            let m = CompanionModel(isDevBuild: LaunchOptions.isDev)
            model = m
            Windows.model = m
            status = StatusItemController(model: m)
            StatusItemController.current = status
            m.start(fixture: LaunchOptions.fixture)
            Updater.shared.start()
            if !m.prefs.onboardingDone, LaunchOptions.fixture == nil { Windows.show(.welcome) }
        }
    }

    /// Opening the app again while it runs (Finder, Spotlight, `open`) shows nothing new: it lives in the menu bar.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { false }

    /// Hidden for a menu-bar app, but its key equivalents make ⌘, ⌘W, ⌘Q and copy and paste work in our windows.
    @MainActor static func mainMenu() -> NSMenu {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let host = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let m = NSMenu(title: title)
            items.forEach(m.addItem)
            host.submenu = m
            main.addItem(host)
        }
        func std(_ title: String, _ sel: Selector, _ key: String, _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: title, action: sel, keyEquivalent: key)
            i.keyEquivalentModifierMask = mods
            return i
        }
        sub("AgentPlay", [ClosureMenuItem(title: "Settings…", keyEquivalent: ",") { Windows.show(.settings) },
                          .separator(), std("Quit AgentPlay", #selector(NSApplication.terminate(_:)), "q")])
        sub("Edit", [std("Undo", Selector(("undo:")), "z"), std("Redo", Selector(("redo:")), "z", [.command, .shift]), .separator(),
                     std("Cut", #selector(NSText.cut(_:)), "x"), std("Copy", #selector(NSText.copy(_:)), "c"),
                     std("Paste", #selector(NSText.paste(_:)), "v"), std("Select All", #selector(NSText.selectAll(_:)), "a")])
        sub("Window", [std("Close", #selector(NSWindow.performClose(_:)), "w"),
                       std("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m")])
        return main
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
    enum Kind { case settings, welcome, insights }
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
        case .insights:
            w = InsightsWindow.make(model: model)
        case .welcome:
            w = NSWindow(contentViewController: NSHostingController(rootView: AnyView(OnboardingView().environmentObject(model).tint(Brand.accent))))
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

/// A plain AppKit app: the status item, Settings and the welcome window are all AppKit, so there is no SwiftUI
/// scene to open by itself (a placeholder Settings scene showed up as an empty window on relaunch).
@main
enum CompanionMain {
    static func main() {
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
        if LaunchOptions.isDev, let name = LaunchOptions.value("--switch-check") {
            let confirm = LaunchOptions.args.contains("confirm")
            let sem = DispatchSemaphore(value: 0)
            Task.detached { await Renderer.switchCheck(name: name, confirm: confirm); sem.signal() }
            while sem.wait(timeout: .now()) == .timedOut { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
            exit(0)
        }
        if LaunchOptions.isDev, LaunchOptions.args.contains("--menu-check") {
            setvbuf(stdout, nil, _IOLBF, 0)   // each line as it comes, even into a pipe
            let fire = LaunchOptions.args.contains("fire")
            let sem = DispatchSemaphore(value: 0)
            Task.detached { if #available(macOS 14.4, *) { await Renderer.menuCheck(fire: fire) }; sem.signal() }
            while sem.wait(timeout: .now()) == .timedOut { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
            exit(0)
        }
        if let dir = LaunchOptions.isDev ? LaunchOptions.value("--banner-check") : nil {
            MainActor.assumeIsolated { Renderer.bannerCheck(to: dir) }
            exit(0)
        }
        if LaunchOptions.isDev, LaunchOptions.args.contains("--notify-check") {
            // Dev only: ask macOS for notification permission and print exactly what it answers.
            let sem = DispatchSemaphore(value: 0)
            Task.detached {
                let c = UNUserNotificationCenter.current()
                do {
                    let ok = try await c.requestAuthorization(options: [.alert, .sound])
                    print("notify-check: granted=\(ok)")
                } catch {
                    print("notify-check: error \((error as NSError).domain) \((error as NSError).code): \(error.localizedDescription)")
                }
                let st = await c.notificationSettings()
                print("notify-check: status=\(st.authorizationStatus.rawValue) alert=\(st.alertSetting.rawValue)")
                if LaunchOptions.args.contains("--send") {
                    let content = UNMutableNotificationContent()
                    content.title = "Claude Code needs you"
                    content.body = "docs-site is waiting for your answer."
                    content.sound = .default
                    do {
                        try await c.add(UNNotificationRequest(identifier: "check-\(UUID().uuidString)", content: content, trigger: nil))
                        print("notify-check: posted")
                    } catch { print("notify-check: post failed \(error.localizedDescription)") }
                }
                sem.signal()
            }
            while sem.wait(timeout: .now()) == .timedOut { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
            exit(0)
        }
        if let dir = LaunchOptions.isDev ? LaunchOptions.value("--export-vectors") : nil {
            MainActor.assumeIsolated { Renderer.exportVectors(to: dir) }
            exit(0)
        }
        if let dir = LaunchOptions.isDev ? LaunchOptions.value("--contrast-check") : nil {
            let sem = DispatchSemaphore(value: 0)
            Task.detached { await Renderer.contrastCheck(to: dir); sem.signal() }
            while sem.wait(timeout: .now()) == .timedOut { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
            exit(0)
        }
        if let dir = LaunchOptions.isDev ? LaunchOptions.value("--render-live") : nil {
            let sem = DispatchSemaphore(value: 0)
            Task.detached { await Renderer.renderLive(to: dir); sem.signal() }
            while sem.wait(timeout: .now()) == .timedOut { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
            exit(0)
        }
        if LaunchOptions.isDev, let dir = LaunchOptions.value("--film-render") {
            // Dev only: draw every piece a launch film needs, offscreen, as PNG sequences with alpha (see FilmRender.swift).
            let sem = DispatchSemaphore(value: 0)
            Task { @MainActor in await FilmRender.run(to: dir); sem.signal() }
            while sem.wait(timeout: .now()) == .timedOut { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
            exit(0)
        }
        if LaunchOptions.isDev, let dir = LaunchOptions.value("--film-out") ?? (LaunchOptions.value("--film") != nil ? "/tmp/agentplay-film" : nil) {
            // Dev only: film a scripted take over a fixture (see Film.swift).
            MainActor.assumeIsolated {
                Film.outDir = URL(fileURLWithPath: dir)
                let app = NSApplication.shared
                let delegate = FilmDelegate()
                app.delegate = delegate
                withExtendedLifetime(delegate) { app.run() }
            }
            exit(0)
        }
        if let dir = LaunchOptions.renderDir {
            // Dev only: draw every state to PNG and exit (docs/companion/screens).
            MainActor.assumeIsolated { Renderer.renderAll(to: dir) }
            exit(0)
        }
        MainActor.assumeIsolated {
            let app = NSApplication.shared
            let delegate = AppDelegate()
            app.delegate = delegate
            withExtendedLifetime(delegate) { app.run() }
        }
    }
}
