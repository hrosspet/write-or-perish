# Backend Tests

This directory contains unit tests for the Write or Perish backend, with a focus on the privacy features implementation.

## Running Tests

Tests require the full backend environment with all dependencies installed:

```bash
# Activate your conda/virtual environment first
conda activate write-or-perish

# Install dependencies
pip install -r backend/requirements.txt

# Run all tests
pytest backend/tests/ -v

# Run specific test file
pytest backend/tests/test_privacy_utils.py -v

# Run with coverage
pytest backend/tests/ --cov=backend/utils/privacy --cov-report=html
```

## Test Files

- `test_privacy_utils.py` - Tests for privacy utility functions (validation, authorization, AI usage checks)
- `test_node_privacy.py` - Tests for Node model privacy behavior
- `test_profile_privacy.py` - Tests for UserProfile model privacy behavior

## Test Coverage

The tests cover:

1. **Privacy Enums** - Verify PrivacyLevel and AIUsage enum values
2. **Validation Functions** - Test validate_privacy_level() and validate_ai_usage()
3. **Authorization** - Test can_user_access_node() with various privacy levels
4. **AI Usage Permissions** - Test can_ai_use_node_for_chat() and can_ai_use_node_for_training()
5. **Default Settings** - Verify default privacy settings for nodes and profiles
6. **Edge Cases** - Test missing attributes, unauthenticated users, etc.

## CI/CD Integration

These tests are automatically run by the GitHub Actions CI pipeline on:
- All pull requests
- Pushes to main branch
- Manual workflow dispatch

See `.github/workflows/ci.yml` for the full CI configuration.
