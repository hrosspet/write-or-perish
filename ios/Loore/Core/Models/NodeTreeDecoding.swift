import Foundation

/// Decodes `GET /api/nodes/<id>` without Foundation's nesting limit.
///
/// The answer nests the whole reply tree, two JSON levels per tree level, and
/// Foundation's JSON parser refuses documents nested deeper than 512 levels: a
/// node with about 256 levels of replies below it (an early node of a long voice
/// conversation, the root of a single-thread import) did not open (review M6).
///
/// An iterative scan cuts every node's `children` array out of the bytes, which
/// leaves one shallow JSON object per node. Those are decoded together as a flat
/// array, and the tree is rebuilt bottom-up. No step recurses on the tree depth.
enum NodeTreeDecoding {
    /// One node object in the answer: its byte range, its `children` array's byte
    /// range, and the node it is a child of (`nil`: a child of the focal node).
    private struct Piece {
        var start: Int
        var end = 0
        var childrenRange: Range<Int>?
        var parent: Int?
    }

    private struct Frame {
        var isObject: Bool
        /// Objects: the next string is a key.
        var expectsKey = false
        /// Objects: the last key read was `children`.
        var keyIsChildren = false
        /// Objects that are nodes: `rootNode` for the focal node, else the piece index.
        var node: Int?
        /// Arrays: the `children` array of this node.
        var childrenOf: Int?
        /// Arrays: the offset of the opening bracket.
        var openedAt = 0
    }

    private static let rootNode = -1
    private static let childrenKey = Array("children".utf8)

    static func decodeNodeDetail(_ data: Data, decoder: JSONDecoder) throws -> NodeDetail {
        let bytes = [UInt8](data)
        guard let (root, pieces) = scan(bytes) else {
            // Not the expected shape: let the decoder report what is wrong.
            return try decoder.decode(NodeDetail.self, from: data)
        }
        var detail = try decoder.decode(NodeDetail.self, from: shallow(bytes, root))
        guard !pieces.isEmpty else { return detail }

        var flat = Data("[".utf8)
        for (i, piece) in pieces.enumerated() {
            if i > 0 { flat.append(UInt8(ascii: ",")) }
            flat.append(shallow(bytes, piece))
        }
        flat.append(UInt8(ascii: "]"))
        let decoded = try decoder.decode([LenientTreeNode].self, from: flat).map(\.node)

        // Pieces are in document order, so every node's descendants come after it:
        // building from the last piece back finishes each subtree before its parent.
        var childIndices = [[Int]](repeating: [], count: pieces.count)
        var rootChildren: [Int] = []
        for (i, piece) in pieces.enumerated() {
            if let parent = piece.parent { childIndices[parent].append(i) } else { rootChildren.append(i) }
        }
        var built = decoded
        for i in pieces.indices.reversed() where built[i] != nil {
            let subtree = children(childIndices[i], from: built)
            built[i]?.children = subtree
        }
        detail.children = children(rootChildren, from: built)
        return detail
    }

    /// The nodes at `indices`; none when one of them failed to decode (the whole
    /// `children` array was dropped by the nested decoder's tolerant read, too).
    private static func children(_ indices: [Int], from built: [TreeNode?]) -> [TreeNode] {
        var out: [TreeNode] = []
        out.reserveCapacity(indices.count)
        for index in indices {
            guard let node = built[index] else { return [] }
            out.append(node)
        }
        return out
    }

    /// The piece's bytes with its `children` array replaced by `[]`.
    private static func shallow(_ bytes: [UInt8], _ piece: Piece) -> Data {
        guard let cut = piece.childrenRange else { return Data(bytes[piece.start..<piece.end]) }
        var out = Data(bytes[piece.start..<cut.lowerBound])
        out.append(contentsOf: Array("[]".utf8))
        out.append(contentsOf: bytes[cut.upperBound..<piece.end])
        return out
    }

    /// Finds the focal node object and every node object inside a `children`
    /// array. Returns nil when the bytes are not one well-formed JSON object.
    private static func scan(_ bytes: [UInt8]) -> (Piece, [Piece])? {
        var root: Piece?
        var pieces: [Piece] = []
        var stack: [Frame] = []
        var i = 0
        let count = bytes.count

        while i < count {
            let byte = bytes[i]
            switch byte {
            case UInt8(ascii: "\""):
                let start = i + 1
                i += 1
                while i < count && bytes[i] != UInt8(ascii: "\"") {
                    i += bytes[i] == UInt8(ascii: "\\") ? 2 : 1
                }
                guard i < count else { return nil }
                if let last = stack.indices.last, stack[last].isObject, stack[last].expectsKey {
                    stack[last].expectsKey = false
                    stack[last].keyIsChildren = bytes[start..<i].elementsEqual(childrenKey)
                }
            case UInt8(ascii: "{"):
                var frame = Frame(isObject: true, expectsKey: true)
                if stack.isEmpty {
                    guard root == nil else { return nil }
                    root = Piece(start: i)
                    frame.node = rootNode
                } else if let owner = stack[stack.count - 1].childrenOf {
                    frame.node = pieces.count
                    pieces.append(Piece(start: i, parent: owner == rootNode ? nil : owner))
                }
                stack.append(frame)
            case UInt8(ascii: "["):
                guard !stack.isEmpty else { return nil }
                var frame = Frame(isObject: false, openedAt: i)
                let parent = stack[stack.count - 1]
                if parent.isObject, parent.keyIsChildren, let node = parent.node {
                    frame.childrenOf = node
                }
                stack.append(frame)
            case UInt8(ascii: "}"):
                guard let frame = stack.popLast(), frame.isObject else { return nil }
                if let node = frame.node {
                    if node == rootNode { root?.end = i + 1 } else { pieces[node].end = i + 1 }
                }
            case UInt8(ascii: "]"):
                guard let frame = stack.popLast(), !frame.isObject else { return nil }
                if let owner = frame.childrenOf {
                    let range = frame.openedAt..<(i + 1)
                    if owner == rootNode { root?.childrenRange = range } else { pieces[owner].childrenRange = range }
                }
            case UInt8(ascii: ","):
                if let last = stack.indices.last, stack[last].isObject {
                    stack[last].expectsKey = true
                    stack[last].keyIsChildren = false
                }
            default:
                break
            }
            i += 1
        }
        guard stack.isEmpty, let root, root.end > 0 else { return nil }
        return (root, pieces)
    }
}

/// A reply-tree node whose decoding failure is kept as `nil`, not thrown.
private struct LenientTreeNode: Decodable {
    let node: TreeNode?

    init(from decoder: Decoder) throws {
        node = try? TreeNode(from: decoder)
    }
}
