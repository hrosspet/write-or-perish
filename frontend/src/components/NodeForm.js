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

    const handleSubmit = async (event) => {
      event && event.preventDefault();
      // Validate: require content or audio
      if (!editMode && mediaBlob) {
        // Submit audio recording
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
        } else if (mediaBlob) {
          // Create a new audio node via multipart/form-data
          const formData = new FormData();
          // Append audio file
          formData.append('audio_file', new File([mediaBlob], 'recording.webm', { type: mediaBlob.type }));
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
      isDirty: () => content.trim().length > 0,
    }));

    return (
      <form onSubmit={handleSubmit}>
        {/* Text entry */}
        <textarea
          value={content}
          onChange={(e) => {
            setContent(e.target.value);
            // Discard recording if user types
            if (!editMode && recStatus !== 'idle') {
              resetRecording();
            }
          }}
          rows={20}
          style={{ width: "100%" }}
          placeholder="Write your thoughts here..."
          disabled={!editMode && recStatus === 'recording'}
        />
        {error && <div style={{ color: "red" }}>{error}</div>}
        {!hideSubmit && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <button type="submit" disabled={loading}>
              {loading ? "Submitting..." : "Submit"}
            </button>
            {!editMode && (
              <MicButton
                status={recStatus}
                mediaUrl={mediaUrl}
                duration={recDuration}
                startRecording={startRecording}
                stopRecording={stopRecording}
                resetRecording={resetRecording}
              />
            )}
          </div>
        )}
      </form>
    );
  }
);

export default NodeForm;