"""Load configuration from environment variables and optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Default R&T summary keyword groups. Groups are separated by ``;``; keywords
# within a group are comma-separated. A subtask matches when every keyword in
# at least one group appears in the summary (case-insensitive substring).
DEFAULT_RT_SUMMARY_KEYWORDS = "review,test;code,review;stakeholder,review"

# One AND-group of keywords, e.g. ("review", "test").
RtKeywordGroup = tuple[str, ...]
# OR of AND-groups, e.g. (("review", "test"), ("code", "review")).
RtKeywordGroups = tuple[RtKeywordGroup, ...]


def parse_rt_summary_keywords(raw: str) -> RtKeywordGroups:
    """
    Parse R&T summary keyword groups from an environment value.

    Groups are separated by ``;``. Keywords within a group are comma-separated.
    Empty segments are ignored. At least one group with at least one keyword
    is required.

    A subtask matches when every keyword in at least one group appears in the
    summary, so ``review,test;code,review`` matches both ``Review & Test`` and
    ``Code Review``.
    """
    groups: list[RtKeywordGroup] = []
    for group_raw in raw.split(";"):
        keywords = tuple(
            segment.strip()
            for segment in group_raw.split(",")
            if segment.strip()
        )
        if keywords:
            groups.append(keywords)
    if not groups:
        raise ValueError(
            "JIRA_RT_SUMMARY_KEYWORDS must include at least one non-empty "
            "keyword group."
        )
    return tuple(groups)


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for Jira API access and R&T queue rules.

    ``jira_rt_summary_keywords`` is an OR of AND-groups: a subtask matches
    when every keyword in at least one group appears in its summary.
    """

    jira_base_url: str
    jira_email: str
    jira_api_token: str
    jira_project_key: str
    jira_rt_summary_keywords: RtKeywordGroups
    jira_rt_status_name: str
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

        keywords_raw = os.environ.get(
            "JIRA_RT_SUMMARY_KEYWORDS", DEFAULT_RT_SUMMARY_KEYWORDS
        )
        rt_keywords = parse_rt_summary_keywords(keywords_raw)

        rt_status = os.environ.get("JIRA_RT_STATUS_NAME", "To Do").strip()
        if not rt_status:
            raise ValueError(
                'JIRA_RT_STATUS_NAME must be non-empty (default is "To Do").'
            )

        account_id = os.environ.get("JIRA_ACCOUNT_ID", "").strip()

        deploy_name = os.environ.get("JIRA_DEPLOY_ISSUE_TYPE_NAME", "").strip()
        deploy_id = os.environ.get("JIRA_DEPLOY_ISSUE_TYPE_ID", "").strip()
        if not deploy_name and not deploy_id:
            raise ValueError(
                "Set either JIRA_DEPLOY_ISSUE_TYPE_NAME or JIRA_DEPLOY_ISSUE_TYPE_ID "
                "for Deploy subtask detection."
            )

        return Settings(
            jira_base_url=req("JIRA_BASE_URL").rstrip("/"),
            jira_email=req("JIRA_EMAIL"),
            jira_api_token=req("JIRA_API_TOKEN"),
            jira_project_key=req("JIRA_PROJECT_KEY"),
            jira_rt_summary_keywords=rt_keywords,
            jira_rt_status_name=rt_status,
            jira_account_id=account_id or None,
            jira_deploy_issue_type_name=deploy_name or None,
            jira_deploy_issue_type_id=deploy_id or None,
        )
