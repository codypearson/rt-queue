"""Jira Cloud REST API v3 client for JQL search and issue URLs."""

from __future__ import annotations

import base64
import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

import requests

from rt_queue.api_datetime import parse_api_datetime
from rt_queue.config import Settings, SummaryKeywordGroups
from rt_queue.github_client import GitHubPullRequest

# Exact summary (case/whitespace-insensitive) treated as a Deploy sibling
# when the issue was created as a Sub-task instead of the Deploy type.
DEPLOY_SUMMARY = "deploy"

# Jira issue keys: PROJECT-123 (project key starts with a letter).
JIRA_KEY_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z0-9]+-\d+)\b")
# PR titles like "Eng3 378" without a hyphen.
JIRA_KEY_SPACE_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z0-9]+)\s+(\d+)\b")


@dataclass(frozen=True)
class JiraIssue:
    """Minimal issue fields used by the R&T queue.

    Used for both subtasks and parent issues.
    """

    key: str
    issue_id: str | None
    summary: str
    status_name: str
    issue_type_id: str
    issue_type_name: str
    parent_key: str | None
    assignee_account_id: str | None
    assignee_display_name: str | None
    resolution_date: dt.datetime | None
    updated_at: dt.datetime | None
    is_subtask: bool


def _auth_header(email: str, api_token: str) -> str:
    raw = f"{email}:{api_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def escape_jql_string(value: str) -> str:
    """
    Escape a string for use inside JQL double quotes.

    Backslashes and double quotes are escaped per JQL string rules.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def extract_jira_keys(text: str) -> tuple[str, ...]:
    """
    Extract unique Jira issue keys from ``text`` in first-seen order.

    Keys are normalized to uppercase. Matches hyphenated keys (``PROJ-123``)
    and space-separated project keys in titles (``Eng3 378`` → ``ENG3-378``).
    """
    if not text:
        return ()
    seen: set[str] = set()
    ordered: list[str] = []

    def add_key(raw_key: str) -> None:
        key = raw_key.upper()
        if key not in seen:
            seen.add(key)
            ordered.append(key)

    for match in JIRA_KEY_PATTERN.finditer(text):
        add_key(match.group(1))
    for match in JIRA_KEY_SPACE_PATTERN.finditer(text):
        add_key(f"{match.group(1)}-{match.group(2)}")
    return tuple(ordered)


def format_jql_issue_keys(keys: list[str]) -> str:
    """Format issue keys for a JQL ``in`` clause with quoting and escaping."""
    return ", ".join(f'"{escape_jql_string(key)}"' for key in keys)


def extract_jira_keys_from_pull_request(
    pull_request: GitHubPullRequest,
    commit_messages: list[str] | None = None,
) -> tuple[str, ...]:
    """
    Collect candidate Jira keys from PR title, body, branch, and commit messages.

    Order preserves first-seen across those sources (title first).
    """
    parts: list[str] = [pull_request.title]
    if pull_request.body:
        parts.append(pull_request.body)
    if pull_request.head_ref:
        parts.append(pull_request.head_ref)
    if commit_messages:
        parts.extend(commit_messages)

    combined = "\n".join(parts)
    return extract_jira_keys(combined)


def extract_first_jira_key_from_title(title: str) -> str | None:
    """Return the first Jira key in ``title``, or None."""
    keys = extract_jira_keys(title)
    return keys[0] if keys else None


def _normalize_pr_url(url: str) -> str:
    """Normalize a GitHub PR URL for comparison (no trailing slash, lower host)."""
    u = url.strip().rstrip("/")
    return u


def _pr_urls_match(link_url: str, pr_html_url: str) -> bool:
    """True when ``link_url`` refers to the same pull request as ``pr_html_url``."""
    return _normalize_pr_url(link_url) == _normalize_pr_url(pr_html_url)


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

    def get_issue(self, key: str) -> JiraIssue | None:
        """
        Fetch a single issue by key.

        Returns None when the issue does not exist (404).
        """
        url = f"{self._base}/rest/api/3/issue/{key}"
        resp = self._session.get(
            url,
            params={
                "fields": (
                    "summary,status,assignee,parent,issuetype,resolutiondate,updated"
                )
            },
            timeout=60,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._map_issue(resp.json())

    def resolve_parent_for_pull_request(
        self,
        pull_request: GitHubPullRequest,
        commit_messages: list[str] | None = None,
    ) -> str | None:
        """
        Resolve the parent Jira issue key for a GitHub pull request.

        Prefer an issue that shows this PR in Jira (development panel or remote
        link). Among matches, prefer a key appearing in the PR title, then a
        non-subtask. If no issue shows the PR, fall back to the first key in
        the title only. Subtask matches are promoted to their parent key.
        Returns None when no parent can be resolved.
        """
        candidate_keys = extract_jira_keys_from_pull_request(
            pull_request, commit_messages
        )
        title_keys = extract_jira_keys(pull_request.title)
        title_key_set = set(title_keys)

        linked_matches: list[tuple[JiraIssue, bool, bool]] = []

        for key in candidate_keys:
            issue = self.get_issue(key)
            if issue is None:
                continue
            if not self._issue_shows_pull_request(issue, pull_request):
                continue
            in_title = issue.key.upper() in title_key_set
            linked_matches.append((issue, in_title, issue.is_subtask))

        if linked_matches:
            linked_matches.sort(
                key=lambda item: (
                    not item[1],
                    item[2],
                )
            )
            return self._parent_key_for_issue(linked_matches[0][0])

        title_fallback = extract_first_jira_key_from_title(pull_request.title)
        if title_fallback is None:
            return None
        issue = self.get_issue(title_fallback)
        if issue is None:
            return None
        return self._parent_key_for_issue(issue)

    def _parent_key_for_issue(self, issue: JiraIssue) -> str:
        """Return the parent issue key, walking up from a subtask when needed."""
        if issue.is_subtask and issue.parent_key:
            return issue.parent_key
        return issue.key

    def _issue_shows_pull_request(
        self, issue: JiraIssue, pull_request: GitHubPullRequest
    ) -> bool:
        """True when Jira lists this PR on the issue (dev panel or remote link)."""
        if self._dev_panel_has_pull_request(issue, pull_request):
            return True
        return self._remote_links_include_pull_request(issue.key, pull_request)

    def _dev_panel_has_pull_request(
        self, issue: JiraIssue, pull_request: GitHubPullRequest
    ) -> bool:
        """
        Check Jira development status for a linked GitHub pull request.

        Uses ``/rest/dev-status/1.0/issue/detail``. Returns False on 403/404
        or when the issue has no id.
        """
        if not issue.issue_id:
            return False
        url = f"{self._base}/rest/dev-status/1.0/issue/detail"
        params = {
            "issueId": issue.issue_id,
            "applicationType": "github",
            "dataType": "pullrequest",
        }
        resp = self._session.get(url, params=params, timeout=60)
        if resp.status_code in (403, 404):
            return False
        if not resp.ok:
            return False
        try:
            data = resp.json()
        except ValueError:
            return False
        return self._dev_status_payload_matches_pr(data, pull_request)

    def _dev_status_payload_matches_pr(
        self, data: Any, pull_request: GitHubPullRequest
    ) -> bool:
        """Walk dev-status JSON for a matching repo and pull number."""
        if not isinstance(data, dict):
            return False
        detail_list = data.get("detail")
        if not isinstance(detail_list, list):
            return False

        repo_lower = pull_request.repository_full_name.lower()
        pr_number = pull_request.number

        for detail in detail_list:
            if not isinstance(detail, dict):
                continue
            pull_requests = detail.get("pullRequests")
            if not isinstance(pull_requests, list):
                continue
            for pr_entry in pull_requests:
                if not isinstance(pr_entry, dict):
                    continue
                entry_number = pr_entry.get("id") or pr_entry.get("number")
                if entry_number is not None:
                    try:
                        if int(entry_number) != pr_number:
                            continue
                    except (TypeError, ValueError):
                        continue
                else:
                    continue

                url = (pr_entry.get("url") or "").strip()
                if url and _pr_urls_match(url, pull_request.html_url):
                    return True

                repository = pr_entry.get("repository") or {}
                if isinstance(repository, dict):
                    repo_name = (repository.get("name") or "").strip()
                    owner = repository.get("owner") or {}
                    owner_name = ""
                    if isinstance(owner, dict):
                        owner_name = (owner.get("name") or owner.get("login") or "").strip()
                    full = f"{owner_name}/{repo_name}".lower() if owner_name and repo_name else ""
                    if full == repo_lower:
                        return True

                name_field = (pr_entry.get("name") or "").lower()
                if repo_lower in name_field and str(pr_number) in name_field:
                    return True

        return False

    def _remote_links_include_pull_request(
        self, issue_key: str, pull_request: GitHubPullRequest
    ) -> bool:
        """True when remote links on the issue include this PR URL."""
        url = f"{self._base}/rest/api/3/issue/{issue_key}/remotelink"
        resp = self._session.get(url, timeout=60)
        if resp.status_code == 404:
            return False
        if not resp.ok:
            return False
        try:
            links = resp.json()
        except ValueError:
            return False
        if not isinstance(links, list):
            return False

        pr_url = pull_request.html_url
        pr_url_alt = f"{pr_url}/"

        for link in links:
            if not isinstance(link, dict):
                continue
            object_info = link.get("object") or {}
            if not isinstance(object_info, dict):
                continue
            link_url = (object_info.get("url") or "").strip()
            if not link_url:
                continue
            if _pr_urls_match(link_url, pr_url) or _pr_urls_match(link_url, pr_url_alt):
                return True
        return False

    def search_jql(self, jql: str) -> list[JiraIssue]:
        """
        Run JQL and return all matching issues (paginated).

        Requests summary, status, assignee, parent, issuetype, resolutiondate, and updated.
        """
        url = f"{self._base}/rest/api/3/search/jql"
        page_size = 50
        out: list[JiraIssue] = []
        next_page_token: str | None = None

        while True:
            body: dict[str, Any] = {
                "jql": jql,
                "maxResults": page_size,
                "fields": [
                    "summary",
                    "status",
                    "assignee",
                    "parent",
                    "issuetype",
                    "resolutiondate",
                    "updated",
                ],
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token
            resp = self._session.post(url, json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            for issue in data.get("issues") or []:
                out.append(self._map_issue(issue))

            if data.get("isLast") is True:
                break
            new_token = data.get("nextPageToken")
            if not new_token:
                break
            next_page_token = new_token

        return out

    def _map_issue(self, issue: dict[str, Any]) -> JiraIssue:
        fields = issue.get("fields") or {}
        key = (issue.get("key") or "").strip()
        issue_id = str(issue.get("id") or "").strip() or None
        summary = (fields.get("summary") or "").strip()
        status_name = ((fields.get("status") or {}).get("name") or "").strip()
        issue_type = fields.get("issuetype") or {}
        issue_type_id = str(issue_type.get("id") or "").strip()
        issue_type_name = (issue_type.get("name") or "").strip()
        is_subtask = bool(issue_type.get("subtask"))

        parent_key: str | None = None
        parent_raw = fields.get("parent")
        if isinstance(parent_raw, dict):
            parent_key = (parent_raw.get("key") or "").strip() or None

        assignee_account_id: str | None = None
        assignee_display_name: str | None = None
        assignee = fields.get("assignee")
        if isinstance(assignee, dict):
            assignee_account_id = (assignee.get("accountId") or "").strip() or None
            assignee_display_name = (assignee.get("displayName") or "").strip() or None

        resolution_date = parse_api_datetime(fields.get("resolutiondate"))
        updated_at = parse_api_datetime(fields.get("updated"))

        return JiraIssue(
            key=key,
            issue_id=issue_id,
            summary=summary,
            status_name=status_name,
            issue_type_id=issue_type_id,
            issue_type_name=issue_type_name,
            parent_key=parent_key,
            assignee_account_id=assignee_account_id,
            assignee_display_name=assignee_display_name,
            resolution_date=resolution_date,
            updated_at=updated_at,
            is_subtask=is_subtask,
        )

    def is_deploy_subtask(self, issue: JiraIssue) -> bool:
        """True when the issue is a Deploy sibling.

        Matches the configured Deploy issue type (name or id), or a summary
        that is exactly ``Deploy`` (case/whitespace-insensitive). The summary
        fallback covers Deploys created as a Sub-task instead of the
        Deploy type.
        """
        settings = self._settings
        if (
            settings.jira_deploy_issue_type_id
            and issue.issue_type_id == settings.jira_deploy_issue_type_id
        ):
            return True
        if settings.jira_deploy_issue_type_name:
            deploy_name = settings.jira_deploy_issue_type_name.lower()
            if issue.issue_type_name.lower() == deploy_name:
                return True
        if issue.summary.strip().lower() == DEPLOY_SUMMARY:
            return True
        return False


def summary_matches_keywords(
    issue: JiraIssue, keyword_groups: SummaryKeywordGroups
) -> bool:
    """
    True when the issue summary matches any keyword group (case-insensitive).

    Within a group, every keyword must appear as a substring. Groups are OR'd,
    so ``(("review", "test"), ("code", "review"))`` matches summaries such as
    ``Review & Test`` or ``Code Review``.
    """
    if not keyword_groups:
        return False
    summary_lower = issue.summary.lower()
    return any(
        all(keyword.lower() in summary_lower for keyword in group)
        for group in keyword_groups
        if group
    )


def status_matches(issue: JiraIssue, status_name: str) -> bool:
    """True when issue status equals ``status_name`` (case-insensitive)."""
    return issue.status_name.strip().lower() == status_name.strip().lower()


def status_matches_any(issue: JiraIssue, status_names: tuple[str, ...]) -> bool:
    """True when issue status equals any of ``status_names`` (case-insensitive)."""
    if not status_names:
        return False
    return any(status_matches(issue, name) for name in status_names)


def is_done_status(issue: JiraIssue) -> bool:
    """True when the issue is in Jira status *Done* (case-insensitive)."""
    return issue.status_name.strip().lower() == "done"


def assignee_is_current_user(issue: JiraIssue, current_account_id: str) -> bool:
    """True when the issue is assigned to the given account id."""
    if not issue.assignee_account_id:
        return False
    return issue.assignee_account_id == current_account_id
