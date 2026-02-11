import React, { useState, forwardRef, useImperativeHandle, useEffect, useCallback } from "react";
import { useAsyncTaskPolling } from "../hooks/useAsyncTaskPolling";
import { useDraft } from "../hooks/useDraft";
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

    // Audio file upload state
    const [uploadedFile, setUploadedFile] = useState(null);
    const fileInputRef = React.useRef(null);

    // Track streaming session ID when transcription completes (for "review before save" flow)
    // With draft-based streaming, no node exists until user explicitly saves
    const [streamingSessionId, setStreamingSessionId] = useState(null);
    // Track content that existed before streaming started (to append new transcript to it)
    const preStreamingContentRef = React.useRef("");

    // Track if streaming is in progress (set by StreamingMicButton callbacks)
    const [isStreamingRecording, setIsStreamingRecording] = useState(false);

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
      // Clear text content
      setContent("");
    };

    const handleSubmit = async (event) => {
      event && event.preventDefault();
      // Validate: require content or audio
      if (!editMode && uploadedFile) {
        // Submit uploaded audio file
      } else if (!content.trim()) {
        setError("Content is required.");
        return;
      }
      setLoading(true);
      setError("");
      try {
        let response;

        // Handle streaming transcription completion - draft exists, create node from it
        if (streamingSessionId) {
          // Save the streaming draft as a node with any edits the user made
          const response = await api.post(`/drafts/streaming/${streamingSessionId}/save-as-node`, {
            content
          });
          deleteDraft();
          setHasDraft(false);
          setStreamingSessionId(null);
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
        } else if (uploadedFile) {
          // Upload audio file
          const fileToUpload = uploadedFile;

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
      isDirty: () => content.trim().length > 0 || uploadedFile,
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
            // Discard uploaded file if user types
            if (!editMode && uploadedFile) {
              setUploadedFile(null);
            }
          }}
          rows={20}
          style={{
            width: "100%",
            backgroundColor: "var(--bg-input)",
            border: "1px solid var(--border)",
            borderRadius: "8px",
            padding: "14px 16px",
            fontFamily: "var(--sans)",
            fontSize: "1.05rem",
            fontWeight: 300,
            color: "var(--text-primary)",
            lineHeight: 1.6,
            minHeight: "250px",
            boxSizing: "border-box",
            transition: "border-color 0.3s ease",
          }}
          placeholder="What's present for you right now..."
          disabled={!editMode && uploadedFile}
        />

        {/* Privacy Settings */}
        <PrivacySelector
          privacyLevel={privacyLevel}
          aiUsage={aiUsage}
          onPrivacyChange={setPrivacyLevel}
          onAIUsageChange={setAiUsage}
          disabled={loading}
        />

        {error && <div style={{ color: "var(--accent)", fontFamily: "var(--sans)", fontSize: "0.9rem" }}>{error}</div>}

        {/* Auto-save status indicator */}
        {content.trim() && (
          <div style={{
            marginTop: '4px',
            fontSize: '0.85em',
            fontFamily: 'var(--sans)',
            fontWeight: 300,
            color: 'var(--text-muted)',
            display: 'flex',
            alignItems: 'center',
            gap: '4px'
          }}>
            {isDraftSaving ? (
              <>
                <span style={{ color: 'var(--accent)' }}>Saving...</span>
              </>
            ) : lastSaved ? (
              <>
                <span style={{ color: 'var(--text-muted)' }}>Draft saved {formatTimeAgo(lastSaved)}</span>
              </>
            ) : null}
          </div>
        )}

        {/* Display uploaded file info */}
        {uploadedFile && (
          <div style={{
            marginTop: '8px',
            padding: '8px 12px',
            backgroundColor: 'var(--bg-surface)',
            border: '1px solid var(--border)',
            borderRadius: '6px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            color: 'var(--text-secondary)',
            fontFamily: 'var(--sans)',
            fontSize: '0.85rem',
          }}>
            <span>{uploadedFile.name} ({(uploadedFile.size / 1024 / 1024).toFixed(2)} MB)</span>
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
            <button
              type="submit"
              disabled={loading}
              style={{
                borderColor: 'var(--accent)',
                color: 'var(--accent)',
              }}
            >
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
                <StreamingMicButton
                  parentId={parentId}
                  privacyLevel={privacyLevel}
                  aiUsage={aiUsage}
                  onRecordingStart={() => {
                    // Capture content before streaming starts so we can append to it
                    preStreamingContentRef.current = content;
                    setIsStreamingRecording(true);
                  }}
                  onTranscriptUpdate={(transcript) => {
                    // Append new transcript to pre-existing content
                    const prefix = preStreamingContentRef.current;
                    const separator = prefix && transcript ? '\n\n' : '';
                    setContent(prefix + separator + transcript);
                  }}
                  onComplete={(data) => {
                    // Don't navigate immediately - let user review/edit the transcript first
                    // With draft-based streaming, no node exists yet - just a draft
                    setLoading(false);
                    setIsStreamingRecording(false);
                    // Append final transcript to pre-existing content
                    const prefix = preStreamingContentRef.current;
                    const separator = prefix && data.content ? '\n\n' : '';
                    const combinedContent = prefix + separator + data.content;
                    setContent(combinedContent);
                    setStreamingSessionId(data.sessionId);
                    // Save combined content to the regular draft so it persists on reopen
                    saveDraft(combinedContent);
                    setHasDraft(true);
                  }}
                  onError={(err) => {
                    setError(err.message || 'Streaming transcription failed');
                    setLoading(false);
                    setIsStreamingRecording(false);
                  }}
                  disabled={loading || uploadedFile || aiUsage === 'none'}
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
                  disabled={isStreamingRecording}
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
