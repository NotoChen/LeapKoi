import AppKit

@main
struct CreateIcon {
    static func main() throws {
        guard CommandLine.arguments.count == 4 else {
            throw NSError(domain: "CreateIcon", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "用法：create-icon <源图> <iconset 目录> <菜单栏模板>"
            ])
        }
        let sourceURL = URL(fileURLWithPath: CommandLine.arguments[1])
        let iconset = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        let menuMark = URL(fileURLWithPath: CommandLine.arguments[3])
        guard let source = NSImage(contentsOf: sourceURL),
              let sourceBitmap = NSBitmapImageRep(data: try Data(contentsOf: sourceURL)) else {
            throw NSError(domain: "CreateIcon", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "无法读取应用图标源图"
            ])
        }

        try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
        let icon = try maskedIcon(source)
        try writeIconset(icon, to: iconset)
        try writeMenuMark(sourceBitmap, to: menuMark)
    }

    static func maskedIcon(_ source: NSImage) throws -> NSBitmapImageRep {
        let side = 1024
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: side, pixelsHigh: side,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        let graphics = NSGraphicsContext(bitmapImageRep: bitmap)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = graphics
        graphics.cgContext.clear(CGRect(x: 0, y: 0, width: side, height: side))
        graphics.cgContext.setShouldAntialias(true)
        let rounded = NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: side, height: side),
                                   xRadius: 210, yRadius: 210)
        rounded.addClip()
        source.draw(in: NSRect(x: 0, y: 0, width: side, height: side),
                    from: .zero, operation: .copy, fraction: 1)
        NSGraphicsContext.restoreGraphicsState()
        return bitmap
    }

    static func writeIconset(_ source: NSBitmapImageRep, to directory: URL) throws {
        let entries: [(String, Int)] = [
            ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
            ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
            ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
            ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
            ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024)
        ]
        for (name, size) in entries {
            let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
            let graphics = NSGraphicsContext(bitmapImageRep: bitmap)!
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = graphics
            graphics.imageInterpolation = .high
            graphics.cgContext.clear(CGRect(x: 0, y: 0, width: size, height: size))
            source.draw(in: NSRect(x: 0, y: 0, width: size, height: size),
                        from: .zero, operation: .sourceOver, fraction: 1,
                        respectFlipped: false, hints: nil)
            NSGraphicsContext.restoreGraphicsState()
            try bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent(name))
        }
    }

    static func writeMenuMark(_ source: NSBitmapImageRep, to url: URL) throws {
        let width = source.pixelsWide
        let height = source.pixelsHigh
        var minX = width
        var minY = height
        var maxX = 0
        var maxY = 0
        for y in 0..<height {
            for x in 0..<width {
                guard let color = source.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB),
                      isFishPixel(color) else { continue }
                minX = min(minX, x)
                minY = min(minY, y)
                maxX = max(maxX, x)
                maxY = max(maxY, y)
            }
        }
        guard minX <= maxX, minY <= maxY else {
            throw NSError(domain: "CreateIcon", code: 3, userInfo: [
                NSLocalizedDescriptionKey: "源图中没有找到锦鲤轮廓"
            ])
        }

        let contentWidth = maxX - minX + 1
        let contentHeight = maxY - minY + 1
        let contentSide = max(contentWidth, contentHeight)
        let padding = Double(contentSide) * 0.045
        let side = Double(contentSide) + padding * 2
        let centerX = Double(minX + maxX + 1) / 2
        let centerY = Double(minY + maxY + 1) / 2
        let originX = centerX - side / 2
        let originY = centerY - side / 2
        let pixels = 144
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        let graphics = NSGraphicsContext(bitmapImageRep: bitmap)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = graphics
        graphics.cgContext.clear(CGRect(x: 0, y: 0, width: pixels, height: pixels))
        for y in 0..<pixels {
            let sourceY = originY + (Double(y) + 0.5) * side / Double(pixels)
            for x in 0..<pixels {
                let sourceX = originX + (Double(x) + 0.5) * side / Double(pixels)
                guard let color = source.colorAt(x: min(max(Int(sourceX), 0), width - 1),
                                                 y: min(max(Int(sourceY), 0), height - 1))?
                    .usingColorSpace(.deviceRGB) else { continue }
                let alpha = fishAlpha(color)
                guard alpha > 0.01 else { continue }
                NSColor.black.withAlphaComponent(alpha).setFill()
                // NSGraphicsContext uses a bottom-left origin, while colorAt is top-left.
                graphics.cgContext.fill(CGRect(x: x, y: pixels - y - 1, width: 1, height: 1))
            }
        }
        NSGraphicsContext.restoreGraphicsState()
        try bitmap.representation(using: .png, properties: [:])!.write(to: url)
    }

    static func isFishPixel(_ color: NSColor) -> Bool {
        let red = color.redComponent
        let green = color.greenComponent
        let blue = color.blueComponent
        let value = max(red, max(green, blue))
        return value < 0.50 || (green > red * 1.10 && green >= blue * 0.95 && green > 0.25 && value < 0.78)
    }

    static func fishAlpha(_ color: NSColor) -> CGFloat {
        let red = color.redComponent
        let green = color.greenComponent
        let blue = color.blueComponent
        let value = max(red, max(green, blue))
        let ink = min(1, max(0, (0.58 - value) / 0.24))
        let fin = green > red * 1.10 && green >= blue * 0.95 && green > 0.25 && value < 0.78 ? 1.0 : 0.0
        return max(ink, fin)
    }
}
