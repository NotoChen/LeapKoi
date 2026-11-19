import Foundation
import SwiftUI

struct AppSettings: Codable, Equatable {
    static let currentSchemaVersion = 1
    var schemaVersion = 1
    var profile = ""
    var approvalGroup = "代码审批"
    var approvalGroupId = ""
    var senderGroup = "向钱冲"
    var senderGroupId = ""
    var gitlabHost = "git2.superboss.cc"
    var gitlabToken = ""
    var fallbackToken = ""
    var dwsPath = ""
    var glabPath = ""
    var processWhileLocked = false
    var lockedMessagePolicy = "discard"
    var pausedMessagePolicy = "discard"
    var queueCapacity = 100
    var queueOverflowPolicy = "drop_oldest"
    var maxQueueAgeSeconds = 0
    var maxConcurrentMrs = 3
    var maxMrsPerMessage = 30
    var parseQuotedMessages = true
    var markRead = true
    var sendReceived = true
    var receivedCondition = "all_succeeded_and_owned"
    var includeFallbackForReceived = true
    var useFallbackForOwnMr = true
    var commandTimeoutSeconds = 90
    var mergeTimeoutSeconds = 60
    var mergePollSeconds = 2
    var maxAttempts = 3
    var retryDelays = [2, 5]
    var reconnectDelays = [2, 5, 10, 20, 30]
    var notificationsEnabled = true
    var notifyOnRequest = true
    var notifyOnSuccess = true
    var notifyOnFailure = true
    var launchAtLogin = true
    var startOnLaunch = true
    var showWorkbenchOnLaunch = false
    var appearance = "system"
    var activityLimit = 300
    var debugOutput = false

    static var encoder: JSONEncoder {
        let result = JSONEncoder()
        result.keyEncodingStrategy = .convertToSnakeCase
        result.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return result
    }
    static func normalizedGitlabHost(_ raw: String) -> String? {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !value.isEmpty, value.count <= 253,
              !value.hasPrefix("."), !value.hasSuffix("."), !value.contains("..") else { return nil }
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz0123456789-")
        for label in value.split(separator: ".", omittingEmptySubsequences: false) {
            guard (1...63).contains(label.count),
                  label.first != "-", label.last != "-",
                  label.unicodeScalars.allSatisfy({ allowed.contains($0) }) else { return nil }
        }
        return value
    }
    func dictionary() -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: Self.encoder.encode(self))) as? [String: Any] ?? [:]
    }
    static func load(from url: URL) throws -> AppSettings {
        guard FileManager.default.fileExists(atPath: url.path) else { return AppSettings() }
        let source = try JSONSerialization.jsonObject(with: Data(contentsOf: url))
        guard let dictionary = source as? [String: Any] else {
            throw NSError(domain: "ApprovalDesk", code: 1, userInfo: [NSLocalizedDescriptionKey: "配置文件需要是 JSON 对象"])
        }
        let combined = AppSettings().dictionary().merging(dictionary) { _, new in new }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        var settings = try decoder.decode(AppSettings.self, from: JSONSerialization.data(withJSONObject: combined))
        guard settings.schemaVersion == AppSettings.currentSchemaVersion else {
            throw NSError(domain: "ApprovalDesk", code: 3, userInfo: [NSLocalizedDescriptionKey: "配置文件版本与当前应用不一致，请检查版本或迁移配置"])
        }
        // The current schema is still v1. Keeping the field and this guard
        // makes future migrations explicit instead of silently accepting them.
        settings.schemaVersion = AppSettings.currentSchemaVersion
        if let host = AppSettings.normalizedGitlabHost(settings.gitlabHost) {
            settings.gitlabHost = host
        }
        if let issue = settings.validationIssue {
            throw NSError(domain: "ApprovalDesk", code: 2, userInfo: [NSLocalizedDescriptionKey: issue])
        }
        return settings
    }
    func save(to url: URL) throws {
        if let issue = validationIssue {
            throw NSError(domain: "ApprovalDesk", code: 2, userInfo: [NSLocalizedDescriptionKey: issue])
        }
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        var normalized = self
        normalized.gitlabHost = Self.normalizedGitlabHost(gitlabHost) ?? gitlabHost
        try Self.encoder.encode(normalized).write(to: url, options: .atomic)
    }
    var validationIssue: String? {
        if schemaVersion != AppSettings.currentSchemaVersion { return "配置版本与当前应用不一致" }
        let options: [(String, Set<String>)] = [
            (lockedMessagePolicy, ["queue", "discard"]), (pausedMessagePolicy, ["queue", "discard"]),
            (queueOverflowPolicy, ["reject_new", "drop_oldest"]),
            (receivedCondition, ["any_owned_merge", "all_succeeded_and_owned"]),
            (appearance, ["system", "light", "dark"])
        ]
        if options.contains(where: { !$0.1.contains($0.0) }) { return "配置中包含不支持的选项值" }
        if approvalGroup.trimmingCharacters(in: .whitespaces).isEmpty || senderGroup.trimmingCharacters(in: .whitespaces).isEmpty { return "请填写审批群和人员来源群" }
        if AppSettings.normalizedGitlabHost(gitlabHost) == nil { return "GitLab 地址只填写主机名，例如 git2.superboss.cc" }
        let ranges: [(Int, ClosedRange<Int>, String)] = [
            (queueCapacity, 1...10000, "队列容量"), (maxQueueAgeSeconds, 0...604800, "最长等待时间"),
            (maxConcurrentMrs, 1...16, "并发数量"), (maxMrsPerMessage, 1...200, "单条消息 MR 上限"),
            (commandTimeoutSeconds, 5...600, "命令超时"), (mergeTimeoutSeconds, 5...3600, "合并确认窗口"),
            (mergePollSeconds, 1...60, "合并查询间隔"), (maxAttempts, 1...10, "最大尝试次数"),
            (activityLimit, 50...3000, "活动保留数量")
        ]
        for (value, range, name) in ranges where !range.contains(value) { return "\(name)需在 \(range.lowerBound)–\(range.upperBound) 之间" }
        for values in [retryDelays, reconnectDelays] where values.isEmpty || values.count > 10 || values.contains(where: { !(1...300).contains($0) }) { return "等待间隔需要 1–10 个 1–300 秒的数值" }
        return nil
    }
    var connectionSignature: String {
        // Group selections are compared with the identities resolved by the listener.
        [profile, AppSettings.normalizedGitlabHost(gitlabHost) ?? gitlabHost,
         gitlabToken, fallbackToken, dwsPath, glabPath].joined(separator: "\u{1F}")
    }
    var accountSignature: String {
        [profile, AppSettings.normalizedGitlabHost(gitlabHost) ?? gitlabHost,
         gitlabToken, dwsPath, glabPath].joined(separator: "\u{1F}")
    }
}

struct ListeningGroup: Equatable {
    var id: String
    var title: String

    init?(_ value: Any?) {
        guard let data = value as? [String: Any], let id = data["id"] as? String, !id.isEmpty,
              let title = data["title"] as? String else { return nil }
        self.id = id
        self.title = title
    }
}

struct ListeningConfiguration: Equatable {
    let settings: AppSettings
    var approvalGroup: ListeningGroup?
    var senderGroup: ListeningGroup?

    func needsRestart(for next: AppSettings) -> Bool {
        settings.connectionSignature != next.connectionSignature ||
        !matches(name: next.approvalGroup, id: next.approvalGroupId,
                 originalName: settings.approvalGroup, originalID: settings.approvalGroupId, resolved: approvalGroup) ||
        !matches(name: next.senderGroup, id: next.senderGroupId,
                 originalName: settings.senderGroup, originalID: settings.senderGroupId, resolved: senderGroup)
    }

    private func matches(name: String, id: String, originalName: String, originalID: String, resolved: ListeningGroup?) -> Bool {
        if !id.isEmpty { return id == (resolved?.id ?? originalID) }
        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        return name == originalName.trimmingCharacters(in: .whitespacesAndNewlines) ||
               name == resolved?.title.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

enum AccountStatus: String { case unchecked, checking, ready, error }

struct AccountConnection: Equatable {
    var status: AccountStatus = .unchecked
    var name = ""
    var detail = ""

    var displayName: String {
        switch status {
        case .unchecked: "等待读取"
        case .checking: "正在读取…"
        case .ready: name.isEmpty ? "已登录" : name
        case .error: "读取失败"
        }
    }
    var explanation: String {
        switch status {
        case .unchecked: "打开应用后自动读取当前登录账号"
        case .checking: "正在读取当前登录账号"
        case .ready: detail
        case .error: detail.isEmpty ? "暂时无法读取账号，请刷新重试" : detail
        }
    }
}

struct MRItem: Identifiable, Equatable {
    var id: String { key }
    var key: String
    var url: String
    var title = ""
    var status = "waiting"
    var stage = ""
    var sourceBranch = ""
    var targetBranch = ""
    var changesCount = ""
    var commitCount: Int?
    var author = ""
    var actor = ""
    var mergedBy = ""
    var sha = ""
    var error = ""
    var elapsed: Double = 0
    var approved = false
    var approvers: [String] = []
    var approvalsKnown = false
    var approvedByOwn = false
    var ownedMerge = false
    var mergeOwnerKnown = false
    var createdAt = ""
    var updatedAt = ""
    var approvalSummary: String {
        if !approvalsKnown { return "审批：审批人未获取" }
        if approvers.isEmpty { return "审批：GitLab 未返回审批人" }
        let names = approvers.joined(separator: "、")
        return approvedByOwn ? "审批人：\(names) · 本机参与" : "审批人：\(names) · 均为其他成员"
    }
    var mergeSummary: String {
        if mergedBy.isEmpty {
            guard mergeOwnerKnown else { return "合并人：GitLab 未返回" }
            return ownedMerge
                ? "合并人：本机（GitLab 未返回姓名）"
                : "合并人：其他成员（GitLab 未返回姓名）"
        }
        guard mergeOwnerKnown else { return "合并人：\(mergedBy) · 归属未知" }
        return ownedMerge ? "合并人：\(mergedBy) · 本机" : "合并人：\(mergedBy) · 其他成员"
    }
    var resultSummary: String {
        var parts: [String] = []
        if approvalsKnown {
            let names = approvers.isEmpty ? "未返回" : approvers.joined(separator: "、")
            parts.append("审批人 \(names)（\(approvedByOwn ? "本机参与" : "其他成员")）")
        } else {
            parts.append("审批人未获取")
        }
        if status == "completed" || !mergedBy.isEmpty || mergeOwnerKnown || ownedMerge {
            if mergedBy.isEmpty {
                parts.append("合并人未返回")
            } else {
                let ownership = ownedMerge ? "本机" : mergeOwnerKnown ? "其他成员" : "归属未知"
                parts.append("合并人 \(mergedBy)（\(ownership)）")
            }
        } else if status == "failed" {
            let failedStage = ["preflight": "检查", "approval": "审批", "merge": "合并"][stage] ?? "处理"
            parts.append("\(failedStage)失败")
        } else {
            parts.append("未完成")
        }
        return "\(key)：" + parts.joined(separator: "；")
    }

    init(_ data: [String: Any]) {
        key = data["key"] as? String ?? ""
        url = data["url"] as? String ?? ""
        merge(data)
    }
    mutating func merge(_ data: [String: Any]) {
        if let v = data["url"] as? String, !v.isEmpty { url = v }
        if let v = data["title"] as? String { title = v }
        if let v = data["status"] as? String { status = v }
        if let v = data["stage"] as? String { stage = v }
        if let v = data["source_branch"] as? String { sourceBranch = v }
        if let v = data["target_branch"] as? String { targetBranch = v }
        if let v = data["changes_count"] as? String { changesCount = v }
        if let v = data["commit_count"] as? Int { commitCount = v }
        if let v = data["author"] as? String { author = v }
        if let v = data["actor"] as? String { actor = v }
        if let v = data["merged_by"] as? String { mergedBy = v }
        if let v = data["sha"] as? String { sha = v }
        if let v = data["error"] as? String { error = v }
        if let v = data["elapsed"] as? Double { elapsed = v }
        if let v = data["approved"] as? Bool { approved = v }
        if let v = data["approvers"] as? [String] { approvers = v }
        if let v = data["approvals_known"] as? Bool { approvalsKnown = v }
        if let v = data["approved_by_own"] as? Bool { approvedByOwn = v }
        if let v = data["owned_merge"] as? Bool { ownedMerge = v }
        if let v = data["merge_owner_known"] as? Bool { mergeOwnerKnown = v }
        if let v = data["created_at"] as? String { createdAt = v }
        if let v = data["updated_at"] as? String { updatedAt = v }
    }
}

struct ApprovalTask: Identifiable, Equatable {
    var id: String
    var sender: String
    var senderAvatarURL = ""
    var receivedAt: Date
    var status = "queued"
    var content = ""
    var conversationTitle = ""
    var summary = ""
    var error = ""
    var mrs: [MRItem] = []
    var mrSummaries: [String] = []
    var received = false
    var quoted = false
    var elapsed: Double = 0
    var approvedCount = 0
    var mergedCount = 0
    var ownedMergeCount = 0
    var otherMergeCount = 0
    var unknownMergeCount = 0

    init(_ data: [String: Any]) {
        id = data["id"] as? String ?? UUID().uuidString
        sender = data["sender"] as? String ?? "未知发送人"
        receivedAt = Date(timeIntervalSince1970: data["received_at"] as? Double ?? Date().timeIntervalSince1970)
        content = data["content"] as? String ?? ""
        conversationTitle = data["conversation_title"] as? String ?? ""
        mrs = (data["mrs"] as? [[String: Any]] ?? []).map(MRItem.init)
        quoted = data["quoted"] as? Bool ?? false
        merge(data)
    }
    mutating func merge(_ data: [String: Any]) {
        if let v = data["sender_avatar_url"] as? String { senderAvatarURL = v }
        if let v = data["status"] as? String { status = v }
        if let v = data["conversation_title"] as? String { conversationTitle = v }
        if let v = data["summary"] as? String { summary = v }
        if let v = data["error"] as? String { error = v }
        if let v = data["mr_summaries"] as? [String] { mrSummaries = v }
        if let v = data["received"] as? Bool { received = v }
        if let v = data["elapsed"] as? Double { elapsed = v }
        if let v = data["approved_count"] as? Int { approvedCount = v }
        if let v = data["merged_count"] as? Int { mergedCount = v }
        if let v = data["owned_merge_count"] as? Int { ownedMergeCount = v }
        if let v = data["other_merge_count"] as? Int { otherMergeCount = v }
        if let v = data["unknown_merge_count"] as? Int { unknownMergeCount = v }
    }
    var hasErrors: Bool { ["failed", "partial", "invalid"].contains(status) }
    var waiting: Bool { status == "queued" }
    var running: Bool { status == "running" }
    var resolvedMRSummaries: [String] {
        mrs.isEmpty ? mrSummaries : mrs.map(\.resultSummary)
    }
}

struct ActivityLog: Identifiable {
    let id = UUID()
    var date = Date()
    var level: String
    var message: String
    var taskId: String
    var mrKey: String
}

struct EnvironmentCheck: Identifiable {
    var id: String
    var title: String
    var status: String
    var detail: String
}

struct GroupOption: Identifiable {
    var id: String
    var title: String
    var count: Int
}

struct ProfileOption: Identifiable {
    var id: String
    var name: String
    var organization: String
    var title: String { [name, organization].filter { !$0.isEmpty }.joined(separator: " · ") }
}

enum TaskFilter: String, CaseIterable, Identifiable {
    case all, queued, completed
    var id: String { rawValue }
    var title: String {
        switch self { case .all: "全部请求"; case .queued: "待处理"; case .completed: "已完成" }
    }
    var icon: String {
        switch self { case .all: "tray.full"; case .queued: "clock"; case .completed: "checkmark.circle" }
    }
    func includes(_ task: ApprovalTask) -> Bool {
        switch self {
        case .all: true
        case .queued: task.waiting || task.running
        case .completed: ["completed", "partial"].contains(task.status)
        }
    }
}

func statusTitle(_ status: String) -> String {
    ["idle": "尚未监听", "ready": "准备就绪", "checking": "正在检查", "starting": "正在连接",
     "listening": "正在监听", "paused": "已手动暂停", "locked": "等待解锁", "reconnecting": "正在重连",
     "stopping": "正在停止", "stopped": "已停止", "error": "连接异常", "queued": "等待处理",
     "running": "处理中", "completed": "已完成", "partial": "部分完成", "failed": "处理失败",
     "discarded": "已跳过", "invalid": "消息无效", "waiting": "等待开始", "preflight": "检查 MR",
     "approving": "正在审批", "merging": "正在合并"][status] ?? status
}

extension Color {
    static let ink = Color(nsColor: NSColor(name: "ApprovalDeskInk") { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(white: 0.94, alpha: 1)
            : NSColor(white: 0.12, alpha: 1)
    })
    static let action = Color(nsColor: NSColor(name: "ApprovalDeskAction") { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(white: 0.88, alpha: 1)
            : NSColor(white: 0.19, alpha: 1)
    })
    static let brand = Color.ink
    static let canvas = Color(nsColor: .windowBackgroundColor)
    static let surface = Color(nsColor: .controlBackgroundColor)
    static let subtle = Color.primary.opacity(0.035)
}

func statusColor(_ status: String) -> Color {
    if status == "completed" { return .primary.opacity(0.72) }
    if ["listening", "ready"].contains(status) { return .blue }
    if ["failed", "error", "invalid"].contains(status) { return .red }
    if ["partial", "paused", "locked", "reconnecting"].contains(status) { return .orange }
    if ["running", "preflight", "approving", "merging", "starting", "checking"].contains(status) { return .blue }
    return .secondary
}
