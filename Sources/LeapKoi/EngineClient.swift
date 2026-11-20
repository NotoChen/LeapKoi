import Foundation

protocol EngineConnection: AnyObject {
    var onEvent: (([String: Any]) -> Void)? { get set }
    var onExit: (() -> Void)? { get set }
    var isRunning: Bool { get }
    func launch(dataDirectory: URL) throws
    @discardableResult func send(_ command: String, settings: AppSettings?, values: [String: Any]) -> Bool
    func shutdown()
    func terminate()
}

extension EngineConnection {
    @discardableResult
    func send(_ command: String, settings: AppSettings? = nil) -> Bool {
        send(command, settings: settings, values: [:])
    }
    @discardableResult
    func send(_ command: String, values: [String: Any]) -> Bool {
        send(command, settings: nil, values: values)
    }
}

final class EngineClient: EngineConnection {
    private var process: Process?
    private var input: FileHandle?
    private let readQueue = DispatchQueue(label: "leapkoi.engine.output")
    private var buffer = Data()
    var onEvent: (([String: Any]) -> Void)?
    var onExit: (() -> Void)?
    var isRunning: Bool { process?.isRunning == true }

    func launch(dataDirectory: URL) throws {
        if isRunning { return }
        readQueue.sync { buffer.removeAll(keepingCapacity: true) }
        let resources = Bundle.main.resourceURL
        let source = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let root = resources.flatMap { FileManager.default.fileExists(atPath: $0.appendingPathComponent("Engine/worker.py").path) ? $0 : nil } ?? source
        let embedded = root.appendingPathComponent("Runtime/bin/python3")
        let candidates = [embedded, FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/python3"), URL(fileURLWithPath: "/opt/homebrew/bin/python3"), URL(fileURLWithPath: "/usr/bin/python3")]
        guard let python = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0.path) }) else {
            throw NSError(domain: "LeapKoi", code: 1, userInfo: [NSLocalizedDescriptionKey: "应用内的 Python 运行组件缺失，请重新安装应用"])
        }
        let worker = root.appendingPathComponent("Engine/worker.py")
        guard FileManager.default.fileExists(atPath: worker.path) else {
            throw NSError(domain: "LeapKoi", code: 2, userInfo: [NSLocalizedDescriptionKey: "应用内的执行引擎缺失"])
        }
        let child = Process()
        child.executableURL = python
        child.arguments = ["-B", "-u", worker.path]
        child.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env.removeValue(forKey: "PYTHONHOME")
        env.removeValue(forKey: "PYTHONPATH")
        env["LEAPKOI_DATA_DIR"] = dataDirectory.path
        env["PATH"] = [root.appendingPathComponent("bin").path, NSHomeDirectory() + "/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"].joined(separator: ":")
        child.environment = env
        let stdout = Pipe(), stdin = Pipe(), stderr = Pipe()
        child.standardOutput = stdout
        child.standardInput = stdin
        child.standardError = stderr
        input = stdin.fileHandleForWriting
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            self?.readQueue.async { [weak self] in self?.consume(data) }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            if let text = String(data: data, encoding: .utf8), !text.isEmpty {
                DispatchQueue.main.async { self?.onEvent?(["type": "log", "level": "error", "message": text]) }
            }
        }
        child.terminationHandler = { [weak self] child in
            self?.readQueue.async { [weak self] in
                DispatchQueue.main.async {
                    guard let self, self.process === child else { return }
                    self.input = nil
                    self.onEvent?(["type": "worker_exit", "code": child.terminationStatus])
                    self.onExit?()
                }
            }
        }
        process = child
        try child.run()
    }

    private func consume(_ data: Data) {
        buffer.append(data)
        while let index = buffer.firstIndex(of: 10) {
            let line = Data(buffer[..<index])
            buffer.removeSubrange(...index)
            guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            DispatchQueue.main.async { [weak self] in self?.onEvent?(event) }
        }
    }

    @discardableResult
    func send(_ command: String, settings: AppSettings? = nil, values: [String: Any] = [:]) -> Bool {
        var payload = values
        payload["command"] = command
        if let settings { payload["settings"] = settings.dictionary() }
        guard var data = try? JSONSerialization.data(withJSONObject: payload) else {
            onEvent?(["type": "error", "command": command, "operation_rejected": true,
                      "message": "无法编码执行引擎命令：\(command)"])
            return false
        }
        guard let input else {
            onEvent?(["type": "error", "command": command, "operation_rejected": true,
                      "message": "执行引擎未运行"])
            return false
        }
        data.append(10)
        do {
            try input.write(contentsOf: data)
            return true
        } catch {
            onEvent?(["type": "error", "command": command, "operation_rejected": true,
                      "message": "无法连接执行引擎：\(error.localizedDescription)"])
            return false
        }
    }

    func shutdown() {
        _ = send("shutdown")
        try? input?.close()
        input = nil
    }

    func terminate() {
        guard let child = process, child.isRunning else { return }
        child.terminate()
    }
}
