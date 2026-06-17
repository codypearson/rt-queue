"""Command-line entry: print parent tickets ready for Review & Test.

Output includes the browse URL, parent assignee, and date the last subtask
was completed (sorted oldest first by that date).
"""

from __future__ import annotations

import click
import requests

from rt_queue.config import Settings
from rt_queue.jira_client import JiraClient
from rt_queue.queue import ParentReadyForRt, find_parents_needing_rt

EPILOG = """
Environment variables (see .env.example):

  Required: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY,
            and either JIRA_DEPLOY_ISSUE_TYPE_NAME or JIRA_DEPLOY_ISSUE_TYPE_ID

  Optional: JIRA_RT_SUMMARY_CONTAINS (default: Review & Test),
            JIRA_RT_STATUS_NAME (default: To Do),
            JIRA_ACCOUNT_ID (default: from GET /rest/api/3/myself)

A parent is listed when it has an R&T subtask in the configured status,
unassigned or assigned to you, every other non-Deploy subtask is Done,
and you were never assignee on any other subtask under that parent.
"""


@click.command(
    help="Print parent tickets ready for Review & Test (URL, assignee, last subtask date).",
    epilog=EPILOG,
)
@click.option(
    "--include-worked-on",
    is_flag=True,
    default=False,
    help=(
        "Include parents even when you were previously assignee on other "
        "subtasks (disables the worked-on-siblings filter)."
    ),
)
def main(include_worked_on: bool) -> None:
    """Query Jira and print parent info (URL, assignee, last subtask date) on stdout."""
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1) from exc

    client = JiraClient(settings)
    try:
        parents: list[ParentReadyForRt] = find_parents_needing_rt(
            client,
            settings,
            exclude_worked_on_siblings=not include_worked_on,
        )
    except requests.HTTPError as exc:
        click.echo(f"Jira API error: {exc}", err=True)
        raise SystemExit(1) from exc
    except ValueError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1) from exc

    for parent in parents:
        url = client.issue_url(parent.key)
        last_str = (
            parent.last_subtask_completed.date().isoformat()
            if parent.last_subtask_completed is not None
            else "unknown"
        )
        click.echo(f"{url}  (Assignee: {parent.assignee}, Last subtask: {last_str})")

    if not parents:
        click.echo(
            "No parent tickets ready for Review & Test.",
            err=True,
        )


if __name__ == "__main__":
    main()
