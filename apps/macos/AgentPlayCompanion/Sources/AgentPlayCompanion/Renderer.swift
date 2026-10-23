import AgentPlayCore
import AppKit
import SwiftUI

/// Dev builds only (`--render <dir>`): draws each prototype and state from the synthetic fixtures into PNGs,
/// light and dark, through a real NSHostingView in an offscreen window so native controls render as they ship.
@MainActor
enum Renderer {
    static let fixedNow = Date(timeIntervalSince1970: 1_792_752_360)  // 2026-10-23 10:46 UTC, the fixtures' clock

    static func renderAll(to path: String) {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let dir = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        guard let fixtures = ProcessInfo.processInfo.environment["AGENTPLAY_COMPANION_FIXTURES"] else {
            FileHandle.standardError.write(Data("set AGENTPLAY_COMPANION_FIXTURES\n".utf8)); return
        }

        func model(_ name: String, tab: Tab = .now, direction: Direction = .v1, expanded: Bool = false,
                   showAll: Bool = false, constellation: Bool = false) -> CompanionModel {
            let m = CompanionModel(prefsStore: PrefsStore(defaults: UserDefaults(suiteName: "render.\(UUID().uuidString)")!),
                                   isDevBuild: true)
            if let data = try? Data(contentsOf: URL(fileURLWithPath: fixtures).appendingPathComponent("\(name).json")),
               let s = try? SnapshotDecoder.decode(data) {
                m.loadForRender(s)
            }
            m.tab = tab
            m.direction = direction
            m.showAll = showAll
            m.showConstellation = constellation
            if expanded { m.expandedRun = m.orderedRuns.first?.sessionId }
            return m
        }

        var shots: [(String, CompanionModel, AnyView)] = []
        func popover(_ file: String, _ m: CompanionModel) {
            shots.append((file, m, AnyView(PopoverView(now: fixedNow).environmentObject(m))))
        }

        popover("v1-now", model("two-active"))
        popover("v1-now-expanded", model("two-active", expanded: true))
        popover("v1-limits", model("two-active", tab: .limits))
        popover("v1-learn", model("two-active", tab: .learn))
        popover("v1-constellation", model("two-active", constellation: true))
        popover("state-no-agents", model("no-agents"))
        popover("state-detected-idle", model("detected-idle"))
        popover("state-partial", model("partial-unavailable"))
        popover("state-partial-limits", model("partial-unavailable", tab: .limits))
        popover("state-stale-quota", model("stale-quota", tab: .limits))
        popover("state-conflicting-plans", model("conflicting-plans", tab: .limits))
        popover("state-many-view-all", model("many-sessions", showAll: true))
        let scanning = model("two-active"); scanning.setPhaseForRender(.scanning(found: 412))
        popover("state-scanning", scanning)
        let failed = model("no-agents"); failed.setPhaseForRender(.failed(.coreTooOld("agentplay 0.2.3")))
        popover("state-error", failed)
        popover("proto-A-pulse", model("two-active", direction: .pulse))
        popover("proto-B-breakpoint", model("two-active", direction: .breakpoint))
        popover("proto-B-breakpoint-quiet", model("detected-idle", direction: .breakpoint))
        popover("proto-C-lab", model("two-active", direction: .lab))

        for step in 1...5 {
            let m = model("two-active")
            if step == 3 { m.expandedRun = m.orderedRuns.first?.sessionId }
            shots.append(("onboarding-\(step)", m, AnyView(OnboardingView(step: step).environmentObject(m))))
        }
        for pane in [SettingsView.Pane.notifications, .privacy, .account, .sources] {
            let m = model("two-active")
            shots.append(("settings-\(pane.rawValue.lowercased())", m, AnyView(SettingsView(pane: pane).environmentObject(m))))
        }

        for (name, _, view) in shots {
            for dark in [false, true] {
                write(view, to: dir.appendingPathComponent("\(name)-\(dark ? "dark" : "light").png"), dark: dark)
            }
        }
        for (name, pulse, alert) in [("mark", false, false), ("mark-pulse", true, false), ("mark-alert", false, true)] {
            let img = MenuBarMark.image(pulse: pulse, size: 18, alert: alert)
            writeImage(img, to: dir.appendingPathComponent("\(name).png"), scale: 4)
        }
        print("rendered \(shots.count * 2) screens to \(dir.path)")
    }

    /// The app icon: the mark in ink on a pale-lime squircle, as an .iconset for iconutil.
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
            NSColor(srgbRed: 0x1A / 255, green: 0x1A / 255, blue: 0x1A / 255, alpha: 1).setStroke()
            let ring = NSBezierPath(ovalIn: NSRect(x: c.x - center / 2, y: c.y - center / 2, width: center, height: center))
            ring.lineWidth = max(1, tile.width * 0.018); ring.stroke()
            NSGraphicsContext.restoreGraphicsState()
            let name = scale == 1 ? "icon_\(pt)x\(pt).png" : "icon_\(pt)x\(pt)@2x.png"
            try? rep.representation(using: .png, properties: [:])?.write(to: dir.appendingPathComponent(name))
        }
    }

    static func write(_ view: AnyView, to url: URL, dark: Bool) {
        let appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        let hosting = NSHostingView(rootView: view.environment(\.colorScheme, dark ? .dark : .light))
        hosting.appearance = appearance
        let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 400, height: 400),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = appearance
        window.contentView = hosting
        let size = hosting.fittingSize
        window.setContentSize(size)
        hosting.frame = NSRect(origin: .zero, size: size)
        window.orderFrontRegardless()
        // Let onAppear animations (map to moment) settle before the capture.
        RunLoop.current.run(until: Date().addingTimeInterval(0.45))
        hosting.layoutSubtreeIfNeeded()
        guard let rep = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) else { return }
        hosting.cacheDisplay(in: hosting.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: url)
        window.orderOut(nil)
    }

    static func writeImage(_ img: NSImage, to url: URL, scale: CGFloat) {
        let size = NSSize(width: img.size.width * scale, height: img.size.height * scale)
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width), pixelsHigh: Int(size.height),
                                   bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                   colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSColor.white.setFill(); NSRect(origin: .zero, size: size).fill()
        img.isTemplate = false
        img.draw(in: NSRect(origin: .zero, size: size))
        NSGraphicsContext.restoreGraphicsState()
        try? rep.representation(using: .png, properties: [:])?.write(to: url)
    }
}

extension Renderer {
    /// `--render-clips <dir>`: keyframes for the three prototype clips and the proof clip, one PNG per beat,
    /// rendered from the real views over the synthetic fixtures. scripts/companion-clips.sh turns them into MP4s.
    static func renderClips(to path: String) {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        guard let fixtures = ProcessInfo.processInfo.environment["AGENTPLAY_COMPANION_FIXTURES"] else { return }
        let dir = URL(fileURLWithPath: path)
        func model(_ name: String) -> CompanionModel {
            let m = CompanionModel(prefsStore: PrefsStore(defaults: UserDefaults(suiteName: "clip.\(UUID().uuidString)")!),
                                   isDevBuild: true)
            if let d = try? Data(contentsOf: URL(fileURLWithPath: fixtures).appendingPathComponent("\(name).json")),
               let s = try? SnapshotDecoder.decode(d) { m.loadForRender(s) }
            return m
        }
        typealias Beat = (CompanionModel) -> Void
        let clips: [(String, String, Direction, [Beat])] = [
            ("A-pulse", "two-active", .pulse, [
                { _ in },
                { $0.expandedRun = $0.orderedRuns.first?.sessionId },
                { $0.renderFocus = 0 }, { $0.renderFocus = 1 }, { $0.renderFocus = 2 }, { $0.renderFocus = 3 },
                { $0.renderFocus = 4 },
                { $0.renderFocus = nil },
            ]),
            ("B-breakpoint", "detected-idle", .breakpoint, [
                { _ in },
                { m in m.loadForRender(try! SnapshotDecoder.decode(Data(contentsOf: URL(fileURLWithPath: fixtures)
                    .appendingPathComponent("two-active.json")))) },
                { m in m.mutePattern(m.worthALook?.patternKey ?? "") },
            ]),
            ("C-lab", "two-active", .lab, [
                { _ in },
                { m in m.dismissLearn(m.learnCard?.patternKey ?? "") },
            ]),
            ("proof", "two-active", .v1, [
                { _ in },
                { m in m.showAll = false },
            ]),
            ("v1-tour", "two-active", .v1, [
                { _ in },
                { $0.expandedRun = $0.orderedRuns.first?.sessionId },
                { $0.expandedRun = nil; $0.tab = .limits },
                { $0.tab = .learn },
                { $0.tab = .now; $0.showConstellation = true },
            ]),
        ]
        for (name, fixture, direction, beats) in clips {
            let m = model(fixture)
            m.direction = direction
            let sub = dir.appendingPathComponent(name)
            try? FileManager.default.createDirectory(at: sub, withIntermediateDirectories: true)
            for (i, beat) in beats.enumerated() {
                beat(m)
                for dark in [false, true] {
                    write(AnyView(PopoverView(now: fixedNow).environmentObject(m)),
                          to: sub.appendingPathComponent(String(format: "%02d-%@.png", i, dark ? "dark" : "light")), dark: dark)
                }
            }
        }
        print("rendered clip keyframes to \(dir.path)")
    }
}

extension CompanionModel {
    /// Render and test hook: show a fixture as-is (times rebased to the renderer's clock), labelled Sample.
    func loadForRender(_ s: Snapshot) {
        installSampleForRender(SnapshotDecoder.rebased(s, to: Renderer.fixedNow))
    }
}
