# rt-queue

CLI that scans configured GitHub repositories for open pull requests and prints Jira parent tickets ready for **Review & Test**.

## How it works

1. For each configured `owner/repo`, fetch **open**, **non-draft** pull requests **not authored by you**.
2. Resolve each PR to a Jira **parent** issue:
   - Prefer an issue that shows the PR in Jira (development panel or remote link).
   - Otherwise use the first Jira key in the PR **title**.
   - Subtask links are promoted to their parent.
3. Keep parents whose status is **Pending Review** (configurable).
4. Apply the same subtask rules as before:

- A subtask whose summary matches any configured R&T keyword group (default `review,test`, `code,review`) in status **To Do**
- That R&T subtask is unassigned or assigned to you
- Every **other** subtask under the parent is **Done**, except **Deploy** subtasks (configured issue type, or a summary of exactly `Deploy`) and **ignored** subtasks (default `stakeholder,review`), which may be in any status
- You were never assignee on any **other** non-ignored subtask under the same parent (`assignee was currentUser()` in Jira history), unless you use `--include-worked-on`

Ignored subtasks (default Stakeholder Review) are never a queue trigger, never a sibling blocker, and never disqualifying history.

Output is one line per qualifying PR on stdout (parent URL, assignee, PR URL), sorted by oldest pull request first, then oldest Jira parent update. Multiple PRs on the same parent produce multiple lines.

## Setup

### Install

Install with [pipx](https://pipx.pypa.io/) so `rt-queue` is available globally without activating a virtualenv:

```bash
pipx install rt-queue
```

From a clone of this repository:

```bash
pipx install .
```

From GitHub (no clone required):

```bash
pipx install git+https://github.com/codypearson/rt-queue.git
```

If you do not have pipx yet, see the [pipx installation guide](https://pipx.pypa.io/stable/installation/).

### Configuration

```bash
cp .env.example .env
# Edit .env: GITHUB_TOKEN, GITHUB_REPOSITORIES,
# JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
# and either JIRA_DEPLOY_ISSUE_TYPE_NAME or JIRA_DEPLOY_ISSUE_TYPE_ID
```

Run `rt-queue` from any directory; it loads `.env` from the current working directory.

#### GitHub token (`GITHUB_TOKEN`)

The CLI calls `GET /user` (unless you set `GITHUB_LOGIN`), lists open pull requests per repo, and reads commit messages on each PR for Jira key extraction.

**Fine-grained personal access token** (recommended for least privilege):

| Scope | Setting |
|-------|---------|
| Resource owner | Your org or user that owns the repos |
| Repository access | **Only select repositories** — include every repo listed in `GITHUB_REPOSITORIES` |
| Repository permissions → **Pull requests** | **Read** |
| Repository permissions → **Metadata** | **Read** (required whenever other repo permissions are granted) |
| Account permissions → **Profile** | **Read** (only if you omit `GITHUB_LOGIN`; used to learn your login so your own PRs are excluded) |

No other repository or account permissions are required. You do not need Contents, Issues, or Actions access.

**Classic personal access token** alternative: grant the **`repo`** scope (or **`public_repo`** if every configured repository is public). Classic tokens cannot be limited to a subset of repositories the way fine-grained tokens can.

Store the token in `.env` as `GITHUB_TOKEN`. Never commit it.

### Development

For local development with an editable install:

```bash
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
https://imh-internal.atlassian.net/browse/PROJ-123  (Assignee: Jane, PR: https://github.com/inmotionhosting/product-catalog/pull/456)
```

Run `rt-queue --help` for environment variable documentation.

## Configuration

| Variable | Required | Default |
|----------|----------|---------|
| `GITHUB_TOKEN` | yes | — |
| `GITHUB_REPOSITORIES` | yes | — |
| `GITHUB_LOGIN` | no | from `GET /user` |
| `JIRA_BASE_URL` | yes | — |
| `JIRA_EMAIL` | yes | — |
| `JIRA_API_TOKEN` | yes | — |
| `JIRA_PARENT_STATUS_NAME` | no | `Pending Review` |
| `JIRA_DEPLOY_ISSUE_TYPE_NAME` or `JIRA_DEPLOY_ISSUE_TYPE_ID` | yes (one of) | — / — |
| `JIRA_RT_SUMMARY_KEYWORDS` | no | `review,test;code,review` |
| `JIRA_IGNORED_SUMMARY_KEYWORDS` | no | `stakeholder,review` |
| `JIRA_RT_STATUS_NAME` | no | `To Do,Qualified` (comma-separated) |
| `JIRA_ACCOUNT_ID` | no | from `GET /rest/api/3/myself` |

## Requirements

- GitHub REST API (see [GitHub token](#github-token-github_token) above for fine-grained PAT permissions)
- Jira Cloud with REST API v3
- Python 3.11+
