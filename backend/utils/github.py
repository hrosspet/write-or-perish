"""GitHub API utility for creating issues from Voice mode."""
import requests
from flask import current_app


def create_github_issue(title, description, category, username):
    """Create a GitHub issue with auto-applied labels.

    Args:
        title: Issue title.
        description: Issue body (markdown).
        category: One of 'bug', 'feature', 'enhancement'.
        username: Loore username for per-user label.

    Returns:
        dict with 'url' and 'number' on success.

    Raises:
        ValueError: If config is missing.
        RuntimeError: If the GitHub API call fails.
    """
    token = current_app.config.get("GITHUB_TOKEN")
    repo = current_app.config.get("GITHUB_REPO")

    if not token:
        raise ValueError("GITHUB_TOKEN is not configured")
    if not repo:
        raise ValueError("GITHUB_REPO is not configured")

    labels = ["loore", category, f"loore:{username}"]

    response = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        },
        json={
            "title": title,
            "body": description,
            "labels": labels,
        },
        timeout=15,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"GitHub API error {response.status_code}: "
            f"{response.text[:200]}"
        )

    data = response.json()
    return {
        "url": data["html_url"],
        "number": data["number"],
    }
