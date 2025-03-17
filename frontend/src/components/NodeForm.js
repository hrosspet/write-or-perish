import React, { useState, forwardRef, useImperativeHandle } from "react";

const NodeForm = forwardRef(({ parentId, onSuccess, hideSubmit }, ref) => {
  const [content, setContent] = useState("");
  const [error, setError] = useState("");

  const handleSubmit = (event) => {
    event && event.preventDefault();
    if (!content.trim()) {
      setError("Content is required.");
      return;
    }
    // Place your API submission logic here...
    // For example:
    // api.post("/nodes/", { content, parent_id: parentId }).then(response => onSuccess(response.data)).catch(...)

    // For demonstration, we'll simply call onSuccess:
    onSuccess();
  };

  // Expose functions to the parent component via ref:
  useImperativeHandle(ref, () => ({
    submit: () => {
      handleSubmit({ preventDefault: () => {} });
    },
    isDirty: () => content.trim().length > 0
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
      {/* Render the internal submit button unless we're suppressing it */}
      {!hideSubmit && (
        <button type="submit">Submit</button>
      )}
    </form>
  );
});

export default NodeForm;