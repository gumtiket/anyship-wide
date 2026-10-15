"""AI placeholder 결과와, 검토한 변경을 실제 GitHub에 게시하는 기능."""

import ast
import hashlib
import json

from fastapi import HTTPException

from .github_api import GitHubFailure

FILE_NAME = "anyship_ai_placeholder.py"
TITLE = "[AnyShip 테스트] AI 모듈 미연결 안내 추가"
CONTENT = (
    "# AnyShip: AI 분석 모듈이 아직 연결되지 않았습니다.\n"
    "# 테스트 모드에서 실제 저장소 수정과 PR 생성 흐름을 확인하는 임시 파일입니다.\n"
    "# 실제 AI 분석 결과가 아니며, 테스트 후 이 파일을 제거할 수 있습니다.\n"
)


def make_diff(content):
    return (
        f"diff --git a/{FILE_NAME} b/{FILE_NAME}\nnew file mode 100644\n"
        f"--- /dev/null\n+++ b/{FILE_NAME}\n@@ -0,0 +1,{len(content.splitlines())} @@\n"
        + "".join("+" + line for line in content.splitlines(keepends=True))
    )


def review_hash(row, project):
    # 검토 결과를 대기 중인 파일뿐 아니라 게시 대상에도 묶는다.
    payload = [
        row.id,
        project.repository_id,
        project.full_name,
        project.branch,
        row.base_sha,
        row.base_tree,
        row.branch,
        FILE_NAME,
        row.content,
        row.diff,
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def snapshot(github, token, project):
    sha = github.branch(token, project.full_name, project.branch)["commit"]["sha"]
    tree = github.git_commit(token, project.full_name, sha)["tree"]["sha"]
    entries = github.tree(token, project.full_name, tree)["tree"]
    if any(entry["path"] == FILE_NAME for entry in entries):
        raise HTTPException(
            409,
            f"{FILE_NAME}이 이미 있습니다. 기존 파일을 덮어쓰지 않으므로 다른 테스트 저장소를 사용해 주세요.",
        )
    return sha, tree


def apply(row, project):
    if row.content != CONTENT:
        raise HTTPException(409, "임시 수정 항목의 내용이 변경되었습니다. 다시 분석 요청해 주세요.")
    ast.parse(row.content)
    row.diff = make_diff(row.content)
    row.review_hash = review_hash(row, project)
    row.status = "applied"
    row.error = ""


def validate_pr(pr, project, row):
    expected = f"https://github.com/{project.full_name}/pull/{pr.get('number')}"
    if (
        type(pr.get("number")) is not int
        or pr.get("html_url", "").lower() != expected.lower()
        or pr.get("head", {}).get("sha") != row.commit_sha
        or pr.get("head", {}).get("ref") != row.branch
        or pr.get("base", {}).get("ref") != project.branch
        or pr.get("head", {}).get("repo", {}).get("id") != project.repository_id
        or pr.get("base", {}).get("repo", {}).get("id") != project.repository_id
    ):
        raise HTTPException(
            409, "GitHub PR의 대상 또는 커밋이 검토한 변경과 다릅니다. GitHub에서 확인해 주세요."
        )
    return pr


def publish(github, token, project, row, checkpoint):
    """새 ref만 만든다. 사용자 브랜치를 갱신하거나 PR을 머지하지 않는다.

    커밋 체크포인트를 저장하고 브랜치·PR을 조회하므로, 네트워크 응답이 불확실할 때
    다시 시도해도 안전하다. 이미 있는 ref는 검토한 커밋을 가리켜야 한다.
    """
    name = project.full_name
    remote_sha = github.ref(token, name, row.branch)
    if remote_sha and remote_sha != row.commit_sha:
        raise HTTPException(
            409, "작업 브랜치에 다른 변경이 있습니다. 해당 브랜치를 덮어쓰지 않습니다."
        )
    if remote_sha:
        existing = github.find_pr(token, name, row.branch, project.branch)
        if existing:
            return validate_pr(existing, project, row)

    if github.branch(token, name, project.branch)["commit"]["sha"] != row.base_sha:
        raise HTTPException(
            409,
            "기준 브랜치가 변경되었습니다. 최신 코드로 다시 요청하고 변경 내용을 검토해 주세요.",
        )
    if row.commit_sha:
        commit = github.git_commit(token, name, row.commit_sha)
        if commit["tree"]["sha"] != row.tree_sha or [item["sha"] for item in commit["parents"]] != [
            row.base_sha
        ]:
            raise HTTPException(409, "저장된 커밋이 검토한 기준과 일치하지 않습니다.")
    else:
        row.tree_sha = github.create_tree(token, name, row.base_tree, FILE_NAME, row.content)
        checkpoint()
        row.commit_sha = github.create_commit(
            token,
            name,
            row.base_sha,
            row.tree_sha,
            f"chore: add AnyShip AI placeholder\n\nAnyShip-Change: {row.id}",
        )
        checkpoint()
    # 원격 브랜치에 커밋을 노출하기 전에 다시 확인한다.
    if github.branch(token, name, project.branch)["commit"]["sha"] != row.base_sha:
        raise HTTPException(409, "기준 브랜치가 변경되었습니다. 최신 코드로 다시 요청해 주세요.")
    if not remote_sha:
        try:
            github.create_ref(token, name, row.branch, row.commit_sha)
        except GitHubFailure:
            if github.ref(token, name, row.branch) != row.commit_sha:
                raise
    body = (
        "## 변경 내용\n\nAI 모듈 미연결 상태에서 AnyShip의 수정·PR 흐름을 확인하는 테스트입니다.\n"
        f"`{FILE_NAME}`에 안내 주석만 추가했습니다. 실제 AI 분석 결과가 아닙니다.\n\n"
        f"기준 커밋: `{row.base_sha}`\n\n"
        "검증: Python 문법 검사 완료. 대상 프로젝트의 빌드·테스트는 실행하지 않았습니다.\n\n"
        f"<!-- anyship-change:{row.id} -->"
    )
    try:
        pr = github.create_pr(token, name, row.branch, project.branch, TITLE, body)
    except GitHubFailure:
        pr = github.find_pr(token, name, row.branch, project.branch)
        if not pr:
            raise
    return validate_pr(pr, project, row)
