"""Jira Cloud REST API v3 client for JQL search and issue URLs."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import requests

from rt_queue.config import Settings


@dataclass(frozen=True)
class JiraSubtask:
    """Minimal subtask fields used by the R&T queue."""

    key: str
    summary: str
    status_name: str
    issue_type_id: str
    issue_type_name: str
    parent_key: str | None
    assignee_account_id: str | None


def _auth_header(email: str, api_token: str) -> str:
    raw = f"{email}:{api_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def escape_jql_string(value: str) -> str:
    """
    Escape a string for use inside JQL double quotes.

    Backslashes and double quotes are escaped per JQL string rules.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


class JiraClient:
    """
    Thin wrapper around Jira Cloud REST API v3.

    Issue search uses POST ``/rest/api/3/search/jql`` (enhanced JQL search).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": _auth_header(
                    settings.jira_email, settings.jira_api_token
                ),
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self._base = settings.jira_base_url.rstrip("/")
        self._myself_account_id: str | None = None

    def issue_url(self, key: str) -> str:
        """Browse URL for an issue key."""
        return f"{self._base}/browse/{key}"

    def get_myself_account_id(self) -> str:
        """
        Return the current user's Atlassian account id.

        Uses ``JIRA_ACCOUNT_ID`` when set; otherwise caches ``GET /rest/api/3/myself``.
        """
        if self._settings.jira_account_id:
            return self._settings.jira_account_id
        if self._myself_account_id:
            return self._myself_account_id
        url = f"{self._base}/rest/api/3/myself"
        resp = self._session.get(url, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        account_id = (data.get("accountId") or "").strip()
        if not account_id:
            raise ValueError("Jira /myself response did not include accountId")
        self._myself_account_id = account_id
        return account_id

    def search_jql(self, jql: str) -> list[JiraSubtask]:
        """
        Run JQL and return all matching subtasks (paginated).

        Requests summary, status, assignee, and parent fields.
        """
        url = f"{self._base}/rest/api/3/search/jql"
        page_size = 50
        out: list[JiraSubtask] = []
        next_page_token: str | None = None

        while True:
            body: dict[str, Any] = {
                "jql": jql,
                "maxResults": page_size,
                "fields": ["summary", "status", "assignee", "parent", "issuetype"],
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token
            resp = self._session.post(url, json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            for issue in data.get("issues") or []:
                out.append(self._map_subtask(issue))

            if data.get("isLast") is True:
                break
            new_token = data.get("nextPageToken")
            if not new_token:
                break
            next_page_token = new_token

        return out

    def _map_subtask(self, issue: dict[str, Any]) -> JiraSubtask:
        fields = issue.get("fields") or {}
        key = (issue.get("key") or "").strip()
        summary = (fields.get("summary") or "").strip()
        status_name = ((fields.get("status") or {}).get("name") or "").strip()
        issue_type = fields.get("issuetype") or {}
        issue_type_id = str(issue_type.get("id") or "").strip()
        issue_type_name = (issue_type.get("name") or "").strip()

        parent_key: str | None = None
        parent_raw = fields.get("parent")
        if isinstance(parent_raw, dict):
            parent_key = (parent_raw.get("key") or "").strip() or None

        assignee_account_id: str | None = None
        assignee = fields.get("assignee")
        if isinstance(assignee, dict):
            assignee_account_id = (assignee.get("accountId") or "").strip() or None

        return JiraSubtask(
            key=key,
            summary=summary,
            status_name=status_name,
            issue_type_id=issue_type_id,
            issue_type_name=issue_type_name,
            parent_key=parent_key,
            assignee_account_id=assignee_account_id,
        )

    def is_deploy_subtask(self, subtask: JiraSubtask) -> bool:
        """True when the subtask matches the configured Deploy issue type."""
        settings = self._settings
        if (
            settings.jira_deploy_issue_type_id
            and subtask.issue_type_id == settings.jira_deploy_issue_type_id
        ):
            return True
        if settings.jira_deploy_issue_type_name:
            deploy_name = settings.jira_deploy_issue_type_name.lower()
            if subtask.issue_type_name.lower() == deploy_name:
                return True
        return False


def summary_matches_rt(subtask: JiraSubtask, needle: str) -> bool:
    """True when subtask summary contains ``needle`` (case-insensitive)."""
    if not needle:
        return False
    return needle.lower() in subtask.summary.lower()


def parent_in_project(parent_key: str | None, project_key: str) -> bool:
    """True when ``parent_key`` belongs to ``project_key`` (e.g. PROJ-123)."""
    if not parent_key:
        return False
    prefix = project_key.strip() + "-"
    return parent_key.upper().startswith(prefix.upper())


def status_matches(subtask: JiraSubtask, status_name: str) -> bool:
    """True when issue status equals ``status_name`` (case-insensitive)."""
    return subtask.status_name.strip().lower() == status_name.strip().lower()


def is_done_status(subtask: JiraSubtask) -> bool:
    """True when the issue is in Jira status *Done* (case-insensitive)."""
    return subtask.status_name.strip().lower() == "done"


def assignee_is_current_user(
    subtask: JiraSubtask, current_account_id: str
) -> bool:
    """True when the subtask is assigned to the given account id."""
    if not subtask.assignee_account_id:
        return False
    return subtask.assignee_account_id == current_account_id
