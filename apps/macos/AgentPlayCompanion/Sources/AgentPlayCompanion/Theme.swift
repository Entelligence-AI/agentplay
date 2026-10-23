import AppKit
import SwiftUI

/// Native menu metrics and semantic styles only: system label colours, the user's accent, the menu material.
/// No hex colours and no light/dark branches, so Increase Contrast, Reduce Transparency and the accent all work.
enum Menu {
    static let width: CGFloat = 320
    /// Like a native menu: as tall as the screen allows, less room for the menu bar and a margin.
    @MainActor static var maxHeight: CGFloat { max(420, (NSScreen.main?.visibleFrame.height ?? 720) - 40) }
    static let inset: CGFloat = 14       // text inset from the window edge
    static let highlightInset: CGFloat = 5
    static let rowRadius: CGFloat = 6

    static let title = Font.body.weight(.semibold)        // 13 semibold
    static let body = Font.body                           // 13 regular
    static let value = Font.subheadline.monospacedDigit() // 11
    static let secondary = Font.subheadline               // 11
    static let header = Font.subheadline.weight(.semibold)

    static var tertiary: Color { Color(nsColor: .tertiaryLabelColor) }
    static var separator: Color { Color(nsColor: .separatorColor) }

    /// Menus don't animate highlights; disclosure uses a short ease-out, and nothing moves under Reduce Motion.
    static func disclosure(_ reduceMotion: Bool) -> Animation? { reduceMotion ? nil : .easeOut(duration: 0.15) }
}

enum PaceTint {
    case normal, ahead, over, stale

    var color: Color {
        switch self {
        case .normal: .accentColor
        case .ahead: .orange
        case .over: .red
        case .stale: Color(nsColor: .tertiaryLabelColor)
        }
    }
}
