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
            if prefs.asksClaudeForSuggestions, !oldValue.asksClaudeForSuggestions { askClaude(force: true) }
        }
    }
    /// Which tab the menu shows: what's happening now, what to improve, or your setup.
    @Published var menuTab: MenuTab = .now
    /// True while Claude is being asked for suggestions.
    @Published private(set) var askingClaude = false
    /// When the app last asked, answered or not: a failing ask (logged out, offline) waits an hour, not one refresh.
    private var lastAskedClaude: Date?
    /// The extra environment for the core, readable from the background scan without touching the main actor.
    private let envBox = EnvBox()
    @Published var settingsPane: SettingsView.Pane = .general
    /// Which part of the Insights window is showing.
    @Published var insightsSection: InsightsSection = .fixes

    /// The Insights window, on one section.
    func showInsights(_ section: InsightsSection) {
        insightsSection = section
        Windows.show(.insights)
    }
    @Published private(set) var claudeBridgeOn = false
    let claudeBridge = ClaudeBridge()
    @Published var popoverOpen = false {
        didSet { if popoverOpen { scheduleRefresh(reason: .popoverOpened); refreshAccount(force: false); refreshIdentity() } }
    }
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
    private var sampleMode = false {
        didSet { claudeIdentity = isSample ? .sample : ClaudeAccount.readIdentity() }
    }
    private var started = false

    init(prefsStore: PrefsStore = PrefsStore(), isDevBuild: Bool) {
        self.prefsStore = prefsStore
        self.isDevBuild = isDevBuild
        self.prefs = prefsStore.load()
        envBox.value = prefs.folderEnvironment
    }

    var isSample: Bool { sampleMode || snapshot?.isSample == true }

    // MARK: film (dev builds only)

    /// Filming: a fixture shown as if live, with every read of this Mac switched off (no refresh, no live watch,
    /// no login, no account call). Nothing about the person filming can reach the screen.
    private(set) var filmMode = false

    /// The persona on the account card while filming.
    static let filmIdentity = ClaudeAccount.Identity(accountUuid: nil, email: "sam@example.com", organization: "Northwind", seatTier: nil)

    func startFilm(snapshot s: Snapshot) {
        filmMode = true
        started = true
        sampleMode = false
        snapshot = s
        lastUpdated = s.generatedAt
        phase = .ready
        claudeIdentity = Self.filmIdentity
        prefs.onboardingDone = true
        prefs.showActivityInMenuBar = false
        Task { await refreshNotificationStatus() }
    }

    /// Change what the fixture's live sessions say, so Pip's pose follows a script.
    func filmLive(_ live: [LiveSession]) { snapshot?.live = live }

    /// Put an event beside Pip, as a real one would land.
    func filmEvent(_ e: PipEvent?) {
        pipClear?.cancel()
        pipEvent = e
        pipRecent = e
    }

    /// An agent wrote something: Pip's typing frame moves.
    func filmTick() { activityTick &+= 1 }

    /// Claude's windows at these levels, resetting in `resetsIn` seconds.
    func filmLimits(session: Double, weekly: Double, resetsIn: TimeInterval) {
        guard var s = snapshot else { return }
        for i in s.limits.indices where s.limits[i].provider == "claude-code" {
            let pct = (s.limits[i].window == "five_hour" ? session : weekly) * 100
            s.limits[i].used.value?.usedPercent = pct
            s.limits[i].used.value?.leftPercent = 100 - pct
            s.limits[i].used.observedAt = Date()
            s.limits[i].resetsAt = Date().addingTimeInterval(s.limits[i].window == "five_hour" ? resetsIn : 3 * 86400)
        }
        snapshot = s
    }

    /// The first scan, with a counter that climbs while Pip reads.
    private func filmFirstScan() async {
        let s = snapshot
        let total = s?.sources.filter { $0.status == .found }.reduce(0) { $0 + $1.sessionCount } ?? 0
        snapshot = nil
        for n in stride(from: 0, through: total, by: max(1, total / 40)) {
            phase = .scanning(found: n)
            try? await Task.sleep(for: .milliseconds(45))
        }
        snapshot = s
        phase = .ready
    }
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
            // No store yet (a new Mac, a cleared ~/.agentplay): the first look is a full scan, not two days.
            let home = ProcessInfo.processInfo.environment["AGENTPLAY_HOME"].map { URL(fileURLWithPath: $0) }
                ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".agentplay")
            let hasStore = FileManager.default.fileExists(atPath: home.appendingPathComponent("index.json").path)
            Task {
                if hasStore {
                    await refresh(.read); await refresh(.refresh)
                } else {
                    phase = .scanning(found: nil)
                    await refresh(.fullScan)
                }
            }
            startWatching()
        }
        startLiveWatch()
        startAccountTimer()
        refreshAccount(force: true)
        refreshIdentity()
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
        guard !filmMode else { return }
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
            // ~/.claude/sessions may not have existed at launch.
            if sessionsWatcher == nil { startLiveWatch() }
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
        var s = s
        if accountIsFresh { s.limits = ClaudeAccount.merge(s.limits, account: accountLimits) }
        snapshot = s
        lastUpdated = Date()
        lastError = nil
        phase = .ready
        refreshLive()  // the records may be newer than the snapshot
        noticeSnapshotEvents(s)
        deliverAlerts(s)
        askClaude(force: false)
    }

    /// Suggestions from Claude: at most once a day, only when turned on, through the person's own Claude Code.
    /// The core keeps the last answer and skips the call when it's under a day old.
    func askClaude(force: Bool) {
        guard prefs.asksClaudeForSuggestions, !askingClaude, !filmMode, !sampleMode else { return }
        if !force, let at = snapshot?.coach?.generatedAt.flatMap(SnapshotDecoder.parseISO), Date().timeIntervalSince(at) < 20 * 3600 { return }
        if !force, let last = lastAskedClaude, Date().timeIntervalSince(last) < 3600 { return }
        lastAskedClaude = Date()
        askingClaude = true
        Task {
            defer { askingClaude = false }
            guard let claude = await Task.detached(operation: { ClaudeSwitch.claudeExecutable() }).value else {
                toast = "Claude Code isn't installed, so there's no one to ask."
                return
            }
            let r = await command(["coach", "--claude", claude] + (force ? ["--force"] : []), timeout: 180)
            if r.ok { await refresh(.read) } else if force { toast = r.message ?? "Claude didn't answer." }
        }
    }

    // MARK: Pip

    /// The event beside Pip right now, if any; a click acts on it.
    @Published private(set) var pipEvent: PipEvent?
    /// The last event, which colours Pip's pose for a moment after the line beside it has gone.
    @Published private(set) var pipRecent: PipEvent?
    /// Bumps when an agent writes: Pip's typing frame changes with real work, not on a timer.
    @Published private(set) var activityTick = 0
    private var seenAlerts: Set<String>?
    private var limitWasSpent = false
    private var pipClear: Task<Void, Never>?

    /// The pointer is over Pip in the menu bar: it looks up.
    @Published var pipPeek = false
    /// Which of Pip's suggestions the menu shows; clicking Pip moves to the next.
    @Published private(set) var tipIndex = 0
    /// True for a moment after Pip is clicked: a little hop.
    @Published private(set) var pipHop = false

    var tips: [Pip.Tip] { snapshot.map { Pip.tips($0, now: Date()) } ?? [] }
    var currentTip: Pip.Tip? { tips.isEmpty ? nil : tips[tipIndex % tips.count] }

    /// Pip hops: a click on it, or a switch of tabs.
    func hop() {
        pipHop = true
        Task { try? await Task.sleep(for: .milliseconds(350)); pipHop = false }
    }

    func nextTip() {
        tipIndex &+= 1
        pipHop = true
        Task { try? await Task.sleep(for: .milliseconds(350)); pipHop = false }
    }

    func perform(_ tip: Pip.Tip) {
        switch tip.action {
        case .goTo(let sid, let pid): goToSession(sessionId: sid, pid: pid, seq: nil)
        case .fix(let f): f.applicable ? applyFix(f) : openInsights()
        case .usagePage: if let u = URL(string: ClaudeAccount.usagePage) { NSWorkspace.shared.open(u) }
        case .usage: openViewer()
        case .insights: openInsights()
        case .none: break
        }
    }

    /// Render hook: put an event beside Pip.
    func setPipForRender(_ e: PipEvent?) { pipEvent = e; pipRecent = e }

    var pipPose: PipPose {
        Pip.pose(live: snapshot?.live ?? [], limits: snapshot?.limits ?? [], recent: pipRecent, now: Date())
    }

    private func announce(_ events: [PipEvent]) {
        let order: [PipEvent.Kind] = [.needsYou, .failed, .limit, .finished]
        guard let e = events.min(by: { order.firstIndex(of: $0.kind)! < order.firstIndex(of: $1.kind)! }), !isSample else { return }
        if let cur = pipEvent, order.firstIndex(of: cur.kind)! < order.firstIndex(of: e.kind)! { return }  // keep the more urgent one
        pipEvent = prefs.showsPipIsland ? e : nil
        pipRecent = e
        pipClear?.cancel()
        pipClear = Task { [weak self] in
            try? await Task.sleep(for: .seconds(8))
            guard !Task.isCancelled else { return }
            self?.pipEvent = nil
            try? await Task.sleep(for: .seconds(e.kind == .needsYou ? 0 : 12))
            guard !Task.isCancelled else { return }
            self?.pipRecent = nil
        }
    }

    /// A click on the line beside Pip.
    func act(on e: PipEvent) {
        pipEvent = nil
        switch e.kind {
        case .needsYou, .finished:
            if let sid = e.sessionId { goToSession(sessionId: sid, pid: e.pid, seq: e.seq) }
        case .failed:
            openViewer(sessionId: e.sessionId, seq: e.seq, errorsTab: true)
        case .limit:
            if let u = URL(string: ClaudeAccount.usagePage) { NSWorkspace.shared.open(u) }
        }
    }

    /// Alerts and spent windows, compared with the last look.
    private func noticeSnapshotEvents(_ s: Snapshot) {
        let repos = Dictionary(s.runs.compactMap { r in r.repo.map { (r.sessionId, $0) } }, uniquingKeysWith: { a, _ in a })
        var events = Pip.events(liveBefore: [], liveAfter: [], alertsBefore: seenAlerts, alerts: s.alerts, repos: repos)
        seenAlerts = Set(s.alerts.map(\.signature))
        let spent = s.limits.first { w in
            w.used.isAvailable && (w.used.value?.usedPercent ?? 0) >= 100 && (w.resetsAt.map { $0 > Date() } ?? false)
        }
        if let w = spent, !limitWasSpent {
            let reset = w.resetsAt.map { Format.clock($0) } ?? "soon"
            events.append(PipEvent(kind: .limit, text: "\(w.providerLabel) is back at \(reset)", label: "back \(reset)",
                                   sessionId: nil, pid: nil,
                                   seq: nil, id: "limit|\(w.id)|\(reset)"))
        }
        limitWasSpent = spent != nil
        announce(events)
    }

    // MARK: Claude account usage (opt-in)

    @Published private(set) var accountLimits: [ProviderLimitWindow] = []
    /// Who Claude Code is signed in as (local, from ~/.claude.json).
    @Published private(set) var claudeIdentity: ClaudeAccount.Identity?

    func refreshIdentity() {
        guard !filmMode else { return }
        let id = isSample ? .sample : ClaudeAccount.readIdentity()
        if id != claudeIdentity { claudeIdentity = id }
        guard !isSample, let uuid = id?.accountUuid else { return }
        if let saved = savedAccounts.first(where: { $0.accountUuid == uuid }) {
            // Claude Code rotated this account's login since we saved it: keep the saved copy current.
            if let mod = ClaudeSwitch.liveLoginModified(), mod > saved.savedAt.addingTimeInterval(5) { saveCurrentAccount(quiet: true) }
        } else if prefs.rememberClaudeAccounts == true {
            saveCurrentAccount(quiet: false)
        }
    }

    // MARK: Claude accounts (switching)

    /// Hidden with sample data, like the login itself.
    var savedAccounts: [ClaudeSwitch.Saved] { isSample ? [] : prefs.claudeAccounts ?? [] }
    @Published private(set) var switching = false

    private func upsert(_ s: ClaudeSwitch.Saved) {
        var list = prefs.claudeAccounts ?? []
        if let i = list.firstIndex(where: { $0.accountUuid == s.accountUuid }) {
            var merged = s
            merged.lastSession = s.lastSession ?? list[i].lastSession
            merged.lastWeekly = s.lastWeekly ?? list[i].lastWeekly
            merged.lastSeen = s.lastSeen ?? list[i].lastSeen
            list[i] = merged
        } else {
            list.append(s)
        }
        prefs.claudeAccounts = list
    }

    func saveCurrentAccount(quiet: Bool = false) {
        Task {
            let r = await Task.detached { ClaudeSwitch.saveCurrent() }.value
            switch r {
            case .success(let s):
                upsert(s)
                if !quiet { toast = "Saved \(s.title) for switching." }
            case .failure(let f):
                if !quiet { toast = f.message }
            }
        }
    }

    func switchAccount(to target: ClaudeSwitch.Saved) {
        guard !switching else { return }
        let busy = (snapshot?.live ?? []).contains { $0.status == .busy }
        let backups = (ProcessInfo.processInfo.environment["AGENTPLAY_HOME"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".agentplay")).appendingPathComponent("backups")
        switching = true
        Task {
            let r = await Task.detached { ClaudeSwitch.switchTo(target, liveBusy: busy, backups: backups) }.value
            switching = false
            switch r {
            case .success(let s):
                upsert(s)
                accountLimits = []
                refreshIdentity()
                refreshAccount(force: true)
                toast = "Switched to \(s.title). Open sessions follow within about 30 seconds."
            case .failure(let f):
                toast = f.message
            }
        }
    }

    @Published private(set) var addingAccount = false
    private var addTimer: Timer?

    /// One click: keep the account you're on, open Claude's sign-in, and save the new account when it appears.
    func addClaudeAccount() {
        guard !addingAccount else { toast = "Waiting for you to finish signing in."; return }
        let before = claudeIdentity?.accountUuid
        addingAccount = true
        Task {
            let saved = await Task.detached { ClaudeSwitch.saveCurrent() }.value
            switch saved {
            case .success(let s): upsert(s)
            case .failure(.notSignedIn): break  // nothing to keep yet
            case .failure(let f): addingAccount = false; toast = f.message; return
            }
            guard let claude = await Task.detached(operation: { ClaudeSwitch.claudeExecutable() }).value else {
                addingAccount = false
                toast = "Couldn't find the claude command. Run claude auth login in a terminal, and AgentPlay saves the account."
                watchForNewAccount(after: before)
                return
            }
            let home = ProcessInfo.processInfo.environment["AGENTPLAY_HOME"].map { URL(fileURLWithPath: $0) }
                ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".agentplay")
            let script = home.appendingPathComponent("add-claude-account.command")
            try? FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
            try? ClaudeSwitch.loginScript(claude: claude).write(to: script, atomically: true, encoding: .utf8)
            try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
            NSWorkspace.shared.open(script)
            toast = "Sign in with your other account in the window that opened. AgentPlay saves it when you're done."
            watchForNewAccount(after: before)
        }
    }

    /// Up to ten minutes, every two seconds: the moment Claude Code is signed in as someone new, save them.
    private func watchForNewAccount(after before: String?) {
        addingAccount = true
        addTimer?.invalidate()
        let start = Date()
        addTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] t in
            Task { @MainActor in
                guard let self else { t.invalidate(); return }
                let id = ClaudeAccount.readIdentity()
                if let uuid = id?.accountUuid, uuid != before {
                    t.invalidate()
                    self.addingAccount = false
                    self.claudeIdentity = id
                    let r = await Task.detached { ClaudeSwitch.saveCurrent() }.value
                    switch r {
                    case .success(let s):
                        self.upsert(s)
                        self.accountLimits = []
                        self.refreshAccount(force: true)
                        self.toast = "Added \(s.title). Switch between your accounts from the menu."
                    case .failure(let f):
                        self.toast = f.message
                    }
                } else if Date().timeIntervalSince(start) > 600 {
                    t.invalidate()
                    self.addingAccount = false
                }
            }
        }
    }

    /// The account leaves the list only once its login is out of the Keychain, so a refusal doesn't strand a login.
    func forgetAccount(_ s: ClaudeSwitch.Saved) {
        Task {
            let r = await Task.detached { ClaudeSwitch.forget(s.accountUuid) }.value
            switch r {
            case .success: prefs.claudeAccounts = (prefs.claudeAccounts ?? []).filter { $0.accountUuid != s.accountUuid }
            case .failure(let f): toast = f.message
            }
        }
    }
    @Published private(set) var accountFailure: ClaudeAccount.Failure?
    private var lastAccountFetch = Date.distantPast
    private var accountBackoffUntil = Date.distantPast
    private var accountTimer: Timer?
    private var accountFetching = false

    private var accountIsFresh: Bool {
        prefs.usesClaudeAccount && !accountLimits.isEmpty && Date().timeIntervalSince(lastAccountFetch) < 15 * 60
    }

    func setClaudeAccount(_ on: Bool) {
        prefs.claudeAccountUsage = on
        accountTimer?.invalidate()
        accountTimer = nil
        if on {
            startAccountTimer()
            refreshAccount(force: true)
        } else {
            accountLimits = []
            accountFailure = nil
            Task { await refresh(.read) }  // put back what the snapshot says about Claude
        }
    }

    private func startAccountTimer() {
        guard prefs.usesClaudeAccount, accountTimer == nil else { return }
        accountTimer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshAccount(force: false) }
        }
    }

    /// At most every few minutes, never while Anthropic has asked us to wait, never for sample data.
    func refreshAccount(force: Bool) {
        guard !filmMode else { return }
        let now = Date()
        guard prefs.usesClaudeAccount, !isSample, !accountFetching, !switching, now >= accountBackoffUntil else { return }
        guard force || now.timeIntervalSince(lastAccountFetch) >= 120 else { return }
        accountFetching = true
        let version = Self.claudeVersion()
        let account = ClaudeAccount.readIdentity()?.accountUuid
        Task {
            let result = await Task.detached { await ClaudeAccount.fetch(claudeVersion: version) }.value
            accountFetching = false
            // Switched or signed in to another account meanwhile: these numbers are the old one's. Ask again.
            // A failure (rate limit, Keychain denied) still applies below, so its backoff holds.
            if ClaudeAccount.readIdentity()?.accountUuid != account, case .success = result { refreshAccount(force: true); return }
            lastAccountFetch = Date()
            switch result {
            case .success(let rows):
                accountLimits = rows
                accountFailure = nil
                // Remember what this account had left, for the switch menu.
                if let uuid = claudeIdentity?.accountUuid, var s = savedAccounts.first(where: { $0.accountUuid == uuid }) {
                    s.lastSession = rows.first { $0.window == "five_hour" }?.used.value?.usedPercent
                    s.lastWeekly = rows.first { $0.window == "weekly" }?.used.value?.usedPercent
                    s.lastSeen = Date()
                    upsert(s)
                }
            case .failure(let f):
                accountFailure = f
                if case .rateLimited(let until) = f { accountBackoffUntil = until ?? Date().addingTimeInterval(600) }
                if case .keychainDenied = f { prefs.claudeAccountUsage = false; accountTimer?.invalidate(); accountTimer = nil }
            }
            if var s = snapshot {
                let base = s.limits.filter { $0.used.source != .officialProviderAPI }
                s.limits = accountIsFresh ? ClaudeAccount.merge(base, account: accountLimits) : base
                if s.limits != snapshot?.limits { snapshot = s }
            }
        }
    }

    /// Claude Code's version, from its own live records, for the request's User-Agent.
    nonisolated static func claudeVersion() -> String? {
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/sessions")
        guard let f = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil).first(where: { $0.pathExtension == "json" }),
              let d = try? Data(contentsOf: f), let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return nil }
        return o["version"] as? String
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
        guard !filmMode else { return }
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
        let busySince = Dictionary((lastLive ?? []).filter { $0.status == .busy }.map { ($0.sessionId, $0.statusSince) },
                                   uniquingKeysWith: { a, _ in a })
        let repos = Dictionary(s.runs.compactMap { r in r.repo.map { (r.sessionId, $0) } }, uniquingKeysWith: { a, _ in a })
        let events = Pip.events(liveBefore: lastLive, liveAfter: merged, alertsBefore: nil, alerts: [], repos: repos)
        announce(events)
        for e in events where e.kind == .finished {
            if let l = merged.first(where: { $0.sessionId == e.sessionId }) { notifyFinished(l, busySince: busySince[l.sessionId] ?? nil, runs: s.runs) }
        }
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

    private func notifyFinished(_ l: LiveSession, busySince: Date?, runs: [RunActivity]) {
        let a = prefs.alerts
        guard a.enabled.contains(.finished), !a.mutedSessions.contains(l.sessionId) else { return }
        let now = Date()
        if let until = a.snoozeUntil, until > now { return }
        if let q = a.quietHours, q.contains(now, timeZone: .current) { return }
        if a.silenceWhilePresenting, Self.isPresenting() { return }
        if let pid = l.pid, let app = TerminalFocus.app(for: pid), app.isActive { return }
        let run = runs.first { $0.sessionId == l.sessionId }
        guard let note = Redacted.finished(l, run: run, busySince: busySince, includeRepo: a.includeRepoInNotifications, now: now)
        else { return }
        Notifier.postFinished(note, session: l, seq: run?.lastEvent?.seq)
    }

    private func notifyNeedsYou(_ sessions: [LiveSession]) {
        let a = prefs.alerts
        guard a.enabled.contains(.needsYou), !sessions.isEmpty else { return }
        let now = Date()
        if let until = a.snoozeUntil, until > now { return }
        if let q = a.quietHours, q.contains(now, timeZone: .current) { return }
        if a.silenceWhilePresenting, Self.isPresenting() { return }
        for l in sessions where !a.mutedSessions.contains(l.sessionId) {
            // Already looking at it: its terminal is the frontmost app.
            if let pid = l.pid, let app = TerminalFocus.app(for: pid), app.isActive { continue }
            Notifier.postNeedsYou(l, includeRepo: a.includeRepoInNotifications)
        }
    }

    /// From a notification or a menu row: the session's terminal if there is one, else its replay.
    func goToSession(sessionId: String, pid: Int32?, seq: Int?) {
        guard !filmMode else { toast = "Brought Terminal forward."; return }
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
        guard !filmMode else { return }
        let home = FileManager.default.homeDirectoryForCurrentUser
        var paths = [".claude/projects", ".codex/sessions"].map { home.appendingPathComponent($0).path }
        for (_, extra) in prefs.customFolders { paths += extra }
        watcher = FileWatcher(paths: paths.filter { FileManager.default.fileExists(atPath: $0) }) { [weak self] in
            Task { @MainActor in
                self?.activityTick &+= 1  // an agent wrote: Pip types
                self?.scheduleRefresh(reason: .fileEvent)
            }
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
        guard !filmMode else { return }
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

    // MARK: fixes and Autotune, from the menu

    private var storeHome: URL {
        ProcessInfo.processInfo.environment["AGENTPLAY_HOME"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".agentplay")
    }

    private func command(_ args: [String], timeout: TimeInterval = 60) async -> CompanionCommand.Reply {
        if filmMode { return Self.filmReply(args) }
        guard let runtime else { return .init(ok: false, message: "The AgentPlay runtime is missing.", fields: [:]) }
        return await CompanionCommand.run(runtime, args, extraEnv: envBox.value, timeout: timeout)
    }

    /// What the core would answer while filming: the preview and the write of the one fix the script applies.
    private static func filmReply(_ args: [String]) -> CompanionCommand.Reply {
        switch args.first {
        case "fix" where args.dropFirst().first == "preview":
            return .init(ok: true, message: nil, fields: [
                "repo": "payments-api", "file": ".claude/settings.json", "granted": "false", "root": "~/code/payments-api",
                "before": "", "after": "\"SessionStart\": [{\"matcher\": \"startup\", \"hooks\": [\n  {\"type\": \"command\", \"command\": \"scripts/agent-env.sh\"}\n]}]"])
        case "fix":
            return .init(ok: true, message: nil, fields: ["file": ".claude/settings.json"])
        default:
            return .init(ok: true, message: nil, fields: [:])
        }
    }

    /// A write changed what the insights should show: drop their 15-minute cache and look again.
    private func afterWrite() {
        guard !filmMode else { return }
        try? FileManager.default.removeItem(at: storeHome.appendingPathComponent("cache/companion-insights.json"))
        Task { await refresh(.read) }
    }

    /// The preview: what will be written, where, the first-write notice, and the exact lines.
    static func fixAlert(title: String, repo: String, file: String, granted: Bool, root: String?, added: [String],
                         removed: [String] = [], remedy: String? = nil) -> NSAlert {
        let a = NSAlert()
        a.messageText = "Apply \u{201C}\(Plain.text(title))\u{201D}?"
        var info = remedy.map { $0.replacingOccurrences(of: "Apply ", with: "This ") } ?? "Writes to \(file) in \(repo)."
        if !granted {
            let what = repo == "~/.claude" ? "Claude Code's settings.json there" : "CLAUDE.md and .claude/settings there"
            info += "\n\nAgentPlay hasn't written to \(repo) before. Applying lets it edit \(what)"
                + (root.map { " (\($0))" } ?? "") + ". Every change is journaled and can be undone from Run Health."
        }
        a.informativeText = info
        if !added.isEmpty || !removed.isEmpty {
            let text = NSTextView(frame: NSRect(x: 0, y: 0, width: 420, height: 120))
            text.string = (removed.map { "- " + $0 } + added.map { "+ " + $0 }).joined(separator: "\n")
            text.isEditable = false
            text.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
            let scroll = NSScrollView(frame: text.frame)
            scroll.documentView = text
            scroll.hasVerticalScroller = true
            a.accessoryView = scroll
        }
        a.addButton(withTitle: "Apply")
        a.addButton(withTitle: "Cancel")
        return a
    }

    /// Preview the exact change, ask once per repo, then write it through the guarded, journaled writer.
    func applyFix(_ f: Insights.Fix) {
        Task {
            let p = await command(["fix", "preview", f.id])
            guard p.ok else { toast = p.message ?? "Couldn't preview that fix."; return }
            let repo = p.fields["repo"] ?? "this repo", file = p.fields["file"] ?? "a file"
            let granted = p.fields["granted"] == "true"
            let beforeLines = (p.fields["before"] ?? "").components(separatedBy: "\n")
            let afterLines = (p.fields["after"] ?? "").components(separatedBy: "\n")
            let before = Set(beforeLines), after = Set(afterLines)
            let added = afterLines.filter { !$0.isEmpty && !before.contains($0) }
            let removed = beforeLines.filter { !$0.isEmpty && !after.contains($0) }
            NSApp.activate()
            let a = Self.fixAlert(title: f.title, repo: repo, file: file, granted: granted, root: p.fields["root"],
                                  added: added, removed: removed, remedy: f.hook?.remedy)
            let response: NSApplication.ModalResponse
            if filmMode, let host = Film.alertHost(for: a) {
                // Filming drives the pointer from the main actor, which a modal loop would block: a sheet instead.
                response = await withCheckedContinuation { c in a.beginSheetModal(for: host) { c.resume(returning: $0) } }
                host.orderOut(nil)
            } else {
                response = a.runModal()
            }
            guard response == .alertFirstButtonReturn else { return }
            let r = await command(["fix", "apply", f.id] + (granted ? [] : ["--grant"]))
            toast = r.ok ? "Applied to \(r.fields["file"] ?? file). Undo it from Run Health any time." : (r.message ?? "Couldn't apply it.")
            if r.ok { afterWrite() }
        }
    }

    /// Turn a skill, MCP server, subagent or hook off or back on, after saying exactly what changes and where.
    func switchSetup(_ it: SetupReport.Item, on: Bool) {
        if !on {
            let a = NSAlert()
            a.messageText = "Turn off \(it.name)?"
            a.informativeText = Self.switchExplainer(it)
            a.addButton(withTitle: "Turn Off")
            a.addButton(withTitle: "Cancel")
            if ["collab-guard", "validator"].contains(where: { (it.detail ?? it.name).contains($0) }) {
                a.alertStyle = .critical
                a.informativeText += "\n\nThis script is part of how you work with several sessions at once and the validator loop. Turning it off stops that until you turn it back on."
            }
            NSApp.activate(ignoringOtherApps: true)
            guard a.runModal() == .alertFirstButtonReturn else { return }
        }
        Task {
            var args = ["setup", on ? "on" : "off", it.kind, it.name]
            if it.source != "you" { args += ["--source", it.source] }
            if let s = it.detail { args += ["--script", s] }
            let r = await command(args)
            toast = r.ok ? (r.fields["done"] ?? "Done.") : (r.message ?? "Couldn't change it.")
            if r.ok { afterWrite() }
        }
    }

    static func switchExplainer(_ it: SetupReport.Item) -> String {
        switch it.kind {
        case "skill": return "Claude stops seeing it and it leaves the / menu: AgentPlay sets skillOverrides to \"off\" for it in ~/.claude/settings.json, the same switch /skills uses. Turn it back on any time from Tools."
        case "mcp": return "Claude Code stops connecting to it in new sessions: AgentPlay adds it to deniedMcpServers in ~/.claude/settings.json. Its setup and login stay as they are. Turn it back on any time from Tools."
        case "agent": return "Claude stops being able to start it: AgentPlay moves its file from ~/.claude/agents to ~/.agentplay/set-aside/agents. Claude Code has no switch for subagents, so this is the only way. Turn it back on any time from Tools."
        default: return "It stops running \(HooksCard.moment(it.name).lowercased()): AgentPlay takes its entry out of hooks in ~/.claude/settings.json and keeps it, to put back exactly when you turn it on again."
        }
    }

    func undoApplied(_ e: Insights.Applied) {
        Task {
            let r = await command(["fix", "undo", e.id])
            toast = r.ok ? "Undone in \(e.file ?? "the file")." : (r.message ?? "Couldn't undo it.")
            if r.ok { afterWrite() }
        }
    }

    func autotune(_ action: String, _ p: Insights.Autotune.Pending) {
        Task {
            let r = await command(["autotune", action, p.fixId])
            toast = r.ok ? (action == "approve" ? "Approved and applied." : "Dismissed.") : (r.message ?? "That didn't work.")
            if r.ok { afterWrite() }
        }
    }

    func reveal(_ path: String) {
        let url = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    /// The viewer's Run health page, where fixes are previewed, applied and undone.
    func openInsights(page: String = "health") {
        guard !filmMode else { return }
        guard !isSample, let viewer else { openViewer(); return }
        Task {
            guard let base = try? await viewer.ensure() else { toast = "Couldn't start the local viewer."; return }
            let url = ViewerLink.insights(base: base, page: page)
            if ViewerLink.isLocalViewer(url) { NSWorkspace.shared.open(url) }
        }
    }

    func copyText(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
        toast = "Copied"
    }

    func hideLearn(_ key: String) {
        prefs.dismissedLearn[key] = Date()
        toast = "Hidden for two weeks."
    }

    func openViewer(sessionId: String? = nil, seq: Int? = nil, errorsTab: Bool = false) {
        guard !filmMode else { return }
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
        guard !filmMode else { return }
        startWatching()
        Task { await refresh(.read) }
    }

    func runFirstScan() async {
        if filmMode { await filmFirstScan(); return }
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

    /// The system prompt, from a button the user pressed. Nothing is delivered until a preset is on.
    func requestNotifications() {
        Task {
            _ = await Notifier.requestAuthorization()
            await refreshNotificationStatus()
        }
    }

    /// A test notification, asking permission first if macOS hasn't been asked yet.
    func sendTestNotification() {
        Task {
            if notificationStatus == .notDetermined { _ = await Notifier.requestAuthorization() }
            await refreshNotificationStatus()
            guard notificationStatus != .denied else { return }
            Notifier.postTest()
            toast = "Sent. If nothing showed, check System Settings → Notifications."
        }
    }

    func openNotificationSettings() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension")!)
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
        if action == Notifier.snoozeAction { snooze(15 * 60); return }
        if action == Notifier.replayAction { openViewer(sessionId: sessionId, seq: seq); return }
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

    func menuEntries(now: Date, trim: Int = 0) -> [MenuEntry] {
        MenuBuilder.entries(snapshot: snapshot, phase: phase, prefs: prefs, lastError: lastError,
                            isSample: isSample, now: now, bridgeOn: claudeBridgeOn, trim: trim, tab: menuTab,
                            askingClaude: askingClaude)
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
