import React from "react";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function LandingPage() {
  return (
    <div
      style={{
        padding: "20px",
        maxWidth: "750px",       // Constrain the width so that lines are not too long
        margin: "0 auto",
        lineHeight: "1.8",
        fontFamily: "sans-serif",
        fontSize: "18px",
      }}
    >
      <h1>Welcome to Write or Perish</h1>
      <p>
        Write or Perish is a digital journal where people can record and share their personal
        thoughts, stories, and emotions—whether through diaries, biographies, or free-form musings. Its purpose
        is to collect and preserve the rich tapestry of human experience so that, in the future, these writings can be
        used to train AI models. In doing so, the app serves as an archive of humanity, ensuring that even if AI
        eventually plays a dominant role in our lives, it will be deeply infused with the authentic voices, passions,
        and insights of human existence.
      </p>
      <p>
        All entries are public and may be used for training future AI models.
      </p>
      <h3>How It Works</h3>
      <p>
        Create text entries, link your entries to those of others, and form a network of interconnected
        stories. Finally, you have the option to generate an LLM response as a reply to your narrative. The richer
        your texts and interactions with the LLM will be, the higher the chance that OpenAI will include this data in its
        next training.
      </p>
      <h3>Our goal</h3>
      <p>
        Every day, OpenAI supplies Write or Perish with <strong>1,000,000 free tokens</strong> for GPT‑4.5 until the <strong>end of April</strong>. Our community’s common goal
        is to use up all these tokens—filling them with our personal values, aspirations, and insights.
      </p>
      <p>
        Your contributions not only positively shape your own personal journey but also <strong>directly influence the future of AI</strong>.
      </p>
      <p>
        <a href={`${backendUrl}/auth/login`}>Login with Twitter</a> to begin your journey!
      </p>
    </div>
  );
}

export default LandingPage;