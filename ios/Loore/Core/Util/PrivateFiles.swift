import Foundation

/// Private files the app writes to `tmp/` for a moment: the dictation copy for
/// "Save audio" (`loore-dictation.m4a`), the journal export (`loore-export-*.txt`),
/// audio downloads and converted recordings (`<uuid>/node-…`), import working
/// folders (`import-<uuid>`), downloads not yet moved (`CFNetworkDownload_*`) and
/// picked files (`*-Inbox`). Each is deleted once used; the sweep at launch and
/// at sign-out removes what a killed app left (design doc §3, M2).
enum PrivateFiles {
    static func sweepTemporary(in directory: URL = FileManager.default.temporaryDirectory) {
        let fileManager = FileManager.default
        guard let items = try? fileManager.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil) else { return }
        for item in items where isOurs(item.lastPathComponent) {
            try? fileManager.removeItem(at: item)
        }
    }

    static func isOurs(_ name: String) -> Bool {
        name.hasPrefix("loore-") || name.hasPrefix("import-") || name.hasPrefix("CFNetworkDownload_")
            || name.hasSuffix("-Inbox") || UUID(uuidString: name) != nil
    }

    /// Deletes one shared file, and its folder when the file sits alone in a
    /// `<uuid>/` folder (audio downloads).
    static func remove(_ url: URL) {
        let fileManager = FileManager.default
        try? fileManager.removeItem(at: url)
        let parent = url.deletingLastPathComponent()
        if UUID(uuidString: parent.lastPathComponent) != nil {
            try? fileManager.removeItem(at: parent)
        }
    }
}
