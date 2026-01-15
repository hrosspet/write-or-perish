import React, { useState, forwardRef, useImperativeHandle, useEffect, useCallback } from "react";
import { useMediaRecorder } from "../hooks/useMediaRecorder";
import { useAsyncTaskPolling } from "../hooks/useAsyncTaskPolling";
import { useDraft } from "../hooks/useDraft";
import { useStreamingTranscription } from "../hooks/useStreamingTranscription";
import MicButton from "./MicButton";
import StreamingMicButton from "./StreamingMicButton";
import PrivacySelector from "./PrivacySelector";
import api from "../api";
import { uploadFileInChunks } from "../utils/chunkedUpload";

const NodeForm = forwardRef(
  (
    { parentId, onSuccess, hideSubmit, initialContent, editMode = false, nodeId, initialPrivacyLevel, initialAiUsage },
    ref
  ) => {
    const [content, setContent] = useState(initialContent || "");
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(false);
    const [uploadedNodeId, setUploadedNodeId] = useState(null);
    const [uploadProgress, setUploadProgress] = useState(0);
    const [isUploading, setIsUploading] = useState(false);
    const [hasDraft, setHasDraft] = useState(false);

    // Privacy settings state - use initial values if provided (edit mode), otherwise defaults
    // For new nodes with a parent, we'll update these after fetching the parent
    const [privacyLevel, setPrivacyLevel] = useState(initialPrivacyLevel || "private");
    const [aiUsage, setAiUsage] = useState(initialAiUsage || "none");

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

    // Streaming transcription mode (real-time transcription while recording)
    const [useStreamingMode, setUseStreamingMode] = useState(false);
    // Track streaming node ID when transcription completes (for "review before save" flow)
    const [streamingNodeId, setStreamingNodeId] = useState(null);

    // Streaming transcription hook
    const {
      isRecording: isStreamingRecording,
    } = useStreamingTranscription({
      parentId,
      privacyLevel,
      aiUsage,
      chunkIntervalMs: 5 * 60 * 1000, // 5 minutes
      onTranscriptUpdate: (transcript) => {
        // Update the main content to show live transcription
        if (useStreamingMode) {
          setContent(transcript);
        }
      },
      onComplete: (data) => {
        // Streaming transcription complete
        setLoading(false);
        deleteDraft();
        setHasDraft(false);
        onSuccess({ id: data.nodeId, content: data.content });
      },
      onError: (err) => {
        setError(err.message || 'Streaming transcription failed');
        setLoading(false);
      },
    });

    // Transcription polling (for non-streaming mode)
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

    // Fetch parent node privacy settings when creating a child node
    useEffect(() => {
      // Only fetch parent if we have a parentId and we're not in edit mode
      // and we haven't explicitly provided initial privacy settings
      if (parentId && !editMode && !initialPrivacyLevel && !initialAiUsage) {
        api.get(`/nodes/${parentId}`)
          .then((response) => {
            const parent = response.data;
            // Set privacy settings to match parent's settings
            setPrivacyLevel(parent.privacy_level || "private");
            setAiUsage(parent.ai_usage || "none");
          })
          .catch((err) => {
            console.error("Error fetching parent node:", err);
            // If we can't fetch parent, keep the defaults
          });
      }
    }, [parentId, editMode, initialPrivacyLevel, initialAiUsage]);

    // Auto-populate form with draft when loaded
    useEffect(() => {
      if (isDraftLoaded && draft && draft.content) {
        // Auto-populate the form with draft content
        setContent(draft.content);
        setHasDraft(true);
      }
    }, [isDraftLoaded, draft]);

    // Handle discard draft
    const handleDiscardDraft = useCallback(() => {
      deleteDraft();
      setHasDraft(false);
      // Reset content to initial value (empty for new nodes, original content for edit mode)
      setContent(initialContent || "");
    }, [deleteDraft, initialContent]);

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
        setHasDraft(false);
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

        // Handle streaming transcription completion - node already exists
        if (streamingNodeId) {
          // Update the streaming node with any edits the user made
          response = await api.put(`/nodes/${streamingNodeId}`, {
            content,
            privacy_level: privacyLevel,
            ai_usage: aiUsage
          });
          deleteDraft();
          setHasDraft(false);
          setStreamingNodeId(null);
          onSuccess(response.data);
          setLoading(false);
          return;
        }

        if (editMode && nodeId) {
          // Update (edit) existing node (text only)
          response = await api.put(`/nodes/${nodeId}`, {
            content,
            privacy_level: privacyLevel,
            ai_usage: aiUsage
          });
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
                {
                  parent_id: parentId,
                  node_type: 'user',
                  privacy_level: privacyLevel,
                  ai_usage: aiUsage
                },
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
            formData.append('privacy_level', privacyLevel);
            formData.append('ai_usage', aiUsage);

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
          response = await api.post("/nodes/", {
            content,
            parent_id: parentId,
            privacy_level: privacyLevel,
            ai_usage: aiUsage
          });
        }
        // Delete draft after successful save
        deleteDraft();
        setHasDraft(false);
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
        {/* Text entry */}
        <textarea
          value={content}
          onChange={(e) => {
            const newContent = e.target.value;
            setContent(newContent);
            // Auto-save draft when user types
            if (newContent.trim()) {
              saveDraft(newContent);
              setHasDraft(true);
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

        {/* Privacy Settings */}
        <PrivacySelector
          privacyLevel={privacyLevel}
          aiUsage={aiUsage}
          onPrivacyChange={setPrivacyLevel}
          onAIUsageChange={setAiUsage}
          disabled={loading}
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
            {hasDraft && (
              <button
                type="button"
                onClick={handleDiscardDraft}
                disabled={loading}
                style={{ padding: '8px 16px', cursor: 'pointer' }}
              >
                Discard draft
              </button>
            )}
            {!editMode && (
              <>
                {/* Mode toggle for streaming vs regular recording */}
                <label style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                  fontSize: '0.85em',
                  cursor: 'pointer',
                  padding: '4px 8px',
                  backgroundColor: useStreamingMode ? '#e7f3ff' : '#f8f9fa',
                  borderRadius: '4px',
                  border: '1px solid #dee2e6',
                }}>
                  <input
                    type="checkbox"
                    checked={useStreamingMode}
                    onChange={(e) => setUseStreamingMode(e.target.checked)}
                    disabled={recStatus === 'recording' || isStreamingRecording || loading}
                  />
                  <span title="Enable real-time transcription while recording (for long recordings)">
                    Live transcription
                  </span>
                </label>

                {/* Render appropriate mic button based on mode */}
                {useStreamingMode ? (
                  <StreamingMicButton
                    parentId={parentId}
                    privacyLevel={privacyLevel}
                    aiUsage={aiUsage}
                    onTranscriptUpdate={(transcript) => setContent(transcript)}
                    onComplete={(data) => {
                      // Don't navigate immediately - let user review/edit the transcript first
                      setLoading(false);
                      setContent(data.content);
                      setStreamingNodeId(data.nodeId);
                      // Note: The node already exists with this content on the server
                      // User can now edit and click "Save" to finalize
                    }}
                    onError={(err) => {
                      setError(err.message || 'Streaming transcription failed');
                      setLoading(false);
                    }}
                    disabled={loading || uploadedFile}
                  />
                ) : (
                  <MicButton
                    status={recStatus}
                    mediaUrl={mediaUrl}
                    duration={recDuration}
                    startRecording={startRecording}
                    stopRecording={stopRecording}
                    resetRecording={resetRecording}
                  />
                )}
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
                  disabled={recStatus === 'recording' || isStreamingRecording}
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