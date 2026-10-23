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

    var body: some View {
        NavigationSplitView {
            List(Pane.allCases, selection: Binding(get: { model.settingsPane }, set: { model.settingsPane = $0 ?? .general })) { p in
                Label(p.rawValue, systemImage: p.icon).tag(p)
            }
                .navigationSplitViewColumnWidth(170)
        } detail: {
            Form {
                switch model.settingsPane {
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
        }
        .frame(width: 680, height: 520)
        .onAppear { if let p = initialPane { model.settingsPane = p }; model.refreshBridgeState() }
    }
}

struct GeneralPane: View {
    @EnvironmentObject var model: CompanionModel
    var body: some View {
        Section {
            Toggle("Show session % next to the icon", isOn: $model.prefs.showActivityInMenuBar)
            Text("The icon's two bars are the 5-hour and weekly windows of your most-used provider.")
                .font(.caption).foregroundStyle(.secondary)
            Toggle("Launch at login", isOn: Binding(get: { model.loginItemEnabled }, set: { model.setLoginItem($0) }))
            Toggle("Soft sound on actions I take", isOn: $model.prefs.soundOnManualActions)
            Text("Never for background events. Follows the system volume.").font(.caption).foregroundStyle(.secondary)
        }
        Section("Appearance") {
            Text("Follows the system's light or dark mode, Reduce Motion and Increase Contrast.")
                .foregroundStyle(.secondary)
        }
        if model.isDevBuild {
            Section("Developer") { Button("Load Sample Data") { model.loadSample() } }
        }
        Section {
            Button("Quit AgentPlay Companion") { NSApp.terminate(nil) }
            Text("Quitting stops the viewer this app started and its file watchers. A viewer you started yourself keeps running.")
                .font(.caption).foregroundStyle(.secondary)
        }
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
                            Text(src.status == .found ? "\(src.sessionCount) sessions" : src.status == .error ? "Can't read" : "Not found")
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
            Text("Off unless you turn a preset on. Local notifications only: no server, no push service.")
                .font(.caption).foregroundStyle(.secondary)
            if model.notificationStatus == .denied {
                HStack {
                    Text("macOS has notifications off for AgentPlay.").foregroundStyle(.red)
                    Button("Open System Settings") {
                        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension")!)
                    }
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
            Text("None. The companion makes no network requests: no updates check, no analytics, no fonts or scripts from the web. The viewer it opens is served from 127.0.0.1 on this Mac.")
            Text("Links in the Limits tab open your provider's usage page in your browser only when you click them.")
                .foregroundStyle(.secondary)
        }
        Section("Local data") {
            Text("Settings, mutes and alert history live in the \(PrefsStore.domain) preferences. Session maps stay in ~/.agentplay and are not removed if you delete the app.")
            Button("Forget alert history") { model.forgetAlertHistory() }
        }
    }
}

struct AccountPane: View {
    var body: some View {
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
            LabeledContent("Claude Code") {
                Text(model.claudeBridgeOn ? "On" : "Off").foregroundStyle(model.claudeBridgeOn ? .green : .secondary)
            }
            Text("Claude Code sends your 5-hour and weekly usage, and each session's context, to its status line (documented by Anthropic for Pro and Max). Turning this on points that status line at a small script that saves the report in ~/.agentplay/live and then runs your previous status line, so it looks the same.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if model.claudeBridgeOn {
                Button("Turn Off") { model.disableClaudeBridge() }
            }
        }
        if !model.claudeBridgeOn {
            Section("The change to ~/.claude/settings.json") {
                if let preview {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Now").font(.caption).foregroundStyle(.secondary)
                        CodeBlock(text: preview.before)
                        Text("After").font(.caption).foregroundStyle(.secondary).padding(.top, 4)
                        CodeBlock(text: preview.after)
                    }
                    Text("A backup is saved as settings.json.agentplay-backup. Turning it off restores your previous status line.")
                        .font(.caption).foregroundStyle(.secondary)
                    HStack { Spacer(); Button("Turn On") { model.enableClaudeBridge() }.buttonStyle(.borderedProminent) }
                } else if let error {
                    Text(error).foregroundStyle(.red)
                }
            }
            .onAppear(perform: load)
        }
        Section("Codex") {
            Text("Codex writes its limits into its own session files, so they show without any setup when you've used Codex on this Mac.")
                .font(.callout).foregroundStyle(.secondary)
        }
        Section("Others") {
            Text("Cursor, Copilot, OpenCode and Kiro don't report plan limits locally. Their sessions and usage estimates still show.")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    func load() {
        do { preview = try model.claudeBridge.preview(); error = nil } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? "Can't read Claude Code's settings."
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
