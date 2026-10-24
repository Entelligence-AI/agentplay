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
