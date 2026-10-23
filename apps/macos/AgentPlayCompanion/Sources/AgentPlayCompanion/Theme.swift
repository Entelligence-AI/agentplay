import AppKit
import SwiftUI

/// Brand tokens, adapted to system light and dark. One emphasis per state: lime marks "this needs you" or "live".
enum Theme {
    static let lime = Color(hex: 0xA2E435)
    static let paleLime = Color(hex: 0xE7F2D4)
    static let darkLime = Color(hex: 0x2A3326)

    static func ink(_ scheme: ColorScheme) -> Color { scheme == .dark ? Color(hex: 0xF2F2F0) : Color(hex: 0x1A1A1A) }
    static func muted(_ scheme: ColorScheme) -> Color { scheme == .dark ? Color(hex: 0xA3A3A0) : Color(hex: 0x6B6B6B) }
    static func paper(_ scheme: ColorScheme) -> Color { scheme == .dark ? Color(hex: 0x1C1D1B) : Color(hex: 0xFFFFFF) }
    static func card(_ scheme: ColorScheme) -> Color { scheme == .dark ? Color(hex: 0x262825) : Color(hex: 0xF6F6F4) }
    static func hairline(_ scheme: ColorScheme) -> Color { scheme == .dark ? Color.white.opacity(0.09) : Color.black.opacity(0.08) }
    /// Lime as a fill behind ink text: pale in light mode, deep green in dark mode, so text stays AA.
    static func limeWash(_ scheme: ColorScheme) -> Color { scheme == .dark ? darkLime : paleLime }
    /// Lime as a foreground on paper: the raw lime fails contrast on white, so light mode uses a deeper green.
    static func limeInk(_ scheme: ColorScheme) -> Color { scheme == .dark ? lime : Color(hex: 0x3F6B12) }
    static func error(_ scheme: ColorScheme) -> Color { scheme == .dark ? Color(hex: 0xFF8A7A) : Color(hex: 0xB3261E) }

    // 8 pt grid.
    static let s1: CGFloat = 4, s2: CGFloat = 8, s3: CGFloat = 12, s4: CGFloat = 16, s5: CGFloat = 24
    static let popoverWidth: CGFloat = 380
    static let radius: CGFloat = 10

    static let mono = Font.system(.caption, design: .monospaced)

    /// cubic-bezier(.16, 1, .3, 1), under 250 ms. No springs that overshoot.
    static func motion(_ reduce: Bool, _ duration: Double = 0.22) -> Animation? {
        reduce ? nil : .timingCurve(0.16, 1, 0.3, 1, duration: duration)
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB, red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255, opacity: 1)
    }
}

/// Quiet text button: large hit target, no chrome until hovered or focused.
struct QuietButtonStyle: ButtonStyle {
    var prominent = false
    @Environment(\.colorScheme) private var scheme
    @State private var hover = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: prominent ? .semibold : .medium))
            .foregroundStyle(prominent ? Theme.ink(.light) : Theme.ink(scheme))
            .padding(.horizontal, Theme.s3)
            .frame(minHeight: 28)
            .background(
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(prominent ? Theme.lime.opacity(configuration.isPressed ? 0.8 : 1)
                          : Theme.hairline(scheme).opacity(hover || configuration.isPressed ? 1.6 : 1))
            )
            .contentShape(Rectangle())
            .onHover { hover = $0 }
    }
}

struct LinkButtonStyle: ButtonStyle {
    @Environment(\.colorScheme) private var scheme
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(Theme.limeInk(scheme).opacity(configuration.isPressed ? 0.7 : 1))
            .frame(minHeight: 24)
            .contentShape(Rectangle())
    }
}

struct SectionLabel: View {
    var text: String
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .tracking(0.6)
            .foregroundStyle(Theme.muted(scheme))
            .accessibilityAddTraits(.isHeader)
    }
}

/// `Recorded`, `Reported by Codex`, `Unavailable`: the provenance chip every number carries.
struct ProvenanceChip: View {
    var text: String
    var stale = false
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        Text(stale ? "\(text) · stale" : text)
            .font(.system(size: 10, weight: .medium))
            .lineLimit(1)
            .fixedSize()
            .foregroundStyle(stale ? Theme.error(scheme) : Theme.muted(scheme))
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(Capsule().stroke(Theme.hairline(scheme).opacity(2), lineWidth: 1))
    }
}

struct SampleBadge: View {
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        Text("Sample data")
            .font(.system(size: 10, weight: .semibold))
            .lineLimit(1)
            .fixedSize()
            .foregroundStyle(Theme.ink(.light))
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(Capsule().fill(Theme.paleLime))
            .accessibilityLabel("Sample data. Nothing here is from your machine.")
    }
}
