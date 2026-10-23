import AgentPlayCore
import AppKit
import SwiftUI

/// First run in a standard window: welcome, find sessions, live limits, a few choices, stay local.
/// Native controls only; no network anywhere in it.
struct OnboardingView: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openWindow) private var openWindow
    @State var step = 1
    @State private var scanning = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Group {
                switch step {
                case 1: welcome
                case 2: find
                case 3: limits
                case 4: preferences
                default: account
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            Divider()
            HStack {
                Text("Step \(step) of 5").font(.callout).foregroundStyle(.secondary)
                Spacer()
                if step > 1 { Button("Back") { step -= 1 } }
                primary
            }
            .padding(.horizontal, 20).padding(.vertical, 14)
        }
        .frame(width: 480, height: 380)
    }

    @ViewBuilder var primary: some View {
        switch step {
        case 1: Button("Find My Sessions") { step = 2; scan() }.keyboardShortcut(.defaultAction)
        case 5: Button("Done") { finish() }.keyboardShortcut(.defaultAction)
        default: Button("Continue") { step += 1 }.keyboardShortcut(.defaultAction).disabled(scanning && step == 2)
        }
    }

    func page<Content: View>(_ symbol: String, _ title: String, _ subtitle: String,
                             @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Image(systemName: symbol).font(.system(size: 28)).foregroundStyle(.tint)
            Text(title).font(.title2.weight(.semibold))
            Text(subtitle).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            content()
        }
        .padding(24)
    }

    var welcome: some View {
        page("waveform.path.ecg", "Your coding agents, in the menu bar",
             "Usage limits, running sessions and errors from Claude Code, Codex, Cursor and more, read from this Mac. No account, nothing uploaded.") {
            Button("Explore With Sample Data") { model.loadSample(); step = 3 }.buttonStyle(.link)
        }
    }

    var find: some View {
        page("magnifyingglass", scanning ? "Finding your sessions…" : "Found on this Mac",
             "Only the folders these agents use. If your repos are in Documents, macOS may ask once.") {
            if let s = model.snapshot, !scanning {
                Form {
                    ForEach(s.sources.filter { $0.status != .notFound }) { src in
                        LabeledContent(src.label, value: src.status == .found ? "\(src.sessionCount) sessions" : "Can't read")
                    }
                    if s.foundSources.isEmpty { Text("No agents found yet").foregroundStyle(.secondary) }
                }
                .formStyle(.grouped).scrollDisabled(true).frame(maxHeight: 160)
            } else {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    if case .scanning(let n?) = model.phase { Text("\(n) sessions so far").foregroundStyle(.secondary) }
                    Button("Cancel") { model.cancelScan(); scanning = false }
                }
            }
        }
    }

    var limits: some View {
        page("gauge.with.dots.needle.33percent", "Live Claude limits",
             "Claude Code reports your 5-hour and weekly usage to its status line. Turn this on and AgentPlay saves that report on this Mac to show it in the menu. You can turn it off in Settings.") {
            if model.claudeBridgeOn {
                Label("On", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
            } else {
                Button("Review and Turn On…") {
                    model.settingsPane = .claude
                    NSApp.activate(ignoringOtherApps: true)
                    openWindow(id: "settings")
                }
            }
        }
    }

    var preferences: some View {
        page("slider.horizontal.3", "A few choices", "All optional. Alerts stay off unless you turn them on.") {
            Form {
                Toggle("Show session % next to the icon", isOn: $model.prefs.showActivityInMenuBar)
                Toggle("Launch at login", isOn: Binding(get: { model.loginItemEnabled }, set: { model.setLoginItem($0) }))
                Toggle("Notify me when a command fails 3 times", isOn: Binding(
                    get: { model.prefs.alerts.enabled.contains(.repeatedFailure) },
                    set: { model.setPreset(.repeatedFailure, on: $0) }))
            }
            .formStyle(.grouped).scrollDisabled(true)
        }
    }

    var account: some View {
        page("lock.shield", "Everything stays on this Mac",
             "No account is needed for anything here. Entelligence sign-in isn't available yet; when it is, it will be optional and ask before syncing anything.") {
            EmptyView()
        }
    }

    func scan() {
        guard !model.isSample else { return }
        scanning = true
        Task { await model.runFirstScan(); scanning = false }
    }

    func finish() {
        if model.isSample { model.leaveSample() }
        model.finishOnboarding()
        dismiss()
    }
}
