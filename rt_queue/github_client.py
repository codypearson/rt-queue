"""GitHub GraphQL client for listing open pull requests with commit messages."""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any

import requests

from rt_queue.api_datetime import parse_api_datetime
from rt_queue.config import Settings

_GRAPHQL_URL = "https://api.github.com/graphql"
_PULL_REQUESTS_PAGE_SIZE = 100
_COMMITS_PAGE_SIZE = 100


@dataclass(frozen=True)
class GitHubPullRequest:
    """Minimal pull request fields used by the R&T queue."""

    html_url: str
    number: int
    title: str
    body: str | None
    head_ref: str
    repository_full_name: str
    author_login: str
    created_at: dt.datetime
    commit_messages: tuple[str, ...] = ()


def _escape_graphql_string(value: str) -> str:
    """Escape a string for use inside GraphQL double quotes."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _split_repository_slug(repo_slug: str) -> tuple[str, str]:
    """Split ``owner/repo`` into owner and repository name."""
    if "/" not in repo_slug:
        raise ValueError(
            f"Invalid repository slug '{repo_slug}': expected owner/repo format."
        )
    owner, name = repo_slug.split("/", 1)
    owner = owner.strip()
    name = name.strip()
    if not owner or not name:
        raise ValueError(
            f"Invalid repository slug '{repo_slug}': expected owner/repo format."
        )
    return owner, name


def _parse_github_datetime(raw: Any) -> dt.datetime | None:
    """Parse GitHub GraphQL DateTime values."""
    if not isinstance(raw, str):
        return None
    normalized = raw.replace("Z", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2}T", normalized):
        try:
            return dt.datetime.fromisoformat(normalized)
        except ValueError:
            return parse_api_datetime(raw)
    return parse_api_datetime(raw)


class GitHubClient:
    """
    GitHub API client for the R&T queue.

    Loads open pull requests and commit messages across configured repositories
    using GraphQL, with follow-up queries only when pagination requires them.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        self._api_base = "https://api.github.com"
        self._authenticated_login: str | None = None

    def get_authenticated_login(self) -> str:
        """
        Return the GitHub login for the token.

        Uses ``GITHUB_LOGIN`` when set; otherwise caches ``GET /user``.
        """
        if self._settings.github_login:
            return self._settings.github_login
        if self._authenticated_login:
            return self._authenticated_login
        url = f"{self._api_base}/user"
        resp = self._session.get(url, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        login = (data.get("login") or "").strip()
        if not login:
            raise ValueError("GitHub /user response did not include login")
        self._authenticated_login = login
        return login

    def list_open_pull_requests_not_by_me(self) -> list[GitHubPullRequest]:
        """
        Return open, non-draft PRs across configured repos, excluding the current user.

        Commit messages on each pull request are included in the result.
        """
        my_login = self.get_authenticated_login()
        repo_slugs = self._settings.github_repositories
        if not repo_slugs:
            return []

        repo_entries = [
            (_split_repository_slug(slug), slug) for slug in repo_slugs
        ]
        collected: dict[tuple[str, int], GitHubPullRequest] = {}

        pr_cursors: dict[str, str | None] = {slug: None for slug in repo_slugs}
        while True:
            data = self._graphql_pull_requests_page(repo_entries, pr_cursors)
            pending_pagination: dict[str, str | None] = {}

            for alias_index, ((owner, name), repo_slug) in enumerate(repo_entries):
                alias = f"repo{alias_index}"
                repo_data = data.get(alias)
                if not isinstance(repo_data, dict):
                    continue
                pull_requests = repo_data.get("pullRequests") or {}
                if not isinstance(pull_requests, dict):
                    continue
                page_info = pull_requests.get("pageInfo") or {}
                nodes = pull_requests.get("nodes") or []
                if not isinstance(nodes, list):
                    nodes = []

                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    mapped = self._map_graphql_pull_request(node, repo_slug)
                    if mapped is None:
                        continue
                    if mapped.author_login.lower() == my_login.lower():
                        continue
                    key = (repo_slug, mapped.number)
                    existing = collected.get(key)
                    if existing is None:
                        collected[key] = mapped
                    else:
                        merged_messages = tuple(
                            dict.fromkeys(
                                list(existing.commit_messages)
                                + list(mapped.commit_messages)
                            )
                        )
                        collected[key] = GitHubPullRequest(
                            html_url=existing.html_url,
                            number=existing.number,
                            title=existing.title,
                            body=existing.body,
                            head_ref=existing.head_ref,
                            repository_full_name=existing.repository_full_name,
                            author_login=existing.author_login,
                            created_at=existing.created_at,
                            commit_messages=merged_messages,
                        )

                    commit_page = (node.get("commits") or {}).get("pageInfo") or {}
                    if commit_page.get("hasNextPage") is True:
                        end_cursor = (commit_page.get("endCursor") or "").strip()
                        if end_cursor:
                            extra_messages = self._graphql_commit_messages(
                                owner,
                                name,
                                mapped.number,
                                end_cursor,
                            )
                            if extra_messages:
                                current = collected[key]
                                merged_messages = tuple(
                                    dict.fromkeys(
                                        list(current.commit_messages) + extra_messages
                                    )
                                )
                                collected[key] = GitHubPullRequest(
                                    html_url=current.html_url,
                                    number=current.number,
                                    title=current.title,
                                    body=current.body,
                                    head_ref=current.head_ref,
                                    repository_full_name=current.repository_full_name,
                                    author_login=current.author_login,
                                    created_at=current.created_at,
                                    commit_messages=merged_messages,
                                )

                if page_info.get("hasNextPage") is True:
                    end_cursor = (page_info.get("endCursor") or "").strip()
                    pending_pagination[repo_slug] = end_cursor or None
                else:
                    pending_pagination[repo_slug] = None

            pr_cursors = pending_pagination
            if not any(cursor for cursor in pr_cursors.values()):
                break

        return list(collected.values())

    def _graphql_pull_requests_page(
        self,
        repo_entries: list[tuple[tuple[str, str], str]],
        pr_cursors: dict[str, str | None],
    ) -> dict[str, Any]:
        """Run one GraphQL query for a page of open pull requests per repository."""
        selections: list[str] = []
        for alias_index, ((owner, name), repo_slug) in enumerate(repo_entries):
            alias = f"repo{alias_index}"
            after_clause = ""
            cursor = pr_cursors.get(repo_slug)
            if cursor:
                after_clause = f', after: "{_escape_graphql_string(cursor)}"'
            owner_escaped = _escape_graphql_string(owner)
            name_escaped = _escape_graphql_string(name)
            selections.append(
                f"""
    {alias}: repository(owner: "{owner_escaped}", name: "{name_escaped}") {{
      pullRequests(states: OPEN, first: {_PULL_REQUESTS_PAGE_SIZE}{after_clause}) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          number
          title
          body
          url
          isDraft
          createdAt
          headRefName
          author {{ login }}
          commits(first: {_COMMITS_PAGE_SIZE}) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{ commit {{ message }} }}
          }}
        }}
      }}
    }}"""
            )

        query = f"query {{\n{''.join(selections)}\n}}"
        return self._graphql(query)

    def _graphql_commit_messages(
        self,
        owner: str,
        name: str,
        pull_number: int,
        start_cursor: str,
    ) -> tuple[str, ...]:
        """Paginate commit messages for one pull request from ``start_cursor``."""
        messages: list[str] = []
        cursor: str | None = start_cursor
        owner_escaped = _escape_graphql_string(owner)
        name_escaped = _escape_graphql_string(name)

        while cursor:
            query = f"""
query {{
  repository(owner: "{owner_escaped}", name: "{name_escaped}") {{
    pullRequest(number: {pull_number}) {{
      commits(first: {_COMMITS_PAGE_SIZE}, after: "{_escape_graphql_string(cursor)}") {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ commit {{ message }} }}
      }}
    }}
  }}
}}
"""
            data = self._graphql(query)
            repository = data.get("repository") or {}
            pull_request = repository.get("pullRequest") or {}
            commits = pull_request.get("commits") or {}
            nodes = commits.get("nodes") or []
            if isinstance(nodes, list):
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    commit = node.get("commit") or {}
                    message = (commit.get("message") or "").strip()
                    if message:
                        messages.append(message)
            page_info = commits.get("pageInfo") or {}
            if page_info.get("hasNextPage") is True:
                next_cursor = (page_info.get("endCursor") or "").strip()
                cursor = next_cursor or None
            else:
                cursor = None

        return tuple(messages)

    def _graphql(self, query: str) -> dict[str, Any]:
        """POST a GraphQL query and return the ``data`` object."""
        resp = self._session.post(
            _GRAPHQL_URL,
            json={"query": query},
            timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("GitHub GraphQL response was not a JSON object")
        errors = payload.get("errors")
        data = payload.get("data")
        if errors:
            error_text = json.dumps(errors, ensure_ascii=True)
            if self._graphql_error_is_commit_forbidden(error_text):
                raise ValueError(
                    "GitHub token cannot read pull request commits via GraphQL. "
                    "Grant Repository permission Contents: Read on every configured "
                    "repository (see README GitHub token section)."
                )
            if isinstance(data, dict):
                return data
            raise ValueError(f"GitHub GraphQL error: {error_text[:500]}")
        if not isinstance(data, dict):
            raise ValueError("GitHub GraphQL response did not include data")
        return data

    @staticmethod
    def _graphql_error_is_commit_forbidden(error_text: str) -> bool:
        """True when GraphQL errors indicate commit nodes are not accessible."""
        lowered = error_text.lower()
        return (
            "resource not accessible by personal access token" in lowered
            and "commits" in lowered
        )

    def _map_graphql_pull_request(
        self, node: dict[str, Any], repo_slug: str
    ) -> GitHubPullRequest | None:
        """Map a GraphQL pull request node to ``GitHubPullRequest``."""
        if node.get("isDraft") is True:
            return None

        html_url = (node.get("url") or "").strip()
        number = node.get("number")
        title = (node.get("title") or "").strip()
        if not html_url or not isinstance(number, int) or not title:
            return None

        author = node.get("author") or {}
        author_login = (author.get("login") or "").strip()
        if not author_login:
            return None

        head_ref = (node.get("headRefName") or "").strip()
        body_raw = node.get("body")
        body = (body_raw or "").strip() if body_raw else None
        if body == "":
            body = None

        created_at = _parse_github_datetime(node.get("createdAt"))
        if created_at is None:
            return None

        commit_messages: list[str] = []
        commits = node.get("commits") or {}
        nodes = commits.get("nodes") or []
        if isinstance(nodes, list):
            for commit_node in nodes:
                if not isinstance(commit_node, dict):
                    continue
                commit = commit_node.get("commit") or {}
                message = (commit.get("message") or "").strip()
                if message:
                    commit_messages.append(message)

        return GitHubPullRequest(
            html_url=html_url,
            number=number,
            title=title,
            body=body,
            head_ref=head_ref,
            repository_full_name=repo_slug,
            author_login=author_login,
            created_at=created_at,
            commit_messages=tuple(commit_messages),
        )
