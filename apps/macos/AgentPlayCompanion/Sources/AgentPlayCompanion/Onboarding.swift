import AgentPlayCore
import AppKit
import SwiftUI

/// First run as a short conversation: welcome, find sessions, first real value, three optional toggles,
/// and a never-a-wall account step. No network anywhere in it.
struct OnboardingView: View {
    @EnvironmentObject var model: CompanionModel
    @Environment(\.colorScheme) private var scheme
    @Environment(\.dismiss) private var dismiss
    @State var step = 1
    @State private var scanning = false

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s4) {
            HStack(spacing: Theme.s2) {
                Image(nsImage: MenuBarMark.image(pulse: false, size: 22)).renderingMode(.template)
                    .foregroundStyle(Theme.ink(scheme))
                Text("AgentPlay").font(.system(size: 15, weight: .semibold))
                Spacer()
                Text("\(step) of 5").font(.system(size: 11).monospacedDigit()).foregroundStyle(Theme.muted(scheme))
            }
            Group {
                switch step {
                case 1: welcome
                case 2: find
                case 3: firstValue
                case 4: preferences
                default: account
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Spacer(minLength: 0)
        }
        .padding(Theme.s5)
        .frame(width: 440)
        .frame(minHeight: 300)
        .background(Theme.paper(scheme))
    }

    private func title(_ t: String) -> some View {
        Text(t).font(.system(size: 20, weight: .semibold)).foregroundStyle(Theme.ink(scheme)).fixedSize(horizontal: false, vertical: true)
    }

    private func body(_ t: String) -> some View {
        Text(t).font(.system(size: 13)).foregroundStyle(Theme.muted(scheme)).fixedSize(horizontal: false, vertical: true)
    }

    // Step 1
    private var welcome: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            title("See what your coding agents are doing, without digging through transcripts.")
            body("AgentPlay reads the session files your agents already keep on this Mac. No account, and nothing is uploaded.")
            HStack(spacing: Theme.s2) {
                Button("Find my sessions") { step = 2; scan() }.buttonStyle(QuietButtonStyle(prominent: true))
                    .keyboardShortcut(.defaultAction)
                Button("Explore a sample") { model.loadSample(); step = 3 }.buttonStyle(QuietButtonStyle())
            }
        }
    }

    // Step 2
    private var find: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            title(scanning ? "Finding your sessions…" : "Here's what's on this Mac")
            body("Only the folders these agents use are read. No Full Disk Access, no network.")
            if let s = model.snapshot {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(s.sources) { src in
                        HStack {
                            Image(systemName: src.status == .found ? "checkmark.circle.fill"
                                  : src.status == .error ? "exclamationmark.triangle" : "circle")
                                .foregroundStyle(src.status == .found ? Theme.limeInk(scheme) : Theme.muted(scheme))
                            Text(src.label).font(.system(size: 12))
                            Spacer()
                            Text(src.status == .found ? "\(src.sessionCount) sessions" : src.status == .error ? "can't read" : "not found")
                                .font(.system(size: 12).monospacedDigit()).foregroundStyle(Theme.muted(scheme))
                        }
                    }
                }
            } else if case .scanning(let n) = model.phase {
                HStack { ProgressView().controlSize(.small); body(n.map { "\($0) sessions indexed so far" } ?? "Reading agent folders") }
            } else if case .failed(let e) = model.phase {
                body(e.localizedDescription)
            }
            HStack(spacing: Theme.s2) {
                Button("Continue") { step = 3 }.buttonStyle(QuietButtonStyle(prominent: true)).disabled(scanning)
                Button("Add a folder") { model.chooseFolder(for: "claude-code") }.buttonStyle(QuietButtonStyle())
                if scanning { Button("Cancel") { model.cancelScan() }.buttonStyle(QuietButtonStyle()) }
            }
        }
    }

    // Step 3
    private var firstValue: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            if let run = model.orderedRuns.first {
                title(model.isSample ? "This is a synthetic sample" : "Your most recent run")
                RunCard(run: run, now: Date()).onAppear { model.expandedRun = run.sessionId }
                body(model.isSample
                     ? "Nothing here came from your Mac. Start Claude Code or Codex and your own runs replace it."
                     : "Click a tick to open that exact moment in the replay.")
            } else {
                title("No sessions yet")
                body("Start Claude Code, Codex, Cursor or another supported agent, then come back. Or look around with a sample.")
                Button("Explore a sample") { model.loadSample() }.buttonStyle(QuietButtonStyle())
            }
            Button("Next") { step = 4 }.buttonStyle(QuietButtonStyle(prominent: true))
        }
    }

    // Step 4
    private var preferences: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            title("A few choices, all optional")
            Toggle("Show recent activity in the menu bar", isOn: $model.prefs.showActivityInMenuBar)
            Toggle("Launch at login", isOn: Binding(get: { model.loginItemEnabled }, set: { model.setLoginItem($0) }))
            Toggle("Alert me to repeated failures", isOn: Binding(
                get: { model.prefs.alerts.enabled.contains(.repeatedFailure) },
                set: { model.setPreset(.repeatedFailure, on: $0) }))
            body("Alerts are off unless you turn them on. macOS asks for permission only when you do.")
            Button("Next") { step = 5 }.buttonStyle(QuietButtonStyle(prominent: true))
        }
        .toggleStyle(.switch)
        .font(.system(size: 13))
    }

    // Step 5
    private var account: some View {
        VStack(alignment: .leading, spacing: Theme.s3) {
            title("Stay local")
            body("Everything you just saw works without an account: scanning, replay, alerts and context. An Entelligence account would only ever sync settings you choose, and it isn't available yet.")
            HStack(spacing: Theme.s2) {
                Button("Stay local") { finish() }.buttonStyle(QuietButtonStyle(prominent: true)).keyboardShortcut(.defaultAction)
                Button("Connect Entelligence") {}.buttonStyle(QuietButtonStyle()).disabled(true)
                    .help("Not available yet. See docs/companion/ACCOUNT_INTEGRATION.md.")
            }
            body("The AgentPlay newsletter is separate: an email list, not an account. This app never sends anything, so it isn't offered here.")
        }
    }

    private func scan() {
        guard !model.isSample else { return }
        scanning = true
        Task {
            await model.runFirstScan()
            scanning = false
        }
    }

    private func finish() {
        if model.isSample { model.leaveSample() }
        model.finishOnboarding()
        dismiss()
    }
}
