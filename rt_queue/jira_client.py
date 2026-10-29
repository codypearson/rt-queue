"""Jira Cloud REST API v3 client for JQL search and issue URLs."""

from __future__ import annotations

import base64
import datetime as dt
import re
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import requests

from rt_queue.api_datetime import parse_api_datetime
from rt_queue.config import Settings, SummaryKeywordGroups
from rt_queue.github_client import GitHubPullRequest
from rt_queue.jira_dev_summary import (
    can_skip_dev_status_detail,
    discover_development_field_id,
)
from rt_queue.single_flight import SingleFlightCache

# Exact summary (case/whitespace-insensitive) treated as a Deploy sibling
# when the issue was created as a Sub-task instead of the Deploy type.
DEPLOY_SUMMARY = "deploy"

# Status name used when resolving who moved an issue to Done from changelog history.
DONE_STATUS_NAME = "done"

# Maximum issue keys per ``POST /rest/api/3/changelog/bulkfetch`` request.
_BULK_CHANGELOG_MAX_ISSUES = 1000

# Shaped bulk issue fetch limits (see Jira ``/rest/api/3/issue/bulkfetch`` docs).
_BULK_ISSUE_FETCH_MAX = 1000
_BULK_ISSUE_FETCH_FALLBACK = 100
_BULK_ISSUE_FIELD_NAMES = (
    "summary",
    "status",
    "assignee",
    "parent",
    "issuetype",
    "resolutiondate",
    "updated",
)
_JQL_PAGE_SIZE = 100
# Concurrent Jira reads. High enough that the per-issue probe fan-out overlaps
# in a handful of round trips; 429/503 responses are retried.
_PROBE_POOL_SIZE = 32

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
    elif pull_request.commit_messages:
        parts.extend(pull_request.commit_messages)

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
        self._issues_by_key: dict[str, JiraIssue] = {}
        self._development_field_values: dict[str, Any] = {}
        self._development_field_id: str | None = None
        self._development_field_discovered = False
        self._thread_local = threading.local()
        self._cache_lock = threading.Lock()
        self._dev_detail_cache: SingleFlightCache[str, dict[str, Any] | None] = (
            SingleFlightCache()
        )
        self._remote_links_cache: SingleFlightCache[str, list[dict[str, Any]]] = (
            SingleFlightCache()
        )
        self._probe_pool = ThreadPoolExecutor(max_workers=_PROBE_POOL_SIZE)

    def _http(self) -> requests.Session:
        """Return a requests session private to the current thread."""
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._session.headers)
            self._thread_local.session = session
        return session

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """
        Send an HTTP request, retrying rate-limit and unavailable responses.

        Each attempt uses the calling thread's session so concurrent probes do
        not share one connection pool.
        """
        kwargs.setdefault("timeout", 60)
        response: requests.Response | None = None
        for attempt in range(4):
            response = self._http().request(method, url, **kwargs)
            if response.status_code not in (429, 503) or attempt == 3:
                return response
            time.sleep(0.25 * (2**attempt))
        if response is None:
            raise RuntimeError(f"Jira {method} {url} produced no response")
        return response

    def _run_parallel(self, tasks: list[Callable[[], None]]) -> None:
        """Run independent tasks on the probe pool and raise the first failure."""
        if not tasks:
            return
        futures = [self._probe_pool.submit(task) for task in tasks]
        first_error: BaseException | None = None
        for future in futures:
            try:
                future.result()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def prepare_for_queue(self) -> str:
        """
        Load the account id and Development field id concurrently.

        Returns the current user's Atlassian account id.
        """
        field_future = self._probe_pool.submit(self._ensure_development_field_id)
        account_future = self._probe_pool.submit(self.get_myself_account_id)
        field_future.result()
        account_id = account_future.result()
        if not isinstance(account_id, str):
            raise ValueError("Jira account id was not a string")
        return account_id

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
        resp = self._send("GET", url)
        resp.raise_for_status()
        data = resp.json()
        account_id = (data.get("accountId") or "").strip()
        if not account_id:
            raise ValueError("Jira /myself response did not include accountId")
        self._myself_account_id = account_id
        return account_id

    def get_loaded_issue(self, key: str) -> JiraIssue | None:
        """Return a preloaded issue by key, or None when it was not loaded."""
        normalized = key.strip().upper()
        if not normalized:
            return None
        return self._issues_by_key.get(normalized)

    def load_issues_for_keys(self, keys: Iterable[str]) -> None:
        """
        Bulk-load issues for the given keys and parent keys of loaded subtasks.

        Missing or invalid keys are skipped without failing the run.
        """
        normalized_keys = self._normalize_issue_keys(keys)
        self._bulk_fetch_missing_keys(normalized_keys)
        self._bulk_fetch_parent_issues_for_subtasks()

    def ensure_issues_loaded(self, keys: Iterable[str]) -> None:
        """Bulk-fetch any of ``keys`` not already present in the preload cache."""
        normalized_keys = self._normalize_issue_keys(keys)
        missing = [
            key
            for key in normalized_keys
            if key not in self._issues_by_key
        ]
        if missing:
            self._bulk_fetch_missing_keys(missing)

    def _normalize_issue_keys(self, keys: Iterable[str]) -> list[str]:
        """Return de-duplicated uppercase issue keys preserving first-seen order."""
        seen: set[str] = set()
        ordered: list[str] = []
        for raw_key in keys:
            normalized = raw_key.strip().upper()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    def _ensure_development_field_id(self) -> str | None:
        """Discover and cache the Development custom field id once per client."""
        if self._development_field_discovered:
            return self._development_field_id
        with self._cache_lock:
            if self._development_field_discovered:
                return self._development_field_id
        url = f"{self._base}/rest/api/3/field"
        resp = self._send("GET", url)
        resp.raise_for_status()
        payload = resp.json()
        fields_list = payload if isinstance(payload, list) else []
        with self._cache_lock:
            self._development_field_id = discover_development_field_id(fields_list)
            self._development_field_discovered = True
            return self._development_field_id

    def _bulk_fetch_fields(self) -> list[str]:
        """Return explicit bulkfetch field names, including Development when known."""
        field_names = list(_BULK_ISSUE_FIELD_NAMES)
        development_field_id = self._ensure_development_field_id()
        if development_field_id:
            field_names.append(development_field_id)
        return field_names

    def _bulk_fetch_missing_keys(self, keys: list[str]) -> None:
        """POST bulkfetch for keys not already cached."""
        if not keys:
            return
        pending = [key for key in keys if key not in self._issues_by_key]
        if not pending:
            return

        field_names = self._bulk_fetch_fields()
        batch_size = _BULK_ISSUE_FETCH_MAX
        for batch_start in range(0, len(pending), batch_size):
            batch = pending[batch_start : batch_start + batch_size]
            try:
                self._post_bulk_fetch(batch, field_names)
            except requests.HTTPError as exc:
                if (
                    exc.response is not None
                    and exc.response.status_code == 400
                    and batch_size > _BULK_ISSUE_FETCH_FALLBACK
                ):
                    for small_start in range(
                        0, len(batch), _BULK_ISSUE_FETCH_FALLBACK
                    ):
                        small_batch = batch[
                            small_start : small_start + _BULK_ISSUE_FETCH_FALLBACK
                        ]
                        self._post_bulk_fetch(small_batch, field_names)
                else:
                    raise

    def _post_bulk_fetch(self, keys: list[str], field_names: list[str]) -> None:
        """Execute one bulkfetch request, retrying once on retriable errors."""
        url = f"{self._base}/rest/api/3/issue/bulkfetch"
        body = {
            "issueIdsOrKeys": keys,
            "fields": field_names,
            "fieldsByKeys": False,
        }
        data = self._post_json_with_retry(url, body)
        development_field_id = self._development_field_id
        for issue_json in data.get("issues") or []:
            if isinstance(issue_json, dict):
                self._store_preloaded_issue(issue_json, development_field_id)
        for issue_error in data.get("issueErrors") or []:
            if not isinstance(issue_error, dict):
                continue
            message = (
                issue_error.get("errorMessage")
                or issue_error.get("message")
                or ""
            ).strip()
            if self._issue_error_is_not_found(message):
                continue
            raise ValueError(
                f"Jira bulkfetch failed for an issue: {message or issue_error}"
            )

        missing_keys = [
            requested_key
            for requested_key in keys
            if requested_key.strip().upper() not in self._issues_by_key
        ]
        self._run_parallel(
            [
                (
                    lambda requested_key=requested_key: self._preload_issue_via_get(
                        requested_key
                    )
                )
                for requested_key in missing_keys
            ]
        )

    def _preload_issue_via_get(self, key: str) -> None:
        """
        Load one issue through GET when bulkfetch did not map the requested key.

        Bulkfetch may resolve moved keys to a different ``issue.key`` than the
        key that was requested. GET by the requested key preserves lookup by
        text extracted from pull requests.
        """
        development_field_id = self._ensure_development_field_id()
        field_names = list(_BULK_ISSUE_FIELD_NAMES)
        if development_field_id:
            field_names.append(development_field_id)
        fields_param = ",".join(field_names)
        url = f"{self._base}/rest/api/3/issue/{key}"
        resp = self._send("GET", url, params={"fields": fields_param})
        if resp.status_code == 404:
            return
        resp.raise_for_status()
        issue_json = resp.json()
        if not isinstance(issue_json, dict):
            return
        self._store_preloaded_issue(
            issue_json,
            development_field_id,
            alias_keys=(key.strip().upper(),),
        )

    def _post_json_with_retry(
        self, url: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """POST JSON to Jira, retrying once on HTTP errors."""
        last_error: requests.HTTPError | None = None
        for _attempt in range(2):
            resp = self._send("POST", url, json=body, timeout=120)
            if resp.ok:
                payload = resp.json()
                if isinstance(payload, dict):
                    return payload
                raise ValueError("Jira response was not a JSON object")
            last_error = requests.HTTPError(
                f"{resp.status_code} Client Error for {url}", response=resp
            )
            if resp.status_code >= 500:
                continue
            resp.raise_for_status()
        if last_error is not None:
            raise last_error
        raise RuntimeError("Jira POST failed without a response")

    @staticmethod
    def _issue_error_is_not_found(message: str) -> bool:
        """True when a bulkfetch issue error indicates a missing issue key."""
        lowered = message.lower()
        return "not found" in lowered or "does not exist" in lowered

    def _store_preloaded_issue(
        self,
        issue_json: dict[str, Any],
        development_field_id: str | None,
        *,
        alias_keys: tuple[str, ...] = (),
    ) -> None:
        """Map and cache one issue payload under its key and optional aliases."""
        issue = self._map_issue(issue_json)
        if not issue.key:
            return
        normalized_key = issue.key.upper()
        dev_value: Any = None
        if development_field_id:
            fields = issue_json.get("fields") or {}
            if isinstance(fields, dict):
                dev_value = fields.get(development_field_id)
        with self._cache_lock:
            self._issues_by_key[normalized_key] = issue
            if development_field_id:
                self._development_field_values[normalized_key] = dev_value
            for alias in alias_keys:
                alias_normalized = alias.strip().upper()
                if not alias_normalized:
                    continue
                self._issues_by_key[alias_normalized] = issue
                if development_field_id:
                    self._development_field_values[alias_normalized] = dev_value

    def _bulk_fetch_parent_issues_for_subtasks(self) -> None:
        """Bulk-load parent issues for any cached subtasks missing parents."""
        parent_keys: list[str] = []
        for issue in self._issues_by_key.values():
            if not issue.is_subtask or not issue.parent_key:
                continue
            parent_normalized = issue.parent_key.upper()
            if parent_normalized not in self._issues_by_key:
                parent_keys.append(parent_normalized)
        if parent_keys:
            self._bulk_fetch_missing_keys(self._normalize_issue_keys(parent_keys))

    def get_issue(self, key: str) -> JiraIssue | None:
        """
        Fetch a single issue by key.

        Returns None when the issue does not exist (404).
        """
        url = f"{self._base}/rest/api/3/issue/{key}"
        resp = self._send(
            "GET",
            url,
            params={
                "fields": (
                    "summary,status,assignee,parent,issuetype,resolutiondate,updated"
                )
            },
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._map_issue(resp.json())

    def prefetch_link_evidence_for_keys(self, keys: Iterable[str]) -> None:
        """
        Load development-panel detail and remote links for preloaded candidates.

        One HTTP call is issued per distinct issue, and those calls run together.
        Later parent resolution reads the cached payloads.
        """
        tasks: list[Callable[[], None]] = []
        seen: set[str] = set()
        for key in self._normalize_issue_keys(keys):
            issue = self.get_loaded_issue(key)
            if issue is None:
                continue
            identity = (issue.issue_id or issue.key).upper()
            if identity in seen:
                continue
            seen.add(identity)
            tasks.append(
                lambda issue_key=issue.key: self._load_remote_links(issue_key)
            )
            if self._dev_detail_required(issue):
                tasks.append(lambda issue=issue: self._load_dev_detail(issue))
        self._run_parallel(tasks)

    def _dev_detail_required(self, issue: JiraIssue) -> bool:
        """True when dev-status detail must be fetched for ``issue``."""
        if not issue.issue_id:
            return False
        field_value = self._development_field_values.get(issue.key.upper())
        return not can_skip_dev_status_detail(field_value)

    def _load_remote_links(self, issue_key: str) -> None:
        """Populate the remote-link cache for one issue key."""
        normalized_key = issue_key.strip().upper()
        if not normalized_key:
            return
        self._remote_links_cache.get_or_fetch(
            normalized_key,
            lambda: self._fetch_remote_links(normalized_key),
        )

    def _load_dev_detail(self, issue: JiraIssue) -> None:
        """Populate the dev-status detail cache for one issue."""
        if not issue.issue_id:
            return
        issue_id = issue.issue_id
        self._dev_detail_cache.get_or_fetch(
            issue_id,
            lambda: self._fetch_dev_detail_payload(issue),
        )

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

        Requires issues to be preloaded via ``load_issues_for_keys``.
        """
        if commit_messages is None:
            message_list: list[str] | None = None
        else:
            message_list = commit_messages

        candidate_keys = extract_jira_keys_from_pull_request(
            pull_request, message_list
        )
        title_keys = extract_jira_keys(pull_request.title)
        title_key_set = set(title_keys)

        linked_parent = self._resolve_linked_parent_by_rank(
            pull_request,
            candidate_keys,
            title_key_set,
        )
        if linked_parent is not None:
            return linked_parent

        title_fallback = extract_first_jira_key_from_title(pull_request.title)
        if title_fallback is None:
            return None
        issue = self.get_loaded_issue(title_fallback)
        if issue is None:
            return None
        return self._parent_key_for_issue(issue)

    def _resolve_linked_parent_by_rank(
        self,
        pull_request: GitHubPullRequest,
        candidate_keys: tuple[str, ...],
        title_key_set: set[str],
    ) -> str | None:
        """
        Walk candidate keys in rank order and return the first linked parent.

        Rank groups mirror the legacy sort on ``(in_title, is_subtask)`` with
        early exit after a matching in-title subtask (non-title keys skipped).
        """
        rank_groups: tuple[tuple[bool, bool], ...] = (
            (True, False),
            (True, True),
            (False, False),
            (False, True),
        )
        for in_title, is_subtask in rank_groups:
            for key in candidate_keys:
                issue = self.get_loaded_issue(key)
                if issue is None:
                    continue
                if (issue.key.upper() in title_key_set) != in_title:
                    continue
                if issue.is_subtask != is_subtask:
                    continue
                if not self._issue_shows_pull_request(issue, pull_request):
                    continue
                return self._parent_key_for_issue(issue)
        return None

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

        Skips the detail endpoint when the Development field summary is fresh
        and reports zero pull requests.
        """
        if not self._dev_detail_required(issue):
            return False
        issue_id = issue.issue_id
        if issue_id is None:
            return False
        payload = self._dev_detail_cache.get_or_fetch(
            issue_id,
            lambda: self._fetch_dev_detail_payload(issue),
        )
        if not isinstance(payload, dict):
            return False
        return self._dev_status_payload_matches_pr(payload, pull_request)

    def _fetch_dev_detail_payload(self, issue: JiraIssue) -> dict[str, Any] | None:
        """Call dev-status detail and return the JSON payload, or None."""
        url = f"{self._base}/rest/dev-status/1.0/issue/detail"
        params = {
            "issueId": issue.issue_id,
            "applicationType": "github",
            "dataType": "pullrequest",
        }
        resp = self._send("GET", url, params=params)
        if resp.status_code in (403, 404) or not resp.ok:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return data

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
        normalized_key = issue_key.strip().upper()
        if not normalized_key:
            return False
        links = self._remote_links_cache.get_or_fetch(
            normalized_key,
            lambda: self._fetch_remote_links(normalized_key),
        )
        return self._remote_links_payload_includes_pull_request(links, pull_request)

    def _fetch_remote_links(self, issue_key: str) -> list[dict[str, Any]]:
        """Fetch remote web links for one issue (uncached HTTP)."""
        url = f"{self._base}/rest/api/3/issue/{issue_key}/remotelink"
        resp = self._send("GET", url)
        if resp.status_code == 404:
            return []
        if not resp.ok:
            return []
        try:
            links = resp.json()
        except ValueError:
            return []
        if not isinstance(links, list):
            return []
        return [link for link in links if isinstance(link, dict)]

    def _remote_links_payload_includes_pull_request(
        self, links: list[dict[str, Any]], pull_request: GitHubPullRequest
    ) -> bool:
        """Return True when ``links`` includes the pull request URL."""
        pr_url = pull_request.html_url
        pr_url_alt = f"{pr_url}/"

        for link in links:
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
        page_size = _JQL_PAGE_SIZE
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
            resp = self._send("POST", url, json=body)
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

    def get_latest_done_by_for_issues(
        self, issues: list[JiraIssue]
    ) -> dict[str, str]:
        """
        Return who last moved each issue to Done, keyed by issue key.

        Uses bulk status changelog fetch. Issues without a matching history
        entry map to ``Unknown``.
        """
        if not issues:
            return {}

        key_to_issue_id: dict[str, str] = {}
        for issue in issues:
            if issue.issue_id:
                key_to_issue_id[issue.key] = issue.issue_id

        issue_ids = list(dict.fromkeys(key_to_issue_id.values()))
        histories_by_issue_id = self._fetch_status_changelogs_by_issue_id(issue_ids)

        done_by_by_key: dict[str, str] = {}
        for issue_key, issue_id in key_to_issue_id.items():
            histories = histories_by_issue_id.get(issue_id, [])
            done_by_by_key[issue_key] = latest_done_by_from_changelog_histories(
                histories
            )

        for issue in issues:
            if issue.key not in done_by_by_key:
                done_by_by_key[issue.key] = "Unknown"

        return done_by_by_key

    def _fetch_status_changelogs_by_issue_id(
        self, issue_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Fetch status field changelogs for issue ids (paginated, merged per issue).

        Returns a map of Jira issue id to merged ``changeHistories`` entries.
        """
        merged: dict[str, list[dict[str, Any]]] = {}
        if not issue_ids:
            return merged

        url = f"{self._base}/rest/api/3/changelog/bulkfetch"
        for batch_start in range(0, len(issue_ids), _BULK_CHANGELOG_MAX_ISSUES):
            batch = issue_ids[batch_start : batch_start + _BULK_CHANGELOG_MAX_ISSUES]
            next_page_token: str | None = None

            while True:
                body: dict[str, Any] = {
                    "issueIdsOrKeys": batch,
                    "fieldIds": ["status"],
                    "maxResults": 1000,
                }
                if next_page_token:
                    body["nextPageToken"] = next_page_token

                resp = self._send("POST", url, json=body, timeout=120)
                resp.raise_for_status()
                data = resp.json()

                for issue_log in data.get("issueChangeLogs") or []:
                    if not isinstance(issue_log, dict):
                        continue
                    issue_id = str(issue_log.get("issueId") or "").strip()
                    if not issue_id:
                        continue
                    histories = issue_log.get("changeHistories") or []
                    if not isinstance(histories, list):
                        continue
                    merged.setdefault(issue_id, []).extend(histories)

                next_page_token = data.get("nextPageToken")
                if not next_page_token:
                    break

        return merged

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


def _changelog_created_at(created: Any) -> dt.datetime | None:
    """
    Parse a changelog ``created`` value.

    Jira bulk changelog often returns Unix epoch milliseconds as a number or
    digit string; other endpoints may use seconds or ISO-8601 strings.
    """
    if created is None:
        return None

    numeric_value: float | None = None
    if isinstance(created, (int, float)):
        numeric_value = float(created)
    elif isinstance(created, str):
        stripped = created.strip()
        if stripped.isdigit():
            numeric_value = float(stripped)

    if numeric_value is not None:
        try:
            epoch_seconds = numeric_value
            # Values above ~year 5138 in seconds are millisecond timestamps.
            if epoch_seconds > 1e11:
                epoch_seconds = epoch_seconds / 1000.0
            return dt.datetime.fromtimestamp(epoch_seconds, tz=dt.timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    return parse_api_datetime(created)


def _change_item_transitions_to_done(item: dict[str, Any]) -> bool:
    """True when a changelog item is a status change into Done."""
    field = (item.get("field") or "").strip().lower()
    field_id = (item.get("fieldId") or "").strip().lower()
    if field != "status" and field_id != "status":
        return False
    to_string = (item.get("toString") or "").strip().lower()
    return to_string == DONE_STATUS_NAME


def _history_transitions_to_done(history: dict[str, Any]) -> bool:
    """True when a changelog history entry includes a transition to Done."""
    items = history.get("items")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict) and _change_item_transitions_to_done(item)
        for item in items
    )


def _author_display_name_from_history(history: dict[str, Any]) -> str | None:
    """Return the changelog author's display name when present."""
    author = history.get("author")
    if not isinstance(author, dict):
        return None
    display_name = (author.get("displayName") or "").strip()
    return display_name or None


def latest_done_by_from_changelog_histories(
    histories: list[dict[str, Any]],
) -> str:
    """
    Return the display name of who last moved the issue to Done.

    When the issue was reopened, the latest transition to Done wins. Returns
    ``Unknown`` when no Done transition exists or the author has no display name.
    """
    best_created: dt.datetime | None = None
    best_author: str | None = None

    for history in histories:
        if not isinstance(history, dict):
            continue
        if not _history_transitions_to_done(history):
            continue
        created_at = _changelog_created_at(history.get("created"))
        if created_at is None:
            continue
        if best_created is None or created_at > best_created:
            best_created = created_at
            best_author = _author_display_name_from_history(history)

    if best_author:
        return best_author
    return "Unknown"
