import AgentPlayCore
import AppKit
import SwiftUI

/// Dev builds only (`--render <dir>`): draws the menu, onboarding and Settings from the synthetic fixtures into
/// PNGs, light and dark, through a real NSHostingView on the menu material, so native controls render as they ship.
@MainActor
enum Renderer {
    static let fixedNow = Date(timeIntervalSince1970: 1_792_752_360)  // 2026-10-23 10:46 UTC, the fixtures' clock

    static func fixtureModel(_ name: String) -> CompanionModel {
        let m = CompanionModel(prefsStore: PrefsStore(defaults: UserDefaults(suiteName: "render.\(UUID().uuidString)")!),
                               isDevBuild: true)
        // The env override, else the copies a dev build carries in Resources/Fixtures.
        let dir = ProcessInfo.processInfo.environment["AGENTPLAY_COMPANION_FIXTURES"].map { URL(fileURLWithPath: $0) }
            ?? Bundle.main.resourceURL?.appendingPathComponent("Fixtures")
        guard let dir, let data = try? Data(contentsOf: dir.appendingPathComponent("\(name).json")),
              let s = try? SnapshotDecoder.decode(data) else {
            fatalError("render: fixture \(name) not found or undecodable")
        }
        m.loadForRender(s)
        return m
    }

    static func renderAll(to path: String) {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        let dir = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        // The real NSMenu, opened from a real status item and captured while it tracks.
        func menu(_ file: String, _ m: CompanionModel) {
            for dark in [false, true] {
                captureMenu(m, now: fixedNow, to: dir.appendingPathComponent("\(file)-\(dark ? "dark" : "light").png"), dark: dark)
            }
        }
        menu("menu", fixtureModel("two-active"))
        menu("state-no-agents", fixtureModel("no-agents"))
        menu("state-detected-idle", fixtureModel("detected-idle"))
        menu("state-stale-quota", fixtureModel("stale-quota"))
        menu("state-conflicting-plans", fixtureModel("conflicting-plans"))
        menu("state-partial", fixtureModel("partial-unavailable"))
        menu("state-many", fixtureModel("many-sessions"))
        let scanning = fixtureModel("two-active"); scanning.setPhaseForRender(.scanning(found: 412))
        menu("state-scanning", scanning)
        let failed = fixtureModel("no-agents"); failed.setPhaseForRender(.failed(.coreTooOld("agentplay 0.2.3")))
        menu("state-error", failed)

        var shots: [(String, AnyView)] = []
        if let u = fixtureModel("two-active").snapshot?.usage {
            shots.append(("submenu-usage-details", AnyView(UsageDetail(report: u))))
        }
        for step in 1...5 {
            shots.append(("onboarding-\(step)", AnyView(OnboardingView(step: step).environmentObject(fixtureModel("two-active")))))
        }
        for (name, view) in shots {
            let isMenu = name.hasPrefix("submenu")
            for dark in [false, true] {
                write(view, to: dir.appendingPathComponent("\(name)-\(dark ? "dark" : "light").png"), dark: dark, material: isMenu)
            }
        }
        // Settings as the real window: toolbar tabs and all.
        for pane in [SettingsView.Pane.general, .claude, .notifications, .sources] {
            let m = fixtureModel("two-active")
            m.settingsPane = pane
            for dark in [false, true] {
                NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
                let w = SettingsWindow.make(model: m)
                w.setFrameOrigin(NSPoint(x: -10000, y: -10000))
                w.orderFrontRegardless()
                RunLoop.current.run(until: Date().addingTimeInterval(0.5))
                if let v = w.contentView?.superview, let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) {
                    v.cacheDisplay(in: v.bounds, to: rep)
                    let name = "settings-\(pane.rawValue.lowercased().replacingOccurrences(of: " ", with: "-"))-\(dark ? "dark" : "light").png"
                    try? rep.representation(using: .png, properties: [:])?.write(to: dir.appendingPathComponent(name))
                }
                w.orderOut(nil)
            }
        }
        NSApp.appearance = nil
        for (name, img) in [
            ("statusitem-bars", StatusIcon.image(session: 0.34, weekly: 0.58, stale: false, waiting: false, pulse: false)),
            ("statusitem-waiting", StatusIcon.image(session: 0.34, weekly: 0.58, stale: false, waiting: true, pulse: false)),
            ("statusitem-stale", StatusIcon.image(session: 0.34, weekly: 0.58, stale: true, waiting: false, pulse: false)),
            ("statusitem-mark", StatusIcon.image(session: nil, weekly: nil, stale: false, waiting: false, pulse: false)),
        ] {
            for dark in [false, true] {
                writeIcon(img, to: dir.appendingPathComponent("\(name)-\(dark ? "dark" : "light").png"), dark: dark)
            }
        }
        print("rendered \(shots.count * 2 + 8) images to \(dir.path)")
    }

    /// Opens the menu from a throwaway status item, captures its window mid-tracking, then closes it.
    static func captureMenu(_ m: CompanionModel, now: Date, to url: URL, dark: Bool) {
        let controller = StatusItemController(model: m)
        controller.nowOverride = now
        NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        controller.menu.appearance = NSApp.appearance
        controller.rebuild(now: now)
        if ProcessInfo.processInfo.environment["APC_DUMP_MENU"] != nil {
            for i in controller.menu.items {
                print("item", i.isSeparatorItem ? "---" : i.title, i.isAlternate ? "ALT" : "", i.view.map { "view h=\($0.frame.height)" } ?? "")
            }
        }
        let timer = Timer(timeInterval: 0.6, repeats: false) { _ in
            MainActor.assumeIsolated {
                if let w = NSApp.windows.first(where: { $0.isVisible && $0.className.contains("Menu") && !$0.className.contains("StatusBar") }),
                   let v = w.contentView?.superview ?? w.contentView,
                   let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) {
                    v.cacheDisplay(in: v.bounds, to: rep)
                    try? rep.representation(using: .png, properties: [:])?.write(to: url)
                }
                controller.menu.cancelTracking()
            }
        }
        RunLoop.main.add(timer, forMode: .eventTracking)
        if let b = controller.statusItem.button {
            controller.menu.popUp(positioning: nil, at: NSPoint(x: 0, y: -4), in: b)
        }
        NSStatusBar.system.removeStatusItem(controller.statusItem)
    }

    static func write(_ view: AnyView, to url: URL, dark: Bool, material: Bool) {
        let appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        let hosting = NSHostingView(rootView: view)
        hosting.appearance = appearance
        let size = hosting.fittingSize
        let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: size.width, height: size.height),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = appearance
        let container: NSView
        if material {
            // What the MenuBarExtra window draws behind its content: the menu material.
            let fx = NSVisualEffectView(frame: NSRect(origin: .zero, size: size))
            fx.material = .menu
            fx.blendingMode = .withinWindow
            fx.state = .active
            hosting.frame = fx.bounds
            fx.addSubview(hosting)
            container = fx
        } else {
            hosting.frame = NSRect(origin: .zero, size: size)
            container = hosting
        }
        window.contentView = container
        window.setContentSize(size)
        window.orderFrontRegardless()
        RunLoop.current.run(until: Date().addingTimeInterval(0.35))
        container.layoutSubtreeIfNeeded()
        guard let rep = container.bitmapImageRepForCachingDisplay(in: container.bounds) else { return }
        container.cacheDisplay(in: container.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: url)
        window.orderOut(nil)
    }

    /// The icon on a menu-bar coloured strip, tinted the way the system tints template images.
    static func writeIcon(_ img: NSImage, to url: URL, dark: Bool) {
        let scale: CGFloat = 4
        let size = NSSize(width: 44 * scale, height: 24 * scale)
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width), pixelsHigh: Int(size.height),
                                   bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                   colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        (dark ? NSColor(white: 0.16, alpha: 1) : NSColor(white: 0.93, alpha: 1)).setFill()
        NSRect(origin: .zero, size: size).fill()
        let ink = dark ? NSColor.white : NSColor.black
        let r = NSRect(x: (size.width - img.size.width * scale) / 2, y: (size.height - img.size.height * scale) / 2,
                       width: img.size.width * scale, height: img.size.height * scale)
        let tinted = NSImage(size: img.size, flipped: false) { rect in
            img.draw(in: rect)
            ink.set()
            rect.fill(using: .sourceAtop)
            return true
        }
        tinted.draw(in: r)
        NSGraphicsContext.restoreGraphicsState()
        try? rep.representation(using: .png, properties: [:])?.write(to: url)
    }

    /// The app icon: the mark on a pale-lime squircle, as an .iconset for iconutil.
    static func renderIcon(to path: String) {
        _ = NSApplication.shared
        let dir = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        for (pt, scale) in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)] {
            let px = CGFloat(pt * scale)
            let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(px), pixelsHigh: Int(px), bitsPerSample: 8,
                                       samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                       bytesPerRow: 0, bitsPerPixel: 0)!
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            let inset = px * 0.1
            let tile = NSRect(x: inset, y: inset, width: px - 2 * inset, height: px - 2 * inset)
            NSColor(srgbRed: 0xE7 / 255, green: 0xF2 / 255, blue: 0xD4 / 255, alpha: 1).setFill()
            NSBezierPath(roundedRect: tile, xRadius: tile.width * 0.225, yRadius: tile.width * 0.225).fill()
            NSColor(srgbRed: 0x1A / 255, green: 0x1A / 255, blue: 0x1A / 255, alpha: 1).setFill()
            let c = NSPoint(x: px / 2, y: px / 2), spread = tile.width * 0.24, dot = tile.width * 0.13
            for k in 0..<4 {
                let a = Double(k) * .pi / 2 + .pi / 4
                let p = NSPoint(x: c.x + cos(a) * spread, y: c.y + sin(a) * spread)
                NSBezierPath(ovalIn: NSRect(x: p.x - dot / 2, y: p.y - dot / 2, width: dot, height: dot)).fill()
            }
            NSColor(srgbRed: 0xA2 / 255, green: 0xE4 / 255, blue: 0x35 / 255, alpha: 1).setFill()
            let center = tile.width * 0.2
            NSBezierPath(ovalIn: NSRect(x: c.x - center / 2, y: c.y - center / 2, width: center, height: center)).fill()
            NSGraphicsContext.restoreGraphicsState()
            let name = scale == 1 ? "icon_\(pt)x\(pt).png" : "icon_\(pt)x\(pt)@2x.png"
            try? rep.representation(using: .png, properties: [:])?.write(to: dir.appendingPathComponent(name))
        }
    }
}

extension CompanionModel {
    /// Render and test hook: show a fixture as-is (times rebased to the renderer's clock), labelled Sample.
    func loadForRender(_ s: Snapshot) {
        installSampleForRender(SnapshotDecoder.rebased(s, to: Renderer.fixedNow))
    }
}
