import Foundation

/// Keeps debounced text fields from hiding the latest keystrokes from an
/// immediately following action such as “查找并选择” or “开始监听”.
@MainActor
final class PendingTextEdits {
    static let shared = PendingTextEdits()

    private var commits: [UUID: () -> Void] = [:]

    func register(id: UUID, commit: @escaping () -> Void) {
        commits[id] = commit
    }

    func unregister(id: UUID) {
        commits.removeValue(forKey: id)
    }

    func commitAll() {
        for commit in Array(commits.values) {
            commit()
        }
    }
}
