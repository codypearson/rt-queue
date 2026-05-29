# rt-queue

CLI that queries Jira Cloud and prints browse URLs for parent tickets ready for **Review & Test**.

A parent is listed when:

- It has a subtask whose summary contains the configured R&T text (default `Review & Test`) in status **To Do**
- That R&T subtask is unassigned or assigned to you
- Every **other** subtask under the parent is **Done**, except **Deploy** subtasks (configured issue type), which may be in any status
- You were never assignee on any **other** subtask under the same parent (`assignee was currentUser()` in Jira history)

Output is one parent URL per line on stdout (nothing else).

## Setup

```bash
cp .env.example .env
# Edit .env: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY,
# and either JIRA_DEPLOY_ISSUE_TYPE_NAME or JIRA_DEPLOY_ISSUE_TYPE_ID

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

```bash
rt-queue
```

To include parents where you were assignee on other subtasks:

```bash
rt-queue --include-worked-on
```

Example output:

```
https://imh-internal.atlassian.net/browse/PROJ-123
https://imh-internal.atlassian.net/browse/PROJ-456
```

Run `rt-queue --help` for environment variable documentation.

## Configuration

| Variable | Required | Default |
|----------|----------|---------|
| `JIRA_BASE_URL` | yes | — |
| `JIRA_EMAIL` | yes | — |
| `JIRA_API_TOKEN` | yes | — |
| `JIRA_PROJECT_KEY` | yes | — |
| `JIRA_DEPLOY_ISSUE_TYPE_NAME` or `JIRA_DEPLOY_ISSUE_TYPE_ID` | yes (one of) | — / — |
| `JIRA_RT_SUMMARY_CONTAINS` | no | `Review & Test` |
| `JIRA_RT_STATUS_NAME` | no | `To Do` |
| `JIRA_ACCOUNT_ID` | no | from `GET /rest/api/3/myself` |

## Requirements

- Jira Cloud with REST API v3
- Python 3.11+
