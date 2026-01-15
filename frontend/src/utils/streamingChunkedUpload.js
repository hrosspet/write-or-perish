import api from "../api";

/**
 * Uploads a file in chunks with streaming transcription.
 *
 * For uploaded files, we can't transcribe each upload chunk because
 * file.slice() produces raw bytes without audio headers. Instead:
 * 1. Upload all chunks (for efficient large file transfer)
 * 2. Server reassembles the file
 * 3. Server splits the audio properly using ffmpeg (time-based)
 * 4. Server transcribes each proper audio chunk and broadcasts via SSE
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
  const uploadId = `upload_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;

  console.log(
    `Starting streaming upload: ${file.name}, Size: ${(file.size / (1024 * 1024)).toFixed(2)}MB, Chunks: ${totalChunks}`
  );

  // Step 1: Initialize chunked upload with streaming transcription flag
  const initResponse = await api.post("/nodes/upload/init", {
    filename: file.name,
    filesize: file.size,
    total_chunks: totalChunks,
    upload_id: uploadId,
    parent_id: metadata.parent_id,
    node_type: metadata.node_type || "user",
    privacy_level: metadata.privacy_level || "private",
    ai_usage: metadata.ai_usage || "none",
    streaming_transcription: true, // Request streaming transcription after upload
  });

  const { node_id } = initResponse.data;

  console.log(`Initialized upload session ${uploadId} for node ${node_id}`);

  try {
    // Step 2: Upload all chunks
    for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex++) {
      const start = chunkIndex * chunkSize;
      const end = Math.min(start + chunkSize, file.size);
      const chunk = file.slice(start, end);

      const formData = new FormData();
      formData.append("chunk", chunk, `chunk_${chunkIndex}`);
      formData.append("chunk_index", chunkIndex.toString());
      formData.append("upload_id", uploadId);
      formData.append("node_id", node_id.toString());

      await uploadChunkWithRetry(formData, chunkIndex, 3);

      // Calculate and report progress (upload progress is 0-80%)
      const progress = Math.round(((chunkIndex + 1) / totalChunks) * 80);
      if (onProgress) {
        onProgress(progress);
      }

      console.log(
        `Uploaded chunk ${chunkIndex + 1}/${totalChunks} (${progress}%)`
      );
    }

    // Step 3: Finalize upload with streaming transcription mode
    console.log("Finalizing upload with streaming transcription...");
    const finalizeResponse = await api.post("/nodes/upload/finalize", {
      upload_id: uploadId,
      node_id: node_id,
      streaming_transcription: true, // Trigger server-side chunked transcription
    });

    if (onProgress) {
      onProgress(85); // Upload complete, transcription starting
    }

    console.log("Upload complete, streaming transcription started");

    return {
      nodeId: node_id,
      sessionId: finalizeResponse.data.session_id || uploadId,
      totalChunks,
    };
  } catch (error) {
    // Clean up on failure
    try {
      await api.post("/nodes/upload/cleanup", { upload_id: uploadId });
    } catch (cleanupError) {
      console.warn("Cleanup failed:", cleanupError);
    }
    console.error("Streaming upload failed:", error);
    throw error;
  }
}

/**
 * Uploads a single chunk with retry logic
 */
async function uploadChunkWithRetry(formData, chunkIndex, maxRetries = 3) {
  let lastError;

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      await api.post("/nodes/upload/chunk", formData, {
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
