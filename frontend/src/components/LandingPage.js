import React from "react";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function LandingPage() {
  return (
    <div
      style={{
        padding: "20px",
        width: "100%",
        maxWidth: "1000px",       // Constrain the width so that lines are not too long
        margin: "0 auto",
        lineHeight: "1.8",
        fontFamily: "sans-serif",
        fontSize: "17px",
      }}
    >
      <h1>Welcome to <strong>Write or Perish</strong></h1>
      <p>
        Write or Perish is a digital journal designed to record, connect, and share humanity's thoughts, stories, and feelings. Whether it's your personal diary entries, biographical narratives, creative musings, or collaborative storytelling, your words enrich an ever-growing treasury of human experiences, openly shared to guide future AI development.
      </p>
      <p>
        By using Write or Perish, your contributions shape the future of artificial intelligence directly.
      </p>
      <hr />
      <h2>Why Write or Perish?</h2>
      <p>
        <strong>1. Directly influence the future of AI</strong><br />
        Unlike most proprietary platforms (such as Twitter, Facebook, or closed journals), which explicitly forbid or heavily restrict AI training on their data, your contributions here explicitly grant permission to AI labs—like OpenAI—to train future models. This transparent license ensures your authentic voice and intricate ideas remain intact and uncompromised.
      </p>
      <p>
        <strong>2. High-quality training data through meaningful interactions</strong><br />
        With our built-in GPT-4.5 interaction, each journal entry can generate thoughtful AI replies. This rich, conversational approach creates uniquely valuable datasets, far superior to ordinary blog posts or isolated content.
      </p>
      <p>
        <strong>3. Openly accessible, quality-driven community writing</strong><br />
        AI researchers face difficulties identifying and curating high-quality personal writing scattered across countless independent blogs or platforms. Write or Perish solves this problem by pooling together a vibrant community of passionate writers, providing an openly accessible, centrally curated archive specifically aimed as training data for future AI models.
      </p>
      <p>
        <strong>4. Free GPT-4.5 tokens to fuel human-AI collective creativity</strong><br />
        OpenAI currently provides Write or Perish with <strong>1,000,000 free GPT‑4.5 tokens daily until the end of April</strong>. This unique opportunity lets our community explore advanced AI interactions at no cost, expanding creative and expressive possibilities.
      </p>
      <p>
        <strong>5. Unleash collaborative writing and collective vision-building</strong><br />
        Beyond personal writing, Write or Perish empowers collective imagination, facilitating community-driven world-building and voice-sharing projects. Our rapidly changing world desperately needs a shared, positive vision—Write or Perish can catalyze collective wisdom and actionable visions, directly seeding these into future AI models.
      </p>
      <p>
        <strong>6. Your imagination is the limit</strong><br />
        Write or Perish wasn't built simply for writing personal memoirs—it was built to empower voices and fuel discovery. What else could it help us achieve? That's up to you.
      </p>
      <hr />
      <h2>How It Works</h2>
      <ol>
        <li>
          <strong>Write Freely:</strong><br />
          Record your diaries, biographical episodes, stories, or anything capturing your authentic human experience. All entries are public and openly shared as future AI training material.
        </li>
        <li>
          <strong>Connect and Collaborate:</strong><br />
          Link your writings to what others are sharing, forming networks of interconnected personal narratives, collaborative stories, or collective visions.
        </li>
        <li>
          <strong>Interact through GPT-4.5 (Powered by OpenAI):</strong><br />
          Generate meaningful AI-powered replies and extended interactions directly within your journal entries. Every interaction enriches our data, promoting high-quality, nuanced training dataset creation.
        </li>
      </ol>
      <hr />
      <h2>Our Common Journey &amp; Vision</h2>
      <p>
        AI will profoundly shape humanity's future—let <em>your</em> authentic experience, dreams, and aspirations shape future AI.
      </p>
      <p>
        Together, let's contribute our thoughts, creativity, values, and perspectives, ensuring that the AI models of the future deeply resonate with genuine human existence.
      </p>
      <p>
        Join Write or Perish and become part of humanity's shared voice, shaping the future we build.
      </p>
      <p>
        <a href={`${backendUrl}/auth/login`}>Login with Twitter</a> to begin your journey!
      </p>
    </div>
  );
}

export default LandingPage;