# Accelerated Development Roadmap

**Date:** January 21, 2026
**Purpose:** Optimized task ordering for rapid alpha release, development automation, and parallel AI agent development

**Development Model:** Single human (you) + multiple AI agents working on parallel branches

---

## Goals (in priority order)

1. **Alpha Release ASAP** - Current functionality + privacy features for friends
2. **Development Automation** - Reduce friction, enable fast iteration
3. **Parallel Development** - Multiple agents/branches working independently

---

## Current State Assessment

### Already Done
- **Core journaling** (Feature 1) - Production-ready
- **Privacy infrastructure** (Phase -1) - Two-column privacy system implemented, privacy filtering enforced on feed/dashboard/node detail, recursive human owner resolution for LLM access control
- **GCP KMS encryption** (A.4) - Envelope encryption for all content + audio at rest
- **API key separation for chat vs train** (#39) - ✅ Complete
- **Magic link email authentication** (#54) - Passwordless login via email, replacing Twitter-only OAuth
- **Dedicated login page** (#53) - Standalone login page with redirect flow
- **Rebrand to Loore** (A.5) - ✅ Complete: domain (loore.org), all branding, icons, emails, landing page
- **Full UI redesign** (#56) - Warm literary design system across all pages, dark theme, responsive mobile
- **User plan tiers** - Standardized (free/alpha/pro) with admin dashboard management and migration scripts
- **Multi-model LLM updates** - Claude Opus 4.6 support, centralized model config
- **CI/CD pipeline** - GitHub Actions runs tests, deploys to production, path-based job skipping
- **Docker setup** - Files exist but unverified/incomplete
- **Backend tests** - Privacy-related tests only (~3 test files)
- **Frontend tests** - Placeholder only
- **Terms of service** - Acceptance tracking with admin reset on account deactivation

### Alpha Blockers (Critical)
- ✅ All alpha blockers resolved (A.1-A.5 complete)

### Development Velocity Blockers
- Docker setup unverified (may not work out of box)
- Minimal test coverage (risky for parallel development)
- No API documentation (frontend/backend disconnect risk)

---

## Phase A: Alpha Release (1-2 weeks)

**Goal:** Ship current functionality to friends with privacy guarantees.

### A.1 API Key Separation ✅ COMPLETE

**What:** Use different OpenAI/Anthropic API keys based on `ai_usage` setting.

**Status:** Implemented. Separate API keys now used for chat vs train operations.

### A.2 Basic Monitoring (Alpha Requirement)

**What:** Minimal monitoring to catch issues before users report them.

**Why:** Alpha users are friends - you don't want them debugging your app for you.

**Implementation:**
- Sentry integration (backend + frontend) - ~1 day
- Health check endpoint (`/health`) - ~1 hour
- Basic request logging with request IDs - ~1 day

**Scope:** Backend + minimal frontend, configuration-heavy.

### A.3 Alpha Documentation

**What:** User-facing docs for alpha testers.

**Why:** Friends need to understand privacy guarantees and basic usage.

**Implementation:**
- Privacy promise documentation (what's encrypted, what's stored, what AI can see)
- Basic user guide (how to create nodes, set privacy levels)
- Known limitations list

**Scope:** Documentation only, no code.

### A.4 Application-Level Encryption with GCP KMS ✅ COMPLETE

**What:** Encrypt sensitive user data at rest using Google Cloud KMS.

**Why:** Defense in depth - even if database is compromised, encrypted fields remain protected. Critical for user trust and privacy guarantees.

**Status:** Implemented. Key technical decisions and details:
- **Envelope encryption (v2):** Random AES-256 DEK per record, KMS only wraps/unwraps the 32-byte DEK (bypasses 64KB KMS plaintext limit)
- **Format:** `ENC:v2:<base64-wrapped-dek>:<base64(nonce + ciphertext + tag)>`
- **REST transport:** KMS client uses REST instead of gRPC to avoid gevent monkey-patching deadlocks
- **DEK cache:** In-memory LRU cache (4096 entries) avoids repeated KMS calls for already-decrypted content
- **Migration script:** `scripts/encrypt_existing_content.py` encrypts all existing nodes, versions, drafts, profiles, and transcript chunks
- **Audio file encryption:** All audio files (uploads, streaming chunks, TTS output) encrypted at rest via `encrypt_file()`. DB URLs stored without `.enc` — media route handles `.enc` fallback transparently. Transcription tasks decrypt to temp files before sending to OpenAI.
- **Paginated Feed/Dashboard:** Infinite scroll (20 nodes per page) to avoid decrypting too many nodes at once
- **VM Workload Identity:** Service account attached to VM, no JSON key files needed
- **Setup docs:** `docs/GCP-KMS-SETUP.md` covers full GCP configuration
- **Dependencies added:** `google-cloud-kms>=2.0.0`, `cryptography>=41.0.0`

### A.5 Rebrand to Loore ✅ COMPLETE

**What:** Rename the application from "Write or Perish" to "Loore".

**Status:** Implemented. Key changes:
- **Domain:** loore.org (production)
- **Frontend:** All pages rebranded, landing page rewritten with new Loore vision and copy
- **Icons:** Redesigned app icons with new branding
- **Navbar:** Enlarged logo text with waveform icon
- **Email:** Sign-in email redesigned to match Loore branding
- **Login page:** Rebranded and redesigned

**Alpha Deliverable:** ✅ Shipped to friends with privacy guarantees + monitoring + encryption + Loore branding.

### A.6 Full UI Redesign ✅ COMPLETE (Unplanned)

**What:** Complete redesign of all app pages to a warm literary design system.

**Status:** Implemented (#56 + follow-up commits). Key changes:
- Dark theme with warm literary aesthetic across all pages
- Login page redesigned with branded sign-in email
- Contrast and readability improvements throughout
- Privacy selectors, write modal, and feed card spacing refined
- Browser autofill styling overridden to preserve dark theme
- Typography: switched to Outfit (sans-serif) for body text readability
- Font weight increases for write textarea and card titles
- Responsive: mobile button wrapping, modal height constraints, wider desktop modals (780px→1170px)

### A.7 Magic Link Email Authentication ✅ COMPLETE (Unplanned)

**What:** Passwordless authentication via email magic links (#54).

**Status:** Implemented. Replaces Twitter-only OAuth as primary auth method. Includes dedicated login page with redirect flow (#53). Partial email infrastructure now in place (basic SMTP).

### A.8 User Plan Tier Standardization ✅ COMPLETE (Unplanned)

**What:** Standardized plan tiers (free/alpha/pro) with admin management.

**Status:** Implemented. All existing users migrated to alpha plan. Admin dashboard has plan dropdown. Feature gating via `User.has_voice_mode` and plan-based decorators.

---

## Phase B: Development Automation (1-2 weeks, can partially overlap with A)

**Goal:** Make development fast, safe, and easy to parallelize.

### B.1 Docker Verification and Fixes

**What:** Verify Docker setup works end-to-end, fix any issues.

**Why first in this phase:** Docker is the foundation for consistent development environments. If it doesn't work, every developer wastes time on environment issues.

**Current state:** Files exist (`docker-compose.yml`, Dockerfiles, Makefile) but appear unverified.

**Tasks:**
1. Run `make dev` and verify all services start
2. Verify hot reload works (backend + frontend)
3. Test `make test` actually runs tests
4. Verify database migrations work in Docker
5. Document any required `.env` configuration
6. Fix any issues found

**Scope:** DevOps configuration.

**Why this enables parallel development:** Once Docker works, any AI agent session can reference the same environment setup. More importantly, YOU can quickly spin up/tear down test environments, and the CI pipeline has a consistent reference.

### B.2 Backend Test Coverage Expansion

**What:** Add tests for core functionality beyond privacy.

**Why:** Tests are the safety net for parallel development. Without them, merging branches is risky.

**Priority test areas:**
1. Node CRUD operations (create, read, update, delete)
2. LLM provider abstraction (mock API calls)
3. Celery task execution (mock async processing)
4. Audio upload/transcription flow (mock Whisper)
5. Authentication/authorization

**Why this order:**
- Node CRUD is the core - every feature depends on it
- LLM provider is called by multiple features
- Celery tasks are where most async bugs hide
- Audio is complex and error-prone
- Auth is already partially tested via privacy tests

**Target:** 60% backend coverage before parallelizing.

**Scope:** Backend only. Can run in parallel with Docker verification.

### B.3 Frontend Test Basics

**What:** Add tests for critical UI components.

**Why:** Frontend changes are where merge conflicts are most visible (broken UI).

**Priority components:**
1. Node creation form (all input types)
2. Privacy selector component
3. Node tree navigation
4. Audio player
5. LLM response display

**Target:** 40% frontend coverage on critical paths.

**Scope:** Frontend only. Can run in parallel with B.2.

### B.4 API Documentation (Swagger/OpenAPI) - OPTIONAL

**What:** Auto-generate API docs from Flask routes.

**For AI agents:** This is LOW PRIORITY. AI agents can read the code directly, which is often faster than reading docs. The code is the source of truth anyway.

**When it IS useful:**
- If YOU want a quick reference without reading code
- If docs are significantly shorter than the code they describe
- For external API consumers (not applicable for alpha)

**Recommendation:** SKIP for now. Revisit only if you find yourself constantly explaining endpoints to agents or forgetting the API structure yourself. The agents can grep for route definitions.

**If you do want it later:**
- Add `flask-smorest` or `flasgger`
- Document existing endpoints
- ~1 day of work

---

## Phase C: Shared Infrastructure (2-3 weeks)

**Goal:** Build components that multiple features need BEFORE parallelizing feature development.

**Critical insight:** Features 2, 3, and 4 all need certain infrastructure. Building this ONCE prevents duplicate work and merge conflicts later.

### C.1 Embedding Service (Required by: Download, Intention Market)

**What:** Centralized service for generating and storing text embeddings.

**Why before features:** Both Download (semantic search) and Intention Market (matching) need embeddings. Building separately would mean duplicate code and potential inconsistencies.

**Implementation:**
- pgvector extension in PostgreSQL
- Embedding generation Celery task
- Embedding column on Node model
- Basic semantic search API

**Scope:** Backend (database + API + Celery task). No frontend yet.

**Dependencies:** None (can start immediately after Phase B).

### C.2 OAuth Framework (Required by: Upload external platforms)

**What:** Reusable OAuth client for third-party integrations.

**Why before Upload:** Upload needs Twitter, LinkedIn, Substack OAuth. Building a reusable framework prevents duplicate code per platform.

**Implementation:**
- OAuth token storage model
- OAuth flow endpoints (callback handling)
- Token refresh utilities
- Error handling and revocation

**Scope:** Backend only. Groundwork for Upload phase.

**Dependencies:** None (can run parallel with C.1).

### C.3 WebSocket Infrastructure (Required by: Intention Market, real-time features)

**What:** Socket.IO setup for real-time notifications.

**Why before features:** Intention Market needs real-time match notifications. Building this later would require retrofitting.

**Implementation:**
- Socket.IO server setup
- Authentication for WebSocket connections
- Basic event emission framework
- Frontend WebSocket client

**Scope:** Backend + Frontend. Can defer to later if not needed for alpha.

**Dependencies:** Can run parallel with C.1 and C.2, but LOWER priority.

---

## Phase D: Parallel Feature Development

**Goal:** Run multiple agents/branches simultaneously on independent features.

### Parallelization Strategy

After Phase C, three features can be developed in parallel because they touch MOSTLY INDEPENDENT code:

| Feature | Primary Files | Shared Dependencies |
|---------|--------------|---------------------|
| Download (MemeOS) | New: `bookmark.py`, `recommendation.py`, `RecommendationSidebar.tsx` | Embedding service (C.1) |
| Upload (Sharing) | New: `share.py`, `channels/`, `ShareSuggestion.tsx` | OAuth framework (C.2) |
| Intention Market | New: `intention.py`, `match.py`, `Market*.tsx` | Embedding service (C.1), WebSocket (C.3) |

**Why these can run in parallel:**
- Each creates NEW models/files rather than modifying existing core
- They share infrastructure (embeddings, OAuth) but that's built in Phase C
- Frontend components are in separate directories
- API endpoints are additive (new routes, not modifying existing)

### D.1 Download Feature (MemeOS Integration)

**Branch:** `feature/download-memeos`

**New code (low merge conflict risk):**
- `backend/models/bookmark.py` - Bookmark model for MemeOS data
- `backend/services/recommendation.py` - Recommendation engine
- `backend/api/recommendations.py` - API endpoints
- `frontend/src/components/Sidebar/RecommendationWidget.tsx`

**Modifications to existing code (potential conflicts):**
- `backend/api/nodes.py` - Add recommendation context to node responses
- `frontend/src/pages/NodeDetail.tsx` - Add sidebar component

**Merge complexity:** LOW - mostly additive code.

### D.2 Upload Feature (Broadcasting)

**Branch:** `feature/upload-sharing`

**New code (low merge conflict risk):**
- `backend/models/share.py` - Share model
- `backend/models/circle.py` - Circles model
- `backend/services/content_analysis.py` - LLM-based shareability scoring
- `backend/services/channels/` - Per-platform adapters (twitter.py, linkedin.py, etc.)
- `backend/api/shares.py` - API endpoints
- `frontend/src/components/Sharing/` - All sharing UI components

**Modifications to existing code (potential conflicts):**
- `frontend/src/pages/NodeDetail.tsx` - Add share button/suggestions

**Merge complexity:** LOW to MEDIUM - mostly additive, but NodeDetail is a conflict point.

### D.3 Intention Market Feature

**Branch:** `feature/intention-market`

**New code (low merge conflict risk):**
- `backend/models/intention.py` - Intention model
- `backend/models/match.py` - Match model
- `backend/models/connection.py` - Connection model
- `backend/services/matching.py` - Matching algorithm
- `backend/api/intentions.py` - API endpoints
- `backend/api/matches.py` - Match API endpoints
- `frontend/src/pages/Market/` - New page hierarchy
- `frontend/src/components/Market/` - All market UI components

**Modifications to existing code (potential conflicts):**
- Navigation/routing - Add market link
- `frontend/src/App.tsx` - Add routes

**Merge complexity:** LOW - almost entirely new code.

### Merge Strategy for Phase D

1. **Frequent rebasing:** Each feature branch rebases on `main` at least weekly (you do this before resuming work with an agent)
2. **Small PRs:** Break features into sub-PRs that can merge independently
3. **NodeDetail coordination:** Since multiple features modify this file, either:
   - Split it into sub-components BEFORE starting Phase D (recommended)
   - Or: merge one feature's NodeDetail changes first, then rebase others
4. **API namespace separation:**
   - Download: `/api/recommendations/*`
   - Upload: `/api/shares/*`, `/api/circles/*`
   - Market: `/api/intentions/*`, `/api/matches/*`
5. **Agent context:** When starting an agent on a feature branch, give it the file ownership map (see Appendix) so it knows what it "owns" vs what to avoid modifying

### What CANNOT Be Parallelized

These must be sequential:

1. **Embedding service (C.1) → Download or Market** - Both features need embeddings to work
2. **OAuth framework (C.2) → Upload external platforms** - Can't publish to Twitter without OAuth
3. **WebSocket (C.3) → Market notifications** - Real-time matching needs WebSockets

**But note:** Within Phase D, features can start before C.3 is complete. Only the notification piece of Intention Market requires WebSockets.

---

## Phase E: Integration and Polish

**Goal:** Connect all features into the unified flywheel.

**Why sequential after D:** Integration requires all features to exist.

### E.1 Cross-Feature Event System

**What:** Webhook/event bus connecting features.

**Examples:**
- Journal entry created → Generate share suggestions
- Journal entry created → Surface relevant bookmarks
- Share posted to Market → Available for matching

**Implementation:**
- Event emitter pattern in backend
- Subscriber registration per feature
- Async event handling via Celery

### E.2 Unified Notification System

**What:** Consolidate notifications from all features.

**Implementation:**
- Notification model
- Digest preferences per user
- Email + in-app + push delivery

### E.3 Analytics Dashboard

**What:** Track flywheel metrics.

**Implementation:**
- Event tracking
- Dashboard UI
- Cycle time measurements

---

## Timeline Summary

| Phase | Duration | Dependencies | Parallelizable? |
|-------|----------|--------------|-----------------|
| A: Alpha Release | 1-2 weeks | None | Partially (A.2 + A.3 parallel) |
| B: Dev Automation | 1-2 weeks | A.1 complete | Yes (B.1-B.3 parallel, B.4 skipped) |
| C: Shared Infra | 2-3 weeks | B complete | Yes (C.1-C.3 all parallel) |
| D: Features | 4-6 weeks | C.1, C.2 complete | **Yes (D.1-D.3 all parallel)** |
| E: Integration | 2-3 weeks | D complete | Partially |

**Total to alpha:** 1-2 weeks
**Total to parallel-ready:** 4-6 weeks (can run 3 AI agents on separate branches)
**Total to complete ecosystem:** 10-16 weeks (vs. 33 weeks in original roadmap)

**Why faster than original roadmap:**
1. Alpha release decoupled from full feature set
2. Parallel AI agent development after shared infrastructure
3. Less emphasis on production hardening before MVP
4. AI agents can work faster than the original human-time estimates assumed

---

## Risk Mitigation

### Risk: Merge conflicts in Phase D

**Mitigation:**
- Split `NodeDetail.tsx` into sub-components before Phase D starts
- Clear API namespace boundaries
- Rebase feature branches before resuming agent work
- You (human) resolve conflicts during merge - agents work on clean branches

### Risk: Shared infrastructure (Phase C) takes longer than expected

**Mitigation:**
- Can start Phase D features without waiting for all of C
- Download can start with basic embedding (skip optimization)
- Upload can start with circles/analysis (skip external platforms)
- Market can start with model design (skip real-time notifications)

### Risk: Tests aren't comprehensive enough for safe merging

**Mitigation:**
- Require passing CI before merge
- Add integration tests for cross-feature scenarios in Phase E
- You do manual QA before merging each feature branch

### Risk: AI agents make inconsistent architectural decisions across branches

**Mitigation:**
- Phase C (shared infrastructure) establishes patterns BEFORE agents work on features
- File ownership map prevents agents from reinventing shared code
- You review PRs for consistency before merging
- Consider creating a CLAUDE.md section with architectural conventions

---

## How to Run Parallel AI Agents

Once Phase C is complete, here's the workflow:

### Setup (one-time)
```bash
# Create feature branches from main
git checkout main
git checkout -b feature/download-memeos
git push -u origin feature/download-memeos

git checkout main
git checkout -b feature/upload-sharing
git push -u origin feature/upload-sharing

git checkout main
git checkout -b feature/intention-market
git push -u origin feature/intention-market
```

### Daily workflow
```bash
# Terminal 1: Agent working on Download
cd /path/to/write-or-perish
git checkout feature/download-memeos
git pull origin main --rebase  # Stay current
claude  # Start agent with context: "Working on Download feature, see docs/ACCELERATED-DEVELOPMENT-ROADMAP.md for file ownership"

# Terminal 2: Agent working on Upload
cd /path/to/write-or-perish
git checkout feature/upload-sharing
git pull origin main --rebase
claude  # "Working on Upload feature..."

# Terminal 3: Agent working on Market
cd /path/to/write-or-perish
git checkout feature/intention-market
git pull origin main --rebase
claude  # "Working on Intention Market feature..."
```

### Merge workflow
1. Agent finishes a chunk of work → commits to feature branch
2. You review the diff
3. If good: merge to main, then rebase other branches
4. If conflicts: you resolve them (agents work on clean branches)

---

## Next Actions

1. ✅ **COMPLETED:** A.1 (API key separation)
2. ✅ **COMPLETED:** A.4 (GCP KMS encryption)
3. ✅ **COMPLETED:** A.5 (Rebrand to Loore)
4. ✅ **COMPLETED:** A.6 (Full UI redesign)
5. ✅ **COMPLETED:** A.7 (Magic link email authentication)
6. ✅ **COMPLETED:** A.8 (User plan tier standardization)
7. **NOW:** A.2 (Sentry integration) + A.3 (Alpha docs)
8. **THEN:** Phase B (Docker + tests) - all items can run parallel
9. **THEN:** Phase C infrastructure, leading to Phase D parallelization

---

## Appendix: File Ownership Map for Parallel Development

This map shows which files each feature primarily owns, helping identify conflict points.

### Core (Don't Modify During Phase D)
- `backend/models/node.py` - Stable, tested
- `backend/models/user.py` - Stable
- `backend/utils/privacy.py` - Stable, tested
- `backend/api/nodes.py` - Minimize changes, add via extensions
- `frontend/src/pages/NodeDetail.tsx` - CONFLICT POINT, split into components first

### Download Feature Owns
- `backend/models/bookmark.py` (new)
- `backend/services/recommendation.py` (new)
- `backend/api/recommendations.py` (new)
- `frontend/src/components/Sidebar/RecommendationWidget.tsx` (new)

### Upload Feature Owns
- `backend/models/share.py` (new)
- `backend/models/circle.py` (new)
- `backend/services/content_analysis.py` (new)
- `backend/services/channels/*` (new)
- `backend/api/shares.py` (new)
- `backend/api/circles.py` (new)
- `frontend/src/components/Sharing/*` (new)

### Intention Market Owns
- `backend/models/intention.py` (new)
- `backend/models/match.py` (new)
- `backend/models/connection.py` (new)
- `backend/services/matching.py` (new)
- `backend/api/intentions.py` (new)
- `backend/api/matches.py` (new)
- `frontend/src/pages/Market/*` (new)
- `frontend/src/components/Market/*` (new)

### Shared Infrastructure (Build in Phase C)
- `backend/services/embedding.py` - Shared by Download + Market
- `backend/services/oauth.py` - Shared by Upload channels
- `backend/services/websocket.py` - Shared by Market + future real-time features

---

## Agent Prompts for Each Feature

Copy-paste these when starting an agent on a feature branch:

### Download Feature Agent
```
You're working on the Download feature (MemeOS integration) for Write or Perish.

Read docs/FOUR-FEATURE-ECOSYSTEM.md section "Feature 2: Download" for requirements.
Read docs/TECHNICAL-ROADMAP.md "Phase 1-2" for technical approach.

Your owned files (create these, don't modify core):
- backend/models/bookmark.py (new)
- backend/services/recommendation.py (new)
- backend/api/recommendations.py (new)
- frontend/src/components/Sidebar/RecommendationWidget.tsx (new)

Use the embedding service at backend/services/embedding.py (don't recreate it).
Minimize changes to existing files, especially NodeDetail.tsx.
```

### Upload Feature Agent
```
You're working on the Upload feature (broadcasting/sharing) for Write or Perish.

Read docs/FOUR-FEATURE-ECOSYSTEM.md section "Feature 3: Upload" for requirements.
Read docs/TECHNICAL-ROADMAP.md "Phase 4-5" for technical approach.

Your owned files (create these, don't modify core):
- backend/models/share.py (new)
- backend/models/circle.py (new)
- backend/services/content_analysis.py (new)
- backend/services/channels/* (new - one file per platform)
- backend/api/shares.py (new)
- frontend/src/components/Sharing/* (new)

Use the OAuth framework at backend/services/oauth.py (don't recreate it).
Minimize changes to existing files, especially NodeDetail.tsx.
```

### Intention Market Agent
```
You're working on the Intention Market feature for Write or Perish.

Read docs/FOUR-FEATURE-ECOSYSTEM.md section "Feature 4: Intention Market" for requirements.
Read docs/TECHNICAL-ROADMAP.md "Phase 6-7" for technical approach.

Your owned files (create these, don't modify core):
- backend/models/intention.py (new)
- backend/models/match.py (new)
- backend/models/connection.py (new)
- backend/services/matching.py (new)
- backend/api/intentions.py (new)
- backend/api/matches.py (new)
- frontend/src/pages/Market/* (new)
- frontend/src/components/Market/* (new)

Use the embedding service at backend/services/embedding.py (don't recreate it).
Add new routes to App.tsx but don't modify other existing pages.
```
