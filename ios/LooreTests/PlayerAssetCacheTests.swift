import XCTest
@testable import Loore

/// The player holds assets for reuse while it is open, and none after a close.
@MainActor
final class PlayerAssetCacheTests: XCTestCase {
    func testClosingThePlayerEmptiesItsAssetCache() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("assets-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let a = dir.appendingPathComponent("chunk_0000.mp3")
        let b = dir.appendingPathComponent("chunk_0001.mp3")
        try Data(repeating: 0x42, count: 64).write(to: a)
        try Data(repeating: 0x42, count: 64).write(to: b)

        let player = ChunkQueuePlayer()
        player.load(urls: [a.absoluteString, b.absoluteString], durations: [15, 15], title: "Rec", source: .node(1))
        XCTAssertGreaterThan(player.heldAssetCount, 0)

        player.close()
        XCTAssertEqual(player.heldAssetCount, 0)
    }
}
