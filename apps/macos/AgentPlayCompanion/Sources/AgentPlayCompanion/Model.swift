import AgentPlayCore
import AppKit
import Combine
import CoreGraphics
import ServiceManagement
import SwiftUI
import UserNotifications
import os

let companionLog = Logger(subsystem: "ai.entelligence.agentplay.companion", category: "refresh")

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
    @Published var settingsPane: SettingsView.Pane = .general
    @Published private(set) var claudeBridgeOn = false
    let claudeBridge = ClaudeBridge()
    @Published var popoverOpen = false { didSet { if popoverOpen { scheduleRefresh(reason: .popoverOpened) } } }
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
        refreshBridgeState()
        refreshBridgeScript()
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
        startLiveWatch()
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
        snapshot = s
        lastUpdated = Date()
        lastError = nil
        phase = .ready
        refreshLive()  // the records may be newer than the snapshot
        deliverAlerts(s)
    }

    // MARK: live sessions

    private var sessionsWatcher: FileWatcher?
    /// The live list as last shown; nil until the first look, so a session already waiting at launch isn't news.
    private var lastLive: [LiveSession]?
    private var claudeSessionsDir: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/sessions")
    }

    /// Claude Code rewrites `~/.claude/sessions/<pid>.json` when a session starts or stops waiting. Watching that
    /// folder alone, with a short latency, is what makes "needs you" arrive in about a second.
    private func startLiveWatch() {
        let dir = claudeSessionsDir
        guard FileManager.default.fileExists(atPath: dir.path) else { return }
        sessionsWatcher = FileWatcher(paths: [dir.path], latency: 0.3) { [weak self] in
            Task { @MainActor in self?.refreshLive() }
        }
        sessionsWatcher?.start()
    }

    func refreshLive() {
        guard var s = snapshot, !isSample else { return }
        let merged = LiveRecords.merge(snapshot: s.live, records: LiveRecords.read(dir: claudeSessionsDir))
        let news = LiveRecords.newlyWaiting(before: lastLive, after: merged)
        let stillWaiting = Set(merged.filter { $0.status == .waiting }.map(\.sessionId))
        for old in lastLive ?? [] where old.status == .waiting && !stillWaiting.contains(old.sessionId) {
            Notifier.clearNeedsYou(sessionId: old.sessionId)  // answered: the banner shouldn't linger
        }
        lastLive = merged
        if merged != (s.live ?? []) {
            s.live = merged
            snapshot = s
        }
        notifyNeedsYou(news)
    }

    private func notifyNeedsYou(_ sessions: [LiveSession]) {
        let a = prefs.alerts
        guard a.enabled.contains(.needsYou), !sessions.isEmpty else { return }
        let now = Date()
        if let until = a.snoozeUntil, until > now { return }
        if let q = a.quietHours, q.contains(now, timeZone: .current) { return }
        if Self.isPresenting() { return }
        for l in sessions where !a.mutedSessions.contains(l.sessionId) {
            // Already looking at it: its terminal is the frontmost app.
            if let pid = l.pid, let app = TerminalFocus.app(for: pid), app.isActive { continue }
            Notifier.postNeedsYou(l, includeRepo: a.includeRepoInNotifications)
        }
    }

    /// From a notification or a menu row: the session's terminal if there is one, else its replay.
    func goToSession(sessionId: String, pid: Int32?, seq: Int?) {
        if let pid, TerminalFocus.bringForward(pid: pid) { return }
        openViewer(sessionId: sessionId, seq: seq)
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
        Task {
            await refresh(.read)
            if prefs.onboardingDone { scheduleRefresh(reason: .manual) }
        }
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
            Task { @MainActor in self?.scheduleRefresh(reason: .wake) }
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

    func muteRun(_ id: String) {
        prefs.alerts.mutedSessions.insert(id)
        toast = "Alerts muted for this session"
    }

    func mutePattern(_ key: String) {
        prefs.alerts.mutedPatterns.insert(key)
        toast = "Muted. Unmute in Settings → Notifications."
    }

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

    func handleNotificationTap(sessionId: String?, seq: Int?, action: String, patternKey: String?, pid: Int32? = nil) {
        if action == Notifier.muteAction, let patternKey { mutePattern(patternKey); return }
        if action == Notifier.muteSessionAction, let sessionId { muteRun(sessionId); return }
        if let sessionId, pid != nil { goToSession(sessionId: sessionId, pid: pid, seq: seq); return }
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

    // MARK: menu

    func menuEntries(now: Date) -> [MenuEntry] {
        MenuBuilder.entries(snapshot: snapshot, phase: phase, prefs: prefs, lastError: lastError,
                            isSample: isSample, now: now, bridgeOn: claudeBridgeOn)
    }

    func refreshBridgeState() {
        claudeBridgeOn = claudeBridge.state() == .on
    }

    /// An installed bridge gets this build's script (fixes in it reach people who turned it on earlier).
    private func refreshBridgeScript() {
        guard let url = Bundle.main.url(forResource: "claude-statusline-bridge", withExtension: "sh"),
              let script = try? String(contentsOf: url, encoding: .utf8) else { return }
        claudeBridge.refreshScript(script)
    }

    func enableClaudeBridge() {
        guard let url = Bundle.main.url(forResource: "claude-statusline-bridge", withExtension: "sh"),
              let script = try? String(contentsOf: url, encoding: .utf8) else {
            toast = "The bridge script isn't in this build."; return
        }
        do {
            try claudeBridge.install(script: script)
            toast = "Live Claude limits on. They appear after Claude Code's next reply."
        } catch {
            toast = (error as? LocalizedError)?.errorDescription ?? "Couldn't change Claude Code's settings."
        }
        refreshBridgeState()
    }

    func disableClaudeBridge() {
        do {
            try claudeBridge.uninstall()
            toast = "Live Claude limits off. Your previous status line is back."
        } catch {
            toast = (error as? LocalizedError)?.errorDescription ?? "Couldn't change Claude Code's settings."
        }
        refreshBridgeState()
    }

    /// What the status item draws: the tightest provider's session and weekly fractions.
    var statusBars: (session: Double?, weekly: Double?, stale: Bool) {
        guard let s = snapshot, !isSample else { return (nil, nil, false) }
        guard let p = ProviderSummary.build(s.limits).first(where: { !$0.windows.isEmpty }) else { return (nil, nil, false) }
        let five = p.window("five_hour"), week = p.window("weekly")
        let now = Date()
        let stale = [five, week].compactMap { $0 }.allSatisfy {
            $0.used.freshness == .stale || ($0.resetsAt.map { $0 <= now } ?? false) || $0.used.isStale(now: now, maxAge: 1800)
        }
        return (five?.used.value.map { $0.usedPercent / 100 }, week?.used.value.map { $0.usedPercent / 100 }, stale)
    }

    var hasWaiting: Bool { snapshot?.live?.contains { $0.status == .waiting } == true }

    /// When every live Claude session was started from an editor's agent panel: that editor's name.
    var claudeEditorOnly: String? {
        let claude = (snapshot?.live ?? []).filter { $0.agentTool == "claude-code" }
        guard !claude.isEmpty, claude.allSatisfy(\.isSDK) else { return nil }
        return claude.lazy.compactMap { $0.pid.flatMap { TerminalFocus.app(for: $0)?.localizedName } }.first ?? "an editor"
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
