import AppKit
import ImageIO
import SwiftUI

@MainActor
final class AvatarImageCache {
    static let shared = AvatarImageCache()
    static let allowedHostSuffixes = ["dingtalk.com", "alicdn.com"]
    static let maximumBytes = 2 * 1024 * 1024
    private let images = NSCache<NSURL, NSImage>()
    private var requests: [URL: Task<NSImage?, Never>] = [:]
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
        images.countLimit = 256
        images.totalCostLimit = 16 * 1024 * 1024
    }

    static func isAllowed(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "https",
              let host = url.host?.lowercased(),
              url.user == nil, url.password == nil,
              url.port == nil || url.port == 443 else { return false }
        return allowedHostSuffixes.contains { host == $0 || host.hasSuffix("." + $0) }
    }

    private nonisolated static func thumbnail(from data: Data) -> CGImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        return CGImageSourceCreateThumbnailAtIndex(source, 0, [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: 128
        ] as CFDictionary)
    }

    func image(for url: URL) async -> NSImage? {
        guard Self.isAllowed(url) else { return nil }
        if let image = images.object(forKey: url as NSURL) { return image }
        if let pending = requests[url] { return await pending.value }
        let pending = Task<NSImage?, Never> {
            do {
                let request = URLRequest(url: url, cachePolicy: .useProtocolCachePolicy, timeoutInterval: 10)
                let (data, response) = try await session.data(for: request)
                guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                      data.count <= Self.maximumBytes,
                      http.expectedContentLength <= Int64(Self.maximumBytes) || http.expectedContentLength < 0 else {
                    return nil
                }
                // Image decoding can be a few milliseconds per image. Run it
                // away from the main actor so a burst of avatars cannot delay
                // task list updates or approval controls.
                let bitmap = await Task.detached(priority: .utility) {
                    Self.thumbnail(from: data)
                }.value
                guard let bitmap else { return nil }
                return NSImage(
                    cgImage: bitmap,
                    size: NSSize(width: bitmap.width, height: bitmap.height)
                )
            } catch { return nil }
        }
        requests[url] = pending
        let image = await pending.value
        requests[url] = nil
        if let image { images.setObject(image, forKey: url as NSURL, cost: 128 * 128 * 4) }
        return image
    }
}

struct SenderAvatar: View {
    let name: String
    let url: String
    var size: CGFloat = 32
    @State private var image: NSImage?
    @State private var loadedURL = ""
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
        ZStack {
            if loadedURL == url, let image {
                Image(nsImage: image).resizable().scaledToFill()
            } else {
                Text(String(name.prefix(1))).font(.system(size: size * 0.42, weight: .medium))
                    .foregroundStyle(Color.brand)
            }
        }
        .frame(width: size, height: size)
        .background(Color.primary.opacity(scheme == .dark ? 0.055 : 0.035))
        .clipShape(shape)
        .overlay(shape.strokeBorder(Color.primary.opacity(scheme == .dark ? 0.1 : 0.07), lineWidth: 0.5))
        .accessibilityHidden(true)
        .task(id: url) {
            guard let target = URL(string: url), AvatarImageCache.isAllowed(target) else {
                image = nil; loadedURL = url; return
            }
            let result = await AvatarImageCache.shared.image(for: target)
            guard !Task.isCancelled else { return }
            image = result; loadedURL = url
        }
    }
}
