import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lua_ci_action import github_comments, reporting


class GitHubCommentsTest(unittest.TestCase):
    def test_loads_pull_request_target_from_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "event.json"
            event_path.write_text(
                json.dumps(
                    {
                        "number": 42,
                        "pull_request": {"base": {"sha": "base-sha"}},
                    }
                ),
                encoding="utf-8",
            )

            target = github_comments.target_from_environment(
                {
                    "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_REPOSITORY": "owner/repository",
                    "GITHUB_API_URL": "https://example.test/api",
                }
            )

        self.assertEqual(
            github_comments.PullRequestTarget(
                "https://example.test/api", "owner/repository", 42, "base-sha"
            ),
            target,
        )

    def test_publish_updates_project_comment_and_creates_pull_request_comment(self) -> None:
        target = github_comments.PullRequestTarget(
            "https://api.github.com", "owner/repository", 42, "base-sha"
        )
        client = github_comments.GitHubCommentClient("token", target)
        comments = [
            {
                "id": 7,
                "body": f"{reporting.PROJECT_MARKER}\nold",
                "user": {"login": github_comments.BOT_LOGIN},
            }
        ]

        with mock.patch.object(
            client,
            "_request",
            side_effect=(comments, {}, {}),
        ) as request:
            client.publish("project", "pull request")

        self.assertEqual(
            mock.call("PATCH", "/repos/owner/repository/issues/comments/7", {"body": "project"}),
            request.call_args_list[1],
        )
        self.assertEqual(
            mock.call(
                "POST",
                "/repos/owner/repository/issues/42/comments",
                {"body": "pull request"},
            ),
            request.call_args_list[2],
        )


if __name__ == "__main__":
    unittest.main()
