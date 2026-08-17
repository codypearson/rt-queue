"""Find parent Jira issues ready for Review & Test."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from rt_queue.config import RtKeywordGroup, Settings
from rt_queue.jira_client import (
    JiraClient,
    JiraIssue,
    assignee_is_current_user,
    escape_jql_string,
    is_done_status,
    parent_in_project,
    status_matches,
    summary_matches_rt,
)


@dataclass(frozen=True)
class ParentReadyForRt:
    """A parent issue that is ready for Review & Test.

    Attributes:
        key: The Jira issue key (e.g. PROJ-123).
        assignee: Display name of the parent assignee, or "Unassigned".
        last_subtask_completed: The most recent resolution date/time among the
            parent's subtasks, or None if none had a resolution date.
    """

    key: str
    assignee: str
    last_subtask_completed: dt.datetime | None

# Batch size for ``parent in (...)`` JQL queries.
_PARENT_BATCH_SIZE = 50


def _keyword_group_to_jql(group: RtKeywordGroup) -> str:
    """JQL for one keyword group: every keyword must appear in the summary."""
    clauses = " AND ".join(
        f'summary ~ "{escape_jql_string(keyword)}"' for keyword in group
    )
    if len(group) > 1:
        return f"({clauses})"
    return clauses


def _build_rt_subtasks_jql(settings: Settings) -> str:
    """JQL for R&T subtasks in To Do, unassigned or assigned to current user.

    Keyword groups are OR'd; keywords within a group are AND'd. A subtask
    matches when every keyword in at least one group appears in the summary
    (e.g. Review & Test, Code Review, or Stakeholder Review).
    """
    project_key = escape_jql_string(settings.jira_project_key)
    status_name = escape_jql_string(settings.jira_rt_status_name)
    group_clauses = [
        _keyword_group_to_jql(group)
        for group in settings.jira_rt_summary_keywords
        if group
    ]
    summary_clauses = " OR ".join(group_clauses)
    if len(group_clauses) > 1:
        summary_clauses = f"({summary_clauses})"
    return (
        f'project = "{project_key}" '
        f"AND issuetype in subTaskIssueTypes() "
        f"AND {summary_clauses} "
        f'AND status = "{status_name}" '
        f"AND (assignee is EMPTY OR assignee = currentUser())"
    )


def _build_sibling_work_jql(parent_keys: list[str]) -> str:
    """JQL for subtasks under parents where assignee was ever currentUser()."""
    keys_csv = ", ".join(parent_keys)
    return (
        f"parent in ({keys_csv}) "
        f"AND issuetype in subTaskIssueTypes() "
        f"AND assignee was currentUser()"
    )


def _build_siblings_jql(parent_keys: list[str]) -> str:
    """JQL for all subtasks under the given parent keys."""
    keys_csv = ", ".join(parent_keys)
    return (
        f"parent in ({keys_csv}) AND issuetype in subTaskIssueTypes()"
    )


def siblings_ready_for_rt(
    client: JiraClient,
    parent_key: str,
    rt_subtask_key: str,
    siblings: list[JiraIssue],
) -> bool:
    """
    Return True when every non-Deploy sibling except the R&T subtask is Done.

    Deploy subtasks may be in any status. The R&T subtask itself is ignored
    (it is expected to be in To Do).
    """
    for subtask in siblings:
        if subtask.parent_key != parent_key:
            continue
        if subtask.key == rt_subtask_key:
            continue
        if client.is_deploy_subtask(subtask):
            continue
        if not is_done_status(subtask):
            return False
    return True


def find_parents_needing_rt(
    client: JiraClient,
    settings: Settings,
    *,
    exclude_worked_on_siblings: bool = True,
) -> list[ParentReadyForRt]:
    """
    Return parent issues ready for Review & Test.

    Parents are included when they have an R&T subtask matching configured
    summary/status/assignee rules and every other non-Deploy subtask is Done.

    When ``exclude_worked_on_siblings`` is True (default), parents are excluded
    if the current user was ever assignee on any other subtask under that parent.
    This exclusion is relaxed for historical assignments to Review & Test
    subtasks that are now in "Done" status: completing a prior R&T subtask
    on a parent does not disqualify the parent for a subsequent R&T subtask.

    Results include the parent assignee and the date/time the last of its
    subtasks was completed. Results are sorted by last completed subtask
    date/time, oldest first.
    """
    current_account_id = client.get_myself_account_id()
    rt_jql = _build_rt_subtasks_jql(settings)
    rt_subtasks = client.search_jql(rt_jql)

    parent_to_rt_key: dict[str, str] = {}
    for subtask in rt_subtasks:
        if not summary_matches_rt(subtask, settings.jira_rt_summary_keywords):
            continue
        if not status_matches(subtask, settings.jira_rt_status_name):
            continue
        if not parent_in_project(subtask.parent_key, settings.jira_project_key):
            continue
        if subtask.assignee_account_id is not None:
            if not assignee_is_current_user(subtask, current_account_id):
                continue

        parent_key = subtask.parent_key
        if not parent_key:
            continue
        if parent_key not in parent_to_rt_key:
            parent_to_rt_key[parent_key] = subtask.key

    if not parent_to_rt_key:
        return []

    excluded_parents: set[str] = set()
    parent_keys = list(parent_to_rt_key.keys())

    if exclude_worked_on_siblings:
        for batch_start in range(0, len(parent_keys), _PARENT_BATCH_SIZE):
            batch = parent_keys[batch_start : batch_start + _PARENT_BATCH_SIZE]
            sibling_jql = _build_sibling_work_jql(batch)
            worked_subtasks = client.search_jql(sibling_jql)

            for historical_subtask in worked_subtasks:
                parent_key = historical_subtask.parent_key
                if not parent_key or parent_key not in parent_to_rt_key:
                    continue
                rt_subtask_key = parent_to_rt_key[parent_key]
                if historical_subtask.key == rt_subtask_key:
                    continue

                # Relaxed worked-on-siblings rule:
                # Ignore historical assignments to *completed* Review & Test subtasks.
                # Previously, any assignment (via "assignee was currentUser()") on a
                # sibling subtask would exclude the parent. Now we allow a fresh R&T
                # subtask on a parent when prior R&T work on the same parent has been
                # completed (status "Done").
                if (
                    summary_matches_rt(
                        historical_subtask, settings.jira_rt_summary_keywords
                    )
                    and is_done_status(historical_subtask)
                ):
                    continue

                excluded_parents.add(parent_key)

    # Map from parent key to the most recent resolution date among its subtasks.
    parent_to_last_completed: dict[str, dt.datetime | None] = {}

    for batch_start in range(0, len(parent_keys), _PARENT_BATCH_SIZE):
        batch = parent_keys[batch_start : batch_start + _PARENT_BATCH_SIZE]
        siblings_jql = _build_siblings_jql(batch)
        all_siblings = client.search_jql(siblings_jql)

        for parent_key in batch:
            if parent_key in excluded_parents:
                continue
            rt_key = parent_to_rt_key[parent_key]
            if not siblings_ready_for_rt(
                client, parent_key, rt_key, all_siblings
            ):
                excluded_parents.add(parent_key)
                continue

            # Determine the latest completion date across all siblings for this parent.
            max_completed: dt.datetime | None = None
            for sibling in all_siblings:
                if sibling.parent_key != parent_key:
                    continue
                if sibling.resolution_date is None:
                    continue
                if max_completed is None or sibling.resolution_date > max_completed:
                    max_completed = sibling.resolution_date
            parent_to_last_completed[parent_key] = max_completed

    final_parent_keys = [
        key for key in parent_keys if key not in excluded_parents
    ]

    # Fetch assignee display names for the final parents (batched).
    parent_to_assignee: dict[str, str] = {}
    for batch_start in range(0, len(final_parent_keys), _PARENT_BATCH_SIZE):
        batch = final_parent_keys[batch_start : batch_start + _PARENT_BATCH_SIZE]
        if not batch:
            continue
        keys_csv = ", ".join(batch)
        parent_jql = f"key in ({keys_csv})"
        parent_issues = client.search_jql(parent_jql)
        for parent_issue in parent_issues:
            if parent_issue.key in batch:
                display_name = parent_issue.assignee_display_name or "Unassigned"
                parent_to_assignee[parent_issue.key] = display_name

    # Build results and sort by last completed subtask date (oldest first).
    # Parents with no resolution date (None) are placed after those with dates.
    results: list[ParentReadyForRt] = []
    for key in final_parent_keys:
        assignee = parent_to_assignee.get(key, "Unassigned")
        last_completed = parent_to_last_completed.get(key)
        results.append(
            ParentReadyForRt(
                key=key,
                assignee=assignee,
                last_subtask_completed=last_completed,
            )
        )

    results.sort(key=lambda item: (item.last_subtask_completed is None, item.last_subtask_completed))
    return results
