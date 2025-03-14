import React, { useState } from "react";
import api from "../api";

function NodeForm({ parentId = null, onSuccess = () => {} }) {
  const [content, setContent] = useState("");
  const [error, setError] = useState("");

  const handleSubmit = (event) => {
    event.preventDefault();
    if (!content) {
      setError("Content is required.");
      return;
    }
    api
      .post("/nodes/", { content, parent_id: parentId })
      .then((response) => {
        setContent("");
        setError("");
        onSuccess(response.data);
      })
      .catch((err) => {
        console.error(err);
        setError("Error creating node.");
      });
  };

  return (
    <form onSubmit={handleSubmit} style={{ margin: "10px 0" }}>
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        rows={4}
        style={{ width: "100%" }}
        placeholder="Write your thoughts here..."
      />
      {error && <div style={{ color: "red" }}>{error}</div>}
      <button type="submit">Submit</button>
    </form>
  );
}

export default NodeForm;