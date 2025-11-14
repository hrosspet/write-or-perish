# Multi-Model LLM Support – Design & Implementation Plan

This document outlines the work required to add **Multi-Model LLM Support** to the product. Currently, the LLM model is hardcoded (via environment variable `LLM_NAME`). This feature will allow users to:
1. Select from multiple LLM providers and models via a **dropdown menu** for each text node.
2. Have new LLM nodes **inherit the model** from the closest predecessor LLM node.
3. Maintain the naming convention where **assistants are named by their model name**.

---

## 1  User-Experience Changes

### 1.1  Model Selection Dropdown
A **standalone dropdown menu** is displayed next to the **"LLM Response"** button, allowing users to select the model before generating a response.

**Supported Models (in dropdown):**
- **OpenAI:**
  - GPT-5 (default for new responses)
- **Anthropic Claude:**
  - Claude 4.5 Sonnet
  - Claude 4.1 Opus
  - Claude 3 Opus

**Note:** GPT-4.5 Preview is a legacy model that will NOT appear in the dropdown. It will only be set for existing LLM nodes during migration to preserve historical data.

**UX Layout:**
```
[Model Dropdown ▼] [LLM Response]
```

**UX Flow:**
1. Dropdown is visible next to the **"LLM Response"** button at all times
2. The dropdown is **pre-selected** to:
   - The model used by the most recent LLM node in the thread ancestry
   - Or the system default (GPT-5) if no predecessor LLM node exists
3. User can change model selection via the dropdown
4. User clicks **"LLM Response"** button to generate response with selected model
5. System generates response using the selected model
6. The new LLM node displays the model name as its username

### 1.2  Model Display
- Each LLM node's footer displays the model name as the username (e.g., "gpt-5", "claude-sonnet-4.5")
- This maintains the current convention where assistants are named by their model
- Users can visually track which model generated which response in a thread

### 1.3  Mobile & Accessibility
- Dropdown should be accessible via keyboard navigation
- Touch-friendly on mobile devices
- Clear visual indication of selected model

---

## 2  Data Model & Storage

### 2.1  Database Schema Changes
Add a new column to the `nodes` table:

```sql
ALTER TABLE nodes ADD COLUMN llm_model VARCHAR(64) NULL;
```

**Field Details:**
- `llm_model` (VARCHAR(64), nullable): Stores the model identifier used to generate this node
- Only populated for nodes with `node_type='llm'`
- NULL for user-created nodes (where `node_type='user'`)
- Existing LLM nodes will be populated with "gpt-4.5-preview" during migration (for historical tracking only)
- Examples for new nodes: "gpt-5", "claude-sonnet-4.5", "claude-opus-4.1", "claude-opus-3"
- Examples for legacy nodes: "gpt-4.5-preview"

### 2.2  Model Identifier Mapping
Internal model identifiers map to API model strings:

**Available Models (in dropdown):**
| Display Name | Internal ID | OpenAI API Model | Anthropic API Model | Notes |
|--------------|-------------|------------------|---------------------|-------|
| GPT-5 | `gpt-5` | `gpt-5` | N/A | Default for new responses |
| Claude 4.5 Sonnet | `claude-sonnet-4.5` | N/A | `claude-sonnet-4-5-20250929` | |
| Claude 4.1 Opus | `claude-opus-4.1` | N/A | `claude-opus-4-1-20250514` | |
| Claude 3 Opus | `claude-opus-3` | N/A | `claude-3-opus-20240229` | |

**Legacy Models (migration only, NOT in dropdown):**
| Display Name | Internal ID | Notes |
|--------------|-------------|-------|
| GPT-4.5 Preview | `gpt-4.5-preview` | Set for existing LLM nodes during migration; no longer available for new responses |

### 2.3  Migration Strategy
- During migration, all existing LLM nodes (`node_type='llm'`) will have `llm_model` set to "gpt-4.5-preview"
- User-created nodes (where `node_type='user'`) will have `llm_model` remain NULL
- The column is nullable by design to distinguish between user nodes (NULL) and LLM nodes (non-NULL)
- Default model for new LLM responses: "gpt-5"
- "gpt-4.5-preview" will NOT be shown in the dropdown menu (legacy only)

---

## 3  Backend API Changes

### 3.1  Endpoint Modification: `/nodes/:id/llm`

**Current Behavior:**
```python
@nodes_bp.route("/<int:node_id>/llm", methods=["POST"])
def request_llm_response(node_id):
    # Uses model_name = os.environ.get("LLM_NAME")
```

**New Behavior:**
```python
@nodes_bp.route("/<int:node_id>/llm", methods=["POST"])
def request_llm_response(node_id):
    """
    Request body (JSON):
    {
        "model": "gpt-5" | "claude-sonnet-4.5" | "claude-opus-4.1" | "claude-opus-3"
    }
    """
```

**Implementation Steps:**
1. Accept `model` parameter in request JSON body
2. Validate model is in supported list
3. Determine provider (OpenAI vs Anthropic) based on model prefix
4. Initialize appropriate API client based on provider
5. Store selected `model` in the new `llm_model` column
6. Create LLM user with username matching the model identifier

### 3.2  New Endpoint: `/nodes/:id/suggested-model`

**Purpose:** Return the suggested model for a new LLM response based on the thread's context.

```
GET /nodes/:id/suggested-model

Response:
{
    "suggested_model": "claude-sonnet-4.5",
    "source": "predecessor" | "default"
}
```

**Logic:**
1. Walk up the thread ancestry from the given node
2. Find the most recent node with `node_type='llm'` AND `llm_model` IS NOT NULL
3. If found AND the model is in the supported models list, return that model as `suggested_model` with `source='predecessor'`
4. If the model is "gpt-4.5-preview" (legacy), return "gpt-5" instead with `source='default'`
5. If no predecessor found, return system default ("gpt-5") with `source='default'`

### 3.3  Configuration Updates

**Environment Variables:**
```bash
# Add new API key for Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Keep existing for backward compatibility
OPENAI_API_KEY=sk-...

# Legacy model name (kept for backward compatibility with existing nodes)
LLM_NAME=gpt-5  # Default model when none specified
```

**Config.py Updates:**
```python
class Config:
    # ... existing config ...

    # API Keys
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

    # Default model (for backward compatibility and fallback)
    DEFAULT_LLM_MODEL = os.environ.get("LLM_NAME", "gpt-5")

    # Supported models configuration
    SUPPORTED_MODELS = {
        "gpt-5": {
            "provider": "openai",
            "api_model": "gpt-5",
            "display_name": "GPT-5"
        },
        "claude-sonnet-4.5": {
            "provider": "anthropic",
            "api_model": "claude-sonnet-4-5-20250929",
            "display_name": "Claude 4.5 Sonnet"
        },
        "claude-opus-4.1": {
            "provider": "anthropic",
            "api_model": "claude-opus-4-1-20250514",
            "display_name": "Claude 4.1 Opus"
        },
        "claude-opus-3": {
            "provider": "anthropic",
            "api_model": "claude-3-opus-20240229",
            "display_name": "Claude 3 Opus"
        }
    }
```

---

## 4  LLM Provider Integration

### 4.1  Provider Abstraction Layer
Create a unified interface for calling different LLM providers:

```python
# backend/llm_providers.py

from anthropic import Anthropic
from openai import OpenAI

class LLMProvider:
    @staticmethod
    def get_completion(model_id: str, messages: list, api_keys: dict) -> dict:
        """
        Returns: {
            "content": str,  # The generated text
            "total_tokens": int  # Total tokens used
        }
        """
        config = current_app.config["SUPPORTED_MODELS"].get(model_id)
        if not config:
            raise ValueError(f"Unsupported model: {model_id}")

        provider = config["provider"]
        api_model = config["api_model"]

        if provider == "openai":
            return LLMProvider._call_openai(api_model, messages, api_keys["openai"])
        elif provider == "anthropic":
            return LLMProvider._call_anthropic(api_model, messages, api_keys["anthropic"])
        else:
            raise ValueError(f"Unknown provider: {provider}")

    @staticmethod
    def _call_openai(model: str, messages: list, api_key: str) -> dict:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=1,
            max_completion_tokens=10000,
        )
        return {
            "content": response.choices[0].message.content,
            "total_tokens": response.usage.total_tokens
        }

    @staticmethod
    def _call_anthropic(model: str, messages: list, api_key: str) -> dict:
        """
        Convert OpenAI-style messages to Anthropic format and make API call.
        Anthropic uses a different message format:
        - System messages go in a separate 'system' parameter
        - Messages must alternate between 'user' and 'assistant'
        """
        client = Anthropic(api_key=api_key)

        # Extract system messages
        system_messages = [m for m in messages if m.get("role") == "system"]
        system_text = "\n\n".join([m["content"][0]["text"] for m in system_messages if m.get("content")])

        # Convert remaining messages to Anthropic format
        anthropic_messages = []
        for msg in messages:
            if msg["role"] in ["user", "assistant"]:
                content = msg["content"]
                # Convert content format if needed
                if isinstance(content, list) and len(content) > 0:
                    if isinstance(content[0], dict) and "text" in content[0]:
                        content = content[0]["text"]
                anthropic_messages.append({
                    "role": msg["role"],
                    "content": content
                })

        # Make API call
        response = client.messages.create(
            model=model,
            max_tokens=10000,
            system=system_text if system_text else None,
            messages=anthropic_messages
        )

        # Extract text content from response
        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        # Calculate total tokens (Anthropic reports input/output separately)
        total_tokens = response.usage.input_tokens + response.usage.output_tokens

        return {
            "content": content,
            "total_tokens": total_tokens
        }
```

### 4.2  Message Format Compatibility
- **OpenAI:** Uses `messages` array with `role` and `content`
- **Anthropic:** Uses `messages` array but requires alternating user/assistant messages and separate `system` parameter
- The provider layer handles format conversion automatically

### 4.3  Error Handling
- API key missing: Return 500 with clear error message
- Model not available: Return 400 with list of supported models
- Rate limiting: Return 429 with retry-after header
- API errors: Log full error, return sanitized message to user

---

## 5  Frontend Changes (React)

### 5.1  New Component: ModelSelector

**Location:** `frontend/src/components/ModelSelector.js`

```jsx
// Standalone dropdown component for selecting LLM model
// Displays inline next to the "LLM Response" button
const ModelSelector = ({ nodeId, selectedModel, onModelChange }) => {
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Fetch suggested model on mount
    api.get(`/nodes/${nodeId}/suggested-model`)
      .then(response => {
        onModelChange(response.data.suggested_model);
        setLoading(false);
      })
      .catch(() => {
        onModelChange("gpt-5"); // Fallback to default
        setLoading(false);
      });
  }, [nodeId, onModelChange]);

  const models = [
    { id: "gpt-5", name: "GPT-5", provider: "OpenAI" },
    { id: "claude-sonnet-4.5", name: "Claude 4.5 Sonnet", provider: "Anthropic" },
    { id: "claude-opus-4.1", name: "Claude 4.1 Opus", provider: "Anthropic" },
    { id: "claude-opus-3", name: "Claude 3 Opus", provider: "Anthropic" },
  ];

  return (
    <select
      className="model-selector-dropdown"
      value={selectedModel || "gpt-5"}
      onChange={(e) => onModelChange(e.target.value)}
      disabled={loading}
      style={{ marginRight: "8px" }}
    >
      {models.map(m => (
        <option key={m.id} value={m.id}>
          {m.name} ({m.provider})
        </option>
      ))}
    </select>
  );
};
```

**Note:** This is a simple dropdown (not a modal) that displays inline next to the "LLM Response" button.

### 5.2  NodeDetail.js Updates

**Current Flow:**
```jsx
const handleLLMResponse = () => {
  api.post(`/nodes/${id}/llm`)
    .then(response => navigate(`/node/${response.data.node.id}`))
    .catch(err => setError("Error requesting LLM response."));
};
```

**Updated Flow:**
```jsx
const [selectedModel, setSelectedModel] = useState("gpt-5");

const handleLLMResponse = () => {
  api.post(`/nodes/${id}/llm`, { model: selectedModel })
    .then(response => navigate(`/node/${response.data.node.id}`))
    .catch(err => setError("Error requesting LLM response."));
};
```

**UI Changes:**
```jsx
<div style={{ marginTop: "8px", display: 'flex', alignItems: 'center', gap: '8px' }}>
  <button onClick={() => setShowChildFormOverlay(true)}>Add Text</button>

  {/* NEW: Model selector dropdown */}
  <ModelSelector
    nodeId={node.id}
    selectedModel={selectedModel}
    onModelChange={setSelectedModel}
  />

  <button onClick={handleLLMResponse}>LLM Response</button>
  {isOwner && <button onClick={() => setShowEditOverlay(true)}>Edit</button>}
  {isOwner && <button onClick={handleDelete}>Delete</button>}
  <SpeakerIcon nodeId={node.id} />
</div>
```

**Summary:**
- Add `selectedModel` state to track the currently selected model
- Place `ModelSelector` dropdown inline between "Add Text" and "LLM Response" buttons
- Pass selected model to API when "LLM Response" is clicked

### 5.3  Styling
- Inline dropdown that matches existing button styling
- Clear visual hierarchy with model name and provider in parentheses
- Adequate padding and font size for readability
- Loading state with disabled appearance while fetching suggested model
- Mobile-friendly touch targets

---

## 6  Migration Strategy

### 6.1  Database Migration

**Migration File:** `backend/migrations/versions/XXXX_add_llm_model_to_nodes.py`

```python
"""Add llm_model column to nodes table

Revision ID: XXXX
"""
from alembic import op
import sqlalchemy as sa

def upgrade():
    # Add llm_model column (nullable for backward compatibility)
    op.add_column('node', sa.Column('llm_model', sa.String(64), nullable=True))

def downgrade():
    op.drop_column('node', 'llm_model')
```

### 6.2  Data Migration for Existing Nodes
Populate `llm_model` for all existing LLM nodes with "gpt-4.5-preview":

```python
def upgrade():
    # Add column
    op.add_column('node', sa.Column('llm_model', sa.String(64), nullable=True))

    # Populate existing LLM nodes with "gpt-4.5-preview" (the legacy model)
    # User-created nodes (node_type != 'llm') will remain NULL
    op.execute("""
        UPDATE node
        SET llm_model = 'gpt-4.5-preview'
        WHERE node_type = 'llm' AND llm_model IS NULL
    """)
```

**Important:** All existing LLM responses were generated with gpt-4.5-preview, so this migration ensures historical accuracy. The column remains nullable so that user-created nodes (where `node_type='user'`) have NULL as their `llm_model` value.

### 6.3  Rollback Plan
- If rollback is needed, the downgrade migration will drop the `llm_model` column
- Backend will fall back to using `LLM_NAME` environment variable if the column doesn't exist
- Frontend can be deployed independently (will send model parameter; old backend will ignore it)
- User-created nodes always have NULL `llm_model`, so the nullable design is crucial

---

## 7  Testing Plan

### 7.1  Unit Tests

**Backend Tests:**
- Test model selection API with each supported model
- Test suggested model endpoint with various thread structures
- Test provider abstraction layer (OpenAI and Anthropic)
- Test message format conversion for Anthropic
- Test error handling (invalid model, missing API key)
- Test backward compatibility (NULL llm_model)

**Frontend Tests:**
- Test ModelSelector component rendering
- Test model suggestion fetching
- Test model selection and submission
- Test dropdown interaction and keyboard navigation

### 7.2  Integration Tests
- Create LLM response with GPT-5
- Create LLM response with each Claude model
- Verify model inheritance (child node suggests parent's model)
- Verify username matches model identifier
- Test thread with mixed models (GPT-5 → Claude → GPT-5)

### 7.3  Manual Testing Checklist
- [ ] Model dropdown appears on "LLM Response" click
- [ ] Suggested model is pre-selected correctly
- [ ] Each model generates responses successfully
- [ ] Model name appears as username in node footer
- [ ] Second LLM node in thread suggests first LLM's model
- [ ] Backward compatibility: existing LLM nodes still work
- [ ] Error messages are clear when API keys missing
- [ ] Mobile/touch interface works smoothly
- [ ] Keyboard navigation works in dropdown

---

## 8  Security & Privacy

### 8.1  API Key Management
- Store API keys in environment variables (never in code or database)
- Use separate keys for development and production
- Rotate keys regularly
- Monitor API usage for each provider

### 8.2  Input Validation
- Validate model selection against whitelist
- Sanitize all user inputs before sending to LLM APIs
- Prevent injection attacks via thread content

### 8.3  Rate Limiting
- Apply rate limits per user and per model
- Track API costs per model provider
- Alert when approaching quota limits

### 8.4  Data Privacy
- Update Terms of Service to mention multiple LLM providers
- Clarify that content is sent to third-party APIs (OpenAI, Anthropic)
- Ensure GDPR compliance for data sent to AI providers

---

## 9  Cost Management

### 9.1  Token Accounting
- Track tokens per provider separately
- Update dashboard statistics to show breakdown by model
- Add cost estimation based on provider pricing

### 9.2  Model Pricing (as of 2025)
Estimated pricing per 1M tokens:

| Model | Input Cost | Output Cost |
|-------|------------|-------------|
| GPT-5 | $TBD | $TBD |
| Claude 4.5 Sonnet | $3.00 | $15.00 |
| Claude 4.1 Opus | $15.00 | $75.00 |
| Claude 3 Opus | $15.00 | $75.00 |

*Note: Prices are estimates and should be verified with current provider pricing*

### 9.3  Cost Optimization
- Set per-user monthly quotas by model tier
- Consider cheaper models as defaults for free users
- Implement caching for repeated similar prompts
- Add warnings when selecting expensive models

---

## 10  Rollout Plan

### Phase 1: Backend Foundation (Week 1)
1. Add `llm_model` column to database schema
2. Implement provider abstraction layer
3. Create `/nodes/:id/suggested-model` endpoint
4. Update `/nodes/:id/llm` to accept model parameter
5. Add Anthropic API integration
6. Write unit tests for backend changes

### Phase 2: Frontend Integration (Week 2)
1. Create ModelSelector component
2. Update NodeDetail.js to show model selector
3. Add styling and animations
4. Implement suggested model fetching
5. Write frontend unit tests

### Phase 3: Testing & Refinement (Week 3)
1. Integration testing with all models
2. Manual QA testing
3. Performance testing (API response times)
4. Mobile/accessibility testing
5. Fix bugs and polish UX

### Phase 4: Deployment (Week 4)
1. Deploy database migration
2. Deploy backend changes
3. Deploy frontend changes
4. Monitor API usage and errors
5. Gather user feedback

### Phase 5: Iteration (Ongoing)
1. Add more models based on user demand
2. Optimize costs based on usage patterns
3. Improve model recommendation algorithm
4. Add model performance analytics

---

## 11  Future Enhancements

### 11.1  Advanced Features (Future Versions)
- **Model Comparison:** Allow users to generate responses from multiple models simultaneously
- **Model Analytics:** Show stats on which models produce better responses (thumbs up/down)
- **Custom Parameters:** Allow users to adjust temperature, max tokens, etc.
- **Model Profiles:** Save preferred models per user or per thread topic
- **Cost Display:** Show estimated cost before generating response

### 11.2  Additional Models
- GPT-4 Turbo (for cost-conscious users)
- Claude 3.5 Sonnet (if still relevant)
- Google Gemini models
- Open-source models (Llama, Mistral) via API

### 11.3  Smart Model Selection
- Auto-suggest model based on:
  - Thread complexity (longer threads → more capable models)
  - Content type (code → GPT-5, creative writing → Claude)
  - User history and preferences
  - Cost vs. quality tradeoff

---

## 12  Dependencies

### 12.1  Backend Dependencies
```bash
# Add to backend/requirements.txt
anthropic>=0.18.0  # Anthropic Python SDK
```

### 12.2  Environment Variables
```bash
# Required for full functionality
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Optional (for backward compatibility)
LLM_NAME=gpt-5
```

### 12.3  Configuration Updates
- Update `backend/config.py` with model configuration
- Update deployment docs with new environment variables
- Update README with model selection feature

---

## 13  Open Questions

1. **Default Model Selection:** Should the default model be configurable per deployment, or always GPT-5?
2. **Model Availability:** How should we handle cases where a model becomes unavailable or deprecated?
3. **Thread Mixing:** Should we allow or discourage mixing models within a single thread?
4. **Cost Transparency:** Should users see estimated costs before generating responses?
5. **Model Capabilities:** Should we guide users on which models are best for different tasks?
6. **Legacy Data:** Should we backfill `llm_model` for existing LLM nodes, or leave as NULL?

---

## 14  Success Metrics

### 14.1  Functional Metrics
- All models generate responses successfully (>99% success rate)
- Model inheritance works correctly (100% of child nodes suggest parent model)
- No regression in existing functionality

### 14.2  User Engagement
- Percentage of users trying multiple models
- Most popular model choices
- User satisfaction with model quality

### 14.3  Technical Metrics
- API response time per provider
- Error rates by provider
- Cost per response by model
- API quota usage

---

Prepared for branch **multi-model-llm-support** – v1.0
