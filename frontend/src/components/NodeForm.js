import React, { useState, forwardRef, useImperativeHandle } from "react";
import { useMediaRecorder } from "../hooks/useMediaRecorder";
import MicButton from "./MicButton";
import api from "../api";

const NodeForm = forwardRef(
  (
    { parentId, onSuccess, hideSubmit, initialContent, editMode = false, nodeId },
    ref
  ) => {
    const [content, setContent] = useState(initialContent || "");
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(false);
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

    const handleFileSelect = (event) => {
      const file = event.target.files[0];
      if (!file) return;

      // Validate file size (100 MB)
      const maxSize = 100 * 1024 * 1024;
      if (file.size > maxSize) {
        setError("File size must be under 100 MB");
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
      try {
        let response;
        if (editMode && nodeId) {
          // Update (edit) existing node (text only)
          response = await api.put(`/nodes/${nodeId}`, { content });
        } else if (mediaBlob || uploadedFile) {
          // Create a new audio node via multipart/form-data
          const formData = new FormData();
          // Append audio file (either recorded or uploaded)
          if (uploadedFile) {
            formData.append('audio_file', uploadedFile);
          } else {
            formData.append('audio_file', new File([mediaBlob], 'recording.webm', { type: mediaBlob.type }));
          }
          if (parentId) formData.append('parent_id', parentId);
          response = await api.post("/nodes/", formData, {
            headers: { 'Content-Type': 'multipart/form-data' }
          });
        } else {
          // Create a new text node
          response = await api.post("/nodes/", { content, parent_id: parentId });
        }
        onSuccess(response.data);
      } catch (err) {
        console.error("Error in NodeForm:", err);
        setError("Error submitting form.");
      }
      setLoading(false);
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

        {!hideSubmit && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '8px' }}>
            <button type="submit" disabled={loading}>
              {loading ? "Submitting..." : "Submit"}
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