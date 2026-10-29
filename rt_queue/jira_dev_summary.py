"""Parse Jira Development custom field values for pull-request summary gating."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# Schema custom type for the Development panel field on Jira Cloud.
DEVELOPMENT_FIELD_SCHEMA_CUSTOM = (
    "com.atlassian.jira.plugins.jira-development-integration-plugin:devsummarycf"
)


@dataclass(frozen=True)
class DevelopmentPullRequestSummary:
    """Pull-request counts from the Development field cached summary."""

    count: int
    is_stale: bool
    parse_confident: bool


def discover_development_field_id(fields_payload: list[dict[str, Any]]) -> str | None:
    """
    Return the field id for the Development panel custom field, if present.

    Matches fields whose schema custom type is the dev-integration summary type.
    """
    for field in fields_payload:
        if not isinstance(field, dict):
            continue
        field_id = (field.get("id") or "").strip()
        if not field_id:
            continue
        schema = field.get("schema") or {}
        if not isinstance(schema, dict):
            continue
        custom = (schema.get("custom") or "").strip()
        if custom == DEVELOPMENT_FIELD_SCHEMA_CUSTOM:
            return field_id
    return None


def parse_development_pull_request_summary(
    field_value: Any,
) -> DevelopmentPullRequestSummary:
    """
    Parse a Development field value for pull-request count and staleness.

    Returns ``parse_confident`` False when the payload cannot be interpreted;
    callers must not skip dev-status detail in that case.
    """
    if field_value is None:
        return DevelopmentPullRequestSummary(
            count=0, is_stale=True, parse_confident=False
        )

    parsed_json: dict[str, Any] | None = None
    is_stale = True

    if isinstance(field_value, dict):
        parsed_json = field_value
    elif isinstance(field_value, str):
        stripped = field_value.strip()
        json_match = re.search(r"json=(\{.*\})\s*$", stripped, re.DOTALL)
        if json_match:
            try:
                parsed_json = json.loads(json_match.group(1))
            except json.JSONDecodeError:
                parsed_json = None
        if parsed_json is None:
            try:
                parsed_json = json.loads(stripped)
            except json.JSONDecodeError:
                parsed_json = None

    if not isinstance(parsed_json, dict):
        return DevelopmentPullRequestSummary(
            count=0, is_stale=True, parse_confident=False
        )

    stale_raw = parsed_json.get("isStale")
    if stale_raw is False:
        is_stale = False
    elif stale_raw is True:
        is_stale = True

    cached = parsed_json.get("cachedValue")
    if not isinstance(cached, dict):
        return DevelopmentPullRequestSummary(
            count=0, is_stale=is_stale, parse_confident=False
        )

    summary = cached.get("summary")
    if not isinstance(summary, dict):
        return DevelopmentPullRequestSummary(
            count=0, is_stale=is_stale, parse_confident=False
        )

    pullrequest = summary.get("pullrequest")
    if not isinstance(pullrequest, dict):
        return DevelopmentPullRequestSummary(
            count=0, is_stale=is_stale, parse_confident=True
        )

    overall = pullrequest.get("overall")
    if not isinstance(overall, dict):
        return DevelopmentPullRequestSummary(
            count=0, is_stale=is_stale, parse_confident=False
        )

    count_raw = overall.get("count")
    try:
        count = int(count_raw)
    except (TypeError, ValueError):
        return DevelopmentPullRequestSummary(
            count=0, is_stale=is_stale, parse_confident=False
        )

    return DevelopmentPullRequestSummary(
        count=count, is_stale=is_stale, parse_confident=True
    )


def can_skip_dev_status_detail(field_value: Any) -> bool:
    """
    True when dev-status detail cannot contain pull requests for this issue.

    Skips only when the summary was parsed confidently, is not stale, and the
    pull-request count is zero.
    """
    summary = parse_development_pull_request_summary(field_value)
    if not summary.parse_confident:
        return False
    if summary.is_stale:
        return False
    return summary.count == 0
