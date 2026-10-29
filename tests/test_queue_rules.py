"""Tests for sibling readiness rules (unchanged behavior)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from rt_queue.config import Settings
from rt_queue.jira_client import JiraClient, JiraIssue
from rt_queue.queue import siblings_ready_for_rt


def _settings() -> Settings:
    return Settings(
        github_token="gh",
        github_repositories=("org/repo",),
        github_login="me",
        jira_base_url="https://example.atlassian.net",
        jira_email="a@b.com",
        jira_api_token="tok",
        jira_parent_status_name="Pending Review",
        jira_rt_summary_keywords=(("review", "test"),),
        jira_ignored_summary_keywords=(("stakeholder", "review"),),
        jira_rt_status_names=("To Do",),
        jira_account_id="acc",
        jira_deploy_issue_type_name="Deploy",
        jira_deploy_issue_type_id=None,
    )


class QueueRulesTests(unittest.TestCase):
    """Sibling gating for Review & Test."""

    def test_deploy_subtask_any_status(self) -> None:
        """Deploy siblings do not block the queue."""
        client = JiraClient(_settings())
        client.is_deploy_subtask = MagicMock(return_value=True)
        siblings = [
            JiraIssue(
                key="P-1",
                issue_id="1",
                summary="Review & Test",
                status_name="To Do",
                issue_type_id="2",
                issue_type_name="Sub-task",
                parent_key="PARENT-1",
                assignee_account_id=None,
                assignee_display_name=None,
                resolution_date=None,
                updated_at=None,
                is_subtask=True,
            ),
            JiraIssue(
                key="P-2",
                issue_id="2",
                summary="Deploy",
                status_name="In Progress",
                issue_type_id="3",
                issue_type_name="Deploy",
                parent_key="PARENT-1",
                assignee_account_id=None,
                assignee_display_name=None,
                resolution_date=None,
                updated_at=None,
                is_subtask=True,
            ),
        ]
        self.assertTrue(
            siblings_ready_for_rt(
                client, _settings(), "PARENT-1", "P-1", siblings
            )
        )
