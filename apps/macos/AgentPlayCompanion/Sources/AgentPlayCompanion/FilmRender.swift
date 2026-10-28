import AgentPlayCore
import AppKit
import SwiftUI

/// Every piece a launch film needs, drawn by the app itself, offscreen, at exact pixel size with alpha
/// (dev builds only): `--film-render <dir>`. Nothing is recorded from the screen and the pointer is never used.
///
///   pip/<pose>/0000.png …     Pip at 24x, 30 fps, 3 s loops: blink, typing, waving, the hop
///   bar/<state>.png           the status item at 8x: Pip and the line beside it
///   onboarding/0000.png …     the welcome window playing itself, 30 fps
///   menu/main.png             the real menu; menu/health.png and menu/fix.png its submenus
///   sheet.png                 the fix preview
///   toast/<name>.png          the two toasts
///
/// The menu captures pop the real NSMenu for half a second inside a window parked far off the screen.
@MainActor
enum FilmRender {
    static let fps = 30.0

    static func run(to path: String) async {
        let dir = URL(fileURLWithPath: path)
        do { try Film.setUp(fixture: LaunchOptions.value("--fixture") ?? "film", backdrop: false) } catch { print("film-render: \(error)"); return }
        NSApp.appearance = NSAppearance(named: .darkAqua)
        for sub in ["pip", "bar", "onboarding", "menu", "toast"] {
            try? FileManager.default.createDirectory(at: dir.appendingPathComponent(sub), withIntermediateDirectories: true)
        }
        let only = LaunchOptions.value("--film-render-only").map { Set($0.split(separator: ",").map(String.init)) }
        func wants(_ p: String) -> Bool { only?.contains(p) ?? true }
        if wants("pip") { pipSequences(dir.appendingPathComponent("pip")) }
        if wants("bar") { barStrips(dir.appendingPathComponent("bar")) }
        if wants("toast") { toasts(dir.appendingPathComponent("toast")) }
        if wants("sheet") { sheet(dir.appendingPathComponent("sheet.png")) }
        if wants("menu") { await menus(dir.appendingPathComponent("menu")) }
        if wants("onboarding") { await onboarding(dir.appendingPathComponent("onboarding")) }
        print("film-render: wrote \(dir.path)")
    }

    // MARK: bitmaps

    /// A transparent bitmap `w` x `h` px; `draw` works in points at `scale` px per point.
    static func bitmap(w: Int, h: Int, scale: CGFloat, _ draw: (NSGraphicsContext) -> Void) -> NSBitmapImageRep {
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8, samplesPerPixel: 4,
                                   hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        rep.size = NSSize(width: CGFloat(w) / scale, height: CGFloat(h) / scale)
        NSGraphicsContext.saveGraphicsState()
        let ctx = NSGraphicsContext(bitmapImageRep: rep)!
        NSGraphicsContext.current = ctx
        ctx.cgContext.clear(CGRect(x: 0, y: 0, width: w, height: h))
        draw(ctx)
        NSGraphicsContext.restoreGraphicsState()
        return rep
    }

    static func write(_ rep: NSBitmapImageRep, _ url: URL) {
        try? rep.representation(using: .png, properties: [:])?.write(to: url)
    }

    /// Pip's template image as white ink on nothing, on a square pixel grid at `scale` px per point.
    static func drawPip(_ img: NSImage, at origin: NSPoint, scale: CGFloat, ink: NSColor = .white) {
        guard let ctx = NSGraphicsContext.current else { return }
        ctx.imageInterpolation = .none
        let r = NSRect(origin: origin, size: img.size)
        ctx.cgContext.saveGState()
        img.draw(in: r, from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: [.interpolation: NSImageInterpolation.none.rawValue])
        ink.setFill()
        r.fill(using: .sourceIn)
        ctx.cgContext.restoreGState()
    }

    // MARK: Pip, large

    static let poses: [(PipPose, Double)] = [(.sleep, 0.10), (.work, 0.30), (.wave, 0.45), (.cheer, 0.55), (.oops, 0.62), (.sweat, 0.90), (.limit, 1.0)]

    static func pipSequences(_ dir: URL) {
        let scale: CGFloat = 24   // 20x18 pt -> 480x432 px
        for (pose, fill) in poses {
            let d = dir.appendingPathComponent(pose.rawValue)
            try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
            for i in 0..<90 {   // 3 s at 30 fps
                let t = Double(i) / fps
                let tick = Int(t * 4)
                let blink = (60...66).contains(i) && pose != .sleep && pose != .limit
                let frame = (pose == .work || pose == .wave) ? tick / 2 : 0
                let hop = pose == .cheer && i < 10
                let img = PipIcon.image(pose, frame: frame, session: fill, weekly: 0.58, eyes: blink ? .closed : .pose, hop: hop)
                let rep = bitmap(w: Int(20 * scale), h: Int(18 * scale), scale: scale) { _ in drawPip(img, at: .zero, scale: scale) }
                write(rep, d.appendingPathComponent(String(format: "%04d.png", i)))
            }
        }
        // the look-up, as a still
        let img = PipIcon.image(.wave, frame: 0, session: 0.45, weekly: 0.58, eyes: .up)
        write(bitmap(w: 480, h: 432, scale: scale) { _ in drawPip(img, at: .zero, scale: scale) }, dir.appendingPathComponent("wave-up.png"))
    }

    // MARK: the bar

    static let barStates: [(String, PipPose, String?, Double)] = [
        ("sleep", .sleep, nil, 0.10), ("work-a", .work, nil, 0.30), ("work-b", .work, nil, 0.30),
        ("wave-a", .wave, "docs-site", 0.45), ("wave-b", .wave, "docs-site", 0.45),
        ("cheer", .cheer, "payments-api", 0.55), ("oops", .oops, "pnpm test ×3", 0.62),
        ("sweat", .sweat, nil, 0.90), ("limit", .limit, "until 11:24 PM", 1.0)]

    /// The status item as the bar shows it: Pip, a space, the line, on nothing, at 8 px per point.
    static func barStrips(_ dir: URL) {
        let scale: CGFloat = 8
        for (name, pose, label, fill) in barStates {
            let frame = name.hasSuffix("-b") ? 1 : 0
            let img = PipIcon.image(pose, frame: frame, session: fill, weekly: 0.58)
            let font = NSFont.systemFont(ofSize: 12, weight: .medium)
            let text = label.map { NSAttributedString(string: $0, attributes: [.font: font, .foregroundColor: NSColor.white]) }
            let tw = text?.size().width ?? 0
            let w = 8 + 20 + (text == nil ? 0 : 6 + tw) + 8
            let rep = bitmap(w: Int(ceil(w * scale)), h: Int(22 * scale), scale: scale) { _ in
                drawPip(img, at: NSPoint(x: 8, y: 2), scale: scale)
                if let text { text.draw(at: NSPoint(x: 8 + 20 + 6, y: 11 - text.size().height / 2 + 1)) }
            }
            write(rep, dir.appendingPathComponent("\(name).png"))
        }
    }

    // MARK: toasts and the sheet

    static func toasts(_ dir: URL) {
        for (name, text) in [("terminal", "Brought Terminal forward."), ("applied", "Applied to .claude/settings.json. Undo it from Run Health any time.")] {
            let vc = NSHostingController(rootView: AnyView(
                Text(text).font(.callout).multilineTextAlignment(.leading)
                    .frame(maxWidth: 280, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(Color(nsColor: NSColor(white: 0.18, alpha: 1)), in: RoundedRectangle(cornerRadius: 10, style: .continuous))))
            vc.sizingOptions = []
            let size = vc.sizeThatFits(in: NSSize(width: 304, height: 400))
            write(capture(view: vc.view, size: size, scale: 4), dir.appendingPathComponent("\(name).png"))
        }
    }

    static func sheet(_ url: URL) {
        let a = CompanionModel.fixAlert(title: "Fix the `SessionStart:startup` hook", repo: "payments-api", file: ".claude/settings.json",
                                        granted: false, root: "~/code/payments-api",
                                        added: ["\"SessionStart\": [{\"matcher\": \"startup\", \"hooks\": [", "  {\"type\": \"command\", \"command\": \"scripts/agent-env.sh\"}", "]}]"])
        a.layout()
        let w = a.window
        w.appearance = NSAppearance(named: .darkAqua)
        write(captureWindow(w, scale: 2), url)
    }

    /// A view drawn into a bitmap at `scale`, offscreen, in a window parked far away so AppKit lays it out.
    static func capture(view: NSView, size: NSSize, scale: CGFloat) -> NSBitmapImageRep {
        let host = NSWindow(contentRect: NSRect(origin: NSPoint(x: -20000, y: -20000), size: size), styleMask: .borderless, backing: .buffered, defer: false)
        host.isOpaque = false
        host.backgroundColor = .clear
        host.appearance = NSAppearance(named: .darkAqua)
        view.frame = NSRect(origin: .zero, size: size)
        host.contentView = view
        host.orderFrontRegardless()
        view.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale), pixelsHigh: Int(size.height * scale), bitsPerSample: 8,
                                   samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        rep.size = size
        view.cacheDisplay(in: view.bounds, to: rep)
        host.orderOut(nil)
        return rep
    }

    static func captureWindow(_ w: NSWindow, scale: CGFloat) -> NSBitmapImageRep {
        w.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        w.orderFrontRegardless()
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        let v = w.contentView!.superview ?? w.contentView!
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(v.bounds.width * scale), pixelsHigh: Int(v.bounds.height * scale), bitsPerSample: 8,
                                   samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        rep.size = v.bounds.size
        v.cacheDisplay(in: v.bounds, to: rep)
        w.orderOut(nil)
        return rep
    }

    // MARK: menus

    /// Pops a menu inside a window parked off the screen, captures its window, closes it.
    static func captureMenu(_ menu: NSMenu, to url: URL) async {
        let park = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: 10, height: 10), styleMask: .borderless, backing: .buffered, defer: false)
        park.orderFrontRegardless()
        menu.appearance = NSAppearance(named: .darkAqua)
        let timer = Timer(timeInterval: 0.5, repeats: false) { _ in
            MainActor.assumeIsolated {
                if let w = NSApp.windows.first(where: { $0.isVisible && $0.className.contains("Menu") && !$0.className.contains("StatusBar") }),
                   let v = w.contentView?.superview ?? w.contentView {
                    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(v.bounds.width * 2), pixelsHigh: Int(v.bounds.height * 2), bitsPerSample: 8,
                                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
                    rep.size = v.bounds.size
                    v.cacheDisplay(in: v.bounds, to: rep)
                    write(rep, url)
                    print("film-render: menu \(url.lastPathComponent) \(Int(v.bounds.width))x\(Int(v.bounds.height)) at \(w.frame.origin)")
                }
                menu.cancelTracking()
            }
        }
        RunLoop.main.add(timer, forMode: .eventTracking)
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: 0), in: park.contentView)
        park.orderOut(nil)
        try? await Task.sleep(for: .milliseconds(200))
    }

    static func menus(_ dir: URL) async {
        let status = Film.status!
        status.rebuild(now: Date())
        await captureMenu(status.menu, to: dir.appendingPathComponent("main.png"))
        if let h = status.menu.items.first(where: { $0.title.hasPrefix("Run Health") })?.submenu {
            await captureMenu(h, to: dir.appendingPathComponent("health.png"))
        }
        if let f = status.menu.items.first(where: { $0.title.hasPrefix("Fix the") })?.submenu {
            await captureMenu(f, to: dir.appendingPathComponent("fix.png"))
        }
        if let s = status.menu.items.first(where: { $0.title == "Steering" })?.submenu {
            await captureMenu(s, to: dir.appendingPathComponent("steering.png"))
        }
    }

    // MARK: onboarding

    /// The welcome window plays itself for 17 s; one frame every 1/30 s.
    static func onboarding(_ dir: URL) async {
        let model = Film.model!
        model.prefs.onboardingDone = false
        let w = NSWindow(contentViewController: NSHostingController(rootView: AnyView(OnboardingView(filmAutoplay: true).environmentObject(model))))
        w.title = "Welcome to AgentPlay"
        w.styleMask = [.titled, .closable, .fullSizeContentView]
        w.titleVisibility = .hidden
        w.titlebarAppearsTransparent = true
        w.appearance = NSAppearance(named: .darkAqua)
        w.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        w.orderFrontRegardless()
        let v = w.contentView!.superview ?? w.contentView!
        let start = Date()
        var i = 0
        while Date().timeIntervalSince(start) < 17.0 {
            let due = start.addingTimeInterval(Double(i) / fps)
            // Suspend, never spin: the view's own dispatches (go, scan) run on the main queue between frames.
            try? await Task.sleep(for: .milliseconds(max(1, Int(due.timeIntervalSinceNow * 1000))))
            let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(v.bounds.width * 2), pixelsHigh: Int(v.bounds.height * 2), bitsPerSample: 8,
                                       samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
            rep.size = v.bounds.size
            v.cacheDisplay(in: v.bounds, to: rep)
            write(rep, dir.appendingPathComponent(String(format: "%04d.png", i)))
            i += 1
        }
        w.orderOut(nil)
        print("film-render: onboarding \(i) frames")
    }
}
