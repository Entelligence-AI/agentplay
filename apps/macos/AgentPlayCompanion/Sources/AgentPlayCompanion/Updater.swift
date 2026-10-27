import AppKit
import Sparkle

/// Updates, through Sparkle, from the appcast attached to the latest GitHub release. Once a day it asks for the
/// appcast (a request to github.com, the only one the app makes besides the opt-in usage fetch) and, when a newer
/// version is there, offers it; the download is installed only after the person says so. Every update is
/// checked against the project's EdDSA public key in Info.plist before it is opened, so a tampered file is
/// refused whatever server it came from. Off with one switch in Settings → General.
@MainActor
final class Updater: NSObject, SPUUpdaterDelegate {
    static let shared = Updater()
    private var controller: SPUStandardUpdaterController?

    /// Dev builds and ad-hoc signed copies never check: there is nothing for them to update to.
    var available: Bool {
        guard Bundle.main.bundleURL.pathExtension == "app",
              let key = Bundle.main.infoDictionary?["SUPublicEDKey"] as? String, !key.hasPrefix("__") else { return false }
        return true
    }

    func start() {
        guard available, controller == nil else { return }
        controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)
    }

    var checksAutomatically: Bool {
        get { controller?.updater.automaticallyChecksForUpdates ?? false }
        set { controller?.updater.automaticallyChecksForUpdates = newValue }
    }

    var lastChecked: Date? { controller?.updater.lastUpdateCheckDate }

    func checkNow() {
        start()
        controller?.checkForUpdates(nil)
    }

    // MARK: SPUUpdaterDelegate

    /// Nothing about this Mac goes with the request: no system profile, no counts.
    nonisolated func updater(_ updater: SPUUpdater, shouldSendSystemProfileUsingUpdater: Bool) -> Bool { false }
    nonisolated func feedParameters(for updater: SPUUpdater, sendingSystemProfile: Bool) -> [[String: String]] { [] }
}
