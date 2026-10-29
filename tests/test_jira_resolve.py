"""Tests for batched Jira parent resolution."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from rt_queue.config import Settings
from rt_queue.github_client import GitHubPullRequest
from rt_queue.jira_client import JiraClient, JiraIssue


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


def _issue(
    key: str,
    *,
    is_subtask: bool = False,
    parent_key: str | None = None,
) -> JiraIssue:
    return JiraIssue(
        key=key,
        issue_id=f"id-{key}",
        summary=key,
        status_name="To Do",
        issue_type_id="1",
        issue_type_name="Task",
        parent_key=parent_key,
        assignee_account_id=None,
        assignee_display_name=None,
        resolution_date=None,
        updated_at=None,
        is_subtask=is_subtask,
    )


def _pull_request(title: str, *, commits: tuple[str, ...] = ()) -> GitHubPullRequest:
    import datetime as dt

    return GitHubPullRequest(
        html_url="https://github.com/org/repo/pull/1",
        number=1,
        title=title,
        body=None,
        head_ref="branch",
        repository_full_name="org/repo",
        author_login="other",
        created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        commit_messages=commits,
    )


class JiraResolveTests(unittest.TestCase):
    """Rank-walk parent resolution with preloaded issues."""

    def setUp(self) -> None:
        self.client = JiraClient(_settings())
        self.client._development_field_discovered = True
        self.client._development_field_id = "customfield_dev"
        self.client._development_field_values["ENG-1"] = (
            '{json={"cachedValue":{"summary":{"pullrequest":{"overall":{"count":0}}}},'
            '"isStale":false}}'
        )
        self.client._development_field_values["ENG-2"] = (
            '{json={"cachedValue":{"summary":{"pullrequest":{"overall":{"count":0}}}},'
            '"isStale":false}}'
        )

    def test_title_fallback_without_second_get(self) -> None:
        """Title fallback uses the preloaded issue only."""
        self.client._issues_by_key["ENG-1"] = _issue("ENG-1")
        pull_request = _pull_request("ENG-1: work")
        with patch.object(
            self.client, "_issue_shows_pull_request", return_value=False
        ):
            with patch.object(self.client, "get_issue") as get_issue_mock:
                parent = self.client.resolve_parent_for_pull_request(pull_request)
        self.assertEqual(parent, "ENG-1")
        get_issue_mock.assert_not_called()

    def test_in_title_non_subtask_wins_over_commit_key(self) -> None:
        """In-title linked non-subtasks beat later non-title keys."""
        self.client._issues_by_key["ENG-1"] = _issue("ENG-1")
        self.client._issues_by_key["ENG-2"] = _issue("ENG-2")
        pull_request = _pull_request(
            "ENG-1 review",
            commits=("ENG-2 in commit",),
        )

        def shows(issue: JiraIssue, _pr: GitHubPullRequest) -> bool:
            return issue.key == "ENG-1"

        with patch.object(self.client, "_issue_shows_pull_request", side_effect=shows):
            parent = self.client.resolve_parent_for_pull_request(pull_request)
        self.assertEqual(parent, "ENG-1")

    def test_subtask_promotion_on_title_fallback(self) -> None:
        """Title fallback promotes subtasks to their parent key."""
        self.client._issues_by_key["ENG-10"] = _issue(
            "ENG-10", is_subtask=True, parent_key="ENG-9"
        )
        self.client._issues_by_key["ENG-9"] = _issue("ENG-9")
        pull_request = _pull_request("ENG-10: task")
        with patch.object(
            self.client, "_issue_shows_pull_request", return_value=False
        ):
            parent = self.client.resolve_parent_for_pull_request(pull_request)
        self.assertEqual(parent, "ENG-9")

    def test_preload_aliases_requested_key_after_get_fallback(self) -> None:
        """GET fallback stores issues under the requested moved key."""
        canonical = _issue("ENG-NEW")

        def preload(key: str) -> None:
            self.client._store_preloaded_issue(
                {
                    "id": "1",
                    "key": canonical.key,
                    "fields": {
                        "summary": "moved",
                        "status": {"name": "To Do"},
                        "issuetype": {"id": "1", "name": "Task", "subtask": False},
                    },
                },
                self.client._development_field_id,
                alias_keys=(key.strip().upper(),),
            )

        with patch.object(
            self.client, "_post_json_with_retry", return_value={"issues": [], "issueErrors": []}
        ):
            with patch.object(self.client, "_preload_issue_via_get", side_effect=preload):
                self.client._post_bulk_fetch(["ENG-OLD"], self.client._bulk_fetch_fields())
        loaded = self.client.get_loaded_issue("ENG-OLD")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.key, "ENG-NEW")
