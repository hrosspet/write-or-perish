# Loore Positioning: Personal AI Knowledge Base

**Date:** April 6, 2026
**Context:** Andrej Karpathy's "LLM Knowledge Bases" tweet (April 2, 2026, 17M+ views) and Farza's "Farzapedia" (1.5M views) validated the concept of LLM-maintained personal knowledge bases. Loore has been building this for personal life domains since late 2025.

**Purpose of this document:** Strategic positioning reference. Captures how Loore relates to the emerging "LLM knowledge base" category, what differentiates it, and how to talk about it. Not for publication — for informing landing page copy, pitches, and future content.

---

## The Concept Karpathy Described

Raw data from various sources is collected, then compiled by an LLM into a structured wiki (markdown files), then operated on by the LLM to do Q&A, linting, and incremental enhancement. The human curates sources and asks questions; the LLM does all the maintenance and bookkeeping. Key properties he and Farza emphasize:

1. **Explicit** — The knowledge artifact is navigable, you can see what the AI knows
2. **Yours** — Data is on your computer, not locked in a provider's system
3. **File over app** — Universal formats (markdown, images), interoperable with any tool
4. **BYOAI** — Plug in whatever AI you want, including finetuned models

His approach is power-user, research-oriented, and manual-setup (Obsidian + CLI + scripts). He explicitly says: *"I think there is room here for an incredible new product instead of a hacky collection of scripts."*

---

## What Loore Already Is

Karpathy described a **general idea** — LLM-maintained knowledge bases — in a form that's currently accessible only to power users (file systems, CLI, Obsidian, agent configuration). Loore is a **specific instance** of that idea — a personal knowledge base for your life — built to be effortless and useful for mainstream users, not just power users. We picked one domain and spent months making it work great.

Mapping the concepts:

| Karpathy's LLM Wiki | Loore |
|---|---|
| Raw data ingested into `raw/` directory | Journal entries via text and voice |
| LLM "compiles" a wiki from raw sources | AI generates and maintains a collection of artifacts: user profile, todo list, AI interaction preferences — with intentions, predictions, and arbitrary user-defined artifacts planned |
| Incremental compilation as new sources arrive | Automatic artifact updates after new entries, hierarchical context freshness |
| Q&A against the wiki | Agentic Voice workflow — AI converses with full context of who you are |
| LLM maintains the wiki, you rarely touch it | AI maintains your artifacts, proposes todos, manages context — you just talk |
| Index files + summaries for navigation | Context artifacts, recent summaries, artifact sections |
| "Linting" for consistency and gaps | Profile integration step, freshness checks across artifacts |
| Filing query outputs back into the wiki | Voice conversations generate todos, insights, issues — all filed automatically into the appropriate artifacts |
| Obsidian as the viewing "IDE" | Loore web app — works on any device, including mobile |
| CLI tools for search and processing | Built-in search (Cmd+K), planned RAG / semantic search |

---

## Where Loore Goes Beyond

### 1. Zero friction — you just talk

Karpathy's approach requires managing directories, running CLI tools, configuring Obsidian plugins. Loore's approach: open the app and start talking. Voice input is the lowest-friction way to capture thoughts. The AI handles transcription, analysis, profile updates, and todo extraction. There's nothing to set up, no files to organize, no commands to run.

### 2. Reflection-first, not research-first

Karpathy's wiki is built for research — ingesting papers, articles, repos, and building structured knowledge. Loore is built for personal reflection and life management — you talk about what's on your mind, and the AI helps you process it, extract actionable next steps, and maintain a growing picture of who you are and what matters to you. The input is your life, not a collection of documents. (Future features will connect Loore to external sources and let it act outward — posting to channels, surfacing relevant content — but today the focus is on the personal knowledge base and reflection loop.)

### 3. Privacy by design, not by location

Karpathy's privacy model is "files on your laptop." That feels more private, but in practice almost everyone is sending their data to Claude/OpenAI APIs anyway — so the trust question is similar, just less explicit. Loore encrypts all content and audio at rest (GCP KMS envelope encryption), with granular privacy levels and explicit AI usage controls (none/chat/train). It's not end-to-end encrypted — the backend decrypts to serve content and run AI — so it's still a trust-based model. But it's a deliberate tradeoff: cloud convenience, multi-device access, and AI-powered features in exchange for trusting the service with your data. For most people, that's the right balance.

### 4. Works for everyone, not just power users

Karpathy's approach is by power users, for power users. Loore is built by a power user to bring peak AI capabilities to people who don't even know LLMs have them. Under the hood, Loore encodes hard-won knowledge about how to get the most out of current LLMs: what to track, what artifacts to maintain and when to update them, how to structure prompts and workflows, what interactions to encourage and which to discourage. The user doesn't see any of this — they just talk or type, and get capabilities (persistent memory, agentic tool use, incremental knowledge compilation, contextual awareness) that people like Karpathy can achieve manually but only with significant setup and ongoing effort. The ceiling is just as high as Karpathy's DIY approach, but it's effortless — and the floor is radically lower.

### 5. The four-feature vision (where it gets bigger)

What Karpathy describes is single-player knowledge management. Loore's roadmap extends far beyond:

- **Feature 1 (now):** Effortless Journaling — the personal AI knowledge base, voice-first
- **Feature 2 (partially working / planned):** Download — AI already surfaces relevant content from your own archive during conversations; planned expansion to external sources (bookmarks, social media, articles)
- **Feature 3 (planned):** Upload — AI helps you share insights to appropriate audiences and channels (the knowledge base generates outward value)
- **Feature 4 (planned):** Intention Market — serendipitous connection through complementary needs and offerings across users (knowledge bases interact)

The single-player knowledge base is the foundation. The vision is a distributed intelligence network where private reflection fuels public contribution, and expressed intentions enable collaboration. None of this is possible with files on a laptop.

---

## What Karpathy's Approach Does Better (Honestly)

- **Full data ownership** — Files on your machine, no trust in a third party. Some users will always prefer this.
- **BYOAI** — Use any model, switch freely, even finetune. Loore currently offers OpenAI + Anthropic models, which covers what the vast majority of users want, but doesn't support bringing your own model or local inference.
- **Research use case** — For deep technical research with papers, repos, and datasets, the structured wiki + Obsidian graph view is excellent. Loore is optimized for personal/professional life, not research knowledge management.
- **File portability** — Markdown files can be opened in any editor, versioned with git, processed with Unix tools. Loore's data lives in a database (though all system prompts and artifacts injected into context are fully viewable and editable by the user).

These are genuine trade-offs, not gaps to close. Loore's bet is that most people want the product, not the toolkit.

---

## How to Talk About It

The framing should center on what the user gains (self-understanding, intentionality, clarity, overview of their own life) — not on what the AI does ("gets to know you," which feels dystopian). The AI is the mechanism, not the headline.

### Core framing
Loore helps you reflect on your life and turn that reflection into intentional action. You talk or write about whatever's on your mind, and over time you build up a living, structured picture of your own life — your priorities, your open questions, your patterns. It's like journaling, but the journal works for you.

### For the ML engineer friend (Karpathy context)
"You saw Karpathy's LLM knowledge base tweets? I've been building that as a product, but for personal life instead of research. Voice-first, artifacts maintained automatically, encrypted. Want to try the alpha?"

### For non-technical friends
"It's a place where I talk through whatever's on my mind — work, relationships, plans — and it helps me stay on top of it all. It keeps track of my todos, helps me think things through, and over time I've built up this really useful overview of my own life. Like a journal that actually does something."

### For a landing page (future)
Needs more thought — but direction should be something like: "Reflect. Clarify. Act." or "Turn your thoughts into a living map of your life." Avoid "AI that knows you" as the lead. Lead with what the user experiences, not what the system does.

### For a longer-form piece (blog/essay, future)
Frame around the insight that Karpathy's tweet validated: passive chat with LLMs is a waste of their potential. The real value is when your conversations compound over time into something structured and useful. Position Loore as what that looks like when it's a product for your life — reflection that builds on itself, not conversations that disappear.

---

## Key Quotes to Reference

Karpathy: *"I think there is room here for an incredible new product instead of a hacky collection of scripts."*

Karpathy: *"Your data is yours... You're in control of your information."*

Karpathy: *"The LLM writes and maintains all of the data of the wiki, I rarely touch it directly."*

Karpathy (on Farzapedia): *"I really like this approach to personalization in a number of ways, compared to 'status quo' of an AI that allegedly gets better the more you use it or something."*

Farza: *"It's like this super genius librarian for your brain that's always filing stuff for you perfectly."*

---

## What NOT to Do

- **Don't position as "we built what Karpathy described."** Loore predates the tweet and has a bigger vision. Position as being in the same space, validated by the same insight.
- **Don't chase the research/power-user audience.** That's Karpathy's crowd and they'll build their own. Loore's audience is people who want the benefits without the setup.
- **Don't sell Features 2-4 yet.** They're not built. Sell Feature 1 as a complete, compelling product. Mention the vision only when asked "where is this going?"
- **Don't rush a public launch.** The wave creates urgency, but a bad first impression at scale is worse than a late entrance. Use the wave to fill a waitlist, not to onboard thousands.
