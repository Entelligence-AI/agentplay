import AgentPlayCore
import AppKit
import SwiftUI

/// First run, told by Pip: it says what it does, reads this Mac's sessions while you watch (and asks to notify
/// you while that runs), says three things it learnt, and shows how it'll talk to you from the menu bar.
/// Four short steps; motion follows Reduce Motion.
struct OnboardingView: View {
    @EnvironmentObject var model: CompanionModel
    @State var step = 1
    /// Filming: the first run plays itself, step by step, on the timings a viewer would take.
    var filmAutoplay = false
    @State private var scanning = false
    @State private var shown = 0          // learnings revealed so far
    @State private var opened = Date()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    static let size = CGSize(width: 560, height: 500)

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
                if step > 1 && step != 2 { Button("Back") { go(step - 1) } }
                primary
            }
            .padding(.horizontal, 22).padding(.vertical, 16)
        }
        .frame(width: Self.size.width, height: Self.size.height)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.25), value: step)
    }

    /// Filming: advance on the view's own clock (onAppear never fires in a window parked off the screen).
    func filmAdvance(_ now: Date) {
        guard filmAutoplay else { return }
        let el = now.timeIntervalSince(opened)
        if step == 1, el > 3.6 { DispatchQueue.main.async { go(2); scan() } }   // reading, then learnt by itself
        if step == 3, el > 7.0 { DispatchQueue.main.async { go(4) } }
    }

    @ViewBuilder var primary: some View {
        switch step {
        case 1: Button("Read My Sessions") { go(2); scan() }.keyboardShortcut(.defaultAction)
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

    /// Pip at several times its menu-bar size, moving on a quarter-second beat while this window is up.
    func pip(_ pose: PipPose, wakeAfter: TimeInterval? = nil, size: CGFloat = 120) -> some View {
        TimelineView(.periodic(from: .now, by: reduceMotion ? 3600 : 0.25)) { t in
            let tick = reduceMotion ? 0 : Int(t.date.timeIntervalSinceReferenceDate * 4)
            let _ = filmAdvance(t.date)
            let awake = wakeAfter.map { t.date.timeIntervalSince(opened) > $0 } ?? true
            let p: PipPose = awake ? pose : .sleep
            let blink = awake && tick % 14 == 0 && p != .sleep
            Image(nsImage: PipIcon.image(p, frame: (p == .work || p == .wave) ? tick / 2 : 0,
                                         session: p == .sleep ? 0.15 : 0.45, weekly: 0.6,
                                         eyes: blink ? .closed : .pose))
                .renderingMode(.template).interpolation(.none).resizable()
                .frame(width: size, height: size * 0.9)
                .foregroundStyle(.primary)
                .accessibilityLabel("Pip, \(PipIcon.description(p).lowercased())")
        }
    }

    func title(_ s: String) -> some View {
        Text(s).font(.system(size: 26, weight: .semibold, design: .rounded)).multilineTextAlignment(.center)
    }

    func body(_ s: String) -> some View {
        Text(s).font(.title3).foregroundStyle(.secondary).multilineTextAlignment(.center)
            .fixedSize(horizontal: false, vertical: true).frame(maxWidth: 460)
    }

    // MARK: 1. Meet Pip: what it does, and with which agents

    var meet: some View {
        VStack(spacing: 12) {
            pip(.wave, wakeAfter: 1.2, size: 84).padding(.top, 14)
            title("Hi, I'm Pip.")
            body("I live in your menu bar and watch your coding agents, so you don't have to watch the terminal.")
            HStack(alignment: .top, spacing: 12) {
                tile("eye", "See what's running", "Every live session, what it just did, and its context.")
                tile("hand.wave", "Know when it needs you", "I wave in the menu bar, and notify you if you let me.")
                tile("gauge.with.dots.needle.33percent", "Watch your plan", "5-hour and weekly windows, before they run out.")
            }
            .fixedSize(horizontal: false, vertical: true)
            .padding(.top, 6)
            VStack(spacing: 4) {
                HStack(spacing: 6) {
                    Text("Works with").font(.caption).foregroundStyle(.secondary)
                    ForEach(["Claude Code", "Codex", "Cursor", "Copilot", "OpenCode", "Kiro"], id: \.self) { a in
                        Text(a).font(.caption.weight(.medium))
                            .padding(.horizontal, 7).padding(.vertical, 3)
                            .background(.quaternary.opacity(0.6), in: Capsule())
                    }
                }
                Text("Plan limits come from Claude Code and Codex; the others don't report them.")
                    .font(.caption).foregroundStyle(.tertiary)
            }
            .padding(.top, 4)
            Button("Just Show Me With Sample Data") { model.loadSample(); go(3) }.buttonStyle(.link)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 24)
    }

    func tile(_ symbol: String, _ head: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Image(systemName: symbol).font(.title2).foregroundStyle(.tint)
            Text(head).font(.subheadline.weight(.semibold))
            Text(text).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .padding(12)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .accessibilityElement(children: .combine)
    }

    // MARK: 2. Reading your sessions (and, while that runs, the one permission worth asking for)

    var found: Int {
        if case .scanning(let n?) = model.phase { return n }
        return model.snapshot?.sources.filter { $0.status == .found }.reduce(0) { $0 + $1.sessionCount } ?? 0
    }

    var reading: some View {
        VStack(spacing: 10) {
            pip(scanning ? .work : .cheer, size: 84).padding(.top, 14)
            title(scanning ? "Reading your sessions…" : "All read.")
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("\(found)")
                    .font(.system(size: 40, weight: .semibold, design: .rounded).monospacedDigit())
                    .contentTransition(.numericText())
                    .animation(reduceMotion ? nil : .easeOut(duration: 0.3), value: found)
                Text(found == 1 ? "session" : "sessions").font(.headline).foregroundStyle(.secondary)
            }
            if !scanning, let s = model.snapshot, !s.foundSources.isEmpty {
                Text("From " + s.foundSources.map(\.label).joined(separator: ", ") + ".").font(.callout).foregroundStyle(.secondary)
            }
            Label("Read on this Mac. Nothing leaves it.", systemImage: "lock.fill")
                .font(.callout).foregroundStyle(.secondary)
            permission.padding(.top, 8)
            if scanning {
                Text("macOS may ask about your Desktop or Documents folder: that's me checking which repo a session worked in. Allow keeps repo names; declining hides them.")
                    .font(.caption).foregroundStyle(.tertiary).multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true).frame(maxWidth: 440).padding(.top, 2)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 24)
    }

    /// Notifications, asked for here because this is when there's a moment to answer. Each state says what
    /// to do; the system prompt comes from the button, never by surprise.
    var permission: some View {
        HStack(alignment: .center, spacing: 12) {
            Image(systemName: "bell.badge").font(.title2).foregroundStyle(.tint).frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                switch model.notificationStatus {
                case .authorized, .provisional:
                    Text("Notifications allowed").font(.subheadline.weight(.semibold))
                    Text("Nothing is sent until you turn an alert on, in the next step.").font(.caption).foregroundStyle(.secondary)
                case .denied:
                    Text("Notifications are off for AgentPlay").font(.subheadline.weight(.semibold))
                    Text("Turn them on in System Settings → Notifications if you want a nudge when a session needs you.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                default:
                    Text("May I notify you?").font(.subheadline.weight(.semibold))
                    Text("Only for what you choose: a session waiting on you, a plan window nearly spent. Off by default.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 8)
            switch model.notificationStatus {
            case .authorized, .provisional: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            case .denied: Button("Open System Settings") { model.openNotificationSettings() }
            default: Button("Allow") { model.requestNotifications() }.buttonStyle(.borderedProminent)
            }
        }
        .padding(12)
        .frame(maxWidth: 480)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
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
            pip(.cheer, size: 96).padding(.top, 18)
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
            .frame(maxWidth: 460)
            Spacer()
        }
        .padding(.horizontal, 24)
        .onAppear { if shown == 0 { reveal() } }
    }

    // MARK: 4. Where I live

    var home: some View {
        VStack(spacing: 12) {
            title("Find me in the menu bar").padding(.top, 22)
            body("My pose says what's up. Click me for the details, right-click me for Settings and Quit.")
            HStack(alignment: .top, spacing: 0) {
                legend(.wave, "Needs you")
                legend(.cheer, "Finished")
                legend(.oops, "Keeps failing")
                legend(.sweat, "Going fast")
                legend(.limit, "Limit reached")
            }
            .frame(maxWidth: 480)
            .padding(.vertical, 10)
            Form {
                Toggle(isOn: Binding(get: { model.prefs.alerts.enabled.contains(.needsYou) },
                                     set: { model.setPreset(.needsYou, on: $0) })) {
                    Text("Tell me when a session needs me")
                    Text("A notification when an agent stops and waits for your answer.")
                }
                .disabled(model.notificationStatus == .denied)
                if model.notificationStatus == .denied {
                    // Its own row, so the button stays clickable while the toggle above is off.
                    HStack(spacing: 8) {
                        Image(systemName: "bell.slash").foregroundStyle(.secondary)
                        Text("Notifications are off for AgentPlay in System Settings.").font(.callout)
                        Spacer()
                        Button("Open System Settings") { model.openNotificationSettings() }
                    }
                }
                Toggle(isOn: Binding(get: { model.prefs.usesClaudeAccount }, set: { model.setClaudeAccount($0) })) {
                    Text("Show Claude usage from my account")
                    Text("Asks your Claude account, like claude.ai's usage page. macOS asks once.")
                }
                Toggle("Open AgentPlay when I log in", isOn: Binding(get: { model.loginItemEnabled }, set: { model.setLoginItem($0) }))
            }
            .formStyle(.grouped).scrollDisabled(true).frame(maxWidth: 480, maxHeight: 190)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 24)
    }

    func legend(_ pose: PipPose, _ text: String) -> some View {
        VStack(spacing: 8) {
            Image(nsImage: PipIcon.image(pose, session: 0.45, weekly: 0.6))
                .renderingMode(.template).interpolation(.none).resizable()
                .frame(width: 40, height: 36).foregroundStyle(.primary)
            Text(text).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }

    // MARK: actions

    func scan() {
        guard !model.isSample else { return }
        scanning = true
        Task {
            await model.refreshNotificationStatus()
            await model.runFirstScan()
            scanning = false
            // A beat on "All read." before the reveal.
            try? await Task.sleep(for: .milliseconds(reduceMotion ? 0 : 900))
            if step == 2 { go(3) }
        }
    }

    func finish() {
        if model.isSample { model.leaveSample() }
        model.finishOnboarding()
        Windows.close(.welcome)
    }
}
