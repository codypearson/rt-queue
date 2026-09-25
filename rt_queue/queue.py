"""Find parent Jira issues ready for Review & Test from GitHub pull requests."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from rt_queue.config import Settings
from rt_queue.github_client import GitHubClient, GitHubPullRequest
from rt_queue.jira_client import (
    JiraClient,
    JiraIssue,
    assignee_is_current_user,
    format_jql_issue_keys,
    is_done_status,
    status_matches,
    status_matches_any,
    summary_matches_keywords,
)


@dataclass(frozen=True)
class ParentReadyForRt:
    """A parent issue that is ready for Review & Test, tied to one pull request.

    Attributes:
        key: The Jira issue key (e.g. PROJ-123).
        assignee: Display name of the parent assignee, or "Unassigned".
        pull_request_url: GitHub pull request URL that sourced this queue entry.
        pull_request_created_at: When the pull request was opened on GitHub.
        parent_updated_at: When the parent issue was last updated in Jira, if known.
    """

    key: str
    assignee: str
    pull_request_url: str
    pull_request_created_at: dt.datetime
    parent_updated_at: dt.datetime | None


# Batch size for ``parent in (...)`` JQL queries.
_PARENT_BATCH_SIZE = 50


def _is_ignored_subtask(subtask: JiraIssue, settings: Settings) -> bool:
    """True when the subtask summary matches a configured ignored keyword group."""
    return summary_matches_keywords(subtask, settings.jira_ignored_summary_keywords)


def _child_belongs_to_parent(subtask: JiraIssue, parent_key: str) -> bool:
    """True when ``subtask`` is a direct child of ``parent_key`` (case-insensitive)."""
    if not subtask.parent_key:
        return False
    return subtask.parent_key.upper() == parent_key.upper()


def _build_sibling_work_jql(parent_keys: list[str]) -> str:
    """JQL for child issues under parents where assignee was ever currentUser()."""
    keys_csv = format_jql_issue_keys(parent_keys)
    return f"parent in ({keys_csv}) AND assignee was currentUser()"


def _build_siblings_jql(parent_keys: list[str]) -> str:
    """JQL for all direct child issues under the given parent keys."""
    keys_csv = format_jql_issue_keys(parent_keys)
    return f"parent in ({keys_csv})"


def _select_rt_subtask_for_parent(
    parent_key: str,
    siblings: list[JiraIssue],
    settings: Settings,
    current_account_id: str,
) -> str | None:
    """
    Return the R&T subtask key for ``parent_key`` when one qualifies, else None.

    The subtask must match configured summary keywords, R&T status, and be
    unassigned or assigned to the current user. Ignored summaries never qualify.
    """
    for subtask in siblings:
        if not _child_belongs_to_parent(subtask, parent_key):
            continue
        if _is_ignored_subtask(subtask, settings):
            continue
        if not summary_matches_keywords(subtask, settings.jira_rt_summary_keywords):
            continue
        if not status_matches_any(subtask, settings.jira_rt_status_names):
            continue
        if subtask.assignee_account_id is not None:
            if not assignee_is_current_user(subtask, current_account_id):
                continue
        return subtask.key
    return None


def siblings_ready_for_rt(
    client: JiraClient,
    settings: Settings,
    parent_key: str,
    rt_subtask_key: str,
    siblings: list[JiraIssue],
) -> bool:
    """
    Return True when every non-Deploy, non-ignored sibling except the R&T
    subtask is Done.

    Deploy subtasks (by issue type or exact summary ``Deploy``) may be in any
    status. Subtasks whose summary matches the ignored keyword groups are
    skipped entirely. The R&T subtask itself is ignored (it is expected to
    be in To Do).
    """
    for subtask in siblings:
        if not _child_belongs_to_parent(subtask, parent_key):
            continue
        if subtask.key == rt_subtask_key:
            continue
        if _is_ignored_subtask(subtask, settings):
            continue
        if client.is_deploy_subtask(subtask):
            continue
        if not is_done_status(subtask):
            return False
    return True


def _parent_excluded_for_worked_on_siblings(
    client: JiraClient,
    settings: Settings,
    parent_key: str,
    rt_subtask_key: str,
    worked_subtasks: list[JiraIssue],
) -> bool:
    """
    Return True when the parent should be excluded due to worked-on sibling history.

    Matches the relaxed rule: completed prior R&T subtasks do not disqualify.
    """
    for historical_subtask in worked_subtasks:
        if not _child_belongs_to_parent(historical_subtask, parent_key):
            continue
        if historical_subtask.key == rt_subtask_key:
            continue
        if _is_ignored_subtask(historical_subtask, settings):
            continue
        if (
            summary_matches_keywords(
                historical_subtask, settings.jira_rt_summary_keywords
            )
            and is_done_status(historical_subtask)
        ):
            continue
        return True
    return False


# Sort parents with unknown Jira ``updated`` after those with a known timestamp.
_SORT_UNKNOWN_JIRA_UPDATED = dt.datetime.max.replace(tzinfo=dt.timezone.utc)


@dataclass(frozen=True)
class QueueBuildResult:
    """Result of building the R&T queue from pull requests."""

    entries: list[ParentReadyForRt]
    skipped_unresolved_pr_count: int


def find_parents_needing_rt(
    jira_client: JiraClient,
    github_client: GitHubClient,
    settings: Settings,
    *,
    exclude_worked_on_siblings: bool = True,
) -> QueueBuildResult:
    """
    Return parent issues ready for Review & Test, one entry per qualifying PR.

    Open, non-draft pull requests in configured GitHub repos (not authored by
    you) are resolved to a parent Jira issue. Parents must be in
    ``jira_parent_status_name`` (default Pending Review) and satisfy the
    existing subtask rules: an R&T subtask in the configured status assigned
    to you or unassigned, all other non-Deploy siblings Done, and optional
    worked-on-sibling exclusion.

    Results are sorted by pull request creation time (oldest first), then parent
    issue updated time (oldest first).
    """
    current_account_id = jira_client.get_myself_account_id()
    pull_requests = github_client.list_open_pull_requests_not_by_me()

    resolved_pairs: list[tuple[str, GitHubPullRequest]] = []
    skipped_unresolved = 0

    for pull_request in pull_requests:
        commit_messages = github_client.list_pull_request_commits(
            pull_request.repository_full_name, pull_request.number
        )
        parent_key = jira_client.resolve_parent_for_pull_request(
            pull_request, commit_messages
        )
        if not parent_key:
            skipped_unresolved += 1
            continue
        resolved_pairs.append((parent_key, pull_request))

    if not resolved_pairs:
        return QueueBuildResult(entries=[], skipped_unresolved_pr_count=skipped_unresolved)

    unique_parent_keys = list(dict.fromkeys(key for key, _ in resolved_pairs))

    parent_issues_by_key: dict[str, JiraIssue] = {}
    for batch_start in range(0, len(unique_parent_keys), _PARENT_BATCH_SIZE):
        batch = unique_parent_keys[batch_start : batch_start + _PARENT_BATCH_SIZE]
        keys_csv = format_jql_issue_keys(batch)
        for parent_issue in jira_client.search_jql(f"key in ({keys_csv})"):
            if parent_issue.key in batch:
                parent_issues_by_key[parent_issue.key] = parent_issue

    qualifying_parent_keys: set[str] = set()
    parent_to_assignee: dict[str, str] = {}
    parent_to_updated_at: dict[str, dt.datetime | None] = {}
    parent_to_rt_key: dict[str, str] = {}

    status_filtered_keys: list[str] = []
    for parent_key in unique_parent_keys:
        parent_issue = parent_issues_by_key.get(parent_key)
        if parent_issue is None:
            continue
        if not status_matches(parent_issue, settings.jira_parent_status_name):
            continue
        status_filtered_keys.append(parent_key)
        display_name = parent_issue.assignee_display_name or "Unassigned"
        parent_to_assignee[parent_key] = display_name
        parent_to_updated_at[parent_key] = parent_issue.updated_at

    if not status_filtered_keys:
        return QueueBuildResult(
            entries=[], skipped_unresolved_pr_count=skipped_unresolved
        )

    excluded_parents: set[str] = set()

    for batch_start in range(0, len(status_filtered_keys), _PARENT_BATCH_SIZE):
        batch = status_filtered_keys[batch_start : batch_start + _PARENT_BATCH_SIZE]
        siblings_jql = _build_siblings_jql(batch)
        all_siblings = jira_client.search_jql(siblings_jql)

        for parent_key in batch:
            rt_key = _select_rt_subtask_for_parent(
                parent_key,
                all_siblings,
                settings,
                current_account_id,
            )
            if rt_key is None:
                excluded_parents.add(parent_key)
                continue
            parent_to_rt_key[parent_key] = rt_key
            if not siblings_ready_for_rt(
                jira_client, settings, parent_key, rt_key, all_siblings
            ):
                excluded_parents.add(parent_key)
                continue

    if exclude_worked_on_siblings:
        eligible_keys = [
            key
            for key in status_filtered_keys
            if key not in excluded_parents and key in parent_to_rt_key
        ]
        for batch_start in range(0, len(eligible_keys), _PARENT_BATCH_SIZE):
            batch = eligible_keys[batch_start : batch_start + _PARENT_BATCH_SIZE]
            worked_subtasks = jira_client.search_jql(_build_sibling_work_jql(batch))
            for parent_key in batch:
                rt_key = parent_to_rt_key[parent_key]
                if _parent_excluded_for_worked_on_siblings(
                    jira_client,
                    settings,
                    parent_key,
                    rt_key,
                    worked_subtasks,
                ):
                    excluded_parents.add(parent_key)

    passing_parents = {
        key
        for key in status_filtered_keys
        if key not in excluded_parents and key in parent_to_rt_key
    }

    results: list[ParentReadyForRt] = []
    for parent_key, pull_request in resolved_pairs:
        if parent_key not in passing_parents:
            continue
        results.append(
            ParentReadyForRt(
                key=parent_key,
                assignee=parent_to_assignee.get(parent_key, "Unassigned"),
                pull_request_url=pull_request.html_url,
                pull_request_created_at=pull_request.created_at,
                parent_updated_at=parent_to_updated_at.get(parent_key),
            )
        )

    results.sort(
        key=lambda item: (
            item.pull_request_created_at,
            item.parent_updated_at or _SORT_UNKNOWN_JIRA_UPDATED,
        )
    )
    return QueueBuildResult(
        entries=results,
        skipped_unresolved_pr_count=skipped_unresolved,
    )
