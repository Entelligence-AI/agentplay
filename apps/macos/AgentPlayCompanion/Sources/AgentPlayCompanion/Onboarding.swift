import AgentPlayCore
import AppKit
import SwiftUI

/// First run, told by Pip: it wakes up, reads this Mac's sessions while you watch, says three things it learnt
/// from them, and shows how it'll talk to you from the menu bar. Four short steps; motion follows Reduce Motion.
struct OnboardingView: View {
    @EnvironmentObject var model: CompanionModel
    @State var step = 1
    @State private var scanning = false
    @State private var scanError: String?
    @State private var shown = 0          // learnings revealed so far
    @State private var opened = Date()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(spacing: 0) {
            Group {
                switch step {
                case 1: meet
                case 2: reading
                case 3: learnt
                default: home
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .transition(.opacity)
            HStack {
                HStack(spacing: 6) {
                    ForEach(1...4, id: \.self) { i in
                        Circle().fill(i == step ? Color.accentColor : Color.secondary.opacity(0.3)).frame(width: 6, height: 6)
                    }
                }
                .accessibilityLabel("Step \(step) of 4")
                Spacer()
                if step > 1 && (step != 2 || scanError != nil) { Button("Back") { go(step - 1) } }
                primary
            }
            .padding(.horizontal, 22).padding(.vertical, 16)
        }
        .frame(width: 520, height: 470)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.25), value: step)
    }

    @ViewBuilder var primary: some View {
        switch step {
        case 1: Button("Nice to Meet You") { go(2); scan() }.keyboardShortcut(.defaultAction)
        case 2 where scanError != nil: Button("Retry") { scan() }.keyboardShortcut(.defaultAction)
        case 2: Button(scanning ? "Reading…" : "Continue") { go(3) }.keyboardShortcut(.defaultAction).disabled(scanning)
        case 3: Button("Continue") { go(4) }.keyboardShortcut(.defaultAction)
        default: Button("Start") { finish() }.keyboardShortcut(.defaultAction)
        }
    }

    func go(_ s: Int) {
        step = s
        opened = Date()
        if s == 3 { reveal() }
    }

    // MARK: Pip, big

    /// Pip at six times its menu-bar size, moving on a quarter-second beat while this window is up.
    func pip(_ pose: PipPose, wakeAfter: TimeInterval? = nil) -> some View {
        TimelineView(.periodic(from: .now, by: reduceMotion ? 3600 : 0.25)) { t in
            let tick = reduceMotion ? 0 : Int(t.date.timeIntervalSinceReferenceDate * 4)
            let awake = wakeAfter.map { t.date.timeIntervalSince(opened) > $0 } ?? true
            let p: PipPose = awake ? pose : .sleep
            let blink = awake && tick % 14 == 0 && p != .sleep
            Image(nsImage: PipIcon.image(p, frame: (p == .work || p == .wave) ? tick / 2 : 0,
                                         session: p == .sleep ? 0.15 : 0.45, weekly: 0.6,
                                         eyes: blink ? .closed : .pose))
                .renderingMode(.template).interpolation(.none).resizable()
                .frame(width: 120, height: 108)
                .foregroundStyle(.primary)
                .accessibilityLabel("Pip, \(PipIcon.description(p).lowercased())")
        }
    }

    func title(_ s: String) -> some View {
        Text(s).font(.system(size: 26, weight: .semibold, design: .rounded)).multilineTextAlignment(.center)
    }

    func body(_ s: String) -> some View {
        Text(s).font(.title3).foregroundStyle(.secondary).multilineTextAlignment(.center)
            .fixedSize(horizontal: false, vertical: true).frame(maxWidth: 420)
    }

    // MARK: 1. Meet Pip

    var meet: some View {
        VStack(spacing: 14) {
            Spacer(minLength: 8)
            pip(.wave, wakeAfter: 1.2)
            title("Hi, I'm Pip.")
            body("I live in your menu bar and keep an eye on your coding agents: what's running, what needs you, and how much of your plan is left.")
            Button("Just Show Me With Sample Data") { model.loadSample(); go(3) }.buttonStyle(.link).padding(.top, 4)
            Spacer()
        }
        .padding(.horizontal, 24)
    }

    // MARK: 2. Reading your sessions

    var found: Int {
        if case .scanning(let n?) = model.phase { return n }
        return model.snapshot?.sources.filter { $0.status == .found }.reduce(0) { $0 + $1.sessionCount } ?? 0
    }

    var reading: some View {
        VStack(spacing: 14) {
            Spacer(minLength: 8)
            if let scanError {
                pip(.sleep)
                title("Couldn't read your sessions.")
                Text(scanError).foregroundStyle(.red).multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true).frame(maxWidth: 420)
                Spacer()
            } else {
                pip(scanning ? .work : .cheer)
                title(scanning ? "Reading your sessions…" : "All read.")
                Text("\(found)")
                    .font(.system(size: 44, weight: .semibold, design: .rounded).monospacedDigit())
                    .contentTransition(.numericText())
                    .animation(reduceMotion ? nil : .easeOut(duration: 0.3), value: found)
                Text(found == 1 ? "session" : "sessions").font(.headline).foregroundStyle(.secondary)
                if !scanning, let s = model.snapshot, !s.foundSources.isEmpty {
                    body("From " + s.foundSources.map(\.label).joined(separator: ", ") + ".")
                }
                Label("Read on this Mac. Nothing leaves it.", systemImage: "lock.fill")
                    .font(.callout).foregroundStyle(.secondary)
                Spacer()
            }
        }
        .padding(.horizontal, 24)
    }

    // MARK: 3. What I learnt

    var learnings: [Pip.Learning] { model.snapshot.map(Pip.learnings) ?? [] }

    func reveal() {
        shown = 0
        let n = learnings.count
        guard !reduceMotion else { shown = n; return }
        for i in 0..<n {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35 + 0.45 * Double(i)) {
                withAnimation(.easeOut(duration: 0.35)) { shown = i + 1 }
            }
        }
    }

    var learnt: some View {
        VStack(spacing: 14) {
            pip(.cheer).padding(.top, 18)
            title(model.isSample ? "Here's what I'd tell you" : "Here's what I learnt")
            VStack(alignment: .leading, spacing: 10) {
                ForEach(Array(learnings.enumerated()), id: \.offset) { i, l in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: l.symbol).font(.title3).foregroundStyle(.tint).frame(width: 24)
                        Text(l.text).font(.body).fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                    }
                    .padding(12)
                    .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .opacity(i < shown ? 1 : 0)
                    .offset(y: i < shown ? 0 : 8)
                }
            }
            .frame(maxWidth: 440)
            Spacer()
        }
        .padding(.horizontal, 24)
        .onAppear { if shown == 0 { reveal() } }
    }

    // MARK: 4. Where I live

    var home: some View {
        VStack(spacing: 12) {
            title("Find me in the menu bar").padding(.top, 22)
            body("My pose tells you what needs you. Point at me and I'll look up; click me for the details.")
            HStack(spacing: 18) {
                legend(.wave, "Needs you")
                legend(.cheer, "Finished")
                legend(.oops, "Keeps failing")
                legend(.sweat, "Going fast")
                legend(.limit, "Limit reached")
            }
            .padding(.vertical, 4)
            Form {
                Toggle(isOn: Binding(get: { model.prefs.alerts.enabled.contains(.needsYou) },
                                     set: { model.setPreset(.needsYou, on: $0) })) {
                    Text("Tell me when a session needs me")
                }
                Toggle(isOn: Binding(get: { model.prefs.usesClaudeAccount }, set: { model.setClaudeAccount($0) })) {
                    Text("Show Claude usage from my account")
                    Text("Asks your Claude account, like claude.ai's usage page. macOS asks once.")
                }
                Toggle("Open AgentPlay when I log in", isOn: Binding(get: { model.loginItemEnabled }, set: { model.setLoginItem($0) }))
            }
            .formStyle(.grouped).scrollDisabled(true).frame(maxWidth: 460, maxHeight: 170)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 24)
    }

    func legend(_ pose: PipPose, _ text: String) -> some View {
        VStack(spacing: 6) {
            Image(nsImage: PipIcon.image(pose, session: 0.45, weekly: 0.6))
                .renderingMode(.template).interpolation(.none).resizable()
                .frame(width: 40, height: 36).foregroundStyle(.primary)
            Text(text).font(.caption).foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: actions

    func scan() {
        guard !model.isSample else { return }
        scanning = true
        scanError = nil
        Task {
            await model.runFirstScan()
            scanning = false
            // Failed, or no core to read with: say why and offer Retry instead of "All read."
            if case .failed(let e) = model.phase { scanError = e.localizedDescription; return }
            if let e = model.lastError { scanError = e.localizedDescription; return }
            if model.snapshot == nil { scanError = "AgentPlay couldn't start reading. Try again in a moment."; return }
            // A beat on "All read." before the reveal.
            try? await Task.sleep(for: .milliseconds(reduceMotion ? 0 : 900))
            if step == 2 { go(3) }
        }
    }

    func finish() {
        let wasSample = model.isSample
        if wasSample { model.leaveSample() }
        model.finishOnboarding()
        // The sample skipped the first read of this Mac's sessions: do it now, in the background.
        if wasSample { Task { await model.runFirstScan() } }
        Windows.close(.welcome)
    }
}
