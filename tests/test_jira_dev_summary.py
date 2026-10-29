"""Tests for Development field summary parsing."""

from __future__ import annotations

import unittest

from rt_queue.jira_dev_summary import (
    can_skip_dev_status_detail,
    discover_development_field_id,
    parse_development_pull_request_summary,
)


class JiraDevSummaryTests(unittest.TestCase):
    """Development panel summary gating rules."""

    def test_discover_development_field_id(self) -> None:
        """Match the dev-integration custom field schema type."""
        fields = [
            {"id": "customfield_10000", "schema": {"custom": "other:type"}},
            {
                "id": "customfield_10001",
                "schema": {
                    "custom": (
                        "com.atlassian.jira.plugins.jira-development-integration-plugin:"
                        "devsummarycf"
                    )
                },
            },
        ]
        self.assertEqual(discover_development_field_id(fields), "customfield_10001")

    def test_skip_when_fresh_and_zero_pull_requests(self) -> None:
        """Fresh zero-count summaries skip dev-status detail."""
        raw = {
            "cachedValue": {"summary": {"pullrequest": {"overall": {"count": 0}}}},
            "isStale": False,
        }
        self.assertTrue(can_skip_dev_status_detail(raw))

    def test_do_not_skip_when_stale(self) -> None:
        """Stale summaries still call dev-status detail."""
        raw = (
            '{pullrequest={dataType=pullrequest, state=OPEN, stateCount=0}, '
            'json={"cachedValue":{"summary":{"pullrequest":{"overall":{"count":0}}}},'
            '"isStale":true}}'
        )
        self.assertFalse(can_skip_dev_status_detail(raw))

    def test_do_not_skip_when_unparseable(self) -> None:
        """Unknown payloads must not skip detail."""
        summary = parse_development_pull_request_summary("not-json")
        self.assertFalse(summary.parse_confident)
        self.assertFalse(can_skip_dev_status_detail("not-json"))
