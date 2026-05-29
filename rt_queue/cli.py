"""Command-line entry: print parent ticket URLs ready for Review & Test."""

from __future__ import annotations

import click
import requests

from rt_queue.config import Settings
from rt_queue.jira_client import JiraClient
from rt_queue.queue import find_parents_needing_rt

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
    help="Print Jira browse URLs for parent tickets ready for Review & Test.",
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
    """Query Jira and print one parent browse URL per line on stdout."""
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1) from exc

    client = JiraClient(settings)
    try:
        parent_keys = find_parents_needing_rt(
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

    for parent_key in parent_keys:
        click.echo(client.issue_url(parent_key))

    if not parent_keys:
        click.echo(
            "No parent tickets ready for Review & Test.",
            err=True,
        )


if __name__ == "__main__":
    main()
