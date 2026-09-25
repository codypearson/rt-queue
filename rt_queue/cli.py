"""Command-line entry: print parent tickets ready for Review & Test.

Output includes the browse URL, parent assignee, and GitHub pull request URL
(sorted oldest PR first, then oldest Jira parent update).
"""

from __future__ import annotations

import click
import requests

from rt_queue.config import (
    DEFAULT_IGNORED_SUMMARY_KEYWORDS,
    DEFAULT_JIRA_PARENT_STATUS_NAME,
    DEFAULT_JIRA_RT_STATUS_NAMES,
    DEFAULT_RT_SUMMARY_KEYWORDS,
    Settings,
)
from rt_queue.github_client import GitHubClient
from rt_queue.jira_client import JiraClient
from rt_queue.queue import find_parents_needing_rt

EPILOG = f"""
Environment variables (see .env.example):

  Required: GITHUB_TOKEN, GITHUB_REPOSITORIES (comma-separated owner/repo),
            JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
            and either JIRA_DEPLOY_ISSUE_TYPE_NAME or JIRA_DEPLOY_ISSUE_TYPE_ID

  Optional: GITHUB_LOGIN (default: from GET /user),
            JIRA_PARENT_STATUS_NAME (default: {DEFAULT_JIRA_PARENT_STATUS_NAME}),
            JIRA_RT_SUMMARY_KEYWORDS (default: {DEFAULT_RT_SUMMARY_KEYWORDS}),
            JIRA_IGNORED_SUMMARY_KEYWORDS (default: {DEFAULT_IGNORED_SUMMARY_KEYWORDS}),
            JIRA_RT_STATUS_NAME (default: {DEFAULT_JIRA_RT_STATUS_NAMES}),
            JIRA_ACCOUNT_ID (default: from GET /rest/api/3/myself)

The tool lists open, non-draft GitHub PRs (not authored by you) in the
configured repos, resolves each to a Jira parent (dev panel link or title key),
requires parent status {DEFAULT_JIRA_PARENT_STATUS_NAME}, then applies R&T
subtask rules: an R&T subtask in the configured status unassigned or assigned
to you, every other non-Deploy non-ignored subtask Done, and optional
worked-on-sibling exclusion.
"""


@click.command(
    help=(
        "Print parent tickets ready for Review & Test "
        "(URL, assignee, PR URL)."
    ),
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
    """Query GitHub and Jira; print queue entries on stdout."""
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1) from exc

    jira_client = JiraClient(settings)
    github_client = GitHubClient(settings)
    try:
        build_result = find_parents_needing_rt(
            jira_client,
            github_client,
            settings,
            exclude_worked_on_siblings=not include_worked_on,
        )
    except requests.HTTPError as exc:
        click.echo(f"API error: {exc}", err=True)
        raise SystemExit(1) from exc
    except ValueError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1) from exc

    parents = build_result.entries

    if build_result.skipped_unresolved_pr_count > 0:
        click.echo(
            f"Skipped {build_result.skipped_unresolved_pr_count} pull request(s) "
            "with no resolvable Jira parent.",
            err=True,
        )

    for parent in parents:
        url = jira_client.issue_url(parent.key)
        click.echo(
            f"{url}  (Assignee: {parent.assignee}, PR: {parent.pull_request_url})"
        )

    if not parents:
        click.echo(
            "No parent tickets ready for Review & Test.",
            err=True,
        )


if __name__ == "__main__":
    main()
