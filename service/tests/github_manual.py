import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import requests
from dotenv import load_dotenv

from github import (
    GitHubError,
    GitHubRepository,
    InvalidRepositoryError,
)


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    github_token: str = field(repr=False)
    github_allowed_repo: str


def get_settings() -> Settings:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_path)

    required_names = ("GITHUB_TOKEN", "GITHUB_ALLOWED_REPO")
    values = {name: os.getenv(name, "").strip() for name in required_names}
    missing_names = [name for name, value in values.items() if not value]

    if missing_names:
        raise ConfigurationError(f"Missing settings: {', '.join(missing_names)}")

    return Settings(
        github_token=values["GITHUB_TOKEN"],
        github_allowed_repo=values["GITHUB_ALLOWED_REPO"],
    )


class PullRequestCreationError(RuntimeError):
    def __init__(self, repository: str, branch: str) -> None:
        super().__init__("PR creation could not be confirmed")
        self.repository = repository
        self.branch = branch


def get_repository(repo_url: str, destination: Path, settings: Settings) -> GitHubRepository:
    repo = GitHubRepository(repo_url, destination, settings.github_token)
    if repo.repository.lower() != settings.github_allowed_repo.lower():
        raise ValueError("허용되지 않은 레포입니다.")
    return repo


def prepare_workspace(
    repo_url: str,
    destination: Path,
    *,
    branch: str | None = None,
    settings: Settings | None = None,
) -> dict:
    repo = get_repository(repo_url, destination, settings or get_settings())
    base_branch = repo.get_default_branch()

    if repo.path.exists():
        workspace = repo.inspect(base_branch=base_branch)
        if branch and branch != workspace.branch:
            raise ValueError("기존 작업 폴더의 브랜치가 요청한 브랜치와 다릅니다.")
    else:
        work_branch = branch or f"ai/manual-{uuid.uuid4().hex[:12]}"
        if work_branch == base_branch or work_branch.startswith("-"):
            raise ValueError("유효한 작업 브랜치 이름을 지정하세요.")
        repo.clone(branch=base_branch)
        workspace = repo.create_branch(work_branch)

    return {
        "status": "prepared",
        "repository": workspace.repository,
        "path": str(workspace.path),
        "branch": workspace.branch,
        "base_branch": base_branch,
    }


def publish_workspace(
    repo_url: str,
    destination: Path,
    *,
    open_pr: bool = False,
    settings: Settings | None = None,
) -> dict:
    repo = get_repository(repo_url, destination, settings or get_settings())
    base_branch = repo.get_default_branch()
    workspace = repo.inspect(base_branch=base_branch)
    commit_result = repo.commit(
        "chore: apply automated code changes",
        author_name="Automation Bot",
        author_email="automation-bot@example.com",
    )
    repo.push()

    result = {
        "status": "pushed",
        "repository": workspace.repository,
        "path": str(workspace.path),
        "branch": workspace.branch,
        "base_branch": base_branch,
        "diff_summary": commit_result.summary,
    }
    if open_pr:
        try:
            pull_request = repo.create_pull_request(
                "Automated Code Changes",
                body="코드 수정 도구가 생성한 자동 코드 변경 사항입니다.",
                base=base_branch,
                draft=True,
            )
        except GitHubError as exc:
            raise PullRequestCreationError(workspace.repository, workspace.branch) from exc
        result["pull_request"] = {
            "number": pull_request.number,
            "url": pull_request.url,
        }
        result["status"] = "created"
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="지정한 작업 폴더에서 GitHub 모듈을 수동 테스트합니다.",
        epilog="service/.env에 GITHUB_TOKEN과 GITHUB_ALLOWED_REPO를 설정하세요.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="저장소 복제 및 작업 브랜치 준비")
    publish = commands.add_parser("publish", help="수정 사항 전체를 커밋하고 푸시")
    for command in (prepare, publish):
        command.add_argument("repo_url", help="GitHub HTTPS 저장소 URL")
        command.add_argument("directory", type=Path, help="저장소를 둘 작업 폴더")
        command.add_argument("--json", action="store_true", dest="json_output")
    prepare.add_argument("--branch", help="새 작업 브랜치 이름")
    publish.add_argument("--pr", action="store_true", help="푸시 후 Draft PR 생성")
    return parser


def report_error(message: str, json_output: bool, **context: str) -> None:
    if json_output:
        error = {"status": "error", "message": message, **context}
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return

    print(f"오류: {message}", file=sys.stderr)
    for key, value in context.items():
        print(f"{key}: {value}", file=sys.stderr)


def report_result(result: dict, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
        return

    print(f"상태: {result['status']}")
    print(f"저장소: {result['repository']}")
    print(f"작업 폴더: {result['path']}")
    print(f"브랜치: {result['branch']} → {result['base_branch']}")
    if result.get("diff_summary"):
        print(result["diff_summary"])
    if result.get("pull_request"):
        print(f"Draft PR: {result['pull_request']['url']}")


def run_manual_test(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "prepare":
            result = prepare_workspace(args.repo_url, args.directory, branch=args.branch)
        else:
            result = publish_workspace(args.repo_url, args.directory, open_pr=args.pr)
    except PullRequestCreationError as exc:
        report_error(
            "브랜치 푸시 후 PR 생성 결과를 확인하지 못했습니다. 재실행 전에 GitHub를 확인하세요.",
            args.json_output,
            repository=exc.repository,
            branch=exc.branch,
        )
        return 3
    except (ConfigurationError, InvalidRepositoryError) as exc:
        report_error(str(exc), args.json_output)
        return 2
    except (GitHubError, subprocess.SubprocessError, requests.RequestException, OSError):
        report_error(
            "Git 또는 GitHub 작업에 실패했습니다. 설치·권한·네트워크를 확인하세요.",
            args.json_output,
        )
        return 1
    except ValueError as exc:
        report_error(str(exc), args.json_output)
        return 2
    except KeyboardInterrupt:
        report_error(
            "작업을 중단했습니다. 재실행 전에 GitHub의 브랜치와 PR을 확인하세요.",
            args.json_output,
        )
        return 130

    report_result(result, args.json_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_manual_test())
