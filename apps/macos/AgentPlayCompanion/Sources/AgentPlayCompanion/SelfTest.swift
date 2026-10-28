import AgentPlayCore
import AppKit
import Foundation
import SwiftUI

/// Dev builds only: `--selftest` exercises the real bundle end to end and prints JSON results.
/// Runtime → snapshot → viewer start → health → deep link served → viewer stopped (only ours).
enum SelfTest {
    static func run() async -> Int32 {
        var report: [String: Any] = [:]
        func fail(_ k: String, _ v: Any) -> Int32 { report[k] = v; print(json(report)); return 1 }
        guard case .success(let rt) = Runtime.locate(allowDevOverride: true) else { return fail("runtime", "missing") }
        report["runtime"] = rt.bundled ? "bundled" : rt.python.path
        let svc = SnapshotService(runtime: rt)
        let t0 = Date()
        switch await svc.fetch(.read) {
        case .failure(let e): return fail("snapshot", e.localizedDescription)
        case .success(let s):
            report["snapshotMs"] = Int(Date().timeIntervalSince(t0) * 1000)
            report["runs"] = s.runs.count
            report["active"] = s.activeRuns.count
            report["alerts"] = s.alerts.count
            report["sources"] = s.foundSources.map(\.agent)
            report["contextKinds"] = Array(Set(s.runs.map { $0.context.kind.rawValue })).sorted()
            // Live records read directly (the fast path), and the app a click on each would bring forward.
            let live = LiveRecords.read(dir: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/sessions"))
            report["liveRecords"] = live.map { l in
                "\(l.status.rawValue) → \(l.pid.flatMap { TerminalFocus.app(for: $0)?.localizedName } ?? "no app (tmux, ssh or ended)")"
            }
            // The opt-in account fetch, only if the user turned it on: percentages or why not, never the token.
            if !PrefsStore().load().usesClaudeAccount {
                report["claudeAccount"] = "off"
            } else {
                switch await ClaudeAccount.fetch(claudeVersion: CompanionModel.claudeVersion()) {
                case .success(let rows):
                    report["claudeAccount"] = rows.map { "\($0.window) \(Int($0.used.value?.usedPercent ?? -1))% resets \($0.resetsAt.map { "\($0)" } ?? "?")" }
                case .failure(let f):
                    report["claudeAccount"] = f.message
                }
            }
            report["planKinds"] = s.limits.map { "\($0.provider):\($0.window):\($0.used.kind.rawValue):\($0.used.freshness.rawValue)" }
            let viewer = ViewerManager(runtime: rt)
            let v0 = Date()
            do {
                let base = try await viewer.ensure()
                report["viewerStartMs"] = Int(Date().timeIntervalSince(v0) * 1000)
                report["viewerLoopback"] = ViewerLink.isLocalViewer(base)
                report["viewerHealthy"] = await ViewerManager.healthy(base)
                if let run = s.runs.first {
                    let link = ViewerLink.url(base: base, sessionId: run.sessionId, seq: run.lastEvent?.seq)
                    report["deepLinkShape"] = link.query?.replacingOccurrences(of: #"session=[^&]+"#, with: "session=<id>",
                                                                               options: .regularExpression)
                    let (data, resp) = try await URLSession(configuration: .ephemeral).data(from: link)
                    report["deepLinkStatus"] = (resp as? HTTPURLResponse)?.statusCode
                    report["deepLinkServesViewer"] = String(decoding: data, as: UTF8.self).contains("<div id=\"root\"")
                    // The map the viewer will load for that link exists and carries the seq we jump to.
                    var c = URLComponents(url: base.appendingPathComponent("api/map-doc"), resolvingAgainstBaseURL: false)!
                    c.queryItems = [URLQueryItem(name: "id", value: run.sessionId)]
                    let (mdata, _) = try await URLSession(configuration: .ephemeral).data(from: c.url!)
                    if let obj = try? JSONSerialization.jsonObject(with: mdata) as? [String: Any],
                       let events = obj["events"] as? [[String: Any]], let seq = run.lastEvent?.seq {
                        report["deepLinkSeqExists"] = events.contains { ($0["seq"] as? Int) == seq }
                    }
                }
                // A DNS-rebinding-style request (foreign Host) must be refused by the server's guards.
                var evil = URLRequest(url: base.appendingPathComponent("api/sessions"))
                evil.setValue("evil.example:\(base.port ?? 0)", forHTTPHeaderField: "Host")
                let (_, er) = try await URLSession(configuration: .ephemeral).data(for: evil)
                report["foreignHostStatus"] = (er as? HTTPURLResponse)?.statusCode
                var cross = URLRequest(url: base.appendingPathComponent("api/sessions"))
                cross.setValue("https://evil.example", forHTTPHeaderField: "Origin")
                let (_, cr) = try await URLSession(configuration: .ephemeral).data(for: cross)
                report["foreignOriginStatus"] = (cr as? HTTPURLResponse)?.statusCode
                let pid = viewer.pid
                viewer.stop()
                try? await Task.sleep(for: .milliseconds(500))
                report["viewerStopped"] = pid.map { kill($0, 0) != 0 } ?? false
                // Crash recovery: a viewer left behind is reaped by the next launch through its PID file.
                let pidFile = FileManager.default.temporaryDirectory.appendingPathComponent("apc-selftest-\(getpid()).pid")
                let crashed = ViewerManager(runtime: rt, pidFile: pidFile)
                _ = try await crashed.ensure()
                let orphan = crashed.pid
                let next = ViewerManager(runtime: rt, pidFile: pidFile)
                report["leftoverReaped"] = next.reapLeftover()
                try? await Task.sleep(for: .milliseconds(500))
                report["leftoverGone"] = orphan.map { kill($0, 0) != 0 } ?? false
            } catch {
                return fail("viewer", (error as? SnapshotError)?.localizedDescription ?? "\(error)")
            }
        }
        print(json(report))
        return 0
    }

    static func json(_ d: [String: Any]) -> String {
        String(decoding: (try? JSONSerialization.data(withJSONObject: d, options: [.sortedKeys, .prettyPrinted])) ?? Data(), as: UTF8.self)
    }
}

extension Renderer {
    /// `--render-live <dir>`: the popover over this Mac's real snapshot. Writes to the given dir only;
    /// these images contain your repo and file names and are never committed.
    static func renderLive(to path: String) async {
        _ = NSApplication.shared
        guard case .success(let rt) = Runtime.locate(allowDevOverride: true),
              case .success(let fetched) = await SnapshotService(runtime: rt).fetch(.read) else { print("no snapshot"); return }
        let dir = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        // As the user sees it: their settings (read, never written), and account usage if they turned it on.
        let real = PrefsStore().load()
        var s = fetched
        if real.usesClaudeAccount, case .success(let rows) = await ClaudeAccount.fetch(claudeVersion: CompanionModel.claudeVersion()) {
            s.limits = ClaudeAccount.merge(s.limits, account: rows)
        }
        let snap = s
        await MainActor.run {
            NSApp.setActivationPolicy(.accessory)
            let m = CompanionModel(prefsStore: PrefsStore(defaults: UserDefaults(suiteName: "render.\(UUID().uuidString)")!),
                                   isDevBuild: true)
            m.prefs = real
            m.installLiveForRender(snap)
            m.refreshIdentity()
            let c = StatusItemController(model: m)
            c.updateIcon()
            RunLoop.current.run(until: Date().addingTimeInterval(0.4))
            if let v = c.statusItem.button?.window?.contentView, let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) {
                v.cacheDisplay(in: v.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: dir.appendingPathComponent("live-statusitem.png"))
            }
            NSStatusBar.system.removeStatusItem(c.statusItem)
            for dark in [false, true] {
                captureMenu(m, now: snap.generatedAt, to: dir.appendingPathComponent("live-menu-\(dark ? "dark" : "light").png"), dark: dark)
            }
            for t in [MenuTab.improve, .setup] {
                m.menuTab = t
                captureMenu(m, now: snap.generatedAt, to: dir.appendingPathComponent("live-menu-\(t.rawValue).png"), dark: true)
            }
            m.menuTab = .now
            if let line = MenuBuilder.sessionLines(snap, now: Date()).first {
                write(AnyView(SessionDetail(line: line, now: Date())), to: dir.appendingPathComponent("live-session.png"), dark: true, material: true)
            }
            for t in snap.usage?.setup?.tips ?? [] {
                write(AnyView(TipCard(tip: t)), to: dir.appendingPathComponent("live-tip-\(t.kind).png"), dark: true, material: true)
            }
            if let f = snap.insights?.fixes.first {
                write(AnyView(FixDetail(fix: f)), to: dir.appendingPathComponent("live-fix-detail.png"), dark: true, material: true)
            }
            if let e = MenuBuilder.errorLines(snap, prefs: real, now: snap.generatedAt).first {
                write(AnyView(ErrorDetail(line: e, now: snap.generatedAt)), to: dir.appendingPathComponent("live-error-detail.png"), dark: true, material: true)
            }
            if let sig = snap.usage?.signals {
                write(AnyView(SignalsCard(signals: sig)), to: dir.appendingPathComponent("live-signals.png"), dark: true, material: true)
            }
            if let ins = snap.insights {
                write(AnyView(HealthCard(insights: ins)), to: dir.appendingPathComponent("live-health.png"), dark: true, material: true)
            }
            if let st = snap.usage?.setup {
                for it in st.items.filter({ $0.measured != nil }).prefix(5) {
                    write(AnyView(ImpactCard(item: it)), to: dir.appendingPathComponent("live-impact-\(it.name.replacingOccurrences(of: " ", with: "-")).png"), dark: true, material: true)
                }
                write(AnyView(VStack(alignment: .leading, spacing: 14) { SetupPane(report: st) }.padding(28).frame(width: 720)
                                .background(Color(nsColor: .windowBackgroundColor)).environment(\.colorScheme, .dark)),
                      to: dir.appendingPathComponent("live-setup-pane.png"), dark: true, material: false)
            }
            if let u = snap.usage {
                write(AnyView(UsageDetail(report: u)), to: dir.appendingPathComponent("live-usage-details.png"), dark: true, material: true)
            }
            // Submenus as real menu windows, offscreen: what a hover opens, at the size the menu gives it.
            for tab in [MenuTab.setup, .improve] {
                m.menuTab = tab
                let c = StatusItemController(model: m)
                c.nowOverride = snap.generatedAt
                NSApp.appearance = NSAppearance(named: .darkAqua)
                c.prepare(now: snap.generatedAt)
                for (n, item) in c.menu.items.filter({ $0.submenu != nil && $0.view == nil }).prefix(2).enumerated() {
                    if let sub = item.submenu { Self.captureSubmenu(sub, to: dir.appendingPathComponent("live-sub-\(tab.rawValue)-\(n).png")) }
                }
                NSStatusBar.system.removeStatusItem(c.statusItem)
            }
            m.menuTab = .now
            // The Insights window, each section, as a real window.
            NSApp.appearance = NSAppearance(named: .darkAqua)
            for section in [InsightsSection.overview, .setup, .steering] {
                m.insightsSection = section
                let w = InsightsWindow.make(model: m)
                captureWindow(w, to: dir.appendingPathComponent("live-insights-\(section.rawValue).png"), wait: 0.8)
            }
        }
        print("rendered live screens to \(dir.path)")
    }
}

extension Renderer {
    /// One submenu, popped offscreen and drawn with its window frame, so a row cut off by its own menu shows.
    @MainActor static func captureSubmenu(_ menu: NSMenu, to url: URL) {
        menu.appearance = NSAppearance(named: .darkAqua)
        let timer = Timer(timeInterval: 0.5, repeats: false) { _ in
            MainActor.assumeIsolated {
                if let w = NSApp.windows.first(where: { $0.isVisible && $0.className.contains("PopupMenuWindow") }),
                   let v = w.contentView?.superview ?? w.contentView, let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) {
                    v.cacheDisplay(in: v.bounds, to: rep)
                    try? rep.representation(using: .png, properties: [:])?.write(to: url)
                    for case let item in menu.items where item.view != nil {
                        let h = item.view!
                        print("submenu row: frame \(Int(h.frame.width))x\(Int(h.frame.height)) needs \(Int(h.fittingSize.width))x\(Int(h.fittingSize.height))")
                    }
                }
                menu.cancelTracking()
            }
        }
        RunLoop.main.add(timer, forMode: .eventTracking)
        menu.popUp(positioning: nil, at: NSPoint(x: -3000, y: -3000), in: nil)
    }
}

extension CompanionModel {
    func installLiveForRender(_ s: Snapshot) {
        installSampleForRender(s)
        markLive()
    }
}

extension Renderer {
    /// `--contrast-check <dir>`: the real menu, over this Mac's real data, popped over a bright busy page and
    /// then a dark one, captured from the screen as composited (glass and all). Two captures that match mean the
    /// backdrop doesn't show through. Prints the mean pixel difference (0 to 255) for the glass as the system
    /// draws it and as AgentPlay tints it, in Light and Dark. Takes the screen for a few seconds; no cursor moves.
    static func contrastCheck(to path: String) async {
        _ = NSApplication.shared
        guard case .success(let rt) = Runtime.locate(allowDevOverride: true),
              case .success(let fetched) = await SnapshotService(runtime: rt).fetch(.read) else { print("no snapshot"); return }
        let dir = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let real = PrefsStore().load()
        var s = fetched
        if real.usesClaudeAccount, case .success(let rows) = await ClaudeAccount.fetch(claudeVersion: CompanionModel.claudeVersion()) {
            s.limits = ClaudeAccount.merge(s.limits, account: rows)
        }
        let snap = s
        await MainActor.run {
            NSApp.setActivationPolicy(.accessory)
            let m = CompanionModel(prefsStore: PrefsStore(defaults: UserDefaults(suiteName: "render.\(UUID().uuidString)")!), isDevBuild: true)
            m.prefs = real
            m.installLiveForRender(snap)
            m.refreshIdentity()
            let screen = NSScreen.main!
            let frame = NSRect(x: screen.frame.midX - 420, y: screen.frame.midY - 420, width: 840, height: 840)
            var shots: [String: NSBitmapImageRep] = [:]
            let passes: [(Bool, Bool, Bool, MenuTab)] = [false, true].flatMap { t in [true, false].flatMap { d in [true, false].map { b in (t, d, b, MenuTab.now) } } }
                + [MenuTab.improve, .setup].flatMap { tab in [true, false].map { d in (true, d, true, tab) } }
            var lastPair = ""
            for (tinted, dark, bright, tab) in passes {
                StatusItemController.glassTintOn = tinted
                m.menuTab = tab
                do {
                    do {
                        let back = NSWindow(contentRect: frame, styleMask: .borderless, backing: .buffered, defer: false)
                        back.level = .floating
                        back.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
                        back.contentView = NSHostingView(rootView: BusyPage(bright: bright))
                        back.orderFrontRegardless()
                        let c = StatusItemController(model: m)
                        c.nowOverride = snap.generatedAt
                        NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
                        c.menu.appearance = NSApp.appearance
                        c.prepare(now: snap.generatedAt)
                        let name = tab != .now ? "real-\(tab.rawValue)-\(dark ? "dark" : "light")"
                            : "\(tinted ? "tinted" : "system")-\(dark ? "dark" : "light")-over-\(bright ? "white" : "black")"
                        let url = dir.appendingPathComponent(name + ".png")
                        let timer = Timer(timeInterval: 0.9, repeats: false) { _ in
                            MainActor.assumeIsolated {
                                if let w = NSApp.windows.first(where: { $0.isVisible && $0.className.contains("PopupMenuWindow") }) {
                                    let f = w.frame, top = NSScreen.screens[0].frame.maxY
                                    let p = Process()
                                    p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
                                    p.arguments = ["-x", "-R", "\(Int(f.minX)),\(Int(top - f.maxY)),\(Int(f.width)),\(Int(f.height))", url.path]
                                    try? p.run(); p.waitUntilExit()
                                    if p.terminationStatus != 0 { print("capture failed: window \(f) screens \(NSScreen.screens.map(\.frame))") }
                                    if let img = NSImage(contentsOf: url), let t = img.tiffRepresentation, let rep = NSBitmapImageRep(data: t) { shots[name] = rep }
                                }
                                c.menu.cancelTracking()
                            }
                        }
                        RunLoop.main.add(timer, forMode: .eventTracking)
                        c.menu.popUp(positioning: nil, at: NSPoint(x: frame.minX + 60, y: frame.maxY - 30), in: nil)
                        NSStatusBar.system.removeStatusItem(c.statusItem)
                        back.orderOut(nil)
                        RunLoop.current.run(until: Date().addingTimeInterval(0.2))
                    }
                    let pair = "\(tinted ? "tinted" : "system")-\(dark ? "dark" : "light")"
                    guard tab == .now, !bright, pair != lastPair else { continue }
                    lastPair = pair
                    let a = shots["\(tinted ? "tinted" : "system")-\(dark ? "dark" : "light")-over-white"]
                    let b = shots["\(tinted ? "tinted" : "system")-\(dark ? "dark" : "light")-over-black"]
                    if let a, let b {
                        print("contrast: \(tinted ? "tinted" : "system") \(dark ? "dark " : "light") backdrop bleed \(String(format: "%.1f", Self.meanDiff(a, b))) / 255, "
                              + "inside the rim \(String(format: "%.1f", Self.meanDiff(a, b, inset: 16))) / 255")
                    }
                }
            }
            // Submenus, as they open: the first two of Tools and of Fixes, each popped on its own over the bright page.
            StatusItemController.glassTintOn = true
            for tab in [MenuTab.setup, .improve] {
                for dark in [true, false] {
                    m.menuTab = tab
                    let probe = StatusItemController(model: m)
                    probe.nowOverride = snap.generatedAt
                    NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
                    probe.prepare(now: snap.generatedAt)
                    // Every submenu in Tools and Fixes, in Dark; the first two in Light.
                    let subs = probe.menu.items.filter { $0.submenu != nil && $0.view == nil }.prefix(dark ? 30 : 2)
                    for (n, item) in subs.enumerated() {
                        guard let sub = item.submenu else { continue }
                        sub.appearance = NSApp.appearance
                        let back = NSWindow(contentRect: frame, styleMask: .borderless, backing: .buffered, defer: false)
                        back.level = .floating
                        back.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
                        back.contentView = NSHostingView(rootView: BusyPage(bright: true))
                        back.orderFrontRegardless()
                        let slug = item.title.lowercased().replacingOccurrences(of: " ", with: "-").filter { $0.isLetter || $0.isNumber || $0 == "-" }.prefix(28)
                        let url = dir.appendingPathComponent("real-sub-\(tab.rawValue)-\(n)-\(slug)-\(dark ? "dark" : "light").png")
                        let timer = Timer(timeInterval: 0.9, repeats: false) { _ in
                            MainActor.assumeIsolated {
                                StatusItemController.tintMenuGlass()
                                RunLoop.current.run(until: Date().addingTimeInterval(0.1))
                                if let w = NSApp.windows.first(where: { $0.isVisible && $0.className.contains("PopupMenuWindow") }) {
                                    let f = w.frame, top = NSScreen.screens[0].frame.maxY
                                    let p = Process()
                                    p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
                                    p.arguments = ["-x", "-R", "\(Int(f.minX)),\(Int(top - f.maxY)),\(Int(f.width)),\(Int(f.height))", url.path]
                                    try? p.run(); p.waitUntilExit()
                                    if p.terminationStatus != 0 { print("capture failed: submenu \(f)") }
                                }
                                sub.cancelTracking()
                            }
                        }
                        RunLoop.main.add(timer, forMode: .eventTracking)
                        sub.popUp(positioning: nil, at: NSPoint(x: frame.minX + 60, y: frame.maxY - 30), in: nil)
                        back.orderOut(nil)
                        RunLoop.current.run(until: Date().addingTimeInterval(0.2))
                    }
                    NSStatusBar.system.removeStatusItem(probe.statusItem)
                }
            }
        }
        print("contrast captures in \(dir.path)")
    }

    /// Mean absolute difference per channel between two captures of the same size.
    static func meanDiff(_ a: NSBitmapImageRep, _ b: NSBitmapImageRep, inset: Int = 0) -> Double {
        let w = min(a.pixelsWide, b.pixelsWide), h = min(a.pixelsHigh, b.pixelsHigh)
        let k = inset * a.pixelsWide / max(1, Int(a.size.width))
        var sum = 0.0, n = 0.0
        for y in stride(from: k, to: h - k, by: 2) {
            for x in stride(from: k, to: w - k, by: 2) {
                guard let p = a.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB), let q = b.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB) else { continue }
                sum += (abs(p.redComponent - q.redComponent) + abs(p.greenComponent - q.greenComponent) + abs(p.blueComponent - q.blueComponent)) / 3
                n += 1
            }
        }
        return n > 0 ? 255 * sum / n : 0
    }
}

/// A worst case to put a menu over: a busy page of text, colour and photos-like blocks, bright or dark.
struct BusyPage: View {
    var bright: Bool
    var body: some View {
        let fg: Color = bright ? .black : .white
        ZStack(alignment: .topLeading) {
            (bright ? Color.white : Color.black)
            VStack(alignment: .leading, spacing: 10) {
                ForEach(0..<26, id: \.self) { i in
                    HStack(spacing: 12) {
                        RoundedRectangle(cornerRadius: 6).fill([Color.red, .blue, .green, .orange, .purple, .pink][i % 6]).frame(width: 90, height: 22)
                        Text("Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do \(i)").font(.system(size: 17, weight: i % 3 == 0 ? .bold : .regular)).foregroundStyle(fg)
                    }
                }
            }
            .padding(24)
        }
        .frame(width: 840, height: 840)
    }
}

extension Renderer {
    /// `--export-vectors <dir>`: every SF Symbol the app shows as a one-page PDF (for SVG conversion), and each Pip
    /// pose as a sharp 12x PNG, for rebuilding the design in Figma.
    @MainActor static func exportVectors(to path: String) {
        _ = NSApplication.shared
        let dir = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let symbols = ["person.2", "wrench.and.screwdriver", "xmark.octagon", "xmark.octagon.fill", "gauge.with.dots.needle.67percent",
                       "gauge.with.dots.needle.33percent", "rectangle.compress.vertical", "hand.thumbsup", "sparkle", "sparkles",
                       "lightbulb", "trash", "heart.text.square", "steeringwheel", "chart.bar.xaxis", "macwindow",
                       "puzzlepiece.extension", "folder", "safari", "doc.on.doc", "arrow.uturn.backward.circle",
                       "checkmark.circle.fill", "checkmark.circle", "exclamationmark.triangle.fill", "checkmark.seal.fill",
                       "checkmark.seal", "bolt.horizontal", "arrow.up.forward.app", "play.circle", "command", "shippingbox",
                       "eye.fill", "chevron.right", "circle.fill", "arrow.triangle.2.circlepath", "gearshape", "bell",
                       "hand.raised", "square.stack.3d.up", "shippingbox", "lock.shield", "bell.badge"]
        for name in Set(symbols) {
            guard let base = NSImage(systemSymbolName: name, accessibilityDescription: nil),
                  let img = base.withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 16, weight: .regular)) else {
                print("missing \(name)"); continue
            }
            // A 4x black-on-clear mask: Figma tints it with a colour fill, so it follows Light and Dark.
            let scale: CGFloat = 4
            let w = Int(ceil(img.size.width * scale)), h = Int(ceil(img.size.height * scale))
            guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8, samplesPerPixel: 4,
                                             hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { continue }
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            img.draw(in: NSRect(x: 0, y: 0, width: w, height: h))
            NSGraphicsContext.restoreGraphicsState()
            try? rep.representation(using: .png, properties: [:])?.write(to: dir.appendingPathComponent("sym-\(name).png"))
            print("symbol \(name) \(img.size.width)x\(img.size.height)")
        }
        for pose in PipPose.allCases {
            let img = PipIcon.image(pose, session: 0.34, weekly: 0.58)
            let scale: CGFloat = 12
            let w = Int(img.size.width * scale), h = Int(img.size.height * scale)
            guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8, samplesPerPixel: 4,
                                             hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { continue }
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            NSGraphicsContext.current?.imageInterpolation = .none
            img.draw(in: NSRect(x: 0, y: 0, width: w, height: h))
            NSGraphicsContext.restoreGraphicsState()
            try? rep.representation(using: .png, properties: [:])?.write(to: dir.appendingPathComponent("pip-\(pose.rawValue).png"))
        }
        print("exported to \(dir.path)")
    }
}

extension Renderer {
    /// `--banner-check <dir>`: Pip's banner (each pose) and the walking strip, on the real screen over a bright page,
    /// captured as composited.
    @MainActor static func bannerCheck(to path: String) {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        let dir = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let screen = NSScreen.main!
        let v = screen.visibleFrame
        let back = NSWindow(contentRect: NSRect(x: v.maxX - 520, y: v.maxY - 360, width: 520, height: 360), styleMask: .borderless, backing: .buffered, defer: false)
        back.level = .floating
        back.contentView = NSHostingView(rootView: BusyPage(bright: true))
        back.orderFrontRegardless()
        func shoot(_ name: String, _ r: NSRect) {
            let top = NSScreen.screens[0].frame.maxY
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            p.arguments = ["-x", "-R", "\(Int(r.minX)),\(Int(top - r.maxY)),\(Int(r.width)),\(Int(r.height))", dir.appendingPathComponent(name + ".png").path]
            try? p.run(); p.waitUntilExit()
        }
        for (pose, title, body) in [(PipPose.wave, "Claude Code needs you", "docs-site is waiting for your answer."),
                                    (.cheer, "Done in 14 min", "212 tool calls, 2 errors. Last step: pnpm test."),
                                    (.oops, "pnpm test failed 3 times", "Three in a row in one run, no pass between."),
                                    (.sweat, "Weekly limit: out in about 5 hours", "97% used; resets Thursday 6:30 AM.")] {
            PipBanner.show(.init(title: title, body: body, pose: pose, action: {}))
            RunLoop.main.run(until: Date().addingTimeInterval(1.2))
            let r = NSRect(x: v.maxX - PipBanner.size.width - 30, y: v.maxY - PipBanner.size.height - 26, width: PipBanner.size.width + 36, height: PipBanner.size.height + 36)
            shoot("banner-\(pose.rawValue)", r)
            PipBanner.dismiss()
            RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        }
        let anchor = NSRect(x: v.maxX - 300, y: v.maxY, width: 20, height: 1)
        PipWalker.update(working: 2, line: nil, under: nil)
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        if let w = NSApp.windows.first(where: { $0 is NSPanel && $0.isVisible && $0.frame.width == PipWalker.size.width }) {
            w.setFrameOrigin(NSPoint(x: anchor.minX, y: v.maxY - PipWalker.size.height - 40))
            for i in 0..<3 {
                RunLoop.main.run(until: Date().addingTimeInterval(0.9))
                shoot("walker-\(i)", w.frame.insetBy(dx: -14, dy: -14))
            }
        }
        PipWalker.update(working: 0, line: nil, under: nil)
        back.orderOut(nil)
        print("banner captures in \(dir.path)")
    }
}
