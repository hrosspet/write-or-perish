import api from "../api";

/**
 * Uploads a file in chunks with streaming transcription.
 * Each chunk is transcribed immediately as it's uploaded, allowing
 * the user to see transcript text appear progressively.
 *
 * @param {File} file - The audio file to upload
 * @param {Object} metadata - Additional metadata (parent_id, privacy_level, ai_usage)
 * @param {Function} onProgress - Callback for upload progress (0-100)
 * @param {number} chunkSize - Size of each chunk in bytes (default 5MB)
 * @returns {Promise<Object>} - { nodeId, sessionId } for SSE subscription
 */
export async function uploadFileWithStreamingTranscription(
  file,
  metadata = {},
  onProgress = null,
  chunkSize = 5 * 1024 * 1024 // 5MB chunks
) {
  const totalChunks = Math.ceil(file.size / chunkSize);

  console.log(
    `Starting streaming upload: ${file.name}, Size: ${(file.size / (1024 * 1024)).toFixed(2)}MB, Chunks: ${totalChunks}`
  );

  // Step 1: Initialize streaming transcription session
  const initResponse = await api.post("/nodes/streaming/init", {
    parent_id: metadata.parent_id,
    node_type: metadata.node_type || "user",
    privacy_level: metadata.privacy_level || "private",
    ai_usage: metadata.ai_usage || "none",
  });

  const { node_id, session_id } = initResponse.data;

  console.log(`Initialized streaming session ${session_id} for node ${node_id}`);

  try {
    // Step 2: Upload chunks - each will be transcribed immediately
    for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex++) {
      const start = chunkIndex * chunkSize;
      const end = Math.min(start + chunkSize, file.size);
      const chunk = file.slice(start, end);

      const formData = new FormData();
      formData.append("chunk", chunk, `chunk_${chunkIndex}.webm`);
      formData.append("chunk_index", chunkIndex.toString());
      formData.append("session_id", session_id);

      // Upload chunk - this triggers immediate transcription
      await uploadChunkWithRetry(node_id, formData, chunkIndex, 3);

      // Calculate and report progress
      const progress = Math.round(((chunkIndex + 1) / totalChunks) * 100);
      if (onProgress) {
        onProgress(progress);
      }

      console.log(
        `Uploaded chunk ${chunkIndex + 1}/${totalChunks} (${progress}%) - transcription queued`
      );
    }

    // Step 3: Finalize - assembles all transcribed chunks
    console.log("Finalizing streaming transcription...");
    await api.post(`/nodes/${node_id}/finalize-streaming`, {
      session_id: session_id,
      total_chunks: totalChunks,
    });

    console.log("Upload completed, waiting for transcription to finish");

    return {
      nodeId: node_id,
      sessionId: session_id,
      totalChunks,
    };
  } catch (error) {
    console.error("Streaming upload failed:", error);
    throw error;
  }
}

/**
 * Uploads a single chunk with retry logic
 */
async function uploadChunkWithRetry(nodeId, formData, chunkIndex, maxRetries = 3) {
  let lastError;

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      await api.post(`/nodes/${nodeId}/audio-chunk`, formData, {
        headers: { "Content-Type": "multipart/form-data" },
        timeout: 120000, // 2 minutes per chunk
      });
      return; // Success
    } catch (error) {
      lastError = error;
      console.warn(
        `Chunk ${chunkIndex} upload failed (attempt ${attempt}/${maxRetries}):`,
        error.message
      );

      if (attempt < maxRetries) {
        // Exponential backoff: 1s, 2s, 4s
        const delay = Math.pow(2, attempt - 1) * 1000;
        await new Promise((resolve) => setTimeout(resolve, delay));
      }
    }
  }

  throw new Error(
    `Failed to upload chunk ${chunkIndex} after ${maxRetries} attempts: ${lastError.message}`
  );
}
