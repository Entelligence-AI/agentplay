import AppKit
import SwiftUI

/// Native menu metrics and semantic styles only: system label colours, the user's accent, the menu material.
/// No hex colours and no light/dark branches, so Increase Contrast, Reduce Transparency and the accent all work.
enum Menu {
    /// Custom rows in the NSMenu are drawn at this width; native rows size to fit it.
    static let width: CGFloat = 300
    /// Matches the text inset of native menu items, so custom rows line up with them.
    static let inset: CGFloat = 16
}

/// Entelligence sage: the brand's own green, softer than the system green that means "good", so the accent never
/// reads as success. Lighter on dark, deeper on light, with near-black text on it in dark mode as the brand does.
enum Brand {
    static let accentNS = NSColor(name: "AgentPlayAccent") { ap in
        ap.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(srgbRed: 0xB8 / 255, green: 0xD3 / 255, blue: 0x95 / 255, alpha: 1)
            : NSColor(srgbRed: 0x5E / 255, green: 0x77 / 255, blue: 0x3C / 255, alpha: 1)
    }
    static let onAccentNS = NSColor(name: "AgentPlayOnAccent") { ap in
        ap.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? NSColor(white: 0.09, alpha: 1) : .white
    }
    static let accent = Color(nsColor: accentNS)
    static let onAccent = Color(nsColor: onAccentNS)
}

enum PaceTint {
    case normal, ahead, over, stale

    var color: Color {
        switch self {
        // System colours by NSColor: SwiftUI's own resolve to white inside an NSMenu.
        case .normal: Brand.accent
        case .ahead: Color(nsColor: .systemOrange)
        case .over: Color(nsColor: .systemRed)
        case .stale: Color(nsColor: .tertiaryLabelColor)
        }
    }
}
