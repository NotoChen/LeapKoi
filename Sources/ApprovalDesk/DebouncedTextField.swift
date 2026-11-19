import SwiftUI

/// Text fields commit after a short pause so editing a group name or token
/// does not rewrite the JSON config and notify the worker on every keystroke.
struct DebouncedTextField: View {
    let placeholder: String
    @Binding var text: String
    var monospaced = false
    var fontSize: CGFloat = 11
    var width: CGFloat?
    var secure = false

    @State private var draft = ""
    @State private var commitTask: Task<Void, Never>?
    @State private var revealed = false
    @State private var editID = UUID()

    var body: some View {
        HStack(spacing: 6) {
            field
                .textFieldStyle(.roundedBorder)
                .font(.system(size: fontSize, design: monospaced ? .monospaced : .default))
                .frame(width: width)
            if secure {
                Button { revealed.toggle() } label: {
                    Image(systemName: revealed ? "eye.slash" : "eye")
                }
                .buttonStyle(.borderless)
                .foregroundStyle(.secondary)
                .accessibilityLabel(revealed ? "隐藏 Token" : "显示 Token")
                .help(revealed ? "隐藏 Token" : "显示 Token")
            }
        }
            .onAppear {
                draft = text
                PendingTextEdits.shared.register(id: editID, commit: commit)
            }
            .onChange(of: text) { _, value in
                guard value != draft else { return }
                commitTask?.cancel()
                commitTask = nil
                draft = value
            }
            .onChange(of: draft) { _, value in
                guard value != text else { return }
                commitTask?.cancel()
                commitTask = Task { @MainActor in
                    try? await Task.sleep(nanoseconds: 350_000_000)
                    guard !Task.isCancelled, draft == value else { return }
                    text = value
                }
            }
            .onSubmit(commit)
            .onDisappear {
                PendingTextEdits.shared.unregister(id: editID)
                commit()
            }
    }

    @ViewBuilder
    private var field: some View {
        if secure && !revealed {
            SecureField(placeholder, text: $draft)
        } else {
            TextField(placeholder, text: $draft)
        }
    }

    private func commit() {
        commitTask?.cancel()
        commitTask = nil
        if text != draft { text = draft }
    }
}
