# Open Source Strategy

**Date:** April 7, 2026
**Status:** No decision needed yet — revisit at ~100 users or before seeking investment
**Current state:** Fully open source on GitHub

---

## The Question

Should Loore stay open source? The repo currently includes everything: application code, system prompts, workflow logic, artifact management, and deployment configuration.

---

## Arguments for Closing the Source

### Prompt/workflow IP exposure
The carefully tuned system prompts, artifact management logic, and workflow design are the real competitive advantage — not the application code. All of this is currently visible in the repo. A funded team could read the repo, understand the approach, and build a polished competitor in weeks.

### Security
Easier to find specific vulnerabilities (auth, encryption implementation) when the source is available. Though "security through obscurity" is generally considered a weak defense on its own.

### Feature 1 is easy to clone
At the current stage (only Feature 1 shipped), there's no network effect or switching cost to protect against clones. Once Features 2-4 (social, marketplace) are live, the network effect becomes the moat and cloning matters less.

---

## Arguments for Staying Open Source

### Trust — the strongest argument
Loore handles the most intimate data people have: inner thoughts, reflections, relationships, personal struggles. "Trust us, it's encrypted" is weaker than "here's exactly what we do with your data, verify it yourself." This aligns with Karpathy's emphasis on data being "yours, explicit, inspectable." For a product in this category, transparency is a feature.

### The moat isn't the code
The real competitive advantages are: accumulated expertise about how to make LLMs work well for personal reflection (lives in the founder's head, not just the repo), user relationships and feedback loops, and eventually network effects from Features 2-4. Code is the least defensible part of any startup.

### Who's actually going to clone it?
The number of people who would deploy and operate their own Loore is tiny. Well-funded competitors will build their own thing regardless — they don't need to copy the source code. Karpathy literally told millions of people to build in this space.

### Early-stage credibility
Open source builds trust with technical early adopters, which is exactly the current alpha audience.

---

## Middle Ground Options

### Open core (recommended direction)
Keep the platform open source but extract system prompts and workflow configurations into a private layer. The code is transparent (trust), but the specific "secret sauce" of prompting and LLM orchestration isn't copyable from the repo. This is how many successful open source companies operate.

### Restrictive license (e.g. BSL)
Switch from a permissive license to Business Source License or AGPL. Anyone can inspect the code (trust), but can't legally run a competing commercial service from it. Used by Sentry, Grafana, and others. Gets trust without giving away the business.

### Delayed open source
Keep current development private, open source older versions with a 3-6 month delay. Trust benefit without real-time competitive exposure.

---

## Current Assessment

The cloning risk is real but probably overstated. The people who could clone Loore are going to build in this space anyway. The advantage is being months ahead with real users and hard-won product knowledge — that compounds with time and doesn't depend on source code secrecy.

The trust angle is underrated. Being able to point at the source code and say "look, here's exactly what happens with your data" is powerful, especially contrasted with ChatGPT's opaque "memories" feature.

**Tentative direction:** Open core with a restrictive license (like BSL). Code is inspectable, prompts/workflows are private, no one can legally run a competing service. Trust without giving away the playbook.

**When to decide:** Not urgent with 3 users. Becomes more important at ~100 users, when seeking investment, or if a competitor appears to be cloning the approach.
