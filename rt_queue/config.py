"""Load configuration from environment variables and optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Default R&T summary keyword groups. Groups are separated by ``;``; keywords
# within a group are comma-separated. A subtask matches when every keyword in
# at least one group appears in the summary (case-insensitive substring).
DEFAULT_RT_SUMMARY_KEYWORDS = "review,test;code,review"

# Default ignored summary keyword groups. Matching subtasks are never a
# queue trigger, never a sibling blocker, and never disqualifying history.
DEFAULT_IGNORED_SUMMARY_KEYWORDS = "stakeholder,review"

# Default parent status when a PR-linked ticket is ready for the queue.
DEFAULT_JIRA_PARENT_STATUS_NAME = "Pending Review"

# Default R&T subtask statuses (comma-separated in ``JIRA_RT_STATUS_NAME``).
DEFAULT_JIRA_RT_STATUS_NAMES = "To Do,Qualified"

# One AND-group of keywords, e.g. ("review", "test").
SummaryKeywordGroup = tuple[str, ...]
# OR of AND-groups, e.g. (("review", "test"), ("code", "review")).
SummaryKeywordGroups = tuple[SummaryKeywordGroup, ...]


def parse_summary_keyword_groups(raw: str) -> SummaryKeywordGroups:
    """
    Parse summary keyword groups from an environment value.

    Groups are separated by ``;``. Keywords within a group are comma-separated.
    Empty segments are ignored. An empty result (no groups) is valid and
    means the list is unused.

    A subtask matches when every keyword in at least one group appears in the
    summary, so ``review,test;code,review`` matches both ``Review & Test`` and
    ``Code Review``.
    """
    groups: list[SummaryKeywordGroup] = []
    for group_raw in raw.split(";"):
        keywords = tuple(
            segment.strip()
            for segment in group_raw.split(",")
            if segment.strip()
        )
        if keywords:
            groups.append(keywords)
    return tuple(groups)


def parse_status_names(raw: str) -> tuple[str, ...]:
    """
    Parse comma-separated Jira status names.

    Empty segments are ignored. Raises when the result would be empty.
    """
    names = tuple(segment.strip() for segment in raw.split(",") if segment.strip())
    if not names:
        raise ValueError("JIRA_RT_STATUS_NAME must include at least one status name.")
    return names


def parse_github_repositories(raw: str) -> tuple[str, ...]:
    """
    Parse a comma-separated list of ``owner/repo`` GitHub repository slugs.

    Whitespace around entries is stripped. Empty segments are ignored.
    """
    repos: list[str] = []
    for segment in raw.split(","):
        slug = segment.strip()
        if not slug:
            continue
        if "/" not in slug:
            raise ValueError(
                f"Invalid GITHUB_REPOSITORIES entry '{slug}': "
                "expected owner/repo format."
            )
        repos.append(slug)
    if not repos:
        raise ValueError(
            "GITHUB_REPOSITORIES must include at least one owner/repo entry."
        )
    return tuple(repos)


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for GitHub, Jira API access, and R&T queue rules.

    ``github_repositories`` lists ``owner/repo`` slugs to scan for open PRs.

    ``jira_rt_summary_keywords`` is an OR of AND-groups: a subtask matches
    when every keyword in at least one group appears in its summary.

    ``jira_ignored_summary_keywords`` uses the same group format. Matching
    subtasks are skipped entirely (not a trigger, not a blocker, no
    worked-on history).

    ``jira_parent_status_name`` is the required status on the parent issue
    linked to each qualifying pull request (default Pending Review).
    """

    github_token: str
    github_repositories: tuple[str, ...]
    github_login: str | None
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    jira_parent_status_name: str
    jira_rt_summary_keywords: SummaryKeywordGroups
    jira_ignored_summary_keywords: SummaryKeywordGroups
    jira_rt_status_names: tuple[str, ...]
    jira_account_id: str | None
    jira_deploy_issue_type_name: str | None
    jira_deploy_issue_type_id: str | None

    @staticmethod
    def from_env(dotenv_path: Path | None = None) -> Settings:
        """
        Build settings from the process environment.

        Loads `.env` from the current working directory when present (override
        with ``dotenv_path``). Missing required keys raise ``ValueError``.
        """
        if dotenv_path is not None:
            load_dotenv(dotenv_path, override=False)
        else:
            load_dotenv(override=False)

        def req(key: str) -> str:
            value = os.environ.get(key)
            if not value or not str(value).strip():
                raise ValueError(
                    f"Missing or empty required environment variable: {key}"
                )
            return str(value).strip()

        github_repos_raw = req("GITHUB_REPOSITORIES")
        github_repositories = parse_github_repositories(github_repos_raw)

        github_login = os.environ.get("GITHUB_LOGIN", "").strip() or None

        keywords_raw = os.environ.get(
            "JIRA_RT_SUMMARY_KEYWORDS", DEFAULT_RT_SUMMARY_KEYWORDS
        )
        rt_keywords = parse_summary_keyword_groups(keywords_raw)
        if not rt_keywords:
            raise ValueError(
                "JIRA_RT_SUMMARY_KEYWORDS must include at least one non-empty "
                "keyword group."
            )

        ignored_raw = os.environ.get(
            "JIRA_IGNORED_SUMMARY_KEYWORDS", DEFAULT_IGNORED_SUMMARY_KEYWORDS
        )
        ignored_keywords = parse_summary_keyword_groups(ignored_raw)

        parent_status = os.environ.get(
            "JIRA_PARENT_STATUS_NAME", DEFAULT_JIRA_PARENT_STATUS_NAME
        ).strip()
        if not parent_status:
            raise ValueError(
                'JIRA_PARENT_STATUS_NAME must be non-empty '
                f'(default is "{DEFAULT_JIRA_PARENT_STATUS_NAME}").'
            )

        rt_status_raw = os.environ.get(
            "JIRA_RT_STATUS_NAME", DEFAULT_JIRA_RT_STATUS_NAMES
        ).strip()
        jira_rt_status_names = parse_status_names(rt_status_raw)

        account_id = os.environ.get("JIRA_ACCOUNT_ID", "").strip()

        deploy_name = os.environ.get("JIRA_DEPLOY_ISSUE_TYPE_NAME", "").strip()
        deploy_id = os.environ.get("JIRA_DEPLOY_ISSUE_TYPE_ID", "").strip()
        if not deploy_name and not deploy_id:
            raise ValueError(
                "Set either JIRA_DEPLOY_ISSUE_TYPE_NAME or JIRA_DEPLOY_ISSUE_TYPE_ID "
                "for Deploy subtask detection."
            )

        return Settings(
            github_token=req("GITHUB_TOKEN"),
            github_repositories=github_repositories,
            github_login=github_login,
            jira_base_url=req("JIRA_BASE_URL").rstrip("/"),
            jira_email=req("JIRA_EMAIL"),
            jira_api_token=req("JIRA_API_TOKEN"),
            jira_parent_status_name=parent_status,
            jira_rt_summary_keywords=rt_keywords,
            jira_ignored_summary_keywords=ignored_keywords,
            jira_rt_status_names=jira_rt_status_names,
            jira_account_id=account_id or None,
            jira_deploy_issue_type_name=deploy_name or None,
            jira_deploy_issue_type_id=deploy_id or None,
        )
