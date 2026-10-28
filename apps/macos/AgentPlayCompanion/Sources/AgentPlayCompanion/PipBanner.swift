import AgentPlayCore
import AppKit
import SwiftUI

/// AgentPlay's own banner: Pip, big enough to read its pose, animated, in the top-right corner where macOS shows
/// notifications. It works whether or not macOS lets the app notify, and a click does what the alert is about.
@MainActor
enum PipBanner {
    struct Item {
        var title: String
        var body: String
        var pose: PipPose
        var action: () -> Void
    }

    private static var panel: NSPanel?
    private static var hide: DispatchWorkItem?
    private static var queue: [Item] = []
    static let size = NSSize(width: 360, height: 84)

    static func show(_ item: Item) {
        if panel != nil { queue.append(item); return }
        present(item)
    }

    private static func present(_ item: Item) {
        let screen = NSScreen.main ?? NSScreen.screens[0]
        let host = NSHostingView(rootView: AnyView(PipBannerView(item: item, close: { dismiss() }, open: { item.action(); dismiss() })))
        host.frame = NSRect(origin: .zero, size: size)
        let p = NSPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.level = .statusBar
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        p.appearance = StatusItemController.systemAppearance
        p.contentView = host
        let v = screen.visibleFrame
        p.setFrameOrigin(NSPoint(x: v.maxX - size.width - 12, y: v.maxY - size.height - 8))
        p.alphaValue = 0
        p.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { $0.duration = 0.25; p.animator().alphaValue = 1 }
        panel = p
        let work = DispatchWorkItem { dismiss() }
        hide = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 8, execute: work)
    }

    static func dismiss() {
        hide?.cancel()
        guard let p = panel else { return }
        panel = nil
        NSAnimationContext.runAnimationGroup({ $0.duration = 0.2; p.animator().alphaValue = 0 }, completionHandler: {
            MainActor.assumeIsolated {
                p.orderOut(nil)
                if !queue.isEmpty { present(queue.removeFirst()) }
            }
        })
    }

    /// Keeps the banner up while the pointer is over it.
    static func hold(_ on: Bool) {
        hide?.cancel()
        guard !on, panel != nil else { return }
        let work = DispatchWorkItem { dismiss() }
        hide = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 4, execute: work)
    }
}

struct PipBannerView: View {
    var item: PipBanner.Item
    var close: () -> Void
    var open: () -> Void
    @State private var entered = false
    @State private var hovering = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: 12) {
            TimelineView(.periodic(from: .now, by: reduceMotion ? 3600 : 0.25)) { t in
                let tick = reduceMotion ? 0 : Int(t.date.timeIntervalSinceReferenceDate * 4)
                let moving = item.pose == .wave || item.pose == .work
                Image(nsImage: PipIcon.image(item.pose, frame: moving ? tick / 2 : 0, session: 0.45, weekly: 0.6,
                                             eyes: tick % 16 == 0 ? .closed : .pose, hop: !reduceMotion && item.pose == .cheer && tick % 4 < 2))
                    .renderingMode(.template).interpolation(.none).resizable()
                    .frame(width: 50, height: 45)
                    .foregroundStyle(Ink.primary)
            }
            .offset(x: entered || reduceMotion ? 0 : 40)
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline) {
                    Text(item.title).font(.system(size: 13, weight: .semibold)).foregroundStyle(Ink.primary).lineLimit(1)
                    Spacer(minLength: 6)
                    Text("now").font(.system(size: 11)).foregroundStyle(Ink.secondary)
                }
                Text(item.body).font(.system(size: 12)).foregroundStyle(Ink.primary).lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 14)
        .frame(width: PipBanner.size.width, height: PipBanner.size.height)
        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Color(nsColor: StatusItemController.glassTint)))
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Ink.primary.opacity(0.12), lineWidth: 0.5))
        .overlay(alignment: .topLeading) {
            if hovering {
                Button(action: close) { Image(systemName: "xmark.circle.fill").font(.system(size: 16)).foregroundStyle(Ink.secondary) }
                    .buttonStyle(.plain).offset(x: -6, y: -6)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture(perform: open)
        .onHover { hovering = $0; PipBanner.hold($0) }
        .onAppear { withAnimation(.spring(response: 0.45, dampingFraction: 0.6)) { entered = true } }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
    }
}

/// Optional: while agents work, Pip walks along a small strip under the menu bar, so the state is visible from
/// across the room. Off unless turned on in Settings → Notifications.
@MainActor
enum PipWalker {
    private static var panel: NSPanel?
    static let size = NSSize(width: 230, height: 46)

    static func update(working: Int, line: String?, under button: NSStatusBarButton?) {
        guard working > 0 else { panel?.orderOut(nil); panel = nil; return }
        let host = NSHostingView(rootView: AnyView(PipWalkerView(text: working == 1 ? (line ?? "1 agent working") : "\(working) agents working")))
        host.frame = NSRect(origin: .zero, size: size)
        if panel == nil {
            let p = NSPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered, defer: false)
            p.isOpaque = false
            p.backgroundColor = .clear
            p.hasShadow = true
            p.level = .statusBar
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
            p.appearance = StatusItemController.systemAppearance
            p.ignoresMouseEvents = true
            panel = p
        }
        guard let p = panel else { return }
        p.contentView = host
        if let b = button, let w = b.window {
            let r = w.convertToScreen(b.convert(b.bounds, to: nil))
            p.setFrameOrigin(NSPoint(x: min(r.midX - size.width / 2, (w.screen ?? NSScreen.main!).visibleFrame.maxX - size.width - 8),
                                     y: r.minY - size.height - 6))
        }
        p.orderFrontRegardless()
    }
}

struct PipWalkerView: View {
    var text: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.periodic(from: .now, by: reduceMotion ? 3600 : 0.12)) { t in
            let s = t.date.timeIntervalSinceReferenceDate
            let tick = Int(s * 8)
            // There and back along the strip, facing the way it walks.
            let phase = (s / 6).truncatingRemainder(dividingBy: 1)
            let x = phase < 0.5 ? phase * 2 : (1 - phase) * 2
            ZStack(alignment: .bottomLeading) {
                Capsule().fill(Ink.primary.opacity(0.18)).frame(height: 2).padding(.horizontal, 10).padding(.bottom, 6)
                Image(nsImage: PipIcon.image(.work, frame: tick / 2, session: 0.45, weekly: 0.6))
                    .renderingMode(.template).interpolation(.none).resizable()
                    .frame(width: 28, height: 25)
                    .foregroundStyle(Ink.primary)
                    .scaleEffect(x: phase < 0.5 ? 1 : -1, y: 1)
                    .offset(x: 10 + (reduceMotion ? 0 : x) * (PipWalker.size.width - 60), y: -7)
                Text(text).font(.system(size: 11, weight: .medium)).foregroundStyle(Ink.secondary).lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .trailing).padding(.trailing, 12).padding(.bottom, 26)
            }
        }
        .frame(width: PipWalker.size.width, height: PipWalker.size.height)
        .background(Color(nsColor: StatusItemController.glassTint), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}
