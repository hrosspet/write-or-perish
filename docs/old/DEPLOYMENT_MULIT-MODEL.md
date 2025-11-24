# Multi-Model LLM Feature - Deployment Guide

This guide provides step-by-step instructions for deploying the multi-model LLM feature to your Write or Perish instance.

## Overview of Changes

### Backend Changes:
- Added `anthropic` package dependency
- Added `llm_model` column to `node` table
- Created provider abstraction layer (`backend/llm_providers.py`)
- Added new endpoint: `GET /nodes/:id/suggested-model`
- Updated endpoint: `POST /nodes/:id/llm` to accept model parameter
- Added configuration for multiple LLM providers

### Frontend Changes:
- Created `ModelSelector` component
- Updated `NodeDetail` component to integrate model selection

### Database Changes:
- New migration to add `llm_model` column
- Backfills existing LLM nodes with "gpt-4.5-preview"

---

## Pre-Deployment Checklist

- [ ] Obtain Anthropic API key (if you want to use Claude models)
- [ ] Review the supported models in `backend/config.py`
- [ ] Backup your database before running migrations
- [ ] Ensure you're on the `multi-model` branch

---

## Deployment Steps

### 1. Environment Variables

Add the following to your `.env` file (or environment configuration):

```bash
# Required: Anthropic API key for Claude models
ANTHROPIC_API_KEY=sk-ant-your-key-here

# Optional: Keep existing OpenAI key
OPENAI_API_KEY=sk-your-existing-key

# Optional: Set default model (defaults to "gpt-5" if not set)
LLM_NAME=gpt-5
```

**Note:** If you don't have an Anthropic API key yet, you can still deploy - the system will work with OpenAI models only. Users will see an error if they try to select Claude models without a valid API key.

---

### 2. Backend Deployment

#### Step 2.1: Install Dependencies

```bash
cd backend
pip install -r requirements.txt
```

This will install the new `anthropic==0.72.0` package along with existing dependencies.

#### Step 2.2: Run Database Migration

**IMPORTANT: Backup your database first!**

```bash
# From the project root directory
flask db upgrade
```

This migration will:
- Add the `llm_model` column to the `node` table
- Backfill all existing LLM nodes with `llm_model = 'gpt-4.5-preview'`
- Keep user-created nodes with `llm_model = NULL`

**Verify the migration:**
```bash
# Check the database to confirm the column was added
flask db current
```

You should see revision `a1b2c3d4e5f6` (add_llm_model_to_nodes) as the current revision.

#### Step 2.3: Restart Backend Server

```bash
# Development
flask run

# Or with gunicorn (production)
gunicorn backend.app:app
```

**Verify backend is running:**
```bash
# Test the new endpoint
curl -X GET http://localhost:5000/api/nodes/1/suggested-model \
  -H "Content-Type: application/json" \
  --cookie "session=your-session-cookie"
```

Expected response:
```json
{
  "suggested_model": "gpt-5",
  "source": "default"
}
```

---

### 3. Frontend Deployment

#### Step 3.1: Install Dependencies (if needed)

```bash
cd frontend
npm install
```

No new npm packages are required - all changes use existing dependencies.

#### Step 3.2: Build Frontend

```bash
# Development
npm start

# Production build
npm run build
```

#### Step 3.3: Verify Frontend

Open your browser and navigate to a node detail page. You should see:
- A dropdown menu between "Add Text" and "LLM Response" buttons
- The dropdown should show 4 options:
  - GPT-5 (OpenAI)
  - Claude 4.5 Sonnet (Anthropic)
  - Claude 4.1 Opus (Anthropic)
  - Claude 3 Opus (Anthropic)

---

## Testing the Deployment

### Test 1: Model Selection UI

1. Navigate to any node in your application
2. Verify the model selector dropdown appears
3. Verify it shows all 4 models
4. Verify it pre-selects "GPT-5" by default (or the model from the parent LLM node)

### Test 2: Generate Response with GPT-5

1. Select "GPT-5" from the dropdown
2. Click "LLM Response"
3. Verify a new LLM node is created
4. Verify the node's username shows "gpt-5"

### Test 3: Generate Response with Claude (if API key configured)

1. Select "Claude 4.5 Sonnet" from the dropdown
2. Click "LLM Response"
3. Verify a new LLM node is created
4. Verify the node's username shows "claude-sonnet-4.5"

### Test 4: Model Inheritance

1. Create an LLM response with Claude 4.5 Sonnet
2. Add a text node as a child
3. Verify the model dropdown pre-selects "Claude 4.5 Sonnet" (inherited from parent)
4. Generate another LLM response
5. Verify it uses Claude 4.5 Sonnet

### Test 5: Legacy Node Compatibility

1. Find an existing LLM node (created before this deployment)
2. Verify it displays correctly
3. Add a child text node
4. Verify the model dropdown shows "GPT-5" (default, not the legacy "gpt-4.5-preview")

---

## Rollback Plan

If you need to rollback the deployment:

### Backend Rollback

```bash
# Rollback the database migration
flask db downgrade

# This will:
# - Drop the llm_model column from the node table
# - Revert to the previous schema
```

### Code Rollback

```bash
# Switch back to the previous branch
git checkout main  # or your previous branch
```

### Environment Variables

Remove or comment out the new environment variable:
```bash
# ANTHROPIC_API_KEY=sk-ant-...
```

---

## Troubleshooting

### Issue: "Unsupported model" error

**Cause:** The model ID sent from frontend doesn't match backend configuration.

**Solution:** Verify the model IDs in `backend/config.py` match those in `frontend/src/components/ModelSelector.js`.

### Issue: "LLM API error" with Claude models

**Cause:** Missing or invalid Anthropic API key.

**Solution:**
1. Verify `ANTHROPIC_API_KEY` is set in your environment
2. Test the API key:
```bash
curl https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-4-5-20250929","max_tokens":10,"messages":[{"role":"user","content":"Hi"}]}'
```

### Issue: Model dropdown not appearing

**Cause:** Frontend not rebuilt or browser cache.

**Solution:**
1. Clear browser cache
2. Rebuild frontend: `npm run build`
3. Hard refresh browser (Cmd+Shift+R or Ctrl+Shift+R)

### Issue: Database migration fails

**Cause:** Database connection issues or conflicting migrations.

**Solution:**
1. Check database connection: `flask db current`
2. Ensure you're on the latest migration before this one
3. Check for conflicts: `flask db heads`
4. If multiple heads exist, merge them: `flask db merge`

### Issue: "No module named 'anthropic'"

**Cause:** Dependencies not installed.

**Solution:**
```bash
cd backend
pip install anthropic==0.72.0
# Or reinstall all dependencies
pip install -r requirements.txt
```

---

## Monitoring

After deployment, monitor the following:

### API Usage
- Track API calls to OpenAI vs Anthropic
- Monitor token usage per model
- Check error rates for each provider

### User Behavior
- Which models are users selecting?
- Are users sticking with one model or experimenting?
- Are there any model-specific error patterns?

### Performance
- Response times per model
- API latency differences between providers
- Database query performance (new column indexed?)

---

## Configuration Reference

### Supported Models

Current configuration in `backend/config.py`:

| Model ID | Provider | API Model | Display Name |
|----------|----------|-----------|--------------|
| `gpt-5` | OpenAI | `gpt-5` | GPT-5 |
| `claude-sonnet-4.5` | Anthropic | `claude-sonnet-4-5-20250929` | Claude 4.5 Sonnet |
| `claude-opus-4.1` | Anthropic | `claude-opus-4-1-20250514` | Claude 4.1 Opus |
| `claude-opus-3` | Anthropic | `claude-3-opus-20240229` | Claude 3 Opus |

### Adding New Models

To add a new model:

1. Update `backend/config.py` - add entry to `SUPPORTED_MODELS` dict
2. Update `frontend/src/components/ModelSelector.js` - add to `models` array
3. Ensure API credentials are configured for the provider
4. Test thoroughly before deploying

---

## Security Considerations

1. **API Keys:** Never commit API keys to version control
2. **Rate Limiting:** Consider implementing per-user rate limits
3. **Cost Controls:** Monitor API costs, especially for expensive models
4. **Input Validation:** All model selections are validated server-side
5. **Legacy Data:** Old LLM nodes are preserved with their original model identifier

---

## Support

For issues or questions:
- Check the design doc: `docs/multi-model-llm-design.md`
- Review the code comments in `backend/llm_providers.py`
- Check the git commit history on the `multi-model` branch

---

**Deployment prepared by:** Claude Code
**Date:** 2025-11-11
**Branch:** multi-model
**Revision:** a1b2c3d4e5f6
