import React, { useState, forwardRef, useImperativeHandle, useEffect, useCallback } from "react";
import { useMediaRecorder } from "../hooks/useMediaRecorder";
import { useAsyncTaskPolling } from "../hooks/useAsyncTaskPolling";
import { useDraft } from "../hooks/useDraft";
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
    const [showDraftRecovery, setShowDraftRecovery] = useState(false);

    // Draft auto-save hook
    const {
      draft,
      isLoaded: isDraftLoaded,
      saveDraft,
      deleteDraft,
      lastSaved,
      isSaving: isDraftSaving
    } = useDraft({
      nodeId: editMode ? nodeId : null,
      parentId: editMode ? null : parentId
    });

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

    // Show draft recovery dialog when a draft is loaded
    useEffect(() => {
      if (isDraftLoaded && draft && draft.content) {
        // Only show recovery if draft content differs from initial content
        const draftDiffersFromInitial = draft.content !== (initialContent || "");
        if (draftDiffersFromInitial) {
          setShowDraftRecovery(true);
        }
      }
    }, [isDraftLoaded, draft, initialContent]);

    // Handle draft recovery
    const handleRecoverDraft = useCallback(() => {
      if (draft && draft.content) {
        setContent(draft.content);
      }
      setShowDraftRecovery(false);
    }, [draft]);

    const handleDiscardDraft = useCallback(() => {
      deleteDraft();
      setShowDraftRecovery(false);
    }, [deleteDraft]);

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
        // Delete draft after successful transcription
        deleteDraft();
        const normalizedData = { ...transcriptionData, id: transcriptionData.node_id };
        onSuccess(normalizedData);
        setUploadedNodeId(null);
      } else if (transcriptionStatus === 'failed') {
        setLoading(false);
        setError(transcriptionError || 'Transcription failed');
        setUploadedNodeId(null);
      }
    }, [transcriptionStatus, transcriptionData, transcriptionError, onSuccess, deleteDraft]);

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
              const uploadResult = await uploadFileInChunks(
                fileToUpload,
                { parent_id: parentId, node_type: 'user' },
                (progress) => {
                  setUploadProgress(progress);
                }
              );

              setIsUploading(false);
              setUploadProgress(100);

              // uploadFileInChunks returns data directly (not axios response)
              const nodeId = uploadResult.id;
              setUploadedNodeId(nodeId);
              // Keep loading state active while transcription is in progress
              // Polling will start automatically via useEffect
              return;
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

            // Set the node ID to trigger polling via useEffect
            const nodeId = response.data.id;
            setUploadedNodeId(nodeId);
            // Keep loading state active while transcription is in progress
            // Polling will start automatically via useEffect
            return;
          }
        } else {
          // Create a new text node
          response = await api.post("/nodes/", { content, parent_id: parentId });
        }
        // Delete draft after successful save
        deleteDraft();
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

    // Format time ago for last saved indicator
    const formatTimeAgo = (date) => {
      if (!date) return '';
      const seconds = Math.floor((new Date() - date) / 1000);
      if (seconds < 60) return 'just now';
      const minutes = Math.floor(seconds / 60);
      if (minutes < 60) return `${minutes}m ago`;
      const hours = Math.floor(minutes / 60);
      return `${hours}h ago`;
    };

    return (
      <form onSubmit={handleSubmit}>
        {/* Draft recovery dialog */}
        {showDraftRecovery && (
          <div style={{
            padding: '12px',
            marginBottom: '12px',
            backgroundColor: '#fff3cd',
            border: '1px solid #ffc107',
            borderRadius: '4px'
          }}>
            <div style={{ marginBottom: '8px', fontWeight: 'bold' }}>
              Unsaved draft found
            </div>
            <div style={{ marginBottom: '8px', fontSize: '0.9em', color: '#666' }}>
              You have an unsaved draft from {draft?.updated_at ? new Date(draft.updated_at).toLocaleString() : 'earlier'}.
              Would you like to recover it?
            </div>
            <div style={{ display: 'flex', gap: '8px' }}>
              <button
                type="button"
                onClick={handleRecoverDraft}
                style={{
                  padding: '6px 12px',
                  backgroundColor: '#28a745',
                  color: 'white',
                  border: 'none',
                  borderRadius: '4px',
                  cursor: 'pointer'
                }}
              >
                Recover Draft
              </button>
              <button
                type="button"
                onClick={handleDiscardDraft}
                style={{
                  padding: '6px 12px',
                  backgroundColor: '#dc3545',
                  color: 'white',
                  border: 'none',
                  borderRadius: '4px',
                  cursor: 'pointer'
                }}
              >
                Discard
              </button>
            </div>
          </div>
        )}

        {/* Text entry */}
        <textarea
          value={content}
          onChange={(e) => {
            const newContent = e.target.value;
            setContent(newContent);
            // Auto-save draft when user types
            if (newContent.trim()) {
              saveDraft(newContent);
            }
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

        {/* Auto-save status indicator */}
        {content.trim() && (
          <div style={{
            marginTop: '4px',
            fontSize: '0.85em',
            color: '#666',
            display: 'flex',
            alignItems: 'center',
            gap: '4px'
          }}>
            {isDraftSaving ? (
              <>
                <span style={{ color: '#ffc107' }}>Saving...</span>
              </>
            ) : lastSaved ? (
              <>
                <span style={{ color: '#28a745' }}>Draft saved {formatTimeAgo(lastSaved)}</span>
              </>
            ) : null}
          </div>
        )}

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

        {!hideSubmit && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '8px' }}>
            <button type="submit" disabled={loading}>
              {isUploading
                ? `Uploading... ${uploadProgress}%`
                : loading && transcriptionStatus === 'processing' && transcriptionProgress > 0
                ? `Transcribing... ${transcriptionProgress}%`
                : loading && transcriptionStatus === 'pending'
                ? "Waiting to transcribe..."
                : loading
                ? "Submitting..."
                : "Submit"}
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