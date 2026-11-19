import Foundation

@main
struct ConfigContract {
    static func main() throws {
        let data = try AppSettings.encoder.encode(AppSettings())
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw NSError(domain: "ApprovalDesk", code: 1)
        }
        let normalized = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        FileHandle.standardOutput.write(normalized)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}
