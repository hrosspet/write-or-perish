import api from "../api";

/**
 * Uploads a file in chunks with progress tracking
 * @param {File} file - The file to upload
 * @param {Object} metadata - Additional metadata to send with the upload
 * @param {Function} onProgress - Callback for progress updates (0-100)
 * @param {number} chunkSize - Size of each chunk in bytes (default 5MB)
 * @returns {Promise<Object>} - Server response with node info
 */
export async function uploadFileInChunks(
  file,
  metadata = {},
  onProgress = null,
  chunkSize = 5 * 1024 * 1024 // 5MB chunks
) {
  const totalChunks = Math.ceil(file.size / chunkSize);
  const uploadId = `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

  try {
    // Step 1: Initialize upload session
    const initResponse = await api.post("/nodes/upload/init", {
      filename: file.name,
      filesize: file.size,
      total_chunks: totalChunks,
      upload_id: uploadId,
      ...metadata,
    });

    const { node_id } = initResponse.data;

    // Step 2: Upload chunks
    for (let chunkIndex = 0; chunkIndex < totalChunks; chunkIndex++) {
      const start = chunkIndex * chunkSize;
      const end = Math.min(start + chunkSize, file.size);
      const chunk = file.slice(start, end);

      const formData = new FormData();
      formData.append("chunk", chunk);
      formData.append("chunk_index", chunkIndex);
      formData.append("upload_id", uploadId);
      formData.append("node_id", node_id);

      // Upload chunk with retry logic
      await uploadChunkWithRetry(formData, chunkIndex, 3);

      // Calculate and report progress
      const progress = Math.round(((chunkIndex + 1) / totalChunks) * 100);
      if (onProgress) {
        onProgress(progress);
      }
    }

    // Step 3: Finalize upload
    const finalizeResponse = await api.post("/nodes/upload/finalize", {
      upload_id: uploadId,
      node_id: node_id,
    });

    return finalizeResponse.data;
  } catch (error) {
    console.error("Chunked upload failed:", error);
    // Attempt cleanup on error
    try {
      await api.post("/nodes/upload/cleanup", { upload_id: uploadId });
    } catch (cleanupError) {
      console.error("Cleanup failed:", cleanupError);
    }
    throw error;
  }
}

/**
 * True for an answer from the server that no retry can change (a 4xx other
 * than a timeout or "too many requests"): e.g. 410 when the upload stopped
 * arriving too long ago. Its words go to the form as they are.
 */
export function isFinalRefusal(error) {
  const status = error?.response?.status;
  return status >= 400 && status < 500 && status !== 408 && status !== 429;
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
        timeout: 120000, // 2 minutes per chunk (generous for 5MB)
      });
      return; // Success
    } catch (error) {
      if (isFinalRefusal(error)) {
        // The form shows error.response.data.error, the server's words.
        throw error;
      }
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
