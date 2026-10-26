import AgentPlayCore
import AppKit
import SwiftUI
import UserNotifications

struct SettingsView: View {
    @EnvironmentObject var model: CompanionModel
    var initialPane: Pane? = nil

    enum Pane: String, CaseIterable, Identifiable {
        case general = "General", claude = "Live Limits", sources = "Sources", notifications = "Notifications",
             privacy = "Privacy", account = "Account", diagnostics = "Diagnostics"
        var id: String { rawValue }
        var icon: String {
            switch self {
            case .claude: "gauge.with.dots.needle.33percent"
            case .general: "gearshape"
            case .sources: "folder"
            case .notifications: "bell"
            case .privacy: "hand.raised"
            case .account: "person.crop.circle"
            case .diagnostics: "stethoscope"
            }
        }
    }

    /// Renders use one pane at a time; the window itself is SettingsWindow.
    var body: some View {
        SettingsPaneView(pane: initialPane ?? model.settingsPane)
            .onAppear { model.refreshBridgeState() }
    }
}

/// One pane as a grouped form, the content of one toolbar tab.
struct SettingsPaneView: View {
    @EnvironmentObject var model: CompanionModel
    var pane: SettingsView.Pane

    var body: some View {
        Form {
            switch pane {
            case .general: GeneralPane()
            case .claude: LiveLimitsPane()
            case .sources: SourcesPane()
            case .notifications: NotificationsPane()
            case .privacy: PrivacyPane()
            case .account: AccountPane()
            case .diagnostics: DiagnosticsPane()
            }
        }
        .formStyle(.grouped)
        .frame(width: 560, height: 480)
    }
}

/// The standard Mac settings window: toolbar tabs, the window titled after the pane, last pane remembered.
@MainActor
enum SettingsWindow {
    static func make(model: CompanionModel) -> NSWindow {
        let tabs = SettingsTabs()
        tabs.model = model
        tabs.tabStyle = .toolbar
        tabs.transitionOptions = []
        for pane in SettingsView.Pane.allCases {
            let hc = NSHostingController(rootView: AnyView(SettingsPaneView(pane: pane).environmentObject(model)))
            hc.title = pane.rawValue
            hc.sizingOptions = []
            hc.preferredContentSize = NSSize(width: 560, height: 480)
            let item = NSTabViewItem(viewController: hc)
            item.label = pane.rawValue
            item.image = NSImage(systemSymbolName: pane.icon, accessibilityDescription: pane.rawValue)
            tabs.addTabViewItem(item)
        }
        tabs.selectedTabViewItemIndex = SettingsView.Pane.allCases.firstIndex(of: model.settingsPane) ?? 0
        let w = NSWindow(contentViewController: tabs)
        w.styleMask = [.titled, .closable]
        w.toolbarStyle = .preference
        return w
    }

    /// Keeps the model's pane in step with the tab the user picked, so reopening lands there.
    final class SettingsTabs: NSTabViewController {
        weak var model: CompanionModel?
        override func tabView(_ tabView: NSTabView, didSelect item: NSTabViewItem?) {
            super.tabView(tabView, didSelect: item)
            if let item, let i = tabView.tabViewItems.firstIndex(of: item), i < SettingsView.Pane.allCases.count {
                MainActor.assumeIsolated { model?.settingsPane = SettingsView.Pane.allCases[i] }
            }
        }
    }

    static func select(_ pane: SettingsView.Pane, in w: NSWindow) {
        guard let tabs = w.contentViewController as? NSTabViewController,
              let i = SettingsView.Pane.allCases.firstIndex(of: pane) else { return }
        tabs.selectedTabViewItemIndex = i
    }
}

struct GeneralPane: View {
    @EnvironmentObject var model: CompanionModel
    var body: some View {
        Section("In the menu bar") {
            // Each switch shows the thing it switches, drawn with the real icon code.
            Toggle(isOn: Binding(get: { model.prefs.showsPip }, set: { model.prefs.pipEnabled = $0 })) {
                row("Pip in the menu bar",
                    "Fills with your 5-hour usage and stands on the week; its pose says what's up. Off shows two plain bars.",
                    preview: HStack(spacing: 6) {
                        MenuBarStrip(pip: true)
                        Text("or").font(.caption).foregroundStyle(.tertiary)
                        MenuBarStrip(pip: false)
                    })
            }
            Toggle(isOn: $model.prefs.showActivityInMenuBar) {
                row("Show session % next to the icon",
                    "How much of the 5-hour window is used, from your most constrained provider.",
                    preview: MenuBarStrip(pip: model.prefs.showsPip, text: "34%"))
            }
            Toggle(isOn: Binding(get: { model.prefs.showsPipIsland }, set: { model.prefs.pipIslandEnabled = $0 })) {
                row("Say what happened next to the icon",
                    "The project's name for a few seconds when something changes; click it to go there.",
                    preview: MenuBarStrip(pip: model.prefs.showsPip, pose: .wave, text: "docs-site"))
            }
        }
        Section {
            Toggle("Launch at login", isOn: Binding(get: { model.loginItemEnabled }, set: { model.setLoginItem($0) }))
            Toggle(isOn: $model.prefs.soundOnManualActions) {
                Text("Soft sound on actions I take")
                Text("Never for background events.")
            }
        }
        if model.isDevBuild {
            Section("Developer") { Button("Load Sample Data") { model.loadSample() } }
        }
    }

    func row<P: View>(_ title: String, _ detail: String, preview: P) -> some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                Text(detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            preview
        }
    }
}

/// A slice of menu bar with the status item as it would draw: Pip (or the two bars) and the line beside it.
struct MenuBarStrip: View {
    var pip = true
    var pose: PipPose = .work
    var text: String? = nil
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        HStack(spacing: 4) {
            Image(nsImage: pip ? PipIcon.image(pose, session: 0.34, weekly: 0.58)
                               : StatusIcon.image(session: 0.34, weekly: 0.58, stale: false, waiting: false, pulse: false))
                .renderingMode(.template).interpolation(.none)
            if let text { Text(text).font(.system(size: 12, weight: .medium)) }
        }
        .padding(.horizontal, 8)
        .frame(height: 24)
        .foregroundStyle(.primary)
        .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(scheme == .dark ? Color.white.opacity(0.12) : Color.black.opacity(0.08)))
        .accessibilityLabel(text.map { "Menu bar preview: \($0)" } ?? "Menu bar preview")
    }
}

struct SourcesPane: View {
    @EnvironmentObject var model: CompanionModel
    var body: some View {
        Section("Agent sources") {
            if let s = model.snapshot, !model.isSample {
                ForEach(s.sources) { src in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(src.label)
                            Spacer()
                            Text(src.status == .found ? "\(src.sessionCount) session\(src.sessionCount == 1 ? "" : "s")" : src.status == .error ? "Can't read" : "Not found")
                                .foregroundStyle(src.status == .error ? .red : .secondary)
                            Button("Add folder…") { model.chooseFolder(for: src.agent) }
                        }
                        if !src.paths.isEmpty { Text(src.paths.joined(separator: ", ")).font(.caption.monospaced()).foregroundStyle(.secondary) }
                        if let r = src.remedy { Text(r).font(.caption).foregroundStyle(.red) }
                        ForEach(model.prefs.customFolders[src.agent] ?? [], id: \.self) { path in
                            HStack {
                                Text(path).font(.caption.monospaced())
                                Button("Remove") { model.removeFolder(path, agent: src.agent) }.buttonStyle(.link)
                            }
                        }
                    }
                }
            } else {
                Text(model.isSample ? "Showing sample data. Your sources appear once you leave the sample." : "Not scanned yet.")
                    .foregroundStyle(.secondary)
            }
        }
        Section {
            Button("Rescan everything") { Task { await model.refresh(.fullScan) } }
            Text("Scans only the folders above. A source that can't be read stays listed with what to fix; it is never dropped silently.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}

struct NotificationsPane: View {
    @EnvironmentObject var model: CompanionModel
    @State private var quietOn = false
    @State private var quietStart = Calendar.current.date(bySettingHour: 22, minute: 0, second: 0, of: Date())!
    @State private var quietEnd = Calendar.current.date(bySettingHour: 7, minute: 0, second: 0, of: Date())!

    var body: some View {
        Section {
            // Where things stand with macOS, and a way to see one, before any preset is on.
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(permissionTitle)
                    Text("Local notifications only: no server, no push service. Nothing is sent until a preset below is on.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 12)
                switch model.notificationStatus {
                case .denied: Button("Open System Settings") { model.openNotificationSettings() }
                case .notDetermined: Button("Allow…") { model.requestNotifications() }
                default: Button("Send a Test Notification") { model.sendTestNotification() }
                }
            }
            ForEach(AlertPreset.allCases) { p in
                VStack(alignment: .leading, spacing: 2) {
                    Toggle(p.title, isOn: Binding(get: { model.prefs.alerts.enabled.contains(p) },
                                                  set: { model.setPreset(p, on: $0) }))
                    Text(p.detail).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        Section("Thresholds") {
            Picker("Context near full at", selection: $model.prefs.alerts.contextThreshold) {
                Text("80%").tag(80.0); Text("90%").tag(90.0)
            }
            Picker("Allowance low at", selection: $model.prefs.alerts.allowanceThresholdLeft) {
                Text("20% left").tag(20.0); Text("10% left").tag(10.0)
            }
            Stepper("Long-running tool after \(model.prefs.alerts.longToolMinutes) min",
                    value: $model.prefs.alerts.longToolMinutes, in: 2...120, step: 1)
            Stepper("At most \(model.prefs.alerts.maxPerHour) per hour", value: $model.prefs.alerts.maxPerHour, in: 1...10)
        }
        Section("Quiet") {
            Toggle("Quiet hours", isOn: $quietOn)
            if quietOn {
                DatePicker("From", selection: $quietStart, displayedComponents: .hourAndMinute)
                DatePicker("To", selection: $quietEnd, displayedComponents: .hourAndMinute)
                Text("In this Mac's time zone (\(TimeZone.current.identifier)), so travel moves them with you.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Toggle("Silent while a display is mirrored (presenting)", isOn: $model.prefs.alerts.silenceWhilePresenting)
            Toggle("Show repo names in notifications", isOn: $model.prefs.alerts.includeRepoInNotifications)
            HStack {
                if let until = model.prefs.alerts.snoozeUntil, until > Date() {
                    Text("Snoozed until \(Format.clock(until))")
                    Button("Resume") { model.prefs.alerts.snoozeUntil = nil }
                } else {
                    Button("Snooze 1 hour") { model.snooze(3600) }
                    Button("Snooze for today") { model.snooze(nil) }
                }
            }
        }
        .onAppear(perform: loadQuiet)
        .onChange(of: quietOn) { saveQuiet() }
        .onChange(of: quietStart) { saveQuiet() }
        .onChange(of: quietEnd) { saveQuiet() }
        Section("Muted") {
            if model.prefs.alerts.mutedPatterns.isEmpty && model.prefs.alerts.mutedSessions.isEmpty {
                Text("Nothing muted.").foregroundStyle(.secondary)
            }
            if !model.prefs.alerts.mutedPatterns.isEmpty {
                HStack {
                    Text("\(model.prefs.alerts.mutedPatterns.count) muted pattern(s)")
                    Spacer()
                    Button("Unmute all") { model.prefs.alerts.mutedPatterns = [] }
                }
            }
            if !model.prefs.alerts.mutedSessions.isEmpty {
                HStack {
                    Text("\(model.prefs.alerts.mutedSessions.count) muted run(s)")
                    Spacer()
                    Button("Unmute all") { model.prefs.alerts.mutedSessions = [] }
                }
            }
        }
        Section("History") {
            Text("Kept locally: \(model.prefs.history.delivered.count) alert ids and times, nothing about their content.")
                .foregroundStyle(.secondary)
            Button("Forget alert history") { model.forgetAlertHistory() }
            Text("To turn notifications off entirely, switch every preset off, or turn AgentPlay off in System Settings → Notifications.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .task { await model.refreshNotificationStatus() }
    }

    var permissionTitle: String {
        switch model.notificationStatus {
        case .authorized, .provisional: "macOS allows notifications from AgentPlay"
        case .denied: "macOS has notifications off for AgentPlay"
        default: "macOS hasn't been asked yet"
        }
    }

    private func loadQuiet() {
        guard let q = model.prefs.alerts.quietHours else { return }
        quietOn = true
        let cal = Calendar.current, day = cal.startOfDay(for: Date())
        quietStart = day.addingTimeInterval(TimeInterval(q.start * 60))
        quietEnd = day.addingTimeInterval(TimeInterval(q.end * 60))
    }

    private func saveQuiet() {
        guard quietOn else { model.prefs.alerts.quietHours = nil; return }
        let cal = Calendar.current
        func m(_ d: Date) -> Int { let c = cal.dateComponents([.hour, .minute], from: d); return (c.hour ?? 0) * 60 + (c.minute ?? 0) }
        model.prefs.alerts.quietHours = QuietHours(start: m(quietStart), end: m(quietEnd))
    }
}

struct PrivacyPane: View {
    @EnvironmentObject var model: CompanionModel
    var body: some View {
        Section("What the companion reads") {
            Text("AgentPlay's local maps and index in ~/.agentplay, built from the folders under Sources. For Codex, the numeric usage record Codex writes into its own session files.")
            Text("Running agents are detected with lsof: which process has a transcript open, or runs in its project folder.")
                .foregroundStyle(.secondary)
        }
        Section("What it never shows or sends") {
            Text("Prompt text, session titles, assistant text, file contents, command arguments, tool inputs and outputs, repo remotes.")
            Text("Notifications carry the agent, the command name and a count. Repo names only if you allow it.")
                .foregroundStyle(.secondary)
        }
        Section("Network") {
            Text("None on its own: no updates check, no analytics, no fonts or scripts from the web. The viewer it opens is served from 127.0.0.1 on this Mac.")
            Text("The one exception is \"Show Claude usage from my account\" (offered at setup, and in Live Limits): it asks api.anthropic.com for your usage, with the login Claude Code saved. Links to a provider's usage page open in your browser only when you click them.")
                .foregroundStyle(.secondary)
        }
        Section("Local data") {
            Text("Settings, mutes and alert history live in the \(PrefsStore.domain) preferences. Session maps stay in ~/.agentplay and are not removed if you delete the app.")
            Button("Forget alert history") { model.forgetAlertHistory() }
        }
    }
}

struct AccountPane: View {
    @EnvironmentObject var model: CompanionModel

    var body: some View {
        Section("Claude accounts") {
            if let me = model.claudeIdentity?.line { LabeledContent("Signed in as", value: me) }
            ForEach(model.savedAccounts) { a in
                HStack {
                    VStack(alignment: .leading) {
                        Text(a.title)
                        Text([a.organization, a.seatTier?.capitalized].compactMap { $0 }.joined(separator: " · "))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if a.accountUuid == model.claudeIdentity?.accountUuid {
                        Text("Active").foregroundStyle(.secondary)
                    } else {
                        Button("Switch") { model.switchAccount(to: a) }.disabled(model.switching)
                    }
                    Button("Forget") { model.forgetAccount(a) }
                }
            }
            Button(model.addingAccount ? "Waiting for Sign-In…" : "Add Account…") { model.addClaudeAccount() }
                .disabled(model.addingAccount)
            Toggle(isOn: Binding(get: { model.prefs.rememberClaudeAccounts == true }, set: { model.prefs.rememberClaudeAccounts = $0 })) {
                Text("Remember accounts I sign in to")
                Text("Each new Claude Code login is saved for switching, and you're told when.")
            }
            Text("Switching swaps Claude Code's login in your Keychain and the account in ~/.claude.json, like claude-swap. The account you leave is saved first, your MCP server logins stay, and ~/.claude.json is backed up in ~/.agentplay/backups. Logins stay on this Mac in your Keychain. Not while a session is mid-step; open sessions follow within about 30 seconds.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        Section {
            LabeledContent("Status", value: "Local only")
            Text("Every feature here works without an account, offline.")
            Button("Connect Entelligence") {}.disabled(true)
            Text("Not available yet. There is no Entelligence sign-in service this app can use today, so nothing pretends to sign you in. When there is, it will use your browser and the Keychain, and each kind of data it could sync will ask separately.")
                .font(.caption).foregroundStyle(.secondary)
        }
        Section("Newsletter") {
            Text("The AgentPlay newsletter is an email list, not an account. This app never sends anything, so the viewer it opens doesn't offer it. It appears in the viewer when you run `uvx agentplay` yourself.")
                .foregroundStyle(.secondary)
        }
    }
}

struct DiagnosticsPane: View {
    @EnvironmentObject var model: CompanionModel
    var body: some View {
        Section {
            LabeledContent("Companion", value: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev")
            LabeledContent("AgentPlay core", value: model.snapshot?.agentplayVersion ?? "not loaded")
            LabeledContent("Runtime", value: model.runtime.map { $0.bundled ? "Bundled Python" : $0.python.path } ?? "missing")
            LabeledContent("Snapshot schema", value: "\(supportedSnapshotSchema) v\(supportedSnapshotVersion)")
            LabeledContent("Last update", value: model.lastUpdated.map { Format.clock($0) } ?? "never")
            if let e = model.lastError { Text(e.localizedDescription).foregroundStyle(.red) }
        }
        Section {
            Button("Rebuild every map") { Task { await model.refresh(.fullScan) } }
            Text("The same as `agentplay build`: rebuilds maps whose transcripts changed. Your existing maps stay.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}


struct LiveLimitsPane: View {
    @EnvironmentObject var model: CompanionModel
    @State private var preview: (before: String, after: String)?
    @State private var error: String?

    var body: some View {
        Section {
            HStack(alignment: .center, spacing: 14) {
                LimitPreview()
                VStack(alignment: .leading, spacing: 3) {
                    Text("What you get").font(.subheadline.weight(.semibold))
                    Text("Claude's 5-hour and weekly windows in the menu and in Pip's fill, live, the same numbers as claude.ai's usage page. Pick where they come from; either works, both is fine.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.vertical, 4)
        }
        Section("From your Claude account") {
            Toggle(isOn: Binding(get: { model.prefs.usesClaudeAccount }, set: { model.setClaudeAccount($0) })) {
                Text("Show Claude usage from my account")
                Text("Works however you run Claude Code, editor panels included. Uses the login Claude Code already saved in your Keychain; macOS asks once, choose Always Allow.")
            }
            if model.prefs.usesClaudeAccount {
                if let f = model.accountFailure {
                    Text(f.message).foregroundStyle(.red)
                } else if let w = model.accountLimits.first(where: { $0.window == "five_hour" }), let v = w.used.value {
                    LabeledContent("Right now", value: "Session \(Format.percent(v.usedPercent)) used")
                }
            }
        }
        Section("From Claude Code's status line") {
            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Save what Claude Code reports")
                    Text("Claude Code tells its status line your usage after every reply. This keeps a copy for AgentPlay; your status line stays as it is.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 12)
                if model.claudeBridgeOn {
                    Button("Turn Off") { model.disableClaudeBridge() }
                } else if preview != nil {
                    Button("Turn On") { model.enableClaudeBridge() }.buttonStyle(.borderedProminent)
                }
            }
            if let error { Text(error).foregroundStyle(.red) }
            if model.claudeBridgeOn {
                LabeledContent("Status") { Text("On").foregroundStyle(.green) }
            } else if let preview {
                DisclosureGroup("What changes in ~/.claude/settings.json") {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Now").font(.caption).foregroundStyle(.secondary)
                        CodeBlock(text: preview.before)
                        Text("After").font(.caption).foregroundStyle(.secondary).padding(.top, 4)
                        CodeBlock(text: preview.after)
                        Text("Only the statusLine key. A backup is saved as settings.json.agentplay-backup; Turn Off restores it.")
                            .font(.caption).foregroundStyle(.secondary).padding(.top, 4)
                    }
                }
            }
        }
        .onAppear(perform: load)
        Section("Other agents") {
            Text("Codex shows its limits by itself. Cursor, Copilot, OpenCode and Kiro don't report plan limits; their sessions and estimated usage still show.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    func load() {
        do { preview = try model.claudeBridge.preview(); error = nil } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? "Can't read Claude Code's settings."
        }
    }
}

/// The limits card as it appears in the menu, in miniature: two windows, two bars.
struct LimitPreview: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            bar("Session", 0.34, "resets in 2h 18m")
            bar("Week", 0.58, "resets Thu")
        }
        .padding(10)
        .frame(width: 190)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(.quaternary.opacity(0.6)))
        .accessibilityLabel("Preview: session 34 percent, week 58 percent")
    }

    func bar(_ name: String, _ used: Double, _ when: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(name).font(.caption.weight(.medium))
                Spacer()
                Text("\(Int((used * 100).rounded()))% · \(when)").font(.caption).foregroundStyle(.secondary)
            }
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                Capsule().fill(Color.accentColor).frame(width: 170 * used)
            }
            .frame(height: 4)
        }
    }
}

struct CodeBlock: View {
    var text: String
    var body: some View {
        Text(text).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(8)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color(nsColor: .textBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color(nsColor: .separatorColor)))
    }
}
