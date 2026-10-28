import AgentPlayCore
import AppKit
import SwiftUI
import UserNotifications

struct SettingsView: View {
    @EnvironmentObject var model: CompanionModel
    var initialPane: Pane? = nil

    /// Four tabs. Each setting is one line; the rest is one click away, under a disclosure.
    enum Pane: String, CaseIterable, Identifiable {
        case general = "General", claude = "Limits", notifications = "Notifications", privacy = "Privacy"
        var id: String { rawValue }
        var icon: String {
            switch self {
            case .general: "gearshape"
            case .claude: "gauge.with.dots.needle.67percent"
            case .notifications: "bell"
            case .privacy: "hand.raised"
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
            case .notifications: NotificationsPane()
            case .privacy: PrivacyPane()
            }
        }
        .formStyle(.grouped)
        .frame(width: SettingsWindow.size.width, height: SettingsWindow.size.height)
    }
}

/// The standard Mac settings window: toolbar tabs, the window titled after the pane, last pane remembered.
@MainActor
enum SettingsWindow {
    static let size = NSSize(width: 580, height: 500)

    static func make(model: CompanionModel) -> NSWindow {
        let tabs = SettingsTabs()
        tabs.model = model
        tabs.tabStyle = .toolbar
        tabs.transitionOptions = []
        for pane in SettingsView.Pane.allCases {
            let hc = NSHostingController(rootView: AnyView(SettingsPaneView(pane: pane).environmentObject(model).tint(Brand.accent)))
            hc.title = pane.rawValue
            hc.sizingOptions = []
            hc.preferredContentSize = size
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

// MARK: - General

struct GeneralPane: View {
    @EnvironmentObject var model: CompanionModel
    var body: some View {
        Section("Menu bar") {
            Toggle(isOn: Binding(get: { model.prefs.showsPip }, set: { model.prefs.pipEnabled = $0 })) {
                row("Show Pip", "Off shows two plain bars instead.", preview: MenuBarStrip(pip: model.prefs.showsPip))
            }
            Toggle(isOn: $model.prefs.showActivityInMenuBar) {
                row("Session % beside Pip", "How much of the 5-hour window is used.",
                    preview: MenuBarStrip(pip: model.prefs.showsPip, text: "34%"))
            }
            Toggle(isOn: Binding(get: { model.prefs.showsPipIsland }, set: { model.prefs.pipIslandEnabled = $0 })) {
                row("Name what happened", "A few seconds, then it goes. Click it to go there.",
                    preview: MenuBarStrip(pip: model.prefs.showsPip, pose: .wave, text: "docs-site"))
            }
        }
        Section {
            Toggle("Open at login", isOn: Binding(get: { model.loginItemEnabled }, set: { model.setLoginItem($0) }))
            if Updater.shared.available {
                Toggle(isOn: Binding(get: { Updater.shared.checksAutomatically }, set: { Updater.shared.checksAutomatically = $0 })) {
                    Text("Check for updates once a day")
                    Text(Updater.shared.lastChecked.map { "Last checked \(Format.relative($0, now: Date()))." } ?? "Asks github.com for the latest release; you decide whether to install it.")
                }
            }
            Toggle("Soft sound when I click something", isOn: $model.prefs.soundOnManualActions)
        }
        Section("Agents") {
            if let s = model.snapshot, !model.isSample {
                ForEach(s.sources) { src in
                    HStack {
                        Text(src.label)
                        Spacer()
                        Text(src.status == .found ? "\(src.sessionCount) session\(src.sessionCount == 1 ? "" : "s")"
                             : src.status == .error ? "Can't read" : "Not found")
                            .foregroundStyle(src.status == .error ? .red : .secondary)
                        SwiftUI.Menu {
                            Button("Add a Folder…") { model.chooseFolder(for: src.agent) }
                            ForEach(model.prefs.customFolders[src.agent] ?? [], id: \.self) { path in
                                Button("Remove \((path as NSString).lastPathComponent)") { model.removeFolder(path, agent: src.agent) }
                            }
                        } label: { Image(systemName: "ellipsis.circle") }
                            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                    }
                    .help(src.remedy ?? src.paths.joined(separator: ", "))
                }
            } else {
                Text(model.isSample ? "Showing sample data." : "Not read yet.").foregroundStyle(.secondary)
            }
        }
        Section {
            HStack {
                Text("AgentPlay \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev")")
                    .foregroundStyle(.secondary)
                if let t = model.lastUpdated { Text("· updated \(Format.clock(t))").foregroundStyle(.secondary) }
                Spacer()
                Button("Read Everything Again") { Task { await model.refresh(.fullScan) } }
            }
            if let e = model.lastError { Text(e.localizedDescription).foregroundStyle(.red) }
            if model.isDevBuild { Button("Load Sample Data") { model.loadSample() } }
        }
    }

    func row<P: View>(_ title: String, _ detail: String, preview: P) -> some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            preview
        }
    }
}

// MARK: - Limits

struct LiveLimitsPane: View {
    @EnvironmentObject var model: CompanionModel
    @State private var preview: (before: String, after: String)?
    @State private var error: String?

    var claude: ProviderSummary? {
        model.snapshot.flatMap { ProviderSummary.build($0.limits).first { $0.provider == "claude-code" } }
    }

    var body: some View {
        Section {
            if let c = claude, !c.windows.isEmpty {
                ForEach(c.windows) { w in MeterRow(window: w, now: Date()) }
            } else {
                Text("No limits yet. They appear a moment after you turn on the first switch below.")
                    .foregroundStyle(.secondary)
            }
        } header: {
            Text("Claude, right now")
        }
        Section("Where they come from") {
            Toggle(isOn: Binding(get: { model.prefs.usesClaudeAccount }, set: { model.setClaudeAccount($0) })) {
                Text("Your Claude account")
                Text("The same numbers as claude.ai. Works from editor panels too. macOS asks once.")
            }
            if model.prefs.usesClaudeAccount, let f = model.accountFailure { Text(f.message).foregroundStyle(.red) }
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Claude Code's status line")
                    Text(model.claudeBridgeOn ? "On. Updates after every reply in a terminal."
                         : "Updates after every reply in a terminal. Changes one line in settings.json.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if model.claudeBridgeOn { Button("Turn Off") { model.disableClaudeBridge() } }
                else if preview != nil { Button("Turn On") { model.enableClaudeBridge() } }
            }
            if let error { Text(error).foregroundStyle(.red) }
            if !model.claudeBridgeOn, let preview {
                DisclosureGroup("The change it makes") {
                    VStack(alignment: .leading, spacing: 4) {
                        CodeBlock(text: preview.before)
                        Image(systemName: "arrow.down").foregroundStyle(.secondary).frame(maxWidth: .infinity)
                        CodeBlock(text: preview.after)
                        Text("A backup is kept; Turn Off puts it back.").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .onAppear(perform: load)
        Section("Claude accounts") {
            ForEach(model.savedAccounts) { a in
                HStack {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(a.title)
                        if let org = a.organization { Text(org).font(.caption).foregroundStyle(.secondary) }
                    }
                    Spacer()
                    if a.accountUuid == model.claudeIdentity?.accountUuid {
                        Text("In use").foregroundStyle(.secondary)
                    } else {
                        Button("Switch") { model.switchAccount(to: a) }.disabled(model.switching)
                    }
                    SwiftUI.Menu { Button("Forget This Account", role: .destructive) { model.forgetAccount(a) } } label: { Image(systemName: "ellipsis.circle") }
                        .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                }
            }
            if model.savedAccounts.isEmpty, let me = model.claudeIdentity?.line {
                LabeledContent("Signed in", value: me)
            }
            HStack {
                Button(model.addingAccount ? "Waiting for Sign-In…" : "Add Account…") { model.addClaudeAccount() }
                    .disabled(model.addingAccount)
                Spacer()
                Toggle("Remember new sign-ins", isOn: Binding(get: { model.prefs.rememberClaudeAccounts == true },
                                                               set: { model.prefs.rememberClaudeAccounts = $0 }))
                    .toggleStyle(.checkbox)
            }
            Text("Switching keeps your MCP logins and backs up ~/.claude.json first.").font(.caption).foregroundStyle(.secondary)
        }
    }

    func load() {
        do { preview = try model.claudeBridge.preview(); error = nil } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? "Can't read Claude Code's settings."
        }
    }
}

// MARK: - Notifications

struct NotificationsPane: View {
    @EnvironmentObject var model: CompanionModel
    @State private var quietOn = false
    @State private var quietStart = Calendar.current.date(bySettingHour: 22, minute: 0, second: 0, of: Date())!
    @State private var quietEnd = Calendar.current.date(bySettingHour: 7, minute: 0, second: 0, of: Date())!

    static let main: [AlertPreset] = [.needsYou, .finished, .repeatedFailure, .allowanceLow]
    var allowed: Bool { model.notificationStatus == .authorized || model.notificationStatus == .provisional }

    var body: some View {
        if !allowed && model.prefs.alertsAs != "pip" {
            Section {
                HStack {
                    Label(model.notificationStatus == .denied ? "Notifications are off for AgentPlay" : "AgentPlay can't notify you yet",
                          systemImage: "bell.slash")
                    Spacer()
                    if model.notificationStatus == .denied { Button("Open System Settings") { model.openNotificationSettings() } }
                    else { Button("Allow") { model.requestNotifications() } }
                }
            }
        }
        Section("How alerts look") {
            Picker("Show alerts as", selection: Binding(get: { model.prefs.alertsAs }, set: { model.prefs.alertStyle = $0 })) {
                Text("Pip on screen").tag("pip")
                Text("macOS notifications").tag("system")
                Text("Both").tag("both")
            }
            Toggle(isOn: Binding(get: { model.prefs.walksWhileWorking }, set: { model.prefs.pipWalker = $0 })) {
                Text("Pip walks along while agents work")
                Text("A small strip under the menu bar, so you can see from across the room.")
            }
        }
        Section("Tell me when") {
            ForEach(OnboardingView.choices) { c in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(c.title)
                        Text("\u{201C}\(c.sampleTitle)\u{201D}").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Send Sample") { model.sendSample(title: c.sampleTitle, body: c.sampleBody, pose: c.pose) }
                        .buttonStyle(.link).font(.caption)
                    Toggle("", isOn: Binding(get: { model.prefs.alerts.enabled.contains(c.preset) }, set: { model.setPreset(c.preset, on: $0) }))
                        .labelsHidden().toggleStyle(.switch)
                }
            }
            DisclosureGroup("More") {
                ForEach(AlertPreset.allCases.filter { !Self.main.contains($0) }) { p in
                    Toggle(isOn: Binding(get: { model.prefs.alerts.enabled.contains(p) }, set: { model.setPreset(p, on: $0) })) {
                        Text(p.title)
                        Text(p.detail)
                    }
                }
            }
        }
        Section("Quiet") {
            Toggle("Quiet hours", isOn: $quietOn)
            if quietOn {
                HStack {
                    DatePicker("From", selection: $quietStart, displayedComponents: .hourAndMinute)
                    DatePicker("to", selection: $quietEnd, displayedComponents: .hourAndMinute)
                }
            }
            Toggle("Silent while presenting", isOn: $model.prefs.alerts.silenceWhilePresenting)
            HStack {
                if let until = model.prefs.alerts.snoozeUntil, until > Date() {
                    Text("Snoozed until \(Format.clock(until))")
                    Spacer()
                    Button("Resume") { model.prefs.alerts.snoozeUntil = nil }
                } else {
                    Text("Snooze")
                    Spacer()
                    Button("1 Hour") { model.snooze(3600) }
                    Button("Rest of Today") { model.snooze(nil) }
                }
            }
        }
        .onAppear(perform: loadQuiet)
        .onChange(of: quietOn) { saveQuiet() }
        .onChange(of: quietStart) { saveQuiet() }
        .onChange(of: quietEnd) { saveQuiet() }
        Section {
            DisclosureGroup("Details") {
                Toggle("Name the project in notifications", isOn: $model.prefs.alerts.includeRepoInNotifications)
                Picker("Plan running out at", selection: $model.prefs.alerts.allowanceThresholdLeft) {
                    Text("20% left").tag(20.0); Text("10% left").tag(10.0)
                }
                Stepper("At most \(model.prefs.alerts.maxPerHour) an hour", value: $model.prefs.alerts.maxPerHour, in: 1...10)
                let muted = model.prefs.alerts.mutedPatterns.count + model.prefs.alerts.mutedSessions.count
                HStack {
                    Text(muted == 0 ? "Nothing muted" : "\(muted) muted")
                    Spacer()
                    if muted > 0 {
                        Button("Unmute All") { model.prefs.alerts.mutedPatterns = []; model.prefs.alerts.mutedSessions = [] }
                    }
                }
            }
        }
        .task { await model.refreshNotificationStatus() }
    }

    private func loadQuiet() {
        guard let q = model.prefs.alerts.quietHours else { return }
        quietOn = true
        let day = Calendar.current.startOfDay(for: Date())
        quietStart = day.addingTimeInterval(TimeInterval(q.start * 60))
        quietEnd = day.addingTimeInterval(TimeInterval(q.end * 60))
    }

    private func saveQuiet() {
        guard quietOn else { model.prefs.alerts.quietHours = nil; return }
        func m(_ d: Date) -> Int { let c = Calendar.current.dateComponents([.hour, .minute], from: d); return (c.hour ?? 0) * 60 + (c.minute ?? 0) }
        model.prefs.alerts.quietHours = QuietHours(start: m(quietStart), end: m(quietEnd))
    }
}

// MARK: - Privacy

struct PrivacyPane: View {
    @EnvironmentObject var model: CompanionModel
    var body: some View {
        Section {
            line("internaldrive", "Reads your agents' session files on this Mac.",
                 "Never the text of your prompts, file contents or command arguments.")
            line("network", "Your Claude plan usage, every few minutes.",
                 "To api.anthropic.com, with the login Claude Code saved. Off in Limits.")
            line("bell", "Notifications name the agent and a count.",
                 "The project's name only if you allow it.")
            line("folder", "Everything it keeps stays in ~/.agentplay.",
                 "Deleting the app leaves it; nothing is uploaded.")
        }
        Section {
            Toggle(isOn: Binding(get: { model.prefs.asksClaudeForSuggestions }, set: { model.prefs.claudeSuggestions = $0 })) {
                Text("Ask Claude for suggestions once a day")
                Text("Claude Haiku, through your own Claude Code, reads counts and your skill names, never your prompts or code. A few thousand tokens a day on your plan.")
            }
        }
        Section {
            HStack {
                Button("Read the Full Privacy Note") {
                    if let u = Bundle.main.url(forResource: "PRIVACY", withExtension: "txt") { NSWorkspace.shared.open(u) }
                }
                Spacer()
                Button("Forget Alert History") { model.forgetAlertHistory() }
            }
        }
    }

    func line(_ icon: String, _ title: String, _ detail: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon).font(.title3).foregroundStyle(.tint).frame(width: 26)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                Text(detail).font(.callout).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 3)
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
