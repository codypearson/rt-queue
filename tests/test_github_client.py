"""Tests for GitHub GraphQL client behavior."""

from __future__ import annotations

import unittest

from rt_queue.github_client import GitHubClient


class GitHubGraphqlErrorTests(unittest.TestCase):
    """GraphQL permission errors."""

    def test_commit_forbidden_detection(self) -> None:
        """Detect commit permission errors from GraphQL payloads."""
        error_text = (
            '[{"message": "Resource not accessible by personal access token", '
            '"path": ["repository", "pullRequests", "nodes", 0, "commits"]}]'
        )
        self.assertTrue(GitHubClient._graphql_error_is_commit_forbidden(error_text))
