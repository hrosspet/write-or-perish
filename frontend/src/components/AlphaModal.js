import React, { useState } from 'react';
import api from "../api";

function AlphaModal({ user, onClose, onUpdate }) {
  const [email, setEmail] = useState(user.email || "");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  
  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      // Update the user's email via the same update endpoint.
      const response = await api.put("/dashboard/user", { email });
      setLoading(false);
      onUpdate(response.data.user);
      onClose();
    } catch (err) {
      console.error(err);
      setError("Error updating email. Please try again.");
      setLoading(false);
    }
  };
  
  const modalStyle = {
    position: "fixed",
    top: 0, left: 0, right: 0, bottom: 0,
    backgroundColor: "rgba(0,0,0,0.8)",
    display:"flex",
    alignItems:"center",
    justifyContent:"center",
    zIndex: 2000
  };
  
  const contentStyle = {
    backgroundColor: "#1e1e1e",
    padding: "20px",
    borderRadius: "8px",
    width: "400px",
    color: "#e0e0e0"
  };
  
  // If the user already submitted an email, show a thank-you message.
  if (user.email && user.email.trim() !== "") {
    return (
      <div style={modalStyle}>
        <div style={contentStyle}>
          <h2>Alpha Release – Thank You!</h2>
          <p>
            Thank you for providing your email. You've been added to our waiting list.
            We will contact you at <strong>{user.email}</strong> once your account is approved.
          </p>
          <button onClick={onClose}>Close</button>
        </div>
      </div>
    );
  }
  
  // Otherwise, show the email submission form.
  return (
    <div style={modalStyle}>
      <div style={contentStyle}>
        <h2>Alpha Release – Waiting List</h2>
        <p>
          Thank you for signing up for our limited alpha release! You’ve been added to the waiting list.
          If you’d like to be notified when your account is approved, please provide your email below.
        </p>
        <form onSubmit={handleSubmit}>
          <div>
            <label htmlFor="email">Email (optional):</label>
            <input 
              type="email" 
              id="email" 
              value={email} 
              onChange={(e) => setEmail(e.target.value)}
              style={{ width: "100%", padding: "8px", marginTop: "8px" }} 
            />
          </div>
          {error && <div style={{ color: "red", marginTop: "10px" }}>{error}</div>}
          <div style={{ marginTop: "20px" }}>
            <button type="submit" disabled={loading}>
              {loading ? "Submitting..." : "Submit"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
  
export default AlphaModal;