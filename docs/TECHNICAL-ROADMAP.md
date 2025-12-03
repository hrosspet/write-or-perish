# Technical Roadmap: Write or Perish Four-Feature Ecosystem

**Date:** November 24, 2025
**Status:** Planning Phase
**Purpose:** Technical infrastructure roadmap to build the distributed intelligence network

---

## Executive Summary

This document outlines the technical infrastructure needed to evolve Write or Perish from a journaling app into a complete four-feature ecosystem:

1. **Effortless Journaling** (Input) - ✅ Complete
2. **Download** (Consume) - Context-aware content recommendations from MemeOS
3. **Upload** (Broadcast) - AI-aided sharing to appropriate audiences
4. **Intention Market** (Matchmaking) - Serendipitous connection through complementary needs/offerings

The roadmap includes both feature-specific infrastructure and cross-cutting concerns (testing, monitoring, development velocity).

---

## Current State: Already Built ✅

These technical capabilities are production-ready and form the foundation:

### Core Features
- **Asynchronous processing via Celery** - Long-running tasks (transcription, LLM, TTS) run in background with polling
- **Multi-model LLM provider abstraction** - Unified interface for OpenAI and Anthropic with automatic routing
- **User profile generation system** - AI-powered analysis of user's writing to create personalized profiles
- **OAuth authentication flow** - Twitter login with approval/whitelist system
- **Tree-structured node system** - Hierarchical content organization with parent/child relationships
- **Version history tracking** - Edit history for nodes with timestamps for audit trails
- **Chunked file upload** - Handle large audio files (>10MB) by splitting into chunks and server-side assembly
- **Audio transcription pipeline** - Convert speech to text with file compression and chunk splitting for large files

### CI/CD & DevOps
- **GitHub Actions CI pipeline** - Automated testing on all branches and PRs with backend tests, frontend tests, linting, and security scans
- **Automated deployment pipeline** - Push to main triggers frontend build and SSH deployment to production VM
- **Security scanning** - Bandit for Python security issues and Safety for vulnerable dependencies run on every commit
- **Code quality checks** - Flake8 linting for backend and frontend lint checks catch issues early

---

## Critical Infrastructure Gaps (Priority: BLOCKING)

**These are architectural changes that must be addressed before or alongside feature development.**

### Privacy & Encryption Infrastructure

✅ **Node visibility levels and access control** - COMPLETED: Added privacy_level column (private/circles/public) and ai_usage column (none/chat/train) with API enforcement to control who can read each node

**Application-level encryption with GCP KMS** - Encrypt all node content at rest using Cloud KMS, transparently decrypt on authorized access

**Embedding isolation architecture** - Generate embeddings server-side from plaintext, store with user_id in pgvector, always filter queries by user_id + visibility

**User promise and transparency** - Clear communication: "Journals encrypted at rest. Semantic indexes power search/AI for your account only, never exposed publicly unless you choose"

**Data migration for existing nodes** - Encrypt all existing nodes using KMS, default to privacy_level='training', seamless backward compatibility

### Authentication & Authorization

**API token system (JWT)** - Replace session-only auth with JWT tokens for mobile apps, external integrations, and API access with refresh token rotation

✅ **Fine-grained permission system** - COMPLETED: Authorization layer with `can_user_access_node()`, `can_ai_use_node_for_chat()`, and `can_ai_use_node_for_training()` functions

**OAuth integration framework** - Reusable OAuth client for Twitter, LinkedIn, Substack, GitHub with token storage and refresh handling

### Data Infrastructure

**Cloud file storage with S3/R2** - Migrate from local file storage to cloud object storage for audio files with encryption at rest and CDN integration

**File encryption at rest** - Encrypt all audio files in S3 with user-specific keys, decrypt on-demand for authorized users only

**Email sending infrastructure** - SMTP service (SendGrid/AWS SES) for password recovery, notifications, and digests with template management

**Real-time communication with WebSockets** - WebSocket server for live notifications (matches, shares, messages) without polling, using Socket.IO or similar

### Compliance & Safety

**Audit logging system** - Track all data access (who read which node when) for compliance and security investigation with tamper-proof logs

**GDPR compliance enhancements** - Expand export/delete to include audit logs, encryption keys, and third-party data with clear consent management UI

**Content moderation pipeline** - Automated + manual moderation for public shares and marketplace using OpenAI Moderation API and admin review queue

**User reporting and blocking** - Allow users to report abusive content/users and block connections with admin dashboard for review

**Spam detection and rate limiting** - ML-based spam detection for shares and intentions with progressive penalties (warnings → temp ban → permanent ban)

### Mobile & Offline Support

**Mobile-optimized API design** - RESTful API with pagination, partial responses, and efficient payloads for mobile bandwidth constraints

**Offline-first data sync** - Conflict resolution for offline writes using operational transforms or CRDTs when user reconnects

**Mobile push notifications** - Push notification infrastructure (FCM/APNS) for match alerts and share responses on mobile devices

---

## Development Infrastructure (Priority: URGENT)

**These must be built to enable fast, safe iteration on new features.**

### Testing Infrastructure

**Backend testing framework with pytest** - Unit and integration tests for API endpoints, models, and tasks to catch regressions before deployment

**Frontend testing framework with Jest/React Testing Library** - Component tests, hook tests, and integration tests to ensure UI reliability

**Test database setup with fixtures** - Automated test DB creation/teardown with seed data for reproducible test scenarios

**Celery task testing utilities** - Mock async tasks in tests to avoid expensive LLM API calls and enable fast test execution

**API mocking for external services** - Mock OpenAI, Anthropic, Twitter APIs in tests to eliminate flakiness and cost

**End-to-end testing with Playwright/Cypress** - Critical user flows (login, create node, LLM response, voice recording) tested in real browser

**Test coverage tracking with coverage.py and jest --coverage** - Monitor which code is tested to prioritize where tests are needed most

**Test coverage reporting in CI** - Add coverage reports to CI pipeline to track test coverage over time and enforce minimum thresholds

### Development Velocity Infrastructure

**Local development Docker Compose setup** - One-command setup for PostgreSQL, Redis, Celery, backend, and frontend for new developer onboarding

**Database migration testing** - Automated checks that migrations run cleanly forward/backward without data loss

**API documentation with Swagger/OpenAPI** - Auto-generated API docs from Flask routes so frontend knows exactly what endpoints exist and their contracts

**Component storybook for frontend** - Isolated UI component development and testing without needing full app context

**Hot reload for both backend and frontend** - File changes automatically restart servers for instant feedback during development

**Logging infrastructure with structured logs** - JSON-formatted logs with request IDs for debugging production issues quickly

**Error monitoring with Sentry** - Automatic error capture with stack traces, user context, and breadcrumbs to debug production issues

**Performance monitoring and profiling** - Track slow endpoints, database queries, and LLM calls to optimize bottlenecks

**Feature flags system** - Toggle new features on/off without deployment to enable gradual rollouts and A/B testing

**Database query performance monitoring** - Log slow queries and add indexes proactively to prevent performance degradation as data grows

### Security & Reliability

**Rate limiting per user and per endpoint** - Prevent abuse and control costs with configurable limits on API calls, LLM usage, and file uploads

**Input validation and sanitization library** - Centralized validation for all user inputs to prevent injection attacks and bad data

**CSRF protection for state-changing endpoints** - Prevent cross-site request forgery attacks on POST/PUT/DELETE routes

**Content Security Policy (CSP) headers** - Mitigate XSS attacks by restricting what scripts/styles can load in browser

**Automated Dependabot PRs** - Set up GitHub Dependabot to automatically create PRs for dependency updates (complement to existing Safety checks)

**Database backup and restore automation** - Daily backups with tested restore procedures to prevent data loss

**Health check endpoints** - `/health` and `/ready` endpoints for load balancer and monitoring to detect unhealthy instances

**Circuit breaker for external APIs** - Fail fast and retry with backoff when OpenAI/Anthropic APIs are down to prevent cascading failures

---

## Feature 2: Download (MemeOS Integration)

**Vector database with pgvector extension** - Store and query embeddings for semantic search over nodes and bookmarks, enabling 10ms similarity searches at scale

**Automatic embedding generation pipeline** - Async task to generate embeddings on node create/edit using OpenAI's text-embedding-3-small model ($0.02/1M tokens)

**Semantic search API** - Query interface with cosine similarity ranking to find relevant content based on natural language queries

**RAG (Retrieval Augmented Generation) system** - Build context from top-K similar nodes and feed to LLM for conversational archive exploration

**MemeOS API client or shared database layer** - Integration layer to query bookmarks with filtering by tags, due dates, and spaced repetition status

**Context-aware recommendation engine** - Analyze current writing + thread + profile to surface relevant bookmarks with 70% semantic + 30% urgency ranking

**Theme extraction service** - Use lightweight LLM (GPT-4o-mini) to extract 3-5 key topics from writing context for targeted bookmark retrieval

**Spaced repetition integration** - Factor in review schedules when ranking bookmarks to surface overdue content at the right time

---

## Feature 3: Upload (Broadcasting Internal State)

**Content analysis pipeline** - LLM-based classification to detect shareable insights (mental models, frameworks, lessons) with shareability scoring (0-1 scale)

**Multi-channel publishing adapters** - OAuth integrations and API clients for Twitter, LinkedIn, Substack, GitHub Gists with rate limiting and error handling

**Content transformation engine** - Convert raw journal entries to polished shares with extraction, formatting, contextualization, and audience-appropriate framing

**Privacy analysis system** - Detect sensitive information (names, health, finances) and flag potential risks before sharing with multi-layer protection

**Share approval workflow** - UI and backend state machine for suggested/approved/published/rejected with preview, edit, and rollback capabilities

**Tiered audience management** - Define and manage private circles, professional networks, and public visibility with granular per-share controls

**Multi-entry synthesis** - Track themes across journal entries over time and suggest combining related content into long-form essays

**Draft generation for external platforms** - Create formatted drafts in Substack/LinkedIn with preview links, never auto-publish without explicit user consent

**OAuth credential management** - Secure storage and refresh token handling for third-party platform integrations with per-channel revocation

---

## Feature 4: Intention Market (Matchmaking)

**Intention data model with structured metadata** - Store needs/offerings/opportunities with topics, time commitment, format preferences, capacity, and expiration dates

**Semantic matching algorithm** - Embed intentions and calculate match scores using 40% similarity + 25% topic overlap + 15% format + 10% time + 10% reputation

**Real-time match notification system** - Detect complementary intentions (need↔offering) and notify both parties with match score and reasoning

**Connection management infrastructure** - Handle match acceptance, private messaging threads, and bidirectional relationships with activity tracking

**Reputation scoring engine** - Track completion rates, responsiveness, feedback scores, and giving/taking ratio to weight match quality

**Intention expiration and renewal system** - Auto-expire stale posts (30-90 days) with renewal prompts based on capacity and user activity

**Market discovery API with filtering** - Browse/search intentions by type, topic, recency with personalized feeds based on user's posted intentions and profile

**Feedback and outcome tracking** - Collect post-collaboration ratings and success metrics to improve future matching algorithm weights

**Group matchmaking logic** - Identify multiple users with similar intentions (e.g., 5 people wanting daily writing accountability) and suggest forming groups

---

## Cross-Feature Infrastructure

**Webhook system for inter-feature communication** - Event bus to trigger actions across features (e.g., journal entry → MemeOS recommendation → share suggestion → intention extraction)

**Unified notification framework** - Consolidate alerts from all features with digest options, priority levels, and user-configurable frequency (real-time/daily/weekly)

**Global embedding service** - Centralized embedding generation for nodes, bookmarks, intentions, and profiles to ensure consistency and reduce API costs

**User preference learning system** - Track accept/reject patterns across all AI suggestions to personalize recommendations, shareability thresholds, and match criteria

**Cost tracking and quota management** - Monitor LLM API usage per user per feature to enforce tier limits and optimize model selection (haiku vs sonnet)

**Analytics and insight dashboard** - Track usage patterns, feature adoption, flywheel metrics (journal→download→upload→market→journal cycle time) for product iteration

**Privacy and consent management** - Centralized system for feature-level opt-ins, data sharing permissions, and GDPR-compliant export/deletion across all four features

**Rate limiting and throttling** - Prevent abuse and control costs with per-user limits on recommendations, shares, matches, and API calls

**Caching layer with Redis** - Cache embeddings, LLM responses, match scores, and recommendation results to reduce latency and API costs

**Background job prioritization** - Celery queue management with priority levels (urgent: user-triggered vs low: batch processing) and failure retry logic

---

## Chronological Implementation Order

Based on current project state, dependencies, and strategic value, here's the recommended build sequence:

### Phase -1: Privacy & Encryption Infrastructure (Weeks 1-3) 🔒 ✅ **COMPLETED**

**Priority: BLOCKING - Must be done first before building sharing/marketplace features**

**Status: COMPLETED** - Two-column privacy system implemented for both Nodes and UserProfiles

**Why first:** Currently all nodes are public. Features 3 (Upload) and 4 (Intention Market) require granular privacy controls. Retrofitting privacy after building on public-only architecture would require rebuilding everything. This is a one-time breaking change that must happen now.

**Implementation Decision:** Two-column approach (more flexible than original 5-level design)
- Column 1: `privacy_level` - Who can access (private/circles/public)
- Column 2: `ai_usage` - How AI can use content (none/chat/train)
- This provides clearer separation of concerns and more flexibility

### Completed Items:

1. ✅ **Node privacy schema** - Added `privacy_level` and `ai_usage` columns to Node model
2. ✅ **UserProfile privacy schema** - Added `privacy_level` and `ai_usage` columns to UserProfile model
3. ✅ **Privacy utilities module** - Created enums, validation functions, and authorization checks (backend/utils/privacy.py)
4. ✅ **Fine-grained permission system** - `can_user_access_node()` checks privacy level and ownership
5. ✅ **AI usage permissions** - `can_ai_use_node_for_chat()` and `can_ai_use_node_for_training()` functions
6. ✅ **API endpoints for privacy** - Updated all Node and UserProfile creation/edit endpoints to accept and validate privacy settings
7. ✅ **Authorization enforcement** - GET /nodes/<id> enforces privacy with authorization check
8. ✅ **Data migration scripts**:
   - `backend/scripts/migrate_privacy_settings.py` - Migrates existing nodes to privacy='private', ai_usage='train'
   - `backend/scripts/migrate_profile_privacy_settings.py` - Migrates existing profiles to privacy='private', ai_usage='chat'
9. ✅ **Frontend privacy UI** - Created PrivacySelector component with clear labels and descriptions
10. ✅ **Frontend integration**:
    - NodeForm: Privacy selector for text nodes, voice recordings, and file uploads
    - Dashboard: Privacy selector for user profile editing
    - NodeDetail: Conditionally hide LLM Response button when ai_usage='none' (#38)
11. ✅ **Tests** - Comprehensive unit tests for privacy validation, authorization, and AI usage checks (backend/tests/)
12. ✅ **Profile generation filtering** - Only include nodes with ai_usage='chat' or 'train' in AI-generated profiles, while user data exports include all nodes (#37)

### Remaining Tasks (Still Blocking):

- ⚠️ **Separate API keys for chat vs train** - Use different OpenAI/Anthropic API keys based on node ai_usage setting to ensure proper separation of training data (#39)

### NOT Implemented (Future Work):

- **GCP KMS encryption** - Content not yet encrypted at rest (can be added later without breaking changes)
- **Email infrastructure** - Not yet implemented (SendGrid/AWS SES)
- **Circles implementation** - Privacy level exists but circles functionality not yet built (moved to Phase 4)

**Deliverable:** Two-column privacy system for Nodes and UserProfiles with authorization enforcement and comprehensive tests

**Migration Strategy Implemented:**
- **Existing nodes:** Default to `privacy_level='private'` + `ai_usage='train'` (maintains training data value proposition)
- **Existing profiles:** Default to `privacy_level='private'` + `ai_usage='chat'` (AI can understand user for responses)
- **New nodes:** Default to `privacy_level='private'` + `ai_usage='none'` (fully private)
- **New profiles:** Default to `privacy_level='private'` + `ai_usage='chat'` (useful for AI responses)
- **User control:** Users can change privacy settings at any time via UI

**Privacy Level Options:**
- 🔒 **private** - Only owner can see
- 👥 **circles** - Shared with specific groups (coming soon)
- 🌐 **public** - Anyone can see

**AI Usage Options:**
- 🚫 **none** - No AI access
- 💬 **chat** - AI can use for responses (not training)
- 🎓 **train** - AI can use for training data

---

### Phase 0: Foundation (Weeks 4-6) 🏗️

**Priority: Enable fast, safe development of new features**

**Note:** CI/CD pipeline already exists! GitHub Actions run tests, linting, security scans, and auto-deploy on push to main. This phase focuses on adding actual tests and monitoring.

1. **Backend testing framework with pytest** - Set up pytest, conftest.py, test database fixtures (CI already configured to run pytest)
2. **Frontend testing framework with Jest/React Testing Library** - Write actual component tests (Jest already runs in CI with placeholder test)
3. **Test coverage reporting in CI** - Add coverage.py and jest coverage reports to existing CI pipeline
4. **Local development Docker Compose setup** - Docker compose file for full local stack (PostgreSQL, Redis, Celery, backend, frontend)
5. **Logging infrastructure with structured logs** - Add JSON logging with request IDs for debugging
6. **Error monitoring with Sentry** - Integrate Sentry for both backend and frontend error tracking
7. **Health check endpoints** - `/health` and `/ready` for monitoring and load balancer health checks
8. **Database backup and restore automation** - Automated daily backups with tested restore procedures
9. **Dependabot configuration** - Enable GitHub Dependabot for automated dependency update PRs

**Deliverable:** Solid foundation with actual tests running in existing CI pipeline, monitoring, and development environment

**Why first:** You have the CI/CD infrastructure but no tests. This phase fills that gap. Without tests, building complex features is slow and risky. Every bug becomes a production fire. This investment pays off immediately.

---

### Phase 1: Download Foundation (Weeks 7-9) 📥

**Priority: Enable semantic search and RAG before building on top**

13. **Vector database with pgvector extension** - Install pgvector, add migration for embedding column
14. **Automatic embedding generation pipeline** - Celery task to embed nodes on create/edit (respects privacy: only embeds training/public nodes)
15. **Global embedding service** - Centralized embedding API with cost tracking
16. **Privacy-aware semantic search API** - Endpoint for cosine similarity search that filters by user's access permissions
17. **RAG (Retrieval Augmented Generation) system** - Chat with archive using top-K retrieval (only searches nodes user has access to)
18. **Test coverage for embedding and search** - Comprehensive tests with mocked OpenAI API and permission checks

**Deliverable:** Can search your journal semantically and chat with your archive

**Why now:** This is immediately valuable for personal use and is foundational for Features 2, 3, and 4 (all need semantic matching/search)

---

### Phase 2: Download Complete (Weeks 10-12) 📚

**Priority: Complete MemeOS integration for full "consume" feature**

19. **MemeOS API client or shared database layer** - Integration layer for querying bookmarks
20. **Context-aware recommendation engine** - Analyze writing context + profile to rank bookmarks
21. **Theme extraction service** - LLM-based topic extraction from current writing
22. **Spaced repetition integration** - Factor in review schedules for ranking
23. **Frontend: Recommendation sidebar widget** - UI component showing relevant bookmarks
24. **Caching layer with Redis** - Cache bookmark recommendations to reduce API calls
25. **Tests for recommendation logic** - Unit tests for ranking algorithm

**Deliverable:** MemeOS bookmarks surface in sidebar while you write

**Why now:** Completes Feature 2, provides immediate value, and demonstrates the "closed loop" between consumption and creation

---

### Phase 3: Development Velocity Upgrades (Weeks 13-14) 🚀

**Priority: Invest in speed before building complex features 3 and 4**

26. **API documentation with Swagger/OpenAPI** - Auto-generated API docs
27. **Component storybook for frontend** - Isolated component development
28. **Feature flags system** - Toggle features on/off without deployment
29. **Database query performance monitoring** - Identify and fix slow queries
30. **Performance monitoring and profiling** - Track endpoint latency and bottlenecks
31. **End-to-end testing with Playwright** - Critical flow testing (login → create → LLM)
32. **JWT authentication system** - Add token-based auth alongside session auth for future mobile/API access

**Deliverable:** Faster development workflow, better debugging, gradual rollout capability

**Why now:** Features 3 and 4 are complex with many moving parts. These tools will make building them 2-3x faster.

---

### Phase 4: Upload Foundation (Weeks 15-18) 📤

**Priority: Build sharing infrastructure without external platform complexity**

**Note:** This phase includes Circles implementation, which was deferred from Phase -1 as it's not blocking for basic privacy functionality.

33. **Circles data model and management** - Create Circle model with membership, privacy-aware share targeting (implements the 'circles' privacy level from Phase -1)
34. **Content analysis pipeline** - LLM-based shareability scoring and insight extraction (only analyzes training/public nodes unless user opts in)
35. **Privacy analysis system** - Detect sensitive information and flag risks before sharing
36. **Share approval workflow** - State machine for suggested/approved/published status with audit trail
37. **Content transformation engine** - Convert journal entries to polished shares while preserving attribution to encrypted source
38. **User preference learning system** - Track accept/reject patterns to personalize suggestions
39. **Audit logging for shares** - Log who shared what to whom and when for compliance
40. **Frontend: Circles management UI** - Create/edit circles, add/remove members
41. **Frontend: Share suggestion UI** - Preview, edit, approve/reject with clear privacy indicators
42. **Tests for analysis and transformation** - Mock LLM responses, test scoring logic, verify privacy enforcement

**Deliverable:** Get share suggestions, preview transformations, manage circles (sharing within app only, no external platforms yet)

**Note:** Shares respect privacy levels. Only training/public nodes can be suggested for sharing unless user explicitly opts in.

**Why now:** Proves core sharing value without external API complexity. Users can share to other Write or Perish users.

---

### Phase 5: Upload External Platforms (Weeks 19-21) 🌐

**Priority: Add external publishing capabilities**

43. **OAuth integration framework** - Reusable OAuth client for multiple platforms with secure token storage
44. **Multi-channel publishing adapters** - API clients for Twitter, LinkedIn, Substack, GitHub Gists
45. **Draft generation for external platforms** - Format content for each platform's requirements and character limits
46. **Rate limiting per channel** - Prevent API quota exhaustion with per-platform rate limits
47. **Multi-entry synthesis** - Combine related entries into long-form essays using LLM
48. **Cloud file storage migration (S3/R2)** - Move audio files from local storage to cloud with encryption at rest
49. **Frontend: External platform connection UI** - OAuth flow, channel selection, publish preview
50. **Circuit breaker for external APIs** - Handle third-party API failures gracefully with retry logic

**Deliverable:** Publish shares to Twitter, LinkedIn, Substack with one click

**Why now:** Completes Feature 3, maximizes distribution of user's insights

---

### Phase 6: Intention Market Foundation (Weeks 22-25) 🤝

**Priority: Build marketplace infrastructure and matching algorithm**

51. **Intention data model with structured metadata** - DB schema for needs/offerings/opportunities with privacy controls
52. **Semantic matching algorithm** - Embed intentions and calculate compatibility scores using existing embedding service
53. **Reputation scoring engine** - Track completion rates, responsiveness, feedback with privacy-preserving aggregation
54. **Intention expiration and renewal system** - Auto-expire stale posts, prompt renewals based on activity
55. **Market discovery API with filtering** - Browse/search by type, topic, recency with permission checks
56. **Content moderation for marketplace** - OpenAI Moderation API + admin review queue for flagged intentions
57. **User blocking and reporting** - Allow users to block/report abusive users or spam
58. **Frontend: Intention posting UI** - Form to create intentions with metadata and privacy settings
59. **Frontend: Browse market UI** - Filterable list of active intentions with moderation indicators
60. **Tests for matching algorithm** - Unit tests for score calculation with mock embeddings and permission checks

**Deliverable:** Can post intentions, browse market, see match scores

**Why now:** Core marketplace functionality without real-time notifications complexity

---

### Phase 7: Intention Market Complete (Weeks 26-28) 💬

**Priority: Add connection features and notifications**

61. **WebSocket infrastructure for real-time updates** - Socket.IO server for live match notifications and messages
62. **Real-time match notification system** - Detect complementary intentions and push notifications via WebSocket
63. **Connection management infrastructure** - Accept/decline matches, private messaging with encryption
64. **Feedback and outcome tracking** - Post-collaboration ratings with privacy protections
65. **Group matchmaking logic** - Suggest forming groups for similar intentions (3+ users)
66. **Unified notification framework** - Consolidate all notifications (shares, matches, messages) with digest options
67. **Frontend: Match notification UI** - See matches, view profiles, connect with privacy-safe previews
68. **Frontend: Connection thread UI** - Private messaging between matched users with encryption indicators
69. **Tests for notification delivery** - Ensure matches are detected correctly and permissions enforced

**Deliverable:** Full matchmaking with notifications, connections, and messaging

**Why now:** Completes Feature 4, enables serendipitous collaboration

---

### Phase 8: Cross-Feature Integration (Weeks 29-31) 🔄

**Priority: Connect all four features into unified flywheel**

70. **Webhook system for inter-feature communication** - Event bus triggering actions across features (journal→recommend→share→match)
71. **Cost tracking and quota management** - Per-user LLM usage tracking per feature with tier enforcement
72. **Analytics and insight dashboard** - Track flywheel metrics (journal→download→upload→market→journal cycle time)
73. **Privacy consent management UI** - Centralized dashboard for all privacy settings across features
74. **Background job prioritization** - Optimize Celery queues with priority levels (urgent/normal/low)
75. **Spam detection across features** - ML-based spam detection for shares, intentions, messages
76. **Frontend: Unified dashboard** - Show activity across all four features with privacy indicators
77. **End-to-end flywheel tests** - Test complete cycle from journaling to connection with permission checks

**Deliverable:** Seamless integration where features amplify each other

**Why last:** Requires all four features to exist, demonstrates full vision

---

### Phase 9: Production Hardening (Weeks 32-33) 🛡️

**Priority: Make system bulletproof before scaling**

78. **Input validation and sanitization library** - Centralized validation for all user inputs (XSS, SQL injection, command injection prevention)
79. **CSRF protection for state-changing endpoints** - Prevent cross-site attacks on POST/PUT/DELETE
80. **Content Security Policy (CSP) headers** - Mitigate XSS attacks by restricting script sources
81. **Database migration testing automation** - Ensure migrations run cleanly forward/backward without data loss
82. **Load testing and capacity planning** - Stress test with realistic user loads to find bottlenecks
83. **Penetration testing on encryption** - Verify encryption implementation is secure (hire external auditor)
84. **Comprehensive end-to-end test suite** - Cover all critical user journeys including privacy edge cases

**Deliverable:** Production-ready system that can scale securely

**Why last:** Critical for launch but requires complete system to test properly

**Note:** Security scanning (Bandit, Safety) already runs in CI. This phase focuses on additional hardening and external audit.

---

## Success Metrics Per Phase

### Phase -1 (Privacy & Encryption)
- **Encryption coverage:** 100% of all nodes encrypted at rest with KMS
- **Zero data breaches:** No unauthorized access to encrypted content
- **Embedding isolation:** 100% of queries filtered by user_id
- **Permission enforcement:** 0 unauthorized node access incidents
- **User understanding:** 80%+ of users understand privacy levels and promises

### Phase 0 (Foundation)
- **Test coverage:** 60%+ backend, 50%+ frontend
- **CI pipeline:** All tests run in <5 minutes
- **Error monitoring:** 100% of errors captured in Sentry
- **Developer onboarding:** New dev can run full stack in <30 minutes

### Phase 1-2 (Download)
- **Embedding coverage:** 95%+ of nodes embedded
- **Search latency:** <500ms for semantic search
- **RAG quality:** User reports useful answers 70%+ of time
- **Recommendation relevance:** User clicks through 30%+ of suggestions

### Phase 4-5 (Upload)
- **Share suggestion acceptance:** 40%+ of suggestions approved
- **Privacy violations:** 0 accidental leaks of sensitive info
- **External publish success:** 95%+ publish attempts succeed
- **User satisfaction:** "Makes sharing effortless" - 70%+ agree

### Phase 6-7 (Intention Market)
- **Match quality:** 60%+ of suggested matches accepted
- **Connection engagement:** 40%+ of connections lead to conversation
- **Market activity:** 50%+ of users post at least one intention
- **Collaboration outcomes:** 30%+ of matches lead to reported value

### Phase 8 (Integration)
- **Flywheel completion:** 20%+ of users complete journal→download→upload→market cycle
- **Cross-feature usage:** 60%+ of users active in 2+ features
- **Cycle time:** Median time from journal entry to match <7 days

### Phase 9 (Hardening)
- **Uptime:** 99.5%+ availability
- **Security:** 0 critical vulnerabilities
- **Performance:** 95th percentile latency <2s for all endpoints
- **Data safety:** 0 data loss incidents

---

## Cost Projections

### Development Costs (Time Investment)
- **Phase -1 (Privacy & Encryption):** 3 weeks - BLOCKING change, must be done first (reduced from 4 weeks with KMS approach)
- **Phase 0 (Foundation):** 3 weeks - one-time investment, pays dividends forever
- **Phase 1-2 (Download):** 6 weeks - immediate personal value
- **Phase 3 (Dev Velocity):** 2 weeks - 2-3x speedup on subsequent phases
- **Phase 4-5 (Upload):** 7 weeks - high complexity due to external APIs and privacy
- **Phase 6-7 (Market):** 7 weeks - high complexity due to matching logic and moderation
- **Phase 8 (Integration):** 3 weeks - straightforward once features exist
- **Phase 9 (Hardening):** 2 weeks - essential for production, includes external security audit
- **Total:** ~33 weeks (8.25 months) for complete system

### Infrastructure Costs (Monthly, at scale)

**At 100 users:**
- **Compute:** $50 (DigitalOcean/AWS)
- **Database:** $25 (PostgreSQL + Redis)
- **Storage (S3):** $10 (encrypted audio files)
- **GCP KMS:** $5 (encryption/decryption operations)
- **LLM APIs:** $200 (embeddings, RAG, analysis, transformation)
- **External APIs:** $50 (Twitter, LinkedIn, etc.)
- **Email (SendGrid):** $15 (notifications)
- **Monitoring:** $25 (Sentry, logging)
- **Total:** ~$380/month = $3.80/user

**At 1,000 users:**
- **Compute:** $200
- **Database:** $100
- **Storage (S3):** $50 (encrypted audio with CDN)
- **GCP KMS:** $20 (scales with operations)
- **LLM APIs:** $1,500 (benefits from caching and optimization)
- **External APIs:** $300
- **Email (SendGrid):** $50
- **Monitoring:** $100
- **Total:** ~$2,320/month = $2.32/user (economies of scale)

**Revenue Model:** $10-25/month subscription → profitable at 100+ users

---

## Risk Mitigation

### Technical Risks

**Risk: LLM API costs spiral**
- **Mitigation:** Aggressive caching, model selection (use GPT-4o-mini where possible), quota management, cost tracking per feature

**Risk: External API rate limits hit**
- **Mitigation:** Circuit breakers, exponential backoff, queue management, batch operations where possible

**Risk: Vector search performance degrades**
- **Mitigation:** Start with pgvector (handles 1M+ vectors), monitor query times, plan migration to Pinecone if needed

**Risk: Privacy violation (accidental sharing)**
- **Mitigation:** Multi-layer protection, always require approval, obvious privacy warnings, easy unshare/delete

**Risk: System complexity becomes unmaintainable**
- **Mitigation:** Comprehensive tests (60%+ coverage), feature flags for gradual rollout, monitoring for early issue detection

### Product Risks

**Risk: Features don't integrate into flywheel**
- **Mitigation:** Build Phase 8 integration layer explicitly, measure cycle metrics, user research at each phase

**Risk: Matching algorithm doesn't work**
- **Mitigation:** Start simple (cosine similarity), iterate based on feedback data, track acceptance rates

**Risk: Users don't want to share**
- **Mitigation:** Make sharing optional, start with inner circle only, build trust gradually, emphasize privacy

---

## Open Questions

### Technical Decisions Needed

1. **✅ DECIDED: Encryption Strategy:** Application-level encryption with GCP KMS (pragmatic, no per-user key complexity)
2. **✅ DECIDED: Embedding Strategy:** Store embeddings unencrypted with strict user_id isolation (pragmatic, enables search/AI)
3. **MemeOS Integration Method:** Shared database or API? (Affects Phase 2 timeline)
4. **Vector Database Choice:** Start with pgvector or go straight to Pinecone? (Cost vs simplicity tradeoff)
6. **Notification Delivery:** Email, in-app, WebSocket push, mobile push, or all? (Affects Phase 7 infrastructure complexity)
7. **Testing Philosophy:** Unit tests only or also integration/E2E? (Affects Phase 0 scope)
8. **Deployment Strategy:** Monolith or microservices? (Affects Phase 9 architecture)

### Product Decisions Needed

9. **Privacy Level Marketing:** How to communicate 5 privacy levels without confusing users? (Affects onboarding UX)
10. **Default Privacy Evolution:** When to switch from training→private as default for new nodes? (Affects user trust timeline)
11. **Feature Launch Order:** Should we launch features to users incrementally or wait for all four? (Affects beta strategy)
12. **Monetization Timing:** Free tier for all features or paywall some? (Affects user growth vs revenue)
13. **Network Effects:** Can Feature 4 work with 100 users or need 1,000+? (Affects cold start strategy)

---

## Conclusion

This roadmap prioritizes **privacy & encryption first** (Phase -1), then **foundation** (testing, monitoring), then **personal value** (Feature 2: Download), then **sharing** (Feature 3: Upload), then **community** (Feature 4: Intention Market).

**Critical Insight:** Privacy infrastructure (Phase -1) is BLOCKING. Retrofitting encryption after building Features 3-4 would require rebuilding everything. This one-time architectural change must happen first.

**Key Infrastructure Gaps Identified:**
1. **Privacy & Encryption** - 5-level privacy system with encryption, key management, recovery
2. **Authentication & Authorization** - Fine-grained permissions, JWT tokens, OAuth framework
3. **Data Infrastructure** - Cloud storage (S3), email service, WebSockets for real-time
4. **Compliance & Safety** - Audit logging, GDPR enhancements, content moderation, spam detection
5. **Mobile & Offline** - Mobile-optimized APIs, offline sync, push notifications

**Total Timeline:** ~33 weeks (8.25 months) for complete four-feature ecosystem

**First Milestone:** After Phase -1 + 0 (6 weeks), you'll have KMS encryption + tests foundation
**Second Milestone:** After Phase 1-2 (12 weeks total), you'll have RAG + MemeOS integration for personal use

**Next Steps:**
1. **IMMEDIATE:** Set up GCP KMS (key ring + crypto key)
2. Begin Phase -1: Privacy & Encryption Infrastructure (3 weeks)
3. Migrate existing nodes to encrypted storage (set all to privacy_level='training')
4. Update user-facing docs with clear privacy promise
5. Set up Phase 0: Testing infrastructure alongside encryption work
6. Iterate based on learnings at each phase

This is ambitious but achievable. Each phase delivers value, and the foundation work (especially privacy) ensures sustainable velocity and user trust throughout.

**The KMS approach:** Simpler than per-user encryption (no password recovery complexity, no client-side crypto), still provides strong security with transparent encryption/decryption. Embeddings stored unencrypted but strictly isolated by user_id enables full search/AI functionality.
