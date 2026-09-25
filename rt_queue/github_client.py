"""GitHub REST API client for listing open pull requests."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import requests

from rt_queue.api_datetime import parse_api_datetime
from rt_queue.config import Settings


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


class GitHubClient:
    """
    Thin wrapper around the GitHub REST API.

    Lists open, non-draft pull requests not authored by the authenticated user.
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

        PRs are fetched per repository with pagination. Commit messages on the
        PR head are not loaded here; callers may fetch commits when resolving Jira.
        """
        my_login = self.get_authenticated_login()
        results: list[GitHubPullRequest] = []

        for repo_slug in self._settings.github_repositories:
            results.extend(
                self._list_open_prs_for_repo(repo_slug, exclude_login=my_login)
            )

        return results

    def list_pull_request_commits(self, repository_full_name: str, number: int) -> list[str]:
        """
        Return commit message bodies for a pull request (for Jira key extraction).

        Paginates through ``GET /repos/{owner}/{repo}/pulls/{number}/commits``.
        """
        messages: list[str] = []
        page = 1
        per_page = 100

        while True:
            url = (
                f"{self._api_base}/repos/{repository_full_name}/pulls/{number}/commits"
            )
            resp = self._session.get(
                url,
                params={"per_page": per_page, "page": page},
                timeout=60,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            for commit in batch:
                if not isinstance(commit, dict):
                    continue
                commit_obj = commit.get("commit") or {}
                message = (commit_obj.get("message") or "").strip()
                if message:
                    messages.append(message)
            if len(batch) < per_page:
                break
            page += 1

        return messages

    def _list_open_prs_for_repo(
        self, repo_slug: str, *, exclude_login: str
    ) -> list[GitHubPullRequest]:
        """Paginate open pulls for one ``owner/repo``."""
        out: list[GitHubPullRequest] = []
        page = 1
        per_page = 100

        while True:
            url = f"{self._api_base}/repos/{repo_slug}/pulls"
            resp = self._session.get(
                url,
                params={"state": "open", "per_page": per_page, "page": page},
                timeout=60,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break

            for raw in batch:
                pr = self._map_pull_request(raw, repo_slug)
                if pr is None:
                    continue
                if pr.author_login.lower() == exclude_login.lower():
                    continue
                out.append(pr)

            if len(batch) < per_page:
                break
            page += 1

        return out

    def _map_pull_request(
        self, raw: dict[str, Any], repo_slug: str
    ) -> GitHubPullRequest | None:
        """Map API JSON to ``GitHubPullRequest``, or None when draft or invalid."""
        if raw.get("draft") is True:
            return None

        html_url = (raw.get("html_url") or "").strip()
        number = raw.get("number")
        title = (raw.get("title") or "").strip()
        if not html_url or not isinstance(number, int) or not title:
            return None

        user = raw.get("user") or {}
        author_login = (user.get("login") or "").strip()
        if not author_login:
            return None

        head = raw.get("head") or {}
        head_ref = (head.get("ref") or "").strip()

        body_raw = raw.get("body")
        body = (body_raw or "").strip() if body_raw else None
        if body == "":
            body = None

        created_raw = raw.get("created_at")
        created_at = parse_api_datetime(created_raw)
        if created_at is None:
            return None

        return GitHubPullRequest(
            html_url=html_url,
            number=number,
            title=title,
            body=body,
            head_ref=head_ref,
            repository_full_name=repo_slug,
            author_login=author_login,
            created_at=created_at,
        )
