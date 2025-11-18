import React, { useState, forwardRef, useImperativeHandle, useEffect } from "react";
import { useMediaRecorder } from "../hooks/useMediaRecorder";
import { useAsyncTaskPolling } from "../hooks/useAsyncTaskPolling";
import MicButton from "./MicButton";
import api from "../api";
import { uploadFileInChunks } from "../utils/chunkedUpload";

const NodeForm = forwardRef(
  (
    { parentId, onSuccess, hideSubmit, initialContent, editMode = false, nodeId },
    ref
  ) => {
    const [content, setContent] = useState(initialContent || "");
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(false);
    const [uploadedNodeId, setUploadedNodeId] = useState(null);
    const [uploadProgress, setUploadProgress] = useState(0);
    const [isUploading, setIsUploading] = useState(false);

    // Audio recording state
    const {
      status: recStatus,
      mediaBlob,
      mediaUrl,
      duration: recDuration,
      startRecording,
      stopRecording,
      resetRecording
    } = useMediaRecorder();
    // Audio file upload state
    const [uploadedFile, setUploadedFile] = useState(null);
    const fileInputRef = React.useRef(null);

    // Transcription polling
    const {
      status: transcriptionStatus,
      progress: transcriptionProgress,
      data: transcriptionData,
      error: transcriptionError,
      startPolling: startTranscriptionPolling
    } = useAsyncTaskPolling(
      uploadedNodeId ? `/nodes/${uploadedNodeId}/transcription-status` : null,
      { enabled: false }
    );

    // Auto-start polling when uploadedNodeId is set
    useEffect(() => {
      if (uploadedNodeId) {
        startTranscriptionPolling();
      }
    }, [uploadedNodeId, startTranscriptionPolling]);

    // Handle transcription completion
    useEffect(() => {
      if (transcriptionStatus === 'completed' && transcriptionData) {
        setLoading(false);
        onSuccess(transcriptionData);
        setUploadedNodeId(null);
      } else if (transcriptionStatus === 'failed') {
        setLoading(false);
        setError(transcriptionError || 'Transcription failed');
        setUploadedNodeId(null);
      }
    }, [transcriptionStatus, transcriptionData, transcriptionError, onSuccess]);

    const handleFileSelect = (event) => {
      const file = event.target.files[0];
      if (!file) return;

      // Validate file size (200 MB)
      const maxSize = 200 * 1024 * 1024;
      if (file.size > maxSize) {
        setError("File size must be under 200 MB");
        return;
      }

      // Validate file type
      const allowedTypes = ['audio/webm', 'audio/wav', 'audio/wave', 'audio/x-wav', 'audio/m4a', 'audio/x-m4a',
                           'audio/mp3', 'audio/mpeg', 'audio/mp4', 'audio/ogg', 'audio/flac', 'audio/aac', 'audio/x-aac'];
      const allowedExts = ['.webm', '.wav', '.m4a', '.mp3', '.mp4', '.mpeg', '.mpga', '.ogg', '.oga', '.flac', '.aac'];

      const fileExt = '.' + file.name.split('.').pop().toLowerCase();
      if (!allowedTypes.includes(file.type) && !allowedExts.includes(fileExt)) {
        setError("Invalid file type. Please upload an audio file (mp3, wav, m4a, webm, ogg, flac, aac)");
        return;
      }

      setError("");
      setUploadedFile(file);
      // Reset recording if user uploads a file
      if (recStatus !== 'idle') {
        resetRecording();
      }
      // Clear text content
      setContent("");
    };

    const handleSubmit = async (event) => {
      event && event.preventDefault();
      // Validate: require content or audio
      if (!editMode && (mediaBlob || uploadedFile)) {
        // Submit audio recording or uploaded file
      } else if (!content.trim()) {
        setError("Content is required.");
        return;
      }
      setLoading(true);
      setError("");
      try {
        let response;
        if (editMode && nodeId) {
          // Update (edit) existing node (text only)
          response = await api.put(`/nodes/${nodeId}`, { content });
        } else if (mediaBlob || uploadedFile) {
          // Determine which file to upload
          const fileToUpload = uploadedFile || new File([mediaBlob], 'recording.webm', { type: mediaBlob.type });

          // Check file size - use chunked upload for files larger than 10MB
          const useChunkedUpload = fileToUpload.size > 10 * 1024 * 1024;

          if (useChunkedUpload) {
            // Use chunked upload for large files
            setIsUploading(true);
            setUploadProgress(0);

            try {
              response = await uploadFileInChunks(
                fileToUpload,
                { parent_id: parentId, node_type: 'user' },
                (progress) => {
                  setUploadProgress(progress);
                }
              );

              setIsUploading(false);
              setUploadProgress(100);
            } catch (uploadErr) {
              setIsUploading(false);
              setUploadProgress(0);
              throw uploadErr;
            }
          } else {
            // Use traditional upload for small files (< 10MB)
            const formData = new FormData();
            formData.append('audio_file', fileToUpload);
            if (parentId) formData.append('parent_id', parentId);

            response = await api.post("/nodes/", formData, {
              headers: { 'Content-Type': 'multipart/form-data' }
            });
          }

          // Set the node ID to trigger polling via useEffect
          const nodeId = response.data.id;
          setUploadedNodeId(nodeId);
          // Keep loading state active while transcription is in progress
          // Polling will start automatically via useEffect
          return;
        } else {
          // Create a new text node
          response = await api.post("/nodes/", { content, parent_id: parentId });
        }
        onSuccess(response.data);
        setLoading(false);
      } catch (err) {
        console.error("Error in NodeForm:", err);
        setError(err.response?.data?.error || err.message || "Error submitting form.");
        setLoading(false);
        setIsUploading(false);
        setUploadProgress(0);
      }
    };

    useImperativeHandle(ref, () => ({
      submit: () => handleSubmit({ preventDefault: () => {} }),
      isDirty: () => content.trim().length > 0 || mediaBlob || uploadedFile,
    }));

    return (
      <form onSubmit={handleSubmit}>
        {/* Text entry */}
        <textarea
          value={content}
          onChange={(e) => {
            setContent(e.target.value);
            // Discard recording or uploaded file if user types
            if (!editMode) {
              if (recStatus !== 'idle') {
                resetRecording();
              }
              if (uploadedFile) {
                setUploadedFile(null);
              }
            }
          }}
          rows={20}
          style={{ width: "100%" }}
          placeholder="Write your thoughts here..."
          disabled={!editMode && (recStatus === 'recording' || uploadedFile)}
        />
        {error && <div style={{ color: "red" }}>{error}</div>}

        {/* Display uploaded file info */}
        {uploadedFile && (
          <div style={{ marginTop: '8px', padding: '8px', backgroundColor: '#f0f0f0', borderRadius: '4px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span>📁 {uploadedFile.name} ({(uploadedFile.size / 1024 / 1024).toFixed(2)} MB)</span>
            <button
              type="button"
              onClick={() => {
                setUploadedFile(null);
                if (fileInputRef.current) fileInputRef.current.value = '';
              }}
              style={{ marginLeft: '8px', padding: '4px 8px', cursor: 'pointer' }}
            >
              Remove
            </button>
          </div>
        )}

        {/* Upload progress */}
        {isUploading && (
          <div style={{ marginTop: '12px', padding: '12px', backgroundColor: '#e3f2fd', borderRadius: '8px' }}>
            <div style={{ marginBottom: '8px', fontWeight: 'bold' }}>
              📤 Uploading audio file...
            </div>
            <div style={{ width: '100%', backgroundColor: '#bbdefb', borderRadius: '4px', overflow: 'hidden', height: '24px' }}>
              <div
                style={{
                  width: `${uploadProgress}%`,
                  height: '100%',
                  backgroundColor: '#2196F3',
                  transition: 'width 0.3s ease',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: 'white',
                  fontSize: '12px',
                  fontWeight: 'bold'
                }}
              >
                {uploadProgress}%
              </div>
            </div>
          </div>
        )}

        {/* Transcription status */}
        {loading && transcriptionStatus && !isUploading && (
          <div style={{ marginTop: '12px', padding: '12px', backgroundColor: '#f5f5f5', borderRadius: '8px' }}>
            <div style={{ marginBottom: '8px', fontWeight: 'bold' }}>
              {transcriptionStatus === 'pending' && '⏳ Waiting to transcribe...'}
              {transcriptionStatus === 'processing' && '🎙️ Transcribing audio...'}
              {transcriptionStatus === 'completed' && '✅ Complete!'}
              {transcriptionStatus === 'failed' && '❌ Transcription failed'}
            </div>
            {transcriptionProgress > 0 && (
              <div style={{ width: '100%', backgroundColor: '#e0e0e0', borderRadius: '4px', overflow: 'hidden', height: '24px' }}>
                <div
                  style={{
                    width: `${transcriptionProgress}%`,
                    height: '100%',
                    backgroundColor: '#4CAF50',
                    transition: 'width 0.3s ease',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    color: 'white',
                    fontSize: '12px',
                    fontWeight: 'bold'
                  }}
                >
                  {transcriptionProgress}%
                </div>
              </div>
            )}
          </div>
        )}

        {!hideSubmit && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '8px' }}>
            <button type="submit" disabled={loading}>
              {isUploading ? `Uploading... ${uploadProgress}%` : loading && transcriptionStatus ? "Transcribing..." : loading ? "Submitting..." : "Submit"}
            </button>
            {!editMode && (
              <>
                <MicButton
                  status={recStatus}
                  mediaUrl={mediaUrl}
                  duration={recDuration}
                  startRecording={startRecording}
                  stopRecording={stopRecording}
                  resetRecording={resetRecording}
                />
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".mp3,.wav,.m4a,.webm,.ogg,.oga,.flac,.aac,.mp4,.mpeg,.mpga,audio/*"
                  onChange={handleFileSelect}
                  style={{ display: 'none' }}
                />
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={recStatus === 'recording'}
                  style={{ padding: '8px 16px', cursor: 'pointer' }}
                >
                  Upload
                </button>
              </>
            )}
          </div>
        )}
      </form>
    );
  }
);

export default NodeForm;