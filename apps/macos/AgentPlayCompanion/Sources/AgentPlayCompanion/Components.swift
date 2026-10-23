import AgentPlayCore
import SwiftUI

/// Activity dot. Filled lime: a running process is writing this run. Ring: activity recorded recently. Grey: idle.
struct ActivityDot: View {
    var state: ActivityState
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        ZStack {
            switch state {
            case .active: Circle().fill(Theme.lime)
            case .recent: Circle().stroke(Theme.limeInk(scheme), lineWidth: 1.5)
            case .idle: Circle().fill(Theme.muted(scheme).opacity(0.5))
            }
        }
        .frame(width: 8, height: 8)
        .accessibilityLabel(state == .active ? "Running" : state == .recent ? "Activity recorded recently" : "Idle")
    }
}

/// The run card on Now.
struct RunCard: View {
    @EnvironmentObject var model: CompanionModel
    var run: RunActivity
    var now: Date
    @Environment(\.colorScheme) private var scheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hover = false

    var expanded: Bool { model.expandedRun == run.sessionId }
    var pinned: Bool { model.prefs.pinnedSessions.contains(run.sessionId) }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.s2) {
            Button {
                withAnimation(Theme.motion(reduceMotion)) {
                    model.expandedRun = expanded ? nil : run.sessionId
                }
            } label: { header }
            .buttonStyle(.plain)
            .accessibilityHint(expanded ? "Collapses the run" : "Shows the latest recorded event and replay actions")

            if expanded {
                MicroTimeline(run: run) { seq in model.openViewer(sessionId: run.sessionId, seq: seq) }
                    .transition(.opacity)
                HStack(spacing: Theme.s2) {
                    Button("Open replay") { model.openViewer(sessionId: run.sessionId, seq: run.lastEvent?.seq) }
                        .buttonStyle(QuietButtonStyle(prominent: true))
                        .overlay(alignment: .center) { if model.justCompleted.contains(run.sessionId) { Flourish() } }
                        .keyboardShortcut(.defaultAction)
                    if let err = run.lastError {
                        Button("Jump to last error") { model.openViewer(sessionId: run.sessionId, seq: err.seq, errorsTab: true) }
                            .buttonStyle(QuietButtonStyle())
                    }
                    Spacer(minLength: 0)
                    Button("Full history") { model.openViewer(sessionId: run.sessionId, seq: 0) }
                        .buttonStyle(LinkButtonStyle())
                }
            }
        }
        .padding(Theme.s3)
        .background(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous)
            .fill(expanded || hover ? Theme.card(scheme) : Color.clear))
        .overlay(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous)
            .stroke(Theme.hairline(scheme), lineWidth: expanded ? 0 : 1))
        .onHover { hover = $0 }
        .contextMenu { RunMenu(run: run) }
        .accessibilityElement(children: .contain)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: Theme.s2) {
                ActivityDot(state: run.state)
                Text(run.agentLabel).font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.ink(scheme))
                if let repo = run.repo {
                    Text("/ \(Format.truncate(repo))").font(.system(size: 13)).foregroundStyle(Theme.muted(scheme))
                        .help(repo)
                }
                if pinned { Image(systemName: "pin.fill").font(.system(size: 9)).foregroundStyle(Theme.muted(scheme)) }
                Spacer(minLength: Theme.s2)
                Text(timeLabel).font(.system(size: 11).monospacedDigit()).foregroundStyle(Theme.muted(scheme))
            }
            HStack(spacing: 4) {
                Text(lastLine).lineLimit(1).truncationMode(.middle)
                    .font(.system(size: 12)).foregroundStyle(Theme.ink(scheme).opacity(0.85))
                if let flag = errorFlag {
                    Text("· \(flag)").font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.error(scheme))
                        .lineLimit(1)
                }
            }
            .padding(.leading, 16)
            if let behind = run.mapBehindSeconds, behind >= 60, !model.isSample {
                Text("Replay is \(Format.duration(TimeInterval(behind))) behind the transcript. Catching up.")
                    .font(.system(size: 11)).foregroundStyle(Theme.muted(scheme)).padding(.leading, 16)
            }
            if run.state != .active, run.activity.kind != .unavailable, run.state == .recent {
                Text("Activity recorded \(Format.relative(run.lastActivityAt, now: now)). No running process seen.")
                    .font(.system(size: 11)).foregroundStyle(Theme.muted(scheme)).padding(.leading, 16)
            }
        }
        .contentShape(Rectangle())
    }

    private var timeLabel: String {
        if run.state == .active, let s = run.startedAt { return Format.duration(now.timeIntervalSince(s)) }
        return Format.relative(run.latestAt, now: now)
    }

    private var lastLine: String {
        if let p = run.pendingTool {
            return "Running: \(p.event.label) · \(Format.duration(TimeInterval(p.elapsed.value ?? 0)))"
        }
        guard let e = run.lastEvent else { return "No recorded events" }
        switch e.kind {
        case "edit": return "Last: edited \(e.target ?? "a file")"
        case "read", "search": return "Last: read \(e.target ?? e.label)"
        default: return "Last: \(e.label)"
        }
    }

    private var errorFlag: String? {
        guard let e = run.lastError else { return nil }
        if let last = run.lastEvent, last.seq == e.seq { return "error recorded" }
        return nil
    }
}

struct RunMenu: View {
    @EnvironmentObject var model: CompanionModel
    var run: RunActivity
    var body: some View {
        Button("Open replay") { model.openViewer(sessionId: run.sessionId, seq: run.lastEvent?.seq) }
        Divider()
        Button("Copy redacted run summary") { model.copySummary(run) }
        Button("Copy local replay link") { model.copyLink(run) }
            .help("A 127.0.0.1 link: it opens only on this Mac, with this Mac's AgentPlay data.")
        Divider()
        Button(model.prefs.pinnedSessions.contains(run.sessionId) ? "Unpin" : "Pin to top") { model.togglePin(run.sessionId) }
        Button("Mute alerts for this run") { model.muteRun(run.sessionId) }
    }
}

/// Five ticks from the real recorded sequence. Drag across to read the ordinal steps; click a tick to open that moment.
struct MicroTimeline: View {
    var run: RunActivity
    var open: (Int) -> Void
    @State private var focus: Int?
    @State private var aligned = false
    @Environment(\.colorScheme) private var scheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var events: [SessionEvent] { Array(run.recentEvents.suffix(5)) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            GeometryReader { geo in
                let w = geo.size.width
                let step = events.count > 1 ? w / CGFloat(events.count - 1) : 0
                ZStack(alignment: .leading) {
                    Rectangle().fill(Theme.hairline(scheme).opacity(2)).frame(height: 1)
                    ForEach(Array(events.enumerated()), id: \.element.seq) { i, e in
                        let isLast = i == events.count - 1
                        Tick(event: e, highlighted: focus == i || (focus == nil && isLast))
                            .position(x: events.count > 1 ? CGFloat(i) * step : w / 2, y: 9)
                            .onTapGesture { open(e.seq) }
                            .accessibilityElement()
                            .accessibilityLabel("\(e.step): \(e.label)")
                            .accessibilityAddTraits(.isButton)
                            .accessibilityAction { open(e.seq) }
                    }
                    // Map to moment: four dots settle onto the latest tick when the card opens.
                    if !reduceMotion, let _ = events.last {
                        ForEach(0..<4, id: \.self) { k in
                            let angle = Double(k) * .pi / 2 + .pi / 4
                            Circle().fill(Theme.lime).frame(width: 3, height: 3)
                                .position(x: (events.count > 1 ? w : w / 2) + (aligned ? 0 : cos(angle) * 14),
                                          y: 9 + (aligned ? 0 : sin(angle) * 14))
                                .opacity(aligned ? 0 : 1)
                        }
                    }
                }
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onChanged { v in
                    guard events.count > 1 else { return }
                    focus = min(events.count - 1, max(0, Int((v.location.x / step).rounded())))
                }.onEnded { _ in })
            }
            .frame(height: 18)
            Text(caption).font(Theme.mono).foregroundStyle(Theme.muted(scheme)).lineLimit(1).truncationMode(.middle)
        }
        .onAppear { withAnimation(Theme.motion(reduceMotion)) { aligned = true } }
        .onHover { if !$0 { focus = nil } }
    }

    private var caption: String {
        if let f = focus, events.indices.contains(f) {
            let e = events[f]
            return "#\(e.seq) \(e.step) · \(e.label)\(e.target.map { " · \($0)" } ?? "")"
        }
        return events.map(\.step).joined(separator: " → ")
    }

    struct Tick: View {
        var event: SessionEvent
        var highlighted: Bool
        @Environment(\.colorScheme) private var scheme
        var body: some View {
            let color: Color = event.err ? Theme.error(scheme) : event.pending ? Theme.limeInk(scheme)
                : Theme.ink(scheme).opacity(0.55)
            Capsule().fill(highlighted ? (event.err ? Theme.error(scheme) : Theme.lime) : color)
                .frame(width: highlighted ? 4 : 2, height: highlighted ? 16 : 10)
                .frame(width: 18, height: 18)
                .contentShape(Rectangle())
        }
    }
}

/// One quiet convergence over the replay button when a run just finished.
struct Flourish: View {
    @State private var on = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        ZStack {
            ForEach(0..<4, id: \.self) { k in
                let a = Double(k) * .pi / 2
                Circle().fill(Theme.lime).frame(width: 3, height: 3)
                    .offset(x: on ? 0 : cos(a) * 22, y: on ? 0 : sin(a) * 14)
                    .opacity(on ? 0 : 0.9)
            }
        }
        .allowsHitTesting(false)
        .onAppear { if !reduceMotion { withAnimation(.timingCurve(0.16, 1, 0.3, 1, duration: 0.6)) { on = true } } }
    }
}

/// Session constellation: an abstract radial of real recorded category counts for one run. No invented links.
struct Constellation: View {
    var run: RunActivity
    @Environment(\.colorScheme) private var scheme
    static let order = ["read", "search", "edit", "exec", "verify", "mcp", "subagent", "web", "error", "compaction"]

    var body: some View {
        let counts = Self.order.map { (k: $0, n: run.categories[$0] ?? 0) }.filter { $0.n > 0 }
        let maxN = Double(counts.map(\.n).max() ?? 1)
        VStack(spacing: Theme.s3) {
            Canvas { ctx, size in
                let c = CGPoint(x: size.width / 2, y: size.height / 2)
                let r0: CGFloat = 18, r1 = min(size.width, size.height) / 2 - 24
                ctx.fill(Path(ellipseIn: CGRect(x: c.x - 4, y: c.y - 4, width: 8, height: 8)), with: .color(Theme.lime))
                for (i, item) in counts.enumerated() {
                    let a = Double(i) / Double(max(1, counts.count)) * 2 * .pi - .pi / 2
                    let len = r0 + (r1 - r0) * CGFloat(log1p(Double(item.n)) / log1p(maxN))
                    let p = CGPoint(x: c.x + cos(a) * len, y: c.y + sin(a) * len)
                    var line = Path(); line.move(to: c); line.addLine(to: p)
                    ctx.stroke(line, with: .color(Theme.hairline(scheme).opacity(3)), lineWidth: 1)
                    let dot: CGFloat = item.k == "error" ? 6 : 5
                    ctx.fill(Path(ellipseIn: CGRect(x: p.x - dot / 2, y: p.y - dot / 2, width: dot, height: dot)),
                             with: .color(item.k == "error" ? Theme.error(scheme) : Theme.ink(scheme)))
                    let label = Text("\(item.k) \(item.n)").font(.system(size: 9)).foregroundColor(Theme.muted(scheme))
                    ctx.draw(label, at: CGPoint(x: c.x + cos(a) * (len + 12), y: c.y + sin(a) * (len + 10)))
                }
            }
            .frame(height: 220)
            .accessibilityLabel("Session constellation: " + counts.map { "\($0.n) \($0.k)" }.joined(separator: ", "))
            Text("Session constellation · \(run.agentLabel). Spokes are recorded event counts by kind, log scaled.")
                .font(.system(size: 11)).foregroundStyle(Theme.muted(scheme)).multilineTextAlignment(.center)
        }
    }
}
