import Foundation

/// Where the AgentPlay Python core lives. Release builds use only the runtime inside the app bundle;
/// a dev build may point elsewhere with `AGENTPLAY_COMPANION_PYTHON`. Never a silent fallback to a system Python.
public struct Runtime: Sendable, Equatable {
    public var python: URL
    public var bundled: Bool

    public static func locate(bundle: Bundle = .main, env: [String: String] = ProcessInfo.processInfo.environment,
                              allowDevOverride: Bool) -> Result<Runtime, SnapshotError> {
        if allowDevOverride, let p = env["AGENTPLAY_COMPANION_PYTHON"], !p.isEmpty {
            let u = URL(fileURLWithPath: p)
            return FileManager.default.isExecutableFile(atPath: u.path) ? .success(.init(python: u, bundled: false))
                : .failure(.runtimeMissing(p))
        }
        let embedded = bundle.bundleURL.appendingPathComponent("Contents/Resources/python/bin/python3")
        if FileManager.default.isExecutableFile(atPath: embedded.path) {
            return .success(.init(python: embedded, bundled: true))
        }
        return .failure(.runtimeMissing("Contents/Resources/python"))
    }
}

/// The environment the core runs with: no network, no tracking, a plain PATH (for `lsof`), the AgentPlay
/// variables the user set, and the two folder variables discovery reads (`COPILOT_HOME`, `XDG_DATA_HOME`).
/// Nothing else from the parent leaks in.
public func childEnvironment(parent: [String: String], extra: [String: String] = [:]) -> [String: String] {
    var env: [String: String] = [
        "HOME": parent["HOME"] ?? NSHomeDirectory(),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "AGENTPLAY_NO_NETWORK": "1",
        "DO_NOT_TRACK": "1",
        "NO_COLOR": "1",
    ]
    for (k, v) in parent where k.hasPrefix("AGENTPLAY_") && k != "AGENTPLAY_COMPANION_PYTHON" { env[k] = v }
    if let v = parent["COPILOT_HOME"] { env["COPILOT_HOME"] = v }
    if let v = parent["XDG_DATA_HOME"] { env["XDG_DATA_HOME"] = v }  // OpenCode keeps its database under it
    for (k, v) in extra { env[k] = v }
    env["AGENTPLAY_NO_NETWORK"] = "1"
    env["DO_NOT_TRACK"] = "1"
    return env
}

/// One of the companion's write commands (`agentplay companion fix|autotune ...`), answered in JSON:
/// `{"ok": true, ...}` or `{"ok": false, "code", "message"}`.
public enum CompanionCommand {
    public struct Reply: Sendable, Equatable {
        public var ok: Bool
        public var message: String?
        public var fields: [String: String]
        public init(ok: Bool, message: String?, fields: [String: String]) {
            self.ok = ok; self.message = message; self.fields = fields
        }
    }

    public static func run(_ runtime: Runtime, _ args: [String], extraEnv: [String: String] = [:],
                           timeout: TimeInterval = 60) async -> Reply {
        let env = childEnvironment(parent: ProcessInfo.processInfo.environment, extra: extraEnv)
        guard let r = try? await ChildProcess().run(runtime.python, ["-I", "-m", "agentplay", "companion"] + args,
                                                    env: env, timeout: timeout) else {
            return Reply(ok: false, message: "AgentPlay didn't answer.", fields: [:])
        }
        return parse(r.stdout)
    }

    public static func parse(_ data: Data) -> Reply {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return Reply(ok: false, message: "AgentPlay answered with something unexpected.", fields: [:])
        }
        var fields: [String: String] = [:]
        for (k, v) in obj where k != "ok" { if let s = v as? String { fields[k] = s } else if let b = v as? Bool { fields[k] = b ? "true" : "false" } }
        return Reply(ok: obj["ok"] as? Bool ?? false, message: obj["message"] as? String, fields: fields)
    }
}

public struct ProcessResult: Sendable {
    public var status: Int32
    public var stdout: Data
    public var stderr: String
}

/// Runs one child process to completion, off the main thread, with a timeout and cancellation.
public final class ChildProcess: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?

    public init() {}

    public func run(_ exe: URL, _ args: [String], env: [String: String], timeout: TimeInterval) async throws -> ProcessResult {
        let p = Process()
        p.executableURL = exe
        p.arguments = args
        p.environment = env
        p.currentDirectoryURL = FileManager.default.temporaryDirectory
        // Scans are background work: lower CPU priority and energy impact than anything the user is doing.
        p.qualityOfService = .utility
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = FileHandle.nullDevice
        lock.withLock { process = p }
        // Drain both pipes while the child runs, so a big snapshot can't fill the pipe and deadlock.
        let outBox = DataBox(), errBox = DataBox()
        out.fileHandleForReading.readabilityHandler = { h in outBox.append(h.availableData) }
        err.fileHandleForReading.readabilityHandler = { h in errBox.append(h.availableData) }
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (k: CheckedContinuation<ProcessResult, Error>) in
                let done = OnceFlag()
                p.terminationHandler = { proc in
                    out.fileHandleForReading.readabilityHandler = nil
                    err.fileHandleForReading.readabilityHandler = nil
                    outBox.append(out.fileHandleForReading.readDataToEndOfFile())
                    errBox.append(err.fileHandleForReading.readDataToEndOfFile())
                    guard done.set() else { return }
                    if proc.terminationReason == .uncaughtSignal && done.timedOut {
                        k.resume(throwing: SnapshotError.timedOut)
                    } else {
                        k.resume(returning: ProcessResult(status: proc.terminationStatus, stdout: outBox.data,
                                                          stderr: String(decoding: errBox.data.prefix(4000), as: UTF8.self)))
                    }
                }
                do { try p.run() } catch {
                    guard done.set() else { return }
                    k.resume(throwing: SnapshotError.runtimeMissing(exe.path)); return
                }
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout) { [weak p] in
                    guard let p, p.isRunning else { return }
                    done.markTimedOut()
                    p.terminate()
                }
            }
        } onCancel: { [weak self] in self?.cancel() }
    }

    public func cancel() {
        lock.withLock { if process?.isRunning == true { process?.terminate() } }
    }
}

final class DataBox: @unchecked Sendable {
    private let lock = NSLock()
    private var buf = Data()
    func append(_ d: Data) { lock.withLock { buf.append(d) } }
    var data: Data { lock.withLock { buf } }
}

final class OnceFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var fired = false
    private var _timedOut = false
    func set() -> Bool { lock.withLock { if fired { return false }; fired = true; return true } }
    func markTimedOut() { lock.withLock { _timedOut = true } }
    var timedOut: Bool { lock.withLock { _timedOut } }
}

/// One snapshot at a time. A request while one runs is coalesced into a single follow-up.
public actor SnapshotService {
    public enum Mode: Sendable, Equatable { case read, refresh, fullScan }

    private let runtime: Runtime
    private let extraEnv: @Sendable () -> [String: String]
    private var running = false
    private var queued: Mode?
    private var waiters: [CheckedContinuation<Result<Snapshot, SnapshotError>, Never>] = []
    private nonisolated let child = ChildProcess()
    public private(set) var lastDurationMs: Int = 0

    public init(runtime: Runtime, extraEnv: @escaping @Sendable () -> [String: String] = { [:] }) {
        self.runtime = runtime
        self.extraEnv = extraEnv
    }

    static func arguments(for mode: Mode) -> [String] {
        // -I: ignore PYTHON* variables and the user site, so nothing on the machine can inject code.
        let base = ["-I", "-m", "agentplay"]
        switch mode {
        case .read: return base + ["companion", "snapshot", "--json"]
        case .refresh: return base + ["companion", "snapshot", "--json", "--refresh", "--agents", "claude-code,codex"]
        case .fullScan: return base + ["companion", "snapshot", "--json", "--refresh", "--refresh-days", "0"]
        }
    }

    /// Fetch a snapshot. If one is in flight, this waits for the next one, which starts right after.
    public func fetch(_ mode: Mode) async -> Result<Snapshot, SnapshotError> {
        if running {
            queued = max(queued ?? mode, mode)
            return await withCheckedContinuation { waiters.append($0) }
        }
        running = true
        var result = await runOnce(mode)
        while let next = queued {
            queued = nil
            let pending = waiters
            waiters = []
            result = await runOnce(next)
            for w in pending { w.resume(returning: result) }
        }
        running = false
        return result
    }

    public func cancel() { child.cancel() }

    /// Synchronous, for app shutdown: a Task scheduled at exit may never run.
    public nonisolated func cancelNow() { child.cancel() }

    private func runOnce(_ mode: Mode) async -> Result<Snapshot, SnapshotError> {
        let t0 = Date()
        let env = childEnvironment(parent: ProcessInfo.processInfo.environment, extra: extraEnv())
        let timeout: TimeInterval = mode == .fullScan ? 900 : mode == .refresh ? 120 : 30
        do {
            let r = try await child.run(runtime.python, Self.arguments(for: mode), env: env, timeout: timeout)
            lastDurationMs = Int(Date().timeIntervalSince(t0) * 1000)
            return Self.interpret(r)
        } catch let e as SnapshotError {
            return .failure(e)
        } catch {
            return .failure(.processFailed(code: -1, message: error.localizedDescription))
        }
    }

    static func interpret(_ r: ProcessResult) -> Result<Snapshot, SnapshotError> {
        if r.status == 2, r.stderr.contains("invalid choice") {
            // argparse on an older AgentPlay: there is no `companion` command.
            let v = r.stderr.range(of: #"agentplay \d+\.\d+\.\d+"#, options: .regularExpression).map { String(r.stderr[$0]) }
            return .failure(.coreTooOld(v ?? "an older AgentPlay"))
        }
        guard r.status == 0 else {
            let last = r.stderr.split(separator: "\n").last.map(String.init) ?? ""
            return .failure(.processFailed(code: r.status, message: String(last.prefix(200))))
        }
        do { return .success(try SnapshotDecoder.decode(r.stdout)) } catch let e as SnapshotError {
            return .failure(e)
        } catch { return .failure(.malformed(error.localizedDescription)) }
    }
}

extension SnapshotService.Mode: Comparable {
    private var rank: Int { switch self { case .read: 0; case .refresh: 1; case .fullScan: 2 } }
    public static func < (a: Self, b: Self) -> Bool { a.rank < b.rank }
}

/// The local viewer the companion opens on demand: `agentplay view --no-open --no-build --port 0`.
/// It owns only the process it started, and never stops a viewer the user ran themselves.
public final class ViewerManager: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var baseURL: URL?
    private let runtime: Runtime
    private let extraEnv: @Sendable () -> [String: String]

    /// Where the PID of the viewer this app started is kept, so a crash can't leave it running forever.
    public let pidFile: URL?

    public init(runtime: Runtime, pidFile: URL? = nil, extraEnv: @escaping @Sendable () -> [String: String] = { [:] }) {
        self.runtime = runtime
        self.pidFile = pidFile
        self.extraEnv = extraEnv
    }

    /// Stop a viewer left by an earlier run of this app (crash, force quit). Only a process whose executable is
    /// this app's own Python is touched; a viewer the user started with `uvx agentplay` never matches.
    @discardableResult
    public func reapLeftover(commandOf: (Int32) -> String? = ViewerManager.command(of:)) -> Bool {
        guard let pidFile, let raw = try? String(contentsOf: pidFile, encoding: .utf8),
              let pid = Int32(raw.trimmingCharacters(in: .whitespacesAndNewlines)), pid > 1 else { return false }
        try? FileManager.default.removeItem(at: pidFile)
        guard let cmd = commandOf(pid), Self.isOurViewer(cmd, python: runtime.python.path) else { return false }
        return kill(pid, SIGTERM) == 0
    }

    public static func isOurViewer(_ command: String, python: String) -> Bool {
        command.hasPrefix(python + " ") && command.contains(" -m agentplay view ")
    }

    public static func command(of pid: Int32) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/ps")
        p.arguments = ["-o", "command=", "-p", String(pid)]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return nil }
        p.waitUntilExit()
        let s = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return s.isEmpty ? nil : s
    }

    public var isRunning: Bool { lock.withLock { process?.isRunning == true } }
    public var url: URL? { lock.withLock { process?.isRunning == true ? baseURL : nil } }
    public var pid: Int32? { lock.withLock { process?.isRunning == true ? process?.processIdentifier : nil } }

    /// The viewer's base URL, starting it if needed.
    public func ensure(timeout: TimeInterval = 20) async throws -> URL {
        if let u = url, await Self.healthy(u) { return u }
        stop()
        let p = Process()
        p.executableURL = runtime.python
        p.arguments = ["-I", "-m", "agentplay", "view", "--no-open", "--no-build", "--port", "0"]
        p.environment = childEnvironment(parent: ProcessInfo.processInfo.environment, extra: extraEnv())
        p.currentDirectoryURL = FileManager.default.temporaryDirectory
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        p.standardInput = FileHandle.nullDevice
        let found = DataBox()
        let started: URL = try await withCheckedThrowingContinuation { k in
            let once = OnceFlag()
            out.fileHandleForReading.readabilityHandler = { h in
                let d = h.availableData
                if d.isEmpty { return }
                found.append(d)
                let text = String(decoding: found.data, as: UTF8.self)
                for line in text.split(separator: "\n") {
                    if let u = ViewerLink.parseStartLine(String(line)), once.set() {
                        h.readabilityHandler = nil
                        k.resume(returning: u)
                        return
                    }
                }
            }
            p.terminationHandler = { _ in if once.set() { k.resume(throwing: SnapshotError.processFailed(code: p.terminationStatus, message: "The viewer stopped before it was ready.")) } }
            do { try p.run() } catch {
                if once.set() { k.resume(throwing: SnapshotError.runtimeMissing(runtime.python.path)) }
                return
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                if once.set() { p.terminate(); k.resume(throwing: SnapshotError.timedOut) }
            }
        }
        // Keep draining stdout (the scan-done lines) so the child never blocks on a full pipe.
        out.fileHandleForReading.readabilityHandler = { h in _ = h.availableData }
        lock.withLock { process = p; baseURL = started }
        if let pidFile {
            try? FileManager.default.createDirectory(at: pidFile.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? "\(p.processIdentifier)\n".write(to: pidFile, atomically: true, encoding: .utf8)
        }
        return started
    }

    public func stop() {
        lock.withLock {
            if let p = process, p.isRunning { p.terminate() }
            process = nil
            baseURL = nil
        }
        if let pidFile { try? FileManager.default.removeItem(at: pidFile) }
    }

    /// `GET /api/status` answers like AgentPlay. A client with no Origin passes the server's guards.
    public static func healthy(_ base: URL) async -> Bool {
        var req = URLRequest(url: base.appendingPathComponent("api/status"), timeoutInterval: 2)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        let cfg = URLSessionConfiguration.ephemeral
        cfg.connectionProxyDictionary = [:]
        guard let (data, resp) = try? await URLSession(configuration: cfg).data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return obj["success"] as? Bool == true && obj["mapCount"] != nil
    }
}
