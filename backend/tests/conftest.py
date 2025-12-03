"""Pytest configuration for backend tests.

These are isolated unit tests that don't require a full Flask app or database.
They test the privacy utility functions in isolation.
"""

# Each test file handles its own mocking and imports
# to avoid triggering the full backend initialization
