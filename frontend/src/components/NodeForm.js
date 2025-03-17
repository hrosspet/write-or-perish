import React, { useState, forwardRef, useImperativeHandle } from "react";
import api from "../api";

const NodeForm = forwardRef(
  (
    { parentId, onSuccess, hideSubmit, initialContent, editMode = false, nodeId },
    ref
  ) => {
    const [content, setContent] = useState(initialContent || "");
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
        let response;
        if (editMode && nodeId) {
          // Update (edit) existing node.
          response = await api.put(`/nodes/${nodeId}`, { content });
        } else {
          // Create a new (child) node.
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
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          rows={20}
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
  }
);

export default NodeForm;