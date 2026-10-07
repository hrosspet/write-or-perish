import Foundation
import ZIPFoundation

/// File work for imports (design doc §10): the archive is read with
/// ZIPFoundation from disk (central directory + one entry streamed to a
/// temporary file), never loaded into memory whole; multipart bodies are
/// written to a temporary file and uploaded from it.
enum ImportFiles {
    enum Failure: Error, Equatable {
        /// The web's zip messages, verbatim.
        case unreadableZip
        case noConversations
        case tooLarge
    }

    /// nginx `client_max_body_size 200M` on `/api/`.
    static let uploadLimit: Int64 = 200 * 1024 * 1024
    static let chunkSize = 1 << 20

    static func message(_ failure: Failure, kind: ImportKind) -> String {
        switch failure {
        case .unreadableZip: return "Could not read the zip file. Please make sure it's a valid data export."
        case .noConversations:
            return "Could not find conversations.json in the zip archive. Please upload the original data export."
        case .tooLarge:
            return kind == .chatgpt ? "conversations.json is too large to upload. Please contact support."
                : "This file is larger than 200 MB, the most Loore accepts in one upload."
        }
    }

    /// A private temporary directory for one import's files.
    static func workDirectory() throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("import-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    static func fileSize(_ url: URL) -> Int64 {
        ((try? FileManager.default.attributesOfItem(atPath: url.path)[.size]) as? NSNumber)?.int64Value ?? 0
    }

    /// The web's `extractConversationsBlob`: the entry ending in
    /// `conversations.json`, else the largest `.json` whose top level is an
    /// array. Streams that one entry to `directory/conversations.json`.
    static func extractConversations(from zipURL: URL, into directory: URL) throws -> URL {
        let archive: Archive
        do {
            archive = try Archive(url: zipURL, accessMode: .read)
        } catch {
            throw Failure.unreadableZip
        }
        let files = archive.filter { $0.type == .file }
        if files.isEmpty && fileSize(zipURL) > 0 && !looksLikeZip(zipURL) { throw Failure.unreadableZip }
        let target = directory.appendingPathComponent("conversations.json")
        if let named = files.first(where: { $0.path.hasSuffix("conversations.json") }) {
            try extract(named, from: archive, to: target)
            return target
        }
        let candidates = files.filter { $0.path.lowercased().hasSuffix(".json") }
            .sorted { $0.uncompressedSize > $1.uncompressedSize }
        for entry in candidates {
            let candidate = directory.appendingPathComponent("candidate.json")
            try? FileManager.default.removeItem(at: candidate)
            guard (try? extract(entry, from: archive, to: candidate)) != nil else { continue }
            if topLevelIsArray(candidate) {
                try? FileManager.default.removeItem(at: target)
                try FileManager.default.moveItem(at: candidate, to: target)
                return target
            }
        }
        throw Failure.noConversations
    }

    private static func extract(_ entry: Entry, from archive: Archive, to url: URL) throws {
        try? FileManager.default.removeItem(at: url)
        _ = try archive.extract(entry, to: url, bufferSize: chunkSize)
    }

    private static func looksLikeZip(_ url: URL) -> Bool {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return false }
        defer { try? handle.close() }
        let magic = (try? handle.read(upToCount: 4)) ?? Data()
        return magic.starts(with: [0x50, 0x4B])
    }

    /// The web parses the candidate and checks `Array.isArray`. Natively the
    /// file is not parsed whole: its first non-blank byte must be `[` and the
    /// last `]` (a valid export is one JSON array).
    static func topLevelIsArray(_ url: URL) -> Bool {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return false }
        defer { try? handle.close() }
        let head = (try? handle.read(upToCount: 4096)) ?? Data()
        guard let first = head.first(where: { !isBlank($0) }), first == UInt8(ascii: "[") else { return false }
        let size = (try? handle.seekToEnd()) ?? 0
        let tailStart = size > 4096 ? size - 4096 : 0
        try? handle.seek(toOffset: tailStart)
        let tail = (try? handle.readToEnd()) ?? Data()
        return tail.last(where: { !isBlank($0) }) == UInt8(ascii: "]")
    }

    private static func isBlank(_ byte: UInt8) -> Bool {
        byte == 0x20 || byte == 0x0A || byte == 0x0D || byte == 0x09
    }

    /// Writes `multipart/form-data` with one file part (copied in 1 MB chunks)
    /// to `directory/body.multipart`; returns the body file and its content type.
    static func multipartBody(field: String, filename: String, mimeType: String, source: URL,
                              into directory: URL) throws -> (url: URL, contentType: String) {
        let boundary = "LooreBoundary-\(UUID().uuidString)"
        let bodyURL = directory.appendingPathComponent("body.multipart")
        FileManager.default.createFile(atPath: bodyURL.path, contents: nil)
        let out = try FileHandle(forWritingTo: bodyURL)
        defer { try? out.close() }
        let safeName = filename.replacingOccurrences(of: "\"", with: "")
        try out.write(contentsOf: Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(field)\"; filename=\"\(safeName)\"\r\nContent-Type: \(mimeType)\r\n\r\n".utf8))
        let input = try FileHandle(forReadingFrom: source)
        defer { try? input.close() }
        while let chunk = try input.read(upToCount: chunkSize), !chunk.isEmpty {
            try out.write(contentsOf: chunk)
        }
        try out.write(contentsOf: Data("\r\n--\(boundary)--\r\n".utf8))
        return (bodyURL, "multipart/form-data; boundary=\(boundary)")
    }

    /// Copies a picked (security-scoped) file into `directory`.
    static func copyPicked(_ url: URL, into directory: URL) throws -> URL {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let target = directory.appendingPathComponent(url.lastPathComponent.isEmpty ? "import.zip" : url.lastPathComponent)
        try? FileManager.default.removeItem(at: target)
        try FileManager.default.copyItem(at: url, to: target)
        return target
    }
}
