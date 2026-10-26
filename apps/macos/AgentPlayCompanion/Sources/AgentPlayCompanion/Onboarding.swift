import AgentPlayCore
import AppKit
import SwiftUI

/// First run, told by Pip, one idea per screen: who it is; reading this Mac's sessions while you watch (and
/// the one permission worth asking for while that runs); where it lives, shown with the real icon as it is
/// right now; three choices. Big type, room around it, one button. Motion follows Reduce Motion.
struct OnboardingView: View {
    @EnvironmentObject var model: CompanionModel
    @State var step = 1
    /// Filming: the first run plays itself, step by step, on the timings a viewer would take.
    var filmAutoplay = false
    @State private var scanning = false
    @State private var opened = Date()
    // The three choices, on until switched off; Start applies them. Defaults nobody has to go looking for.
    @State private var wantsAlerts = true
    @State private var wantsAccount = true
    @State private var wantsLogin = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    static let size = CGSize(width: 600, height: 560)

    var body: some View {
        VStack(spacing: 0) {
            Group {
                switch step {
                case 1: meet
                case 2: reading
                case 3: home
                default: choices
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .id(step)
            .transition(reduceMotion ? .opacity : .asymmetric(insertion: .opacity.combined(with: .offset(y: 10)), removal: .opacity))
            HStack {
                HStack(spacing: 7) {
                    ForEach(1...4, id: \.self) { i in
                        Circle().fill(i == step ? Color.primary.opacity(0.7) : Color.primary.opacity(0.15)).frame(width: 6, height: 6)
                    }
                }
                .accessibilityLabel("Step \(step) of 4")
                Spacer()
                if step > 2 { Button("Back") { go(step - 1) }.controlSize(.large) }
                primary.controlSize(.large)
            }
            .padding(.horizontal, 28).padding(.bottom, 24).padding(.top, 8)
        }
        .frame(width: Self.size.width, height: Self.size.height)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.3), value: step)
    }

    /// Filming: advance on the view's own clock (onAppear never fires in a window parked off the screen).
    func filmAdvance(_ now: Date) {
        guard filmAutoplay else { return }
        let el = now.timeIntervalSince(opened)
        if step == 1, el > 3.6 { DispatchQueue.main.async { go(2); scan() } }   // reading, then home by itself
        if step == 3, el > 7.0 { DispatchQueue.main.async { go(4) } }
    }

    @ViewBuilder var primary: some View {
        switch step {
        case 1: Button("Let\u{2019}s Go") { go(2); scan() }.keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent)
        case 2: Button(scanning ? "Reading…" : "Continue") { go(3) }.keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent).disabled(scanning)
        case 3: Button("Continue") { go(4) }.keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent)
        default: Button("Start") { finish() }.keyboardShortcut(.defaultAction).buttonStyle(.borderedProminent)
        }
    }

    func go(_ s: Int) {
        step = s
        opened = Date()
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
        Text(s).font(.system(size: 30, weight: .semibold, design: .rounded)).multilineTextAlignment(.center)
            .fixedSize(horizontal: false, vertical: true).frame(maxWidth: 460)
    }

    func body(_ s: String) -> some View {
        Text(s).font(.system(size: 17)).foregroundStyle(.secondary).multilineTextAlignment(.center)
            .lineSpacing(3).fixedSize(horizontal: false, vertical: true).frame(maxWidth: 400)
    }

    // MARK: 1. Meet Pip

    var meet: some View {
        VStack(spacing: 0) {
            Spacer()
            pip(.wave, wakeAfter: 1.2, size: 132)
            title("Hi, I\u{2019}m Pip.").padding(.top, 28)
            body("I watch your coding agents from the menu bar, so you don\u{2019}t have to watch the terminal.").padding(.top, 12)
            Spacer()
            Button("Try it with sample data instead") { model.loadSample(); go(3) }
                .buttonStyle(.plain).font(.callout).foregroundStyle(.tertiary)
                .padding(.bottom, 4)
        }
        .padding(.horizontal, 40)
    }

    // MARK: 2. Reading your sessions (and, while that runs, the one permission worth asking for)

    var found: Int {
        if case .scanning(let n?) = model.phase { return n }
        return model.snapshot?.sources.filter { $0.status == .found }.reduce(0) { $0 + $1.sessionCount } ?? 0
    }

    var reading: some View {
        VStack(spacing: 0) {
            Spacer()
            pip(scanning ? .work : .cheer, size: 96)
            title(scanning ? "Reading your sessions" : "All read.").padding(.top, 24)
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text("\(found)")
                    .font(.system(size: 44, weight: .semibold, design: .rounded).monospacedDigit())
                    .contentTransition(.numericText())
                    .animation(reduceMotion ? nil : .easeOut(duration: 0.3), value: found)
                Text(found == 1 ? "session" : "sessions").font(.system(size: 17)).foregroundStyle(.secondary)
            }
            .padding(.top, 14)
            Text(sources).font(.callout).foregroundStyle(.secondary).padding(.top, 4)
            Spacer()
            permission
            Text("macOS may ask about your Desktop or Documents folder: that\u{2019}s me checking which repo a session worked in.")
                .font(.caption).foregroundStyle(.tertiary).multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true).frame(maxWidth: 420).padding(.top, 10)
                .opacity(scanning ? 1 : 0)
        }
        .padding(.horizontal, 40)
    }

    var sources: String {
        if let s = model.snapshot, !s.foundSources.isEmpty, !scanning {
            return "From " + s.foundSources.map(\.label).joined(separator: ", ") + ". Read on this Mac; nothing leaves it."
        }
        return "Read on this Mac. Nothing leaves it."
    }

    /// Notifications, asked for here because this is when there's a moment to answer. Each state says what
    /// to do; the system prompt comes from the button, never by surprise.
    var permission: some View {
        HStack(alignment: .center, spacing: 14) {
            Image(systemName: "bell.badge").font(.title2).foregroundStyle(.tint).frame(width: 28)
            VStack(alignment: .leading, spacing: 3) {
                switch model.notificationStatus {
                case .authorized, .provisional:
                    Text("Notifications are allowed").font(.body.weight(.medium))
                    Text("Nothing is sent until you turn an alert on.").font(.callout).foregroundStyle(.secondary)
                case .denied:
                    Text("Notifications are off for AgentPlay").font(.body.weight(.medium))
                    Text("Turn them on in System Settings if you want a nudge when a session needs you.")
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                default:
                    Text("May I notify you?").font(.body.weight(.medium))
                    Text("Only for what you choose. Off by default.").font(.callout).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 8)
            switch model.notificationStatus {
            case .authorized, .provisional: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green).font(.title3)
            case .denied: Button("Open System Settings") { model.openNotificationSettings() }
            default: Button("Allow") { model.requestNotifications() }.buttonStyle(.borderedProminent)
            }
        }
        .padding(16)
        .frame(maxWidth: 480)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    // MARK: 3. Where I live: the real icon, as it is right now, and what each pose means

    var home: some View {
        VStack(spacing: 0) {
            Spacer()
            title("This is me, in your menu bar.")
            body("My pose says what\u{2019}s up. Click me for the details; right-click for Settings and Quit.").padding(.top, 12)
            livePreview.padding(.top, 30)
            HStack(spacing: 0) {
                legend(.wave, "Needs you")
                legend(.cheer, "Finished")
                legend(.oops, "Keeps failing")
                legend(.sweat, "Going fast")
                legend(.limit, "Limit reached")
            }
            .frame(maxWidth: 500)
            .padding(.top, 34)
            Spacer()
        }
        .padding(.horizontal, 40)
    }

    /// A slice of menu bar with the status item exactly as it draws right now: the pose, the fill, the line.
    var livePreview: some View {
        let pose = model.pipPose
        let lv = Pip.levels(model.snapshot?.limits ?? [], now: Date())
        let say = Pip.sentence(pose, live: model.snapshot?.live ?? [], limits: model.snapshot?.limits ?? [],
                               recent: model.pipRecent, now: Date()).title
        return HStack(spacing: 10) {
            Image(nsImage: PipIcon.image(pose, session: lv.session, weekly: lv.weekly))
                .renderingMode(.template).interpolation(.none).resizable()
                .frame(width: 40, height: 36).foregroundStyle(.primary)
            Text(say).font(.system(size: 17, weight: .medium))
        }
        .padding(.horizontal, 18).frame(height: 56)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.primary.opacity(0.07)))
        .accessibilityLabel("Menu bar, right now: \(say)")
    }

    func legend(_ pose: PipPose, _ text: String) -> some View {
        VStack(spacing: 10) {
            Image(nsImage: PipIcon.image(pose, session: 0.45, weekly: 0.6))
                .renderingMode(.template).interpolation(.none).resizable()
                .frame(width: 40, height: 36).foregroundStyle(.primary)
            Text(text).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }

    // MARK: 4. Three choices

    var choices: some View {
        VStack(spacing: 0) {
            Spacer()
            title("Three choices.")
            body("On, as I\u{2019}d suggest. Switch any off here, or change them in Settings later.").padding(.top, 12)
            Form {
                Toggle(isOn: $wantsAlerts) {
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
                Toggle(isOn: $wantsAccount) {
                    Text("Show Claude usage from my account")
                    Text("Live 5-hour and weekly limits, the same numbers as claude.ai\u{2019}s usage page. Reads the login Claude Code saved in your Keychain; macOS asks once.")
                }
                Toggle("Open AgentPlay when I log in", isOn: $wantsLogin)
            }
            .formStyle(.grouped).scrollDisabled(true).scrollContentBackground(.hidden)
            .frame(maxWidth: 500, maxHeight: model.notificationStatus == .denied ? 240 : 200)
            .padding(.top, 18)
            Spacer()
        }
        .padding(.horizontal, 40)
    }

    // MARK: actions

    func scan() {
        guard !model.isSample else { return }
        scanning = true
        Task {
            await model.refreshNotificationStatus()
            await model.runFirstScan()
            scanning = false
            // A beat on "All read." before moving on.
            try? await Task.sleep(for: .milliseconds(reduceMotion ? 0 : 900))
            if step == 2 { go(3) }
        }
    }

    func finish() {
        if model.isSample { model.leaveSample() }
        // The choices take effect now, not as they were toggled: Back and forth changes nothing until Start.
        if wantsAlerts, model.notificationStatus != .denied { model.setPreset(.needsYou, on: true) }
        if wantsAccount != model.prefs.usesClaudeAccount { model.setClaudeAccount(wantsAccount) }
        if wantsLogin != model.loginItemEnabled { model.setLoginItem(wantsLogin) }
        model.finishOnboarding()
        Windows.close(.welcome)
    }
}
