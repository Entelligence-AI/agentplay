import AgentPlayCore
import AppKit
import Foundation
import SwiftUI

/// Dev builds only: `--selftest` exercises the real bundle end to end and prints JSON results.
/// Runtime → snapshot → viewer start → health → deep link served → viewer stopped (only ours).
enum SelfTest {
    static func run() async -> Int32 {
        var report: [String: Any] = [:]
        func fail(_ k: String, _ v: Any) -> Int32 { report[k] = v; print(json(report)); return 1 }
        guard case .success(let rt) = Runtime.locate(allowDevOverride: true) else { return fail("runtime", "missing") }
        report["runtime"] = rt.bundled ? "bundled" : rt.python.path
        let svc = SnapshotService(runtime: rt)
        let t0 = Date()
        switch await svc.fetch(.read) {
        case .failure(let e): return fail("snapshot", e.localizedDescription)
        case .success(let s):
            report["snapshotMs"] = Int(Date().timeIntervalSince(t0) * 1000)
            report["runs"] = s.runs.count
            report["active"] = s.activeRuns.count
            report["alerts"] = s.alerts.count
            report["sources"] = s.foundSources.map(\.agent)
            report["contextKinds"] = Array(Set(s.runs.map { $0.context.kind.rawValue })).sorted()
            // Live records read directly (the fast path), and the app a click on each would bring forward.
            let live = LiveRecords.read(dir: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/sessions"))
            report["liveRecords"] = live.map { l in
                "\(l.status.rawValue) → \(l.pid.flatMap { TerminalFocus.app(for: $0)?.localizedName } ?? "no app (tmux, ssh or ended)")"
            }
            // The opt-in account fetch, only if the user turned it on: percentages or why not, never the token.
            if !PrefsStore().load().usesClaudeAccount {
                report["claudeAccount"] = "off"
            } else {
                switch await ClaudeAccount.fetch(claudeVersion: CompanionModel.claudeVersion()) {
                case .success(let rows):
                    report["claudeAccount"] = rows.map { "\($0.window) \(Int($0.used.value?.usedPercent ?? -1))% resets \($0.resetsAt.map { "\($0)" } ?? "?")" }
                case .failure(let f):
                    report["claudeAccount"] = f.message
                }
            }
            report["planKinds"] = s.limits.map { "\($0.provider):\($0.window):\($0.used.kind.rawValue):\($0.used.freshness.rawValue)" }
            let viewer = ViewerManager(runtime: rt)
            let v0 = Date()
            do {
                let base = try await viewer.ensure()
                report["viewerStartMs"] = Int(Date().timeIntervalSince(v0) * 1000)
                report["viewerLoopback"] = ViewerLink.isLocalViewer(base)
                report["viewerHealthy"] = await ViewerManager.healthy(base)
                if let run = s.runs.first {
                    let link = ViewerLink.url(base: base, sessionId: run.sessionId, seq: run.lastEvent?.seq)
                    report["deepLinkShape"] = link.query?.replacingOccurrences(of: #"session=[^&]+"#, with: "session=<id>",
                                                                               options: .regularExpression)
                    let (data, resp) = try await URLSession(configuration: .ephemeral).data(from: link)
                    report["deepLinkStatus"] = (resp as? HTTPURLResponse)?.statusCode
                    report["deepLinkServesViewer"] = String(decoding: data, as: UTF8.self).contains("<div id=\"root\"")
                    // The map the viewer will load for that link exists and carries the seq we jump to.
                    var c = URLComponents(url: base.appendingPathComponent("api/map-doc"), resolvingAgainstBaseURL: false)!
                    c.queryItems = [URLQueryItem(name: "id", value: run.sessionId)]
                    let (mdata, _) = try await URLSession(configuration: .ephemeral).data(from: c.url!)
                    if let obj = try? JSONSerialization.jsonObject(with: mdata) as? [String: Any],
                       let events = obj["events"] as? [[String: Any]], let seq = run.lastEvent?.seq {
                        report["deepLinkSeqExists"] = events.contains { ($0["seq"] as? Int) == seq }
                    }
                }
                // A DNS-rebinding-style request (foreign Host) must be refused by the server's guards.
                var evil = URLRequest(url: base.appendingPathComponent("api/sessions"))
                evil.setValue("evil.example:\(base.port ?? 0)", forHTTPHeaderField: "Host")
                let (_, er) = try await URLSession(configuration: .ephemeral).data(for: evil)
                report["foreignHostStatus"] = (er as? HTTPURLResponse)?.statusCode
                var cross = URLRequest(url: base.appendingPathComponent("api/sessions"))
                cross.setValue("https://evil.example", forHTTPHeaderField: "Origin")
                let (_, cr) = try await URLSession(configuration: .ephemeral).data(for: cross)
                report["foreignOriginStatus"] = (cr as? HTTPURLResponse)?.statusCode
                let pid = viewer.pid
                viewer.stop()
                try? await Task.sleep(for: .milliseconds(500))
                report["viewerStopped"] = pid.map { kill($0, 0) != 0 } ?? false
                // Crash recovery: a viewer left behind is reaped by the next launch through its PID file.
                let pidFile = FileManager.default.temporaryDirectory.appendingPathComponent("apc-selftest-\(getpid()).pid")
                let crashed = ViewerManager(runtime: rt, pidFile: pidFile)
                _ = try await crashed.ensure()
                let orphan = crashed.pid
                let next = ViewerManager(runtime: rt, pidFile: pidFile)
                report["leftoverReaped"] = next.reapLeftover()
                try? await Task.sleep(for: .milliseconds(500))
                report["leftoverGone"] = orphan.map { kill($0, 0) != 0 } ?? false
            } catch {
                return fail("viewer", (error as? SnapshotError)?.localizedDescription ?? "\(error)")
            }
        }
        print(json(report))
        return 0
    }

    static func json(_ d: [String: Any]) -> String {
        String(decoding: (try? JSONSerialization.data(withJSONObject: d, options: [.sortedKeys, .prettyPrinted])) ?? Data(), as: UTF8.self)
    }
}

extension Renderer {
    /// `--render-live <dir>`: the popover over this Mac's real snapshot. Writes to the given dir only;
    /// these images contain your repo and file names and are never committed.
    static func renderLive(to path: String) async {
        _ = NSApplication.shared
        guard case .success(let rt) = Runtime.locate(allowDevOverride: true),
              case .success(let fetched) = await SnapshotService(runtime: rt).fetch(.read) else { print("no snapshot"); return }
        let dir = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        // As the user sees it: their settings (read, never written), and account usage if they turned it on.
        let real = PrefsStore().load()
        var s = fetched
        if real.usesClaudeAccount, case .success(let rows) = await ClaudeAccount.fetch(claudeVersion: CompanionModel.claudeVersion()) {
            s.limits = ClaudeAccount.merge(s.limits, account: rows)
        }
        let snap = s
        await MainActor.run {
            NSApp.setActivationPolicy(.accessory)
            let m = CompanionModel(prefsStore: PrefsStore(defaults: UserDefaults(suiteName: "render.\(UUID().uuidString)")!),
                                   isDevBuild: true)
            m.prefs = real
            m.installLiveForRender(snap)
            m.refreshIdentity()
            let c = StatusItemController(model: m)
            c.updateIcon()
            RunLoop.current.run(until: Date().addingTimeInterval(0.4))
            if let v = c.statusItem.button?.window?.contentView, let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) {
                v.cacheDisplay(in: v.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: dir.appendingPathComponent("live-statusitem.png"))
            }
            NSStatusBar.system.removeStatusItem(c.statusItem)
            for dark in [false, true] {
                captureMenu(m, now: snap.generatedAt, to: dir.appendingPathComponent("live-menu-\(dark ? "dark" : "light").png"), dark: dark)
            }
            if let line = MenuBuilder.sessionLines(snap, now: Date()).first {
                write(AnyView(SessionDetail(line: line, now: Date())), to: dir.appendingPathComponent("live-session.png"), dark: true, material: true)
            }
            if let f = snap.insights?.fixes.first {
                write(AnyView(FixDetail(fix: f)), to: dir.appendingPathComponent("live-fix-detail.png"), dark: true, material: true)
            }
            if let e = MenuBuilder.errorLines(snap, prefs: real, now: snap.generatedAt).first {
                write(AnyView(ErrorDetail(line: e, now: snap.generatedAt)), to: dir.appendingPathComponent("live-error-detail.png"), dark: true, material: true)
            }
            if let sig = snap.usage?.signals {
                write(AnyView(SignalsCard(signals: sig)), to: dir.appendingPathComponent("live-signals.png"), dark: true, material: true)
            }
            if let ins = snap.insights {
                write(AnyView(HealthCard(insights: ins)), to: dir.appendingPathComponent("live-health.png"), dark: true, material: true)
            }
            if let u = snap.usage {
                write(AnyView(UsageDetail(report: u)), to: dir.appendingPathComponent("live-usage-details.png"), dark: true, material: true)
            }
            // The Insights window, each section, as a real window.
            NSApp.appearance = NSAppearance(named: .darkAqua)
            for section in [InsightsSection.overview, .steering] {
                m.insightsSection = section
                let w = InsightsWindow.make(model: m)
                captureWindow(w, to: dir.appendingPathComponent("live-insights-\(section.rawValue).png"), wait: 0.8)
            }
        }
        print("rendered live screens to \(dir.path)")
    }
}

extension CompanionModel {
    func installLiveForRender(_ s: Snapshot) {
        installSampleForRender(s)
        markLive()
    }
}
