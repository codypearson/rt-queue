"""Find parent Jira issues ready for Review & Test."""

from __future__ import annotations

from rt_queue.config import Settings
from rt_queue.jira_client import (
    JiraClient,
    JiraSubtask,
    assignee_is_current_user,
    escape_jql_string,
    is_done_status,
    parent_in_project,
    status_matches,
    summary_matches_rt,
)

# Batch size for ``parent in (...)`` JQL queries.
_PARENT_BATCH_SIZE = 50


def _build_rt_subtasks_jql(settings: Settings) -> str:
    """JQL for R&T subtasks in To Do, unassigned or assigned to current user."""
    project_key = escape_jql_string(settings.jira_project_key)
    rt_contains = escape_jql_string(settings.jira_rt_summary_contains)
    status_name = escape_jql_string(settings.jira_rt_status_name)
    return (
        f'project = "{project_key}" '
        f"AND issuetype in subTaskIssueTypes() "
        f'AND summary ~ "{rt_contains}" '
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
    siblings: list[JiraSubtask],
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
) -> list[str]:
    """
    Return parent issue keys ready for Review & Test.

    Parents are included when they have an R&T subtask matching configured
    summary/status/assignee rules and every other non-Deploy subtask is Done.

    When ``exclude_worked_on_siblings`` is True (default), parents are excluded
    if the current user was ever assignee on any other subtask under that parent.
    This exclusion is relaxed for historical assignments to Review & Test
    subtasks that are now in "Done" status: completing a prior R&T subtask
    on a parent does not disqualify the parent for a subsequent R&T subtask.
    """
    current_account_id = client.get_myself_account_id()
    rt_jql = _build_rt_subtasks_jql(settings)
    rt_subtasks = client.search_jql(rt_jql)

    parent_to_rt_key: dict[str, str] = {}
    for subtask in rt_subtasks:
        if not summary_matches_rt(subtask, settings.jira_rt_summary_contains):
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
                        historical_subtask, settings.jira_rt_summary_contains
                    )
                    and is_done_status(historical_subtask)
                ):
                    continue

                excluded_parents.add(parent_key)

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

    result = [
        key for key in parent_keys if key not in excluded_parents
    ]
    result.sort()
    return result
