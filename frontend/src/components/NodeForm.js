import React, { useState, forwardRef, useImperativeHandle } from "react";
import api from "../api";

const NodeForm = forwardRef(({ parentId, onSuccess, hideSubmit }, ref) => {
  const [content, setContent] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (event) => {
    event && event.preventDefault();
    if (!content.trim()) {
      setError("Content is required.");
      return;
    }
    setLoading(true);
    try {
      // Adjust the endpoint and payload as needed.
      const response = await api.post("/nodes/", { content, parent_id: parentId });
      // Optionally clear content after a successful submission.
      setContent("");
      // Call onSuccess with the response data (new node info)
      onSuccess(response.data);
    } catch (err) {
      console.error("Error creating node:", err);
      setError("Error creating node.");
    }
    setLoading(false);
  };

  // Expose functions to the parent via ref if needed.
  useImperativeHandle(ref, () => ({
    submit: () => handleSubmit({ preventDefault: () => {} }),
    isDirty: () => content.trim().length > 0,
  }));

  return (
    <form onSubmit={handleSubmit}>
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        rows={4}
        style={{ width: "100%" }}
        placeholder="Write your thoughts here..."
      />
      {error && <div style={{ color: "red" }}>{error}</div>}
      {!hideSubmit && (
        <button type="submit" disabled={loading}>
          {loading ? "Submitting..." : "Submit"}
        </button>
      )}
    </form>
  );
});

export default NodeForm;