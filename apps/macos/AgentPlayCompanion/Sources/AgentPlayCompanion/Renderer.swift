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
        NSApp.setActivationPolicy(.prohibited)
        let dir = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        var shots: [(String, AnyView)] = []
        func menu(_ file: String, _ m: CompanionModel, highlight: String? = nil) {
            shots.append((file, AnyView(MenuView(now: fixedNow, highlighted: highlight).environmentObject(m))))
        }
        menu("menu", fixtureModel("two-active"))
        menu("menu-hover-session", fixtureModel("two-active"), highlight: "session:local:claude-code:sample-9e2b")
        let usage = fixtureModel("two-active"); usage.usageExpanded = true; usage.setExpanded("claude-code", false)
        menu("menu-usage-expanded", usage)
        let both = fixtureModel("two-active"); both.setExpanded("codex", true)
        menu("menu-both-providers", both)
        menu("state-no-agents", fixtureModel("no-agents"))
        menu("state-detected-idle", fixtureModel("detected-idle"))
        let stale = fixtureModel("stale-quota"); stale.setExpanded("codex", true)
        menu("state-stale-quota", stale)
        let conflict = fixtureModel("conflicting-plans"); conflict.setExpanded("codex", true)
        menu("state-conflicting-plans", conflict)
        menu("state-partial", fixtureModel("partial-unavailable"))
        menu("state-many", fixtureModel("many-sessions"))
        let scanning = fixtureModel("two-active"); scanning.setPhaseForRender(.scanning(found: 412))
        menu("state-scanning", scanning)
        let failed = fixtureModel("no-agents"); failed.setPhaseForRender(.failed(.coreTooOld("agentplay 0.2.3")))
        menu("state-error", failed)

        for step in 1...5 {
            shots.append(("onboarding-\(step)", AnyView(OnboardingView(step: step).environmentObject(fixtureModel("two-active")))))
        }
        for pane in [SettingsView.Pane.claude, .notifications, .privacy, .sources] {
            let m = fixtureModel("two-active")
            shots.append(("settings-\(pane.rawValue.lowercased().replacingOccurrences(of: " ", with: "-"))",
                          AnyView(SettingsView(initialPane: pane).environmentObject(m))))
        }

        for (name, view) in shots {
            let isMenu = name.hasPrefix("menu") || name.hasPrefix("state")
            for dark in [false, true] {
                write(view, to: dir.appendingPathComponent("\(name)-\(dark ? "dark" : "light").png"), dark: dark, material: isMenu)
            }
        }
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
