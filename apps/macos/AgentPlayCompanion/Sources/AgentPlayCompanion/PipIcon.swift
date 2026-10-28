import AgentPlayCore
import AppKit

/// Pip, drawn on the pixel grid as a template image (20×18 pt, 40×36 px at 2×) so macOS tints it for any menu
/// bar. The body fills with the 5-hour session and Pip stands on the week; eyes are XOR'd so they read on the
/// fill and on the empty jar alike. `frame` alternates the two poses that move: typing and waving.
enum PipIcon {
    /// Eyes as the pose has them, shut for a blink, or looking up at whoever is pointing at Pip.
    enum Eyes { case pose, closed, up }

    static func image(_ pose: PipPose, frame: Int = 0, session: Double?, weekly: Double?, stale: Bool = false,
                      eyes: Eyes = .pose, hop: Bool = false) -> NSImage {
        let img = NSImage(size: NSSize(width: 20, height: 18), flipped: false) { _ in
            let ink = NSColor.black.withAlphaComponent(stale ? 0.5 : 1)
            func px(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> NSRect {
                NSRect(x: x / 2, y: y / 2, width: w / 2, height: h / 2)
            }
            func fill(_ r: NSRect, _ alpha: CGFloat = 1) {
                ink.withAlphaComponent(ink.alphaComponent * alpha).setFill()
                r.fill(using: NSGraphicsContext.current?.compositingOperation ?? .sourceOver)
            }
            let b = frame % 2 == 1
            if hop { NSGraphicsContext.current?.cgContext.translateBy(x: 0, y: 1.5) }  // a little jump
            // The ground is the week: a track, filled as far as the week is used.
            fill(px(2, 0, 36, 2), 0.3)
            if let weekly { fill(px(2, 0, max(2, 36 * min(1, weekly)), 2)) }
            // Legs; they shuffle while typing.
            fill(px(pose == .work && b ? 12 : 13, 3, 3, 3)); fill(px(pose == .work && !b ? 25 : 24, 3, 3, 3))
            // The jar: a 2 px outline with rounded corners.
            let body = px(8, 6, 24, 22)
            let outline = NSBezierPath(roundedRect: body, xRadius: 2.5, yRadius: 2.5)
            outline.append(NSBezierPath(roundedRect: body.insetBy(dx: 1, dy: 1), xRadius: 1.5, yRadius: 1.5).reversed)
            ink.setFill(); outline.fill()
            // The fill is the 5-hour session, whatever the pose: a spent week shows as the hourglass, not a full jar.
            let level = min(1, max(0, session ?? 0))
            if level > 0 { NSBezierPath(roundedRect: px(10, 8, 20, 18 * level), xRadius: 0.5, yRadius: 0.5).fill() }
            // Eyes.
            let ctx = NSGraphicsContext.current
            ctx?.compositingOperation = .xor
            switch eyes == .closed ? .sleep : eyes == .up ? .cheer : pose {
            case .sleep, .limit:
                fill(px(13, 18, 5, 2)); fill(px(22, 18, 5, 2))
            case .work:
                fill(px(14, 13, 3, 4)); fill(px(23, 13, 3, 4))  // looking down at the work
            case .oops:
                for i in 0..<4 {
                    let d = CGFloat(i) * 1.2
                    fill(px(12.5 + d, 15 + d, 2, 2)); fill(px(16.1 - d, 15 + d, 2, 2))
                    fill(px(21.5 + d, 15 + d, 2, 2)); fill(px(25.1 - d, 15 + d, 2, 2))
                }
            case .wave, .cheer, .sweat:
                let y: CGFloat = eyes == .up ? 20 : 17  // looking up at the pointer
                fill(px(14, y, 3, 5)); fill(px(23, y, 3, 5))
            }
            ctx?.compositingOperation = .sourceOver
            // Arms and marks.
            switch pose {
            case .work:
                if b { fill(px(32, 9, 4, 2)) } else { fill(px(4, 9, 4, 2)) }
            case .wave:
                if b { fill(px(33, 18, 3, 3)); fill(px(35, 20, 3, 8)); fill(px(34, 28, 5, 5)) }
                else { fill(px(33, 16, 3, 12)); fill(px(32, 28, 5, 5)) }
            case .cheer:
                fill(px(4, 18, 3, 12)); fill(px(33, 18, 3, 12))
                fill(px(1, 33, 2, 2)); fill(px(37, 33, 2, 2)); fill(px(19, 32, 2, 3))
            case .sweat:
                fill(px(33, 20, 3, 5)); fill(px(33.5, 25, 2, 2)); fill(px(34, 27, 1, 1))
            case .sleep:  // a Z
                fill(px(32, 33, 6, 2)); fill(px(35, 31, 2, 2)); fill(px(33, 29, 2, 2)); fill(px(32, 27, 6, 2))
            case .limit:  // an hourglass: spent until it resets
                fill(px(33, 32, 6, 2)); fill(px(34, 30, 4, 2)); fill(px(35, 28, 2, 2)); fill(px(34, 26, 4, 2)); fill(px(33, 24, 6, 2))
            case .oops:
                break
            }
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = description(pose)
        return img
    }

    static func description(_ pose: PipPose) -> String {
        switch pose {
        case .sleep: "Nothing running"
        case .work: "An agent is working"
        case .wave: "A session needs you"
        case .cheer: "A session finished"
        case .oops: "A command keeps failing"
        case .sweat: "Ahead of pace on your plan"
        case .limit: "Plan limit reached"
        }
    }
}
