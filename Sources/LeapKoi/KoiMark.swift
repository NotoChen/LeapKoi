import AppKit

// The brush-painted koi is the app icon. This compact monochrome extraction
// keeps the same gesture legible in the menu bar and sidebar.
enum KoiMark {
    enum State { case ready, paused, attention }

    static let image = template(state: .ready)
    static let pausedImage = template(state: .paused)
    static let attentionImage = template(state: .attention)

    static func menuImage(paused: Bool, status: String) -> NSImage {
        if paused || status == "locked" { return pausedImage }
        if status == "error" { return attentionImage }
        return image
    }

    private static let koi: NSImage = {
        let environment = ProcessInfo.processInfo.environment
        var candidates: [URL] = []
        if let directory = environment["LEAPKOI_RESOURCE_DIR"] ?? environment["APPROVALDESK_RESOURCE_DIR"] {
            candidates.append(URL(fileURLWithPath: directory).appendingPathComponent("MenuBarKoi.png"))
        }
        if let url = Bundle.main.url(forResource: "MenuBarKoi", withExtension: "png") {
            candidates.append(url)
        }
        candidates.append(URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .appendingPathComponent("Resources/MenuBarKoi.png"))
        for url in candidates {
            guard let image = NSImage(contentsOf: url) else { continue }
            image.size = NSSize(width: 18, height: 18)
            return image
        }
        return NSImage(systemSymbolName: "fish.fill", accessibilityDescription: "锦鲤") ?? NSImage()
    }()

    private static func template(state: State) -> NSImage {
        let side: CGFloat = 18
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { rect in
            NSGraphicsContext.current?.imageInterpolation = .high
            koi.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1,
                     respectFlipped: false, hints: nil)
            NSColor.black.setFill()
            NSColor.black.setStroke()
            let centerX = rect.minX + rect.width * 0.658
            let centerY = rect.minY + rect.height * 0.867
            switch state {
            case .ready:
                let diameter = rect.width * 0.115
                NSBezierPath(ovalIn: NSRect(x: centerX - diameter / 2, y: centerY - diameter / 2,
                                            width: diameter, height: diameter)).fill()
            case .paused:
                let width = rect.width * 0.075
                let height = rect.height * 0.22
                let gap = rect.width * 0.055
                for direction in [CGFloat(-1), CGFloat(1)] {
                    let x = centerX + direction * (width + gap) / 2
                    NSBezierPath(roundedRect: NSRect(x: x - width / 2, y: centerY - height / 2,
                                                     width: width, height: height),
                                 xRadius: width / 2, yRadius: width / 2).fill()
                }
            case .attention:
                let lineWidth = rect.width * 0.075
                let stem = NSBezierPath()
                stem.lineWidth = lineWidth
                stem.lineCapStyle = .round
                stem.move(to: NSPoint(x: centerX, y: centerY + rect.height * 0.015))
                stem.line(to: NSPoint(x: centerX, y: centerY + rect.height * 0.115))
                stem.stroke()
                let diameter = lineWidth
                NSBezierPath(ovalIn: NSRect(x: centerX - diameter / 2, y: centerY - rect.height * 0.07,
                                            width: diameter, height: diameter)).fill()
            }
            return true
        }
        image.isTemplate = true
        return image
    }
}
