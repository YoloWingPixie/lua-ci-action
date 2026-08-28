from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .reporting import PROJECT_MARKER, PULL_REQUEST_MARKER


API_VERSION = "2022-11-28"
BOT_LOGIN = "github-actions[bot]"


@dataclass(frozen=True)
class PullRequestTarget:
    api_url: str
    repository: str
    number: int
    base_sha: str


def target_from_environment(environment: Mapping[str, str]) -> PullRequestTarget | None:
    event_path = environment.get("GITHUB_EVENT_PATH", "")
    if not event_path or not Path(event_path).is_file():
        return None
    payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return None
    number = payload.get("number")
    base = pull_request.get("base")
    repository = environment.get("GITHUB_REPOSITORY", "")
    if (
        not isinstance(number, int)
        or not isinstance(base, dict)
        or not isinstance(base.get("sha"), str)
        or not repository
    ):
        raise ValueError("invalid pull request event payload")
    return PullRequestTarget(
        api_url=environment.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        repository=repository,
        number=number,
        base_sha=base["sha"],
    )


class GitHubCommentClient:
    def __init__(self, token: str, target: PullRequestTarget):
        self.token = token
        self.target = target

    def publish(self, project: str, pull_request: str) -> None:
        comments = self._comments()
        self._upsert(comments, PROJECT_MARKER, project)
        self._upsert(comments, PULL_REQUEST_MARKER, pull_request)

    def _comments(self) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            path = (
                f"/repos/{self.target.repository}/issues/{self.target.number}/comments"
                f"?per_page=100&page={page}"
            )
            payload = self._request("GET", path)
            if not isinstance(payload, list):
                raise ValueError("invalid GitHub comments response")
            comments.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                return comments
            page += 1

    def _upsert(self, comments: list[dict[str, Any]], marker: str, body: str) -> None:
        existing = next(
            (
                comment
                for comment in comments
                if marker in str(comment.get("body", ""))
                and isinstance(comment.get("user"), dict)
                and comment["user"].get("login") == BOT_LOGIN
                and isinstance(comment.get("id"), int)
            ),
            None,
        )
        if existing is None:
            path = f"/repos/{self.target.repository}/issues/{self.target.number}/comments"
            self._request("POST", path, {"body": body})
            return
        path = f"/repos/{self.target.repository}/issues/comments/{existing['id']}"
        self._request("PATCH", path, {"body": body})

    def _request(self, method: str, path: str, payload: dict[str, str] | None = None) -> object:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            urllib.parse.urljoin(f"{self.target.api_url}/", path.lstrip("/")),
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "lua-ci-action",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read()
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"GitHub comment request failed with HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise RuntimeError("GitHub comment request failed") from error
        return json.loads(content) if content else None
