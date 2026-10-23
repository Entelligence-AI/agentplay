import AgentPlayCore
import AppKit
import Combine
import CoreGraphics
import ServiceManagement
import SwiftUI
import UserNotifications
import os

let companionLog = Logger(subsystem: "ai.entelligence.agentplay.companion", category: "refresh")

enum Tab: String, CaseIterable, Identifiable {
    case now = "Now", limits = "Limits", learn = "Learn"
    var id: String { rawValue }
}

/// Prototype directions (dev builds compare them; release ships the v1 composition).
enum Direction: String, CaseIterable, Identifiable {
    case v1, pulse = "A", breakpoint = "B", lab = "C"
    var id: String { rawValue }
    var title: String {
        switch self {
        case .v1: "v1 (A shell, B alerts, C card)"
        case .pulse: "A · Pulse"
        case .breakpoint: "B · Breakpoint"
        case .lab: "C · Harness Lab"
        }
    }
}

enum Phase: Equatable {
    case starting
    case scanning(found: Int?)
    case ready
    case failed(SnapshotError)
}

@MainActor
final class CompanionModel: ObservableObject {
    @Published private(set) var snapshot: Snapshot?
    @Published private(set) var phase: Phase = .starting
    @Published private(set) var lastUpdated: Date?
    @Published private(set) var lastError: SnapshotError?
    @Published var prefs: CompanionPrefs {
        didSet {
            guard prefs != oldValue else { return }
            prefsStore.save(prefs)
            envBox.value = prefs.folderEnvironment
        }
    }
    /// The extra environment for the core, readable from the background scan without touching the main actor.
    private let envBox = EnvBox()
    @Published var tab: Tab = .now
    @Published var direction: Direction = .v1
    @Published var expandedRun: String?
    @Published var showAll = false
    @Published var showConstellation = false
    @Published var popoverOpen = false { didSet { if popoverOpen { scheduleRefresh(reason: .popoverOpened) } } }
    /// True briefly after new recorded events arrive for an active run: drives the two-frame menu-bar pulse.
    @Published private(set) var heartbeat = false
    @Published private(set) var justCompleted: Set<String> = []
    @Published private(set) var notificationStatus: UNAuthorizationStatus = .notDetermined
    @Published private(set) var loginItemEnabled = false
    @Published var toast: String?
    @Published private(set) var viewerBusy = false
    /// A scan running long enough that something outside it is probably holding it up (a macOS folder prompt).
    @Published private(set) var scanStalled = false

    let prefsStore: PrefsStore
    let isDevBuild: Bool
    private(set) var runtime: Runtime?
    private var service: SnapshotService?
    private var viewer: ViewerManager?
    private var watcher: FileWatcher?
    private var debounce: Task<Void, Never>?
    private var fallback: Timer?
    private var lastRefreshStarted = Date.distantPast
    private var refreshQueued = false
    private var lastRefreshDuration: TimeInterval = 0
    private var lastEventSeen: [String: Date] = [:]
    private var lastStates: [String: ActivityState] = [:]
    private var heartbeatTask: Task<Void, Never>?
    private var sampleMode = false
    private var started = false

    init(prefsStore: PrefsStore = PrefsStore(), isDevBuild: Bool) {
        self.prefsStore = prefsStore
        self.isDevBuild = isDevBuild
        self.prefs = prefsStore.load()
        envBox.value = prefs.folderEnvironment
    }

    var isSample: Bool { sampleMode || snapshot?.isSample == true }
    var now: Date { Date() }

    // MARK: lifecycle

    func start(fixture: String? = nil) {
        guard !started else { return }
        started = true
        refreshLoginItem()
        Task { await refreshNotificationStatus() }
        if let fixture {
            loadSample(named: fixture)
            return
        }
        if isDevBuild, ProcessInfo.processInfo.arguments.contains("--skip-onboarding") { prefs.onboardingDone = true }
        switch Runtime.locate(allowDevOverride: isDevBuild) {
        case .success(let r):
            runtime = r
            let box = envBox
            service = SnapshotService(runtime: r, extraEnv: { box.value })
            let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first!
            viewer = ViewerManager(runtime: r,
                                   pidFile: caches.appendingPathComponent("\(PrefsStore.domain)/viewer.pid"),
                                   extraEnv: { box.value })
            viewer?.reapLeftover()
            installTerminationHandler()
        case .failure(let e):
            phase = .failed(e)
            return
        }
        if prefs.onboardingDone {
            Task { await refresh(.read); await refresh(.refresh) }
            startWatching()
        }
        observeSystem()
    }

    private var sigterm: DispatchSourceSignal?

    /// `kill` and logout send SIGTERM, which skips applicationWillTerminate: stop our viewer first.
    private func installTerminationHandler() {
        signal(SIGTERM, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        src.setEventHandler { [weak self] in
            MainActor.assumeIsolated { self?.shutdown() }
            exit(0)
        }
        src.resume()
        sigterm = src
    }

    func shutdown() {
        viewer?.stop()
        watcher?.stop()
        service?.cancelNow()
    }

    // MARK: data

    func refresh(_ mode: SnapshotService.Mode) async {
        guard let service, !sampleMode else { return }
        lastRefreshStarted = Date()
        if snapshot == nil || mode == .fullScan { phase = .scanning(found: nil) }
        let progress = mode == .fullScan ? Task { await self.watchScanProgress() } : nil
        let t0 = Date()
        let stallWatch = Task { try? await Task.sleep(for: .seconds(45)); if !Task.isCancelled { scanStalled = true } }
        defer { stallWatch.cancel(); scanStalled = false }
        let result = await service.fetch(mode)
        progress?.cancel()
        if mode == .refresh { lastRefreshDuration = Date().timeIntervalSince(t0) }
        // Mode and timing only: never ids, paths or anything from a session.
        companionLog.info("refresh \(String(describing: mode), privacy: .public) \(Int(Date().timeIntervalSince(t0) * 1000), privacy: .public)ms ok=\((try? result.get()) != nil, privacy: .public)")
        switch result {
        case .success(let s):
            apply(s)
        case .failure(let e):
            lastError = e
            phase = snapshot == nil ? .failed(e) : .ready
        }
    }

    func cancelScan() {
        Task { await service?.cancel() }
    }

    /// While a first scan runs, the core saves the index every couple of seconds: count rows as they land.
    private func watchScanProgress() async {
        let home = ProcessInfo.processInfo.environment["AGENTPLAY_HOME"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".agentplay")
        let index = home.appendingPathComponent("index.json")
        while !Task.isCancelled {
            if let data = try? Data(contentsOf: index),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let sessions = obj["sessions"] as? [String: Any] {
                phase = .scanning(found: sessions.count)
            }
            try? await Task.sleep(for: .seconds(1.5))
        }
    }

    private func apply(_ s: Snapshot) {
        let now = Date()
        // Recorded activity heartbeat: only when an active run gained events since the last look.
        var fresh = false
        for r in s.runs {
            if let at = r.latestAt, r.state == .active {
                if let prev = lastEventSeen[r.sessionId], at > prev { fresh = true }
                lastEventSeen[r.sessionId] = at
            }
            // A run that was active and now is not: one quiet flourish on its replay button.
            if lastStates[r.sessionId] == .active, r.state != .active {
                justCompleted.insert(r.sessionId)
                let id = r.sessionId
                Task { try? await Task.sleep(for: .seconds(4)); await MainActor.run { _ = self.justCompleted.remove(id) } }
            }
            lastStates[r.sessionId] = r.state
        }
        snapshot = s
        lastUpdated = now
        lastError = nil
        phase = .ready
        if fresh { beat() }
        deliverAlerts(s)
    }

    private func beat() {
        heartbeatTask?.cancel()
        heartbeatTask = Task {
            // A few low-frequency frames, then stop. No constant bouncing.
            for _ in 0..<3 {
                heartbeat = true
                try? await Task.sleep(for: .milliseconds(700))
                heartbeat = false
                try? await Task.sleep(for: .milliseconds(900))
            }
        }
    }

    func loadSample(named name: String = "two-active") {
        guard let url = Bundle.main.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
                ?? devFixtureURL(name),
              let data = try? Data(contentsOf: url), let s = try? SnapshotDecoder.decode(data) else {
            toast = "The sample isn't bundled in this build."
            return
        }
        sampleMode = true
        let rebased = SnapshotDecoder.rebased(s, to: Date())
        snapshot = rebased
        lastUpdated = Date()
        phase = .ready
        for r in rebased.runs { lastStates[r.sessionId] = r.state }
    }

    func installSampleForRender(_ s: Snapshot) {
        sampleMode = true
        snapshot = s
        lastUpdated = s.generatedAt
        phase = .ready
    }

    /// Render hook: a real snapshot is not a sample.
    func markLive() { sampleMode = false }

    func setPhaseForRender(_ p: Phase) {
        phase = p
        if case .failed = p { snapshot = nil }
        if case .scanning = p { snapshot = nil }
    }

    func leaveSample() {
        sampleMode = false
        snapshot = nil
        phase = .starting
        Task { await refresh(.read) }
    }

    private func devFixtureURL(_ name: String) -> URL? {
        guard isDevBuild, let dir = ProcessInfo.processInfo.environment["AGENTPLAY_COMPANION_FIXTURES"] else { return nil }
        return URL(fileURLWithPath: dir).appendingPathComponent("\(name).json")
    }

    // MARK: watching

    enum RefreshReason { case fileEvent, timer, popoverOpened, wake, manual }

    private func startWatching() {
        let home = FileManager.default.homeDirectoryForCurrentUser
        var paths = [".claude/projects", ".codex/sessions"].map { home.appendingPathComponent($0).path }
        for (_, extra) in prefs.customFolders { paths += extra }
        watcher = FileWatcher(paths: paths.filter { FileManager.default.fileExists(atPath: $0) }) { [weak self] in
            Task { @MainActor in self?.scheduleRefresh(reason: .fileEvent) }
        }
        watcher?.start()
        resetFallbackTimer()
    }

    private func resetFallbackTimer() {
        fallback?.invalidate()
        let active = snapshot?.runs.contains { $0.state != .idle } ?? false
        // A safety net if file events are missed: every 2 minutes while runs are recent, 10 when idle.
        fallback = Timer.scheduledTimer(withTimeInterval: active ? 120 : 600, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.scheduleRefresh(reason: .timer) }
        }
    }

    func scheduleRefresh(reason: RefreshReason) {
        guard service != nil, !sampleMode, prefs.onboardingDone else { return }
        // A refresh costs a few seconds of CPU (see docs/companion/EXPERIMENTS.md), so it is rationed:
        // file events are debounced and spaced out, more tightly while the popover is open.
        let base: TimeInterval = switch reason {
        case .manual, .wake: 0
        case .popoverOpened: 10
        case .fileEvent, .timer: popoverOpen ? 20 : 60
        }
        // Never let the scan run more than about a fifth of the time: a big live transcript can take
        // 10 to 20 s to rebuild. Low Power Mode spaces it out three times further.
        var minGap = urgentReason(reason) ? 0 : max(base, 4 * lastRefreshDuration)
        if ProcessInfo.processInfo.isLowPowerModeEnabled { minGap *= 3 }
        let urgent = urgentReason(reason)
        // Opening the popover shows what is already built at once (a cheap read), then joins the queue.
        if reason == .popoverOpened { Task { await refresh(.read) } }
        // Coalesce: a refresh already queued will pick up these changes too. Later events must not push it
        // back, or an agent writing every second would starve it forever.
        if refreshQueued && !urgent { return }
        let wait = urgent ? 0 : max(reason == .fileEvent ? 4 : 0, minGap - Date().timeIntervalSince(lastRefreshStarted))
        companionLog.debug("schedule \(String(describing: reason), privacy: .public) wait=\(Int(wait), privacy: .public)s")
        debounce?.cancel()
        refreshQueued = true
        debounce = Task {
            if wait > 0 { try? await Task.sleep(for: .seconds(wait)) }
            guard !Task.isCancelled else { return }
            await refresh(.refresh)
            refreshQueued = false
            resetFallbackTimer()
        }
    }

    private func urgentReason(_ r: RefreshReason) -> Bool { r == .manual || r == .wake }

    private func observeSystem() {
        let ws = NSWorkspace.shared.notificationCenter
        ws.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            // After sleep, file times say nothing about what is running now: re-check processes at once.
            Task { @MainActor in self?.lastEventSeen = [:]; self?.scheduleRefresh(reason: .wake) }
        }
        ws.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.debounce?.cancel(); self?.refreshQueued = false }
        }
        NotificationCenter.default.addObserver(forName: .NSSystemTimeZoneDidChange, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.objectWillChange.send() }
        }
    }

    // MARK: actions

    func openViewer(sessionId: String? = nil, seq: Int? = nil, errorsTab: Bool = false) {
        playClick()
        if isSample {
            // The sample's sessions don't exist anywhere. Open the viewer's own bundled demo instead.
            openSampleViewer()
            return
        }
        guard let viewer else { toast = SnapshotError.runtimeMissing("AgentPlay runtime").localizedDescription; return }
        viewerBusy = true
        Task {
            defer { viewerBusy = false }
            do {
                let base = try await viewer.ensure()
                let url = sessionId.map { ViewerLink.url(base: base, sessionId: $0, seq: seq, errorsTab: errorsTab) } ?? base
                guard ViewerLink.isLocalViewer(url) else { return }
                NSWorkspace.shared.open(url)
            } catch {
                toast = "Couldn't start the local viewer. \((error as? SnapshotError)?.localizedDescription ?? "")"
            }
        }
    }

    private func openSampleViewer() {
        guard let viewer else { toast = "Sample replay needs the bundled viewer."; return }
        Task {
            if let base = try? await viewer.ensure(), var c = URLComponents(url: base, resolvingAgainstBaseURL: false) {
                c.query = "demo=1"
                if let u = c.url { NSWorkspace.shared.open(u) }
            }
        }
    }

    func replayLink(for run: RunActivity) async -> URL? {
        guard let viewer, let base = try? await viewer.ensure() else { return nil }
        return ViewerLink.url(base: base, sessionId: run.sessionId, seq: run.lastEvent?.seq)
    }

    func copySummary(_ run: RunActivity) {
        copy(Redacted.runSummary(run, now: Date()))
        toast = "Copied a redacted summary"
    }

    func copyLink(_ run: RunActivity) {
        Task {
            guard let url = await replayLink(for: run) else { toast = "Couldn't start the local viewer."; return }
            copy(url.absoluteString)
            toast = "Copied. The link works on this Mac only, while AgentPlay is open."
        }
    }

    private func copy(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
        playClick()
    }

    func muteRun(_ id: String) {
        prefs.alerts.mutedSessions.insert(id)
        toast = "Alerts muted for this run"
    }

    func mutePattern(_ key: String) {
        prefs.alerts.mutedPatterns.insert(key)
        toast = "Muted. Unmute in Settings → Notifications."
    }

    func togglePin(_ id: String) {
        if let i = prefs.pinnedSessions.firstIndex(of: id) { prefs.pinnedSessions.remove(at: i) }
        else { prefs.pinnedSessions.insert(id, at: 0); prefs.pinnedSessions = Array(prefs.pinnedSessions.prefix(5)) }
    }

    func dismissLearn(_ key: String) { prefs.dismissedLearn[key] = Date() }

    func snooze(_ seconds: TimeInterval?) {
        if let seconds { prefs.alerts.snoozeUntil = Date().addingTimeInterval(seconds) }
        else {
            // "Today": until local midnight.
            let cal = Calendar.current
            prefs.alerts.snoozeUntil = cal.startOfDay(for: Date()).addingTimeInterval(86400)
        }
    }

    func finishOnboarding() {
        prefs.onboardingDone = true
        startWatching()
        Task { await refresh(.read) }
    }

    func runFirstScan() async {
        await refresh(.fullScan)
    }

    func chooseFolder(for agent: String) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "Choose the folder where \(AgentNames.label(agent)) keeps its sessions."
        panel.prompt = "Use folder"
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        prefs.customFolders[agent, default: []].append(url.path)
        Task { await refresh(.fullScan) }
    }

    func removeFolder(_ path: String, agent: String) {
        prefs.customFolders[agent]?.removeAll { $0 == path }
    }

    // MARK: notifications

    func setPreset(_ p: AlertPreset, on: Bool) {
        if !on { prefs.alerts.enabled.remove(p); return }
        Task {
            // Permission is asked for here, when the user turns on a preset, and nowhere else.
            let granted = await Notifier.requestAuthorization()
            await refreshNotificationStatus()
            if granted { prefs.alerts.enabled.insert(p) }
            else { toast = "Notifications are off for AgentPlay in System Settings → Notifications." }
        }
    }

    func refreshNotificationStatus() async {
        notificationStatus = await Notifier.status()
    }

    private func deliverAlerts(_ s: Snapshot) {
        guard !prefs.alerts.enabled.isEmpty else { return }
        let ctx = AlertContext(now: Date(), timeZone: .current, isPresenting: Self.isPresenting())
        let (send, history) = AlertEngine.evaluate(s, settings: prefs.alerts, history: prefs.history, ctx: ctx)
        prefs.history = history
        for a in send { Notifier.post(a) }
    }

    /// A mirrored display usually means a projector or a shared screen.
    static func isPresenting() -> Bool {
        var count: UInt32 = 0
        guard CGGetActiveDisplayList(0, nil, &count) == .success, count > 0 else { return false }
        var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
        CGGetActiveDisplayList(count, &ids, &count)
        return ids.contains { CGDisplayMirrorsDisplay($0) != kCGNullDirectDisplay || CGDisplayIsInMirrorSet($0) != 0 }
    }

    func handleNotificationTap(sessionId: String?, seq: Int?, action: String, patternKey: String?) {
        if action == Notifier.muteAction, let patternKey { mutePattern(patternKey); return }
        openViewer(sessionId: sessionId, seq: seq)
    }

    func forgetAlertHistory() {
        prefs.history.purge(now: Date())
        Notifier.clearDelivered()
        toast = "Alert history cleared"
    }

    // MARK: login item

    func refreshLoginItem() {
        loginItemEnabled = SMAppService.mainApp.status == .enabled
    }

    func setLoginItem(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            toast = "macOS didn't allow that. Check System Settings → General → Login Items."
        }
        refreshLoginItem()
    }

    // MARK: sound

    func playClick() {
        guard prefs.soundOnManualActions else { return }
        let s = NSSound(named: NSSound.Name("Tink"))
        s?.volume = 0.25
        s?.play()
    }

    // MARK: derived

    /// Runs in display order: pinned first, then by latest activity.
    var orderedRuns: [RunActivity] {
        guard let runs = snapshot?.runs else { return [] }
        let pinned = prefs.pinnedSessions
        return runs.sorted { a, b in
            let pa = pinned.firstIndex(of: a.sessionId), pb = pinned.firstIndex(of: b.sessionId)
            if let pa, let pb { return pa < pb }
            if pa != nil { return true }
            if pb != nil { return false }
            return (a.latestAt ?? .distantPast) > (b.latestAt ?? .distantPast)
        }
    }

    var worthALook: AlertCandidate? {
        guard let s = snapshot else { return nil }
        let recent = Date().addingTimeInterval(-24 * 3600)
        return s.alerts
            .filter { !prefs.alerts.mutedPatterns.contains($0.patternKey) && !prefs.alerts.mutedSessions.contains($0.sessionId) }
            .filter { ($0.triggerAt ?? .distantPast) > recent }
            .sorted { ($0.isResolved ? 1 : 0, -( $0.triggerAt?.timeIntervalSince1970 ?? 0)) < ($1.isResolved ? 1 : 0, -($1.triggerAt?.timeIntervalSince1970 ?? 0)) }
            .first
    }

    var learnCard: LearnCard? {
        guard let l = snapshot?.learn, !prefs.isLearnDismissed(l.patternKey, now: Date()) else { return nil }
        return l
    }

    /// The one value the menu bar may show next to the mark, when the user allows it.
    var menuBarText: String? {
        guard prefs.showActivityInMenuBar, let s = snapshot, !isSample else { return nil }
        let unresolved = s.alerts.filter { !$0.isResolved && $0.rule == .repeatedFailure
            && ($0.triggerAt ?? .distantPast) > Date().addingTimeInterval(-3600)
            && !prefs.alerts.mutedPatterns.contains($0.patternKey) }.count
        if unresolved > 0 { return unresolved == 1 ? "1 needs a look" : "\(unresolved) need a look" }
        let active = s.activeRuns.count
        return active > 0 ? "\(active) running" : nil
    }

    func run(_ id: String) -> RunActivity? { snapshot?.runs.first { $0.sessionId == id } }
}

final class EnvBox: @unchecked Sendable {
    private let lock = NSLock()
    private var _value: [String: String] = [:]
    var value: [String: String] {
        get { lock.withLock { _value } }
        set { lock.withLock { _value = newValue } }
    }
}
