import AgentPlayCore
import AppKit
import SwiftUI

/// First run, told by Pip on a dark stage, one idea per screen and motion between them:
///  1. Wake: Pip is asleep; a click wakes it and it types hello.
///  2. Read: your sessions fly in while Pip's jar fills; your own repos, counted on this Mac.
///  3. The first thing it noticed, from your own sessions, with the fix one click away.
///  4. Practice: a pretend menu bar where Pip waves; you click it and the terminal comes forward. Then the alerts,
///     each with what it says and a sample you can send to yourself.
///  5. Move in: two last choices; the window flies into the menu bar, where Pip lands.
/// Every animation follows Reduce Motion.
struct OnboardingView: View {
    @EnvironmentObject var model: CompanionModel
    @State var step = 1
    /// Filming: the first run plays itself on the timings a viewer would take.
    var filmAutoplay = false
    @State private var opened = Date()
    @State private var wokeAt: Date?
    @State private var scanning = false
    @State private var scanError: String?
    @State private var flown = 0               // repo cards that have flown into Pip
    @State private var clickedPip = false      // the practice click
    @State private var showFixAfter = false
    @State private var alertsOn: Set<AlertPreset> = [.needsYou, .finished]
    @State private var wantsAccount = true
    @State private var wantsLogin = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    static let size = CGSize(width: 720, height: 600)
    static let lime = Color.accentColor

    var body: some View {
        ZStack {
            stage
            Group {
                switch step {
                case 1: wake
                case 2: read
                case 3: noticed
                case 4: practice
                default: moveIn
                }
            }
            .id(step)
            .transition(reduceMotion ? .opacity : .asymmetric(insertion: .opacity.combined(with: .scale(scale: 0.97)).combined(with: .offset(y: 16)),
                                                             removal: .opacity.combined(with: .offset(y: -16))))
            VStack {
                Spacer()
                HStack(spacing: 7) {
                    ForEach(1...5, id: \.self) { i in
                        Capsule().fill(i == step ? Self.lime : Color.primary.opacity(0.18)).frame(width: i == step ? 18 : 6, height: 6)
                    }
                }
                .animation(.spring(response: 0.4, dampingFraction: 0.8), value: step)
                .padding(.bottom, 18)
                .accessibilityLabel("Step \(step) of 5")
            }
        }
        .frame(width: Self.size.width, height: Self.size.height)
        .animation(reduceMotion ? nil : .spring(response: 0.55, dampingFraction: 0.86), value: step)
    }

    /// The stage: the system's own window background, light or dark as the Mac is set. No tint, no texture.
    var stage: some View {
        Color(nsColor: .windowBackgroundColor).ignoresSafeArea()
    }

    func go(_ s: Int) {
        withAnimation { step = s }
        opened = Date()
    }

    // MARK: Pip

    /// Pip large, moving on a quarter-second beat. `fill` is the jar, 0...1.
    func pip(_ pose: PipPose, size: CGFloat, fill: Double = 0.45, hop: Bool = false) -> some View {
        TimelineView(.periodic(from: .now, by: reduceMotion ? 3600 : 0.125)) { t in
            let tick = reduceMotion ? 0 : Int(t.date.timeIntervalSinceReferenceDate * 4)
            let _ = filmAdvance(t.date)
            let blink = tick % 14 == 0 && pose != .sleep && pose != .limit
            Image(nsImage: PipIcon.image(pose, frame: (pose == .work || pose == .wave) ? tick / 2 : 0,
                                         session: fill, weekly: 0.6, eyes: blink ? .closed : .pose, hop: hop))
                .renderingMode(.template).interpolation(.none).resizable()
                .frame(width: size, height: size * 0.9)
                .foregroundStyle(.primary)
                .accessibilityLabel("Pip, \(PipIcon.description(pose).lowercased())")
        }
    }

    func headline(_ s: String) -> some View {
        Text(s).font(.system(size: 34, weight: .semibold, design: .rounded)).foregroundStyle(.primary)
            .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true).frame(maxWidth: 560)
    }

    func line(_ s: String) -> some View {
        Text(s).font(.system(size: 18)).foregroundStyle(.primary.opacity(0.68)).multilineTextAlignment(.center)
            .lineSpacing(3).fixedSize(horizontal: false, vertical: true).frame(maxWidth: 500)
    }

    func primaryButton(_ title: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.system(size: 16, weight: .semibold)).foregroundStyle(.white)
                .padding(.horizontal, 22).padding(.vertical, 11)
                .background(Self.lime, in: Capsule())
        }
        .buttonStyle(.plain)
        .keyboardShortcut(.defaultAction)
    }

    // MARK: 1. Wake

    static let hello = "Hi, I\u{2019}m Pip. I watch your coding agents, so you don\u{2019}t have to watch the terminal."

    var wake: some View {
        VStack(spacing: 0) {
            Spacer()
            ZStack {
                if wokeAt == nil {
                    Button { wakeUp() } label: { pip(.sleep, size: 170, fill: 0.12) }.buttonStyle(.plain)
                } else {
                    pip(.wave, size: 170, fill: 0.45, hop: hopping)
                }
            }
            .frame(height: 170)
            TimelineView(.periodic(from: .now, by: 0.03)) { t in
                let n = wokeAt.map { reduceMotion ? Self.hello.count : Int(t.date.timeIntervalSince($0) * 34) } ?? 0
                if wokeAt == nil {
                    Text("Pip is asleep. Click to wake it.").font(.system(size: 18)).foregroundStyle(.primary.opacity(0.55))
                        .opacity(0.55 + 0.45 * abs(sin(t.date.timeIntervalSinceReferenceDate * 1.6)))
                } else {
                    Text(String(Self.hello.prefix(n)))
                        .font(.system(size: 26, weight: .semibold, design: .rounded)).foregroundStyle(.primary)
                        .multilineTextAlignment(.center).frame(maxWidth: 520, minHeight: 70, alignment: .top)
                }
            }
            .padding(.top, 34)
            Spacer()
            Group {
                if wokeAt != nil {
                    primaryButton("Show Me My Agents") { go(2); scan() }
                } else {
                    Button("Just show me with sample data") { model.loadSample(); wakeUp(); go(3) }
                        .buttonStyle(.plain).font(.callout).foregroundStyle(.primary.opacity(0.4))
                }
            }
            .padding(.bottom, 60)
        }
        .padding(.horizontal, 40)
    }

    @State private var hopping = false
    func wakeUp() {
        guard wokeAt == nil else { return }
        withAnimation(.spring(response: 0.35, dampingFraction: 0.5)) { wokeAt = Date(); hopping = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { hopping = false }
    }

    // MARK: 2. Read

    var found: Int {
        if case .scanning(let n?) = model.phase { return n }
        return model.snapshot?.sources.filter { $0.status == .found }.reduce(0) { $0 + $1.sessionCount } ?? 0
    }

    var repos: [String] {
        var seen = Set<String>(), out: [String] = []
        for r in model.snapshot?.runs ?? [] {
            if let repo = r.repo, !seen.contains(repo) { seen.insert(repo); out.append(repo) }
            if out.count == 8 { break }
        }
        return out
    }

    static let agents = ["Claude Code", "Codex", "Cursor", "Copilot", "OpenCode", "Kiro"]

    var read: some View {
        VStack(spacing: 0) {
            Spacer()
            ZStack {
                // The agents it reads, around Pip.
                ForEach(Array(Self.agents.enumerated()), id: \.offset) { i, name in
                    let a = Double(i) / Double(Self.agents.count) * 2 * .pi - .pi / 2
                    Text(name).font(.system(size: 13, weight: .medium)).foregroundStyle(.primary.opacity(0.75))
                        .padding(.horizontal, 10).padding(.vertical, 5)
                        .background(.primary.opacity(0.08), in: Capsule())
                        .offset(x: cos(a) * 210, y: sin(a) * 105)
                        .opacity(scanning || scanError == nil ? 1 : 0.3)
                }
                // Your repos, flying in.
                ForEach(Array(repos.enumerated()), id: \.offset) { i, repo in
                    let a = Double(i) / Double(max(1, repos.count)) * 2 * .pi
                    Text(repo).font(.system(size: 12, weight: .semibold, design: .monospaced)).foregroundStyle(.white)
                        .padding(.horizontal, 8).padding(.vertical, 4)
                        .background(Self.lime, in: Capsule())
                        .offset(x: i < flown ? 0 : cos(a) * 320, y: i < flown ? 0 : sin(a) * 190)
                        .scaleEffect(i < flown ? 0.2 : 1)
                        .opacity(i < flown ? 0 : 1)
                }
                pip(scanning ? .work : scanError != nil ? .sleep : .cheer, size: 130,
                    fill: min(1, 0.08 + 0.9 * (1 - exp(-Double(found) / 150))))
            }
            .frame(height: 260)
            if let scanError {
                headline("I couldn\u{2019}t read your sessions.").padding(.top, 10)
                line(scanError).padding(.top, 8)
            } else {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("\(found)").font(.system(size: 54, weight: .semibold, design: .rounded).monospacedDigit())
                        .foregroundStyle(.primary).contentTransition(.numericText())
                        .animation(reduceMotion ? nil : .easeOut(duration: 0.3), value: found)
                    Text(found == 1 ? "session" : "sessions").font(.system(size: 20)).foregroundStyle(.primary.opacity(0.6))
                }
                .padding(.top, 6)
                line(scanning ? "Reading the sessions already on this Mac. Nothing leaves it." : readSummary).padding(.top, 4)
            }
            Spacer()
            Group {
                if scanError != nil { primaryButton("Try Again") { scan() } }
                else if !scanning { primaryButton("What Did You Find?") { go(3) } }
                else { Text("macOS may ask about your Documents or Desktop: that\u{2019}s me seeing which repo a session worked in.")
                        .font(.caption).foregroundStyle(.primary.opacity(0.4)).multilineTextAlignment(.center).frame(maxWidth: 440) }
            }
            .padding(.bottom, 60)
        }
        .padding(.horizontal, 40)
    }

    var readSummary: String {
        let srcs = model.snapshot?.foundSources.map(\.label) ?? []
        return srcs.isEmpty ? "All read, on this Mac." : "From " + srcs.joined(separator: ", ") + ". All read, on this Mac."
    }

    // MARK: 3. What it noticed

    var noticed: some View {
        VStack(spacing: 0) {
            Spacer()
            pip(.cheer, size: 96, fill: 0.55).padding(.bottom, 18)
            if let fix = model.snapshot?.insights?.fixes.first {
                headline("The first thing I noticed.")
                VStack(alignment: .leading, spacing: 12) {
                    Text(Plain.text(fix.pattern)).font(.system(size: 22, weight: .semibold)).foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let cause = fix.hook?.cause {
                        Text(cause).font(.system(size: 16)).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                    }
                    Text("Fixing it: " + Plain.text(fix.effect)).font(.system(size: 16)).foregroundStyle(.primary.opacity(0.7))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(22)
                .frame(maxWidth: 560, alignment: .leading)
                .background(.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Self.lime.opacity(0.35)))
                .padding(.top, 20)
                Spacer()
                HStack(spacing: 14) {
                    Button("Later") { go(4) }.buttonStyle(.plain).foregroundStyle(.primary.opacity(0.6))
                    primaryButton("Show Me the Fix After This") { showFixAfter = true; go(4) }
                }
                .padding(.bottom, 60)
            } else {
                headline("Here\u{2019}s what I learnt.")
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(Array((model.snapshot.map(Pip.learnings) ?? []).enumerated()), id: \.offset) { _, l in
                        Label(l.text, systemImage: l.symbol).font(.system(size: 16)).foregroundStyle(.primary)
                            .padding(14).frame(maxWidth: 540, alignment: .leading)
                            .background(.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    }
                }
                .padding(.top, 20)
                Spacer()
                primaryButton("Continue") { go(4) }.padding(.bottom, 60)
            }
        }
        .padding(.horizontal, 40)
    }

    // MARK: 4. Practice, then the alerts

    var practice: some View {
        VStack(spacing: 0) {
            headline(clickedPip ? "That\u{2019}s it." : "This is how I get your attention.").padding(.top, 44)
            line(clickedPip ? "A wave means an agent is waiting for you. A click takes you to it."
                 : "When an agent stops and waits, I wave from the menu bar. Click me.").padding(.top, 8)
            fakeBar.padding(.top, 26)
            if clickedPip {
                fakeTerminal.transition(.move(edge: .bottom).combined(with: .opacity)).padding(.top, 14)
                alerts.transition(.opacity).padding(.top, 18)
            }
            Spacer()
            if clickedPip { primaryButton("Continue") { go(5) }.padding(.bottom, 60) }
        }
        .padding(.horizontal, 40)
        .animation(reduceMotion ? nil : .spring(response: 0.5, dampingFraction: 0.82), value: clickedPip)
    }

    /// A slice of menu bar with Pip waving beside "docs-site".
    var fakeBar: some View {
        HStack(spacing: 18) {
            Spacer()
            Image(systemName: "wifi").foregroundStyle(.primary.opacity(0.7))
            Button { withAnimation { clickedPip = true } } label: {
                HStack(spacing: 6) {
                    pip(clickedPip ? .cheer : .wave, size: 30, fill: 0.45)
                    if !clickedPip { Text("docs-site").font(.system(size: 13, weight: .medium)).foregroundStyle(.primary) }
                }
                .padding(.horizontal, 8).padding(.vertical, 3)
                .background(RoundedRectangle(cornerRadius: 6).fill(.white.opacity(clickedPip ? 0.0 : 0.12)))
                .overlay(
                    TimelineView(.periodic(from: .now, by: 0.05)) { t in
                        RoundedRectangle(cornerRadius: 8).strokeBorder(Self.lime, lineWidth: 2)
                            .opacity(clickedPip || reduceMotion ? 0 : 0.35 + 0.65 * abs(sin(t.date.timeIntervalSinceReferenceDate * 3)))
                            .padding(-4)
                    }
                )
            }
            .buttonStyle(.plain)
            Image(systemName: "magnifyingglass").foregroundStyle(.primary.opacity(0.7))
            Text("10:42 AM").font(.system(size: 13)).foregroundStyle(.primary.opacity(0.7))
        }
        .padding(.horizontal, 16).frame(height: 34)
        .background(.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .frame(maxWidth: 560)
    }

    var fakeTerminal: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                ForEach([Color.red, .yellow, .green], id: \.self) { c in Circle().fill(c.opacity(0.8)).frame(width: 9, height: 9) }
                Text("Terminal · docs-site").font(.system(size: 12)).foregroundStyle(.primary.opacity(0.5)).padding(.leading, 6)
            }
            Text("Claude wants to run:  pnpm build").font(.system(size: 13, design: .monospaced)).foregroundStyle(.primary)
            Text("❯ 1. Yes   2. No, tell Claude what to do differently").font(.system(size: 13, design: .monospaced)).foregroundStyle(Self.lime)
        }
        .padding(14)
        .frame(maxWidth: 560, alignment: .leading)
        .background(Color.black.opacity(0.85), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .environment(\.colorScheme, .dark)
    }

    struct AlertChoice: Identifiable {
        var preset: AlertPreset
        var title: String
        var sampleTitle: String
        var sampleBody: String
        var pose: PipPose
        var id: AlertPreset { preset }
    }

    static let choices: [AlertChoice] = [
        .init(preset: .needsYou, title: "A session needs me", sampleTitle: "Claude Code needs you", sampleBody: "docs-site is waiting for your answer.", pose: .wave),
        .init(preset: .finished, title: "A session finishes", sampleTitle: "Done in 14 min", sampleBody: "212 tool calls, 2 errors. Last step: pnpm test.", pose: .cheer),
        .init(preset: .repeatedFailure, title: "A command keeps failing", sampleTitle: "pnpm test failed 3 times", sampleBody: "Three in a row in one run, no pass between.", pose: .oops),
        .init(preset: .allowanceLow, title: "My plan is about to run out", sampleTitle: "Weekly limit: out in about 5 hours", sampleBody: "97% used; resets Thursday 6:30 AM.", pose: .sweat),
    ]

    var alerts: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Tell me when…").font(.system(size: 15, weight: .semibold)).foregroundStyle(.primary)
                Spacer()
                switch model.notificationStatus {
                case .authorized, .provisional: Label("Notifications allowed", systemImage: "checkmark.circle.fill").font(.caption).foregroundStyle(.green)
                case .denied: Button("Turn On in System Settings") { model.openNotificationSettings() }.font(.caption)
                default: Button("Allow Notifications") { model.requestNotifications() }.font(.caption)
                }
            }
            ForEach(Self.choices) { c in
                HStack(spacing: 12) {
                    Toggle("", isOn: Binding(get: { alertsOn.contains(c.preset) },
                                             set: { if $0 { alertsOn.insert(c.preset) } else { alertsOn.remove(c.preset) } }))
                        .labelsHidden().toggleStyle(.switch).controlSize(.small)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(c.title).font(.system(size: 14, weight: .medium)).foregroundStyle(.primary)
                        Text("\u{201C}\(c.sampleTitle): \(c.sampleBody)\u{201D}").font(.system(size: 12)).foregroundStyle(.primary.opacity(0.5)).lineLimit(1)
                    }
                    Spacer()
                    Button("Send a sample") {
                        if model.notificationStatus == .authorized || model.notificationStatus == .provisional {
                            Notifier.postSample(title: c.sampleTitle, body: c.sampleBody, pose: c.pose)
                        } else {
                            model.requestNotifications()
                        }
                    }
                    .font(.caption)
                }
                .padding(.vertical, 4)
            }
        }
        .padding(16)
        .frame(maxWidth: 560)
        .background(.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    // MARK: 5. Move in

    var moveIn: some View {
        VStack(spacing: 0) {
            Spacer()
            pip(.wave, size: 120, fill: 0.45)
            headline("I\u{2019}ll be up in your menu bar.").padding(.top, 22)
            line("Click me for what\u{2019}s running and your plan. Right-click for Settings and Quit.").padding(.top, 8)
            VStack(alignment: .leading, spacing: 12) {
                Toggle(isOn: $wantsAccount) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Show Claude usage from my account").foregroundStyle(.primary)
                        Text("Live 5-hour and weekly limits, as claude.ai shows them. macOS asks once for the login Claude Code saved.")
                            .font(.caption).foregroundStyle(.primary.opacity(0.5)).fixedSize(horizontal: false, vertical: true)
                    }
                }
                Toggle(isOn: $wantsLogin) { Text("Open when I log in").foregroundStyle(.primary) }
            }
            .toggleStyle(.switch)
            .padding(18).frame(maxWidth: 480)
            .background(.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .padding(.top, 24)
            Spacer()
            primaryButton("Move In") { finish() }.padding(.bottom, 60)
        }
        .padding(.horizontal, 40)
    }

    // MARK: actions

    /// Filming: advance on the view's own clock (onAppear never fires in a window parked off the screen).
    func filmAdvance(_ now: Date) {
        guard filmAutoplay else { return }
        let el = now.timeIntervalSince(opened)
        if step == 1, wokeAt == nil, el > 1.2 { DispatchQueue.main.async { wakeUp() } }
        if step == 1, el > 4.0 { DispatchQueue.main.async { go(2); scan() } }
        if step == 3, el > 6.0 { DispatchQueue.main.async { go(4) } }
        if step == 4, !clickedPip, el > 3.0 { DispatchQueue.main.async { withAnimation { clickedPip = true } } }
    }

    func scan() {
        guard !model.isSample else { return }
        scanning = true
        scanError = nil
        flown = 0
        Task {
            await model.refreshNotificationStatus()
            await model.runFirstScan()
            scanning = false
            if case .failed(let e) = model.phase { scanError = e.localizedDescription; return }
            if model.snapshot == nil { scanError = "AgentPlay couldn\u{2019}t start reading. Try again in a moment."; return }
            // The repos fly into Pip one after another.
            for i in 0..<repos.count {
                try? await Task.sleep(for: .milliseconds(reduceMotion ? 0 : 140))
                withAnimation(.spring(response: 0.55, dampingFraction: 0.8)) { flown = i + 1 }
            }
        }
    }

    func finish() {
        let wasSample = model.isSample
        if wasSample { model.leaveSample() }
        // The choices take effect now, not as they were toggled.
        if model.notificationStatus != .denied {
            for c in Self.choices { model.setPreset(c.preset, on: alertsOn.contains(c.preset)) }
        }
        if wantsAccount != model.prefs.usesClaudeAccount { model.setClaudeAccount(wantsAccount) }
        if wantsLogin != model.loginItemEnabled { model.setLoginItem(wantsLogin) }
        model.finishOnboarding()
        if wasSample { Task { await model.runFirstScan() } }
        let thenFix = showFixAfter
        flyIntoMenuBar {
            Windows.close(.welcome)
            if thenFix { model.showInsights(.fixes) }
        }
    }

    /// The window shrinks into Pip's spot in the menu bar, then closes.
    func flyIntoMenuBar(_ done: @escaping () -> Void) {
        guard !reduceMotion, let w = NSApp.windows.first(where: { $0.title == "Welcome to AgentPlay" && $0.isVisible }),
              let target = StatusItemController.current?.pipScreenRect else { done(); return }
        let end = NSRect(x: target.midX - 12, y: target.midY - 10, width: 24, height: 20)
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.55
            ctx.timingFunction = CAMediaTimingFunction(controlPoints: 0.6, 0.0, 0.2, 1.0)
            w.animator().setFrame(end, display: true)
            w.animator().alphaValue = 0
        }, completionHandler: {
            MainActor.assumeIsolated {
                done()
                w.alphaValue = 1
                model.nextTip()   // Pip hops as it lands
            }
        })
    }
}
