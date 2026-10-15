"""분석 상태 저장과, 검토를 마친 여러 파일 변경의 게시."""

import ast
import difflib
import hashlib
import json
import time

from fastapi import HTTPException
from sqlalchemy import delete, select, update

from .analysis_source import MAX_FILE_BYTES, AnalysisError, excluded, safe_path
from .db import AnalysisRun, AnalysisSlot
from .github_api import GitHubFailure

ACTIVE = ("queued", "running")


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def review_hash(row):
    return hashlib.sha256(
        encoded(
            [
                row.id,
                row.repository_id,
                row.repository,
                row.base_branch,
                row.base_sha,
                row.base_tree,
                row.branch,
                row.files_json,
                row.diff,
                row.report_json,
            ]
        ).encode()
    ).hexdigest()


def make_diff(before, after):
    result = []
    for name in sorted(after):
        for line in difflib.unified_diff(
            before.get(name, "").splitlines(keepends=True),
            after[name].splitlines(keepends=True),
            fromfile=f"a/{name}" if name in before else "/dev/null",
            tofile=f"b/{name}",
        ):
            result.append(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            )
    return "".join(result)


def validated_bundle(snapshot, result):
    if result.base_sha != snapshot.base_sha:
        raise AnalysisError("result_sha_mismatch", "분석 결과의 기준 커밋이 다릅니다.")
    changes, seen, total = [], set(), 0
    for proposed in result.files:
        name, content = proposed.path, proposed.content
        if not safe_path(name) or excluded(name) or name.casefold() in seen or "\0" in content:
            raise AnalysisError("invalid_proposal", "허용되지 않은 변경 파일입니다.")
        if any(
            old.startswith(name.casefold() + "/") or name.casefold().startswith(old + "/")
            for old in seen
        ):
            raise AnalysisError("invalid_proposal_path", "변경 파일끼리 경로가 충돌합니다.")
        seen.add(name.casefold())
        entry = snapshot.entries.get(name)
        if proposed.before_sha != (entry["sha"] if entry else None) or (
            entry and name not in snapshot.files
        ):
            raise AnalysisError("invalid_proposal_base", "변경 파일의 원본이 기준 소스와 다릅니다.")
        # 무시된 입력까지 포함해, 대소문자만 다른 경로 충돌과 파일↔디렉터리 교체를 거부한다.
        # 소스 스냅숏은 파일 시스템 링크를 따라가지 않는다.
        for old in snapshot.entries:
            if old != name and (
                old.casefold() == name.casefold()
                or old.casefold().startswith(name.casefold() + "/")
                or name.casefold().startswith(old.casefold() + "/")
            ):
                raise AnalysisError(
                    "invalid_proposal_path", "기존 파일 경로와 충돌하는 변경입니다."
                )
        size = len(content.encode())
        total += size
        if size > MAX_FILE_BYTES or total > 2 * 1024 * 1024:
            raise AnalysisError("proposal_too_large", "변경안 크기를 초과했습니다.")
        if name.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError:
                raise AnalysisError(
                    "proposal_syntax", "변경안의 Python 문법을 확인할 수 없습니다."
                ) from None
        if content != snapshot.files.get(name):
            changes.append({**proposed.model_dump(), "mode": entry["mode"] if entry else "100644"})
    before = {f["path"]: snapshot.files[f["path"]] for f in changes if f["path"] in snapshot.files}
    after = {f["path"]: f["content"] for f in changes}
    return changes, make_diff(before, after)


def run_json(row, *, detail=True):
    data = {
        "id": row.id,
        "status": row.status,
        "provider": row.provider,
        "target_env": row.target_env,
        "base_sha": row.base_sha,
        "base_branch": row.base_branch,
        "branch": row.branch,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
        "publish_status": row.publish_status,
        "commit_sha": row.commit_sha,
        "pr_url": row.pr_url,
        "pr_number": row.pr_number,
        "busy": row.status in ACTIVE or row.lease_until > int(time.time()),
        "error_code": row.error_code,
        "error": row.error,
    }
    if detail:
        data.update(
            report=json.loads(row.report_json),
            files=json.loads(row.files_json),
            diff=row.diff,
            review_hash=row.review_hash,
            logs=json.loads(row.logs_json),
        )
    return data


def interrupt(session, runtime_id=None):
    query = update(AnalysisRun).where(AnalysisRun.status.in_(ACTIVE))
    if runtime_id:
        query = query.where(AnalysisRun.runtime_id == runtime_id)
    ids = select(AnalysisRun.id).where(AnalysisRun.status.in_(ACTIVE))
    if runtime_id:
        ids = ids.where(AnalysisRun.runtime_id == runtime_id)
    session.execute(
        update(AnalysisSlot)
        .where(AnalysisSlot.active_id.in_(ids))
        .values(active_id=None, lease_until=0)
    )
    session.execute(
        query.values(
            status="interrupted",
            finished_at=int(time.time()),
            error_code="worker_interrupted",
            error="서버 종료로 분석이 중단됐습니다. 새 분석을 요청해 주세요.",
        )
    )
    session.commit()


def clear_project(session, project_id):
    # 오래된 조회 결과로 지우지 않고, PR 선점과 원자적으로 경쟁한다.
    session.execute(
        delete(AnalysisRun).where(
            AnalysisRun.project_id == project_id,
            AnalysisRun.status.notin_(ACTIVE),
            AnalysisRun.lease_until <= int(time.time()),
        )
    )
    if session.scalar(select(AnalysisRun.id).where(AnalysisRun.project_id == project_id).limit(1)):
        raise HTTPException(409, "AI 작업 처리 중입니다. 완료 후 연결을 삭제해 주세요.")
    session.execute(delete(AnalysisSlot).where(AnalysisSlot.project_id == project_id))


def publish(github, token, row, checkpoint):
    def validate(pr):
        if (
            type(pr.get("number")) is not int
            or pr.get("html_url", "").lower()
            != f"https://github.com/{row.repository}/pull/{pr.get('number')}".lower()
            or pr.get("head", {}).get("sha") != row.commit_sha
            or pr.get("head", {}).get("ref") != row.branch
            or pr.get("base", {}).get("ref") != row.base_branch
            or any(
                pr.get(side, {}).get("repo", {}).get("id") != row.repository_id
                for side in ("head", "base")
            )
        ):
            raise HTTPException(409, "GitHub PR이 검토한 대상 또는 커밋과 다릅니다.")
        return pr

    remote = github.ref(token, row.repository, row.branch)
    if remote and remote != row.commit_sha:
        raise HTTPException(409, "작업 브랜치에 다른 변경이 있습니다.")
    if remote:
        existing = github.find_pr(token, row.repository, row.branch, row.base_branch)
        if existing:
            return validate(existing)

    def current_base():
        if github.branch(token, row.repository, row.base_branch)["commit"]["sha"] != row.base_sha:
            raise HTTPException(
                409, "기준 브랜치가 변경되었습니다. 새로 분석하고 다시 검토해 주세요."
            )

    current_base()
    if row.commit_sha:
        commit = github.git_commit(token, row.repository, row.commit_sha)
        if commit["tree"]["sha"] != row.tree_sha or [p["sha"] for p in commit["parents"]] != [
            row.base_sha
        ]:
            raise HTTPException(409, "저장된 커밋이 검토한 변경과 다릅니다.")
    else:
        if not row.tree_sha:
            row.tree_sha = github.create_files_tree(
                token, row.repository, row.base_tree, json.loads(row.files_json)
            )
            checkpoint()
        row.commit_sha = github.create_commit(
            token,
            row.repository,
            row.base_sha,
            row.tree_sha,
            f"chore: propose AnyShip deployment changes\n\nAnyShip-Analysis: {row.id}",
        )
        checkpoint()
    current_base()
    if not remote:
        try:
            github.create_ref(token, row.repository, row.branch, row.commit_sha)
        except GitHubFailure:
            if github.ref(token, row.repository, row.branch) != row.commit_sha:
                raise
    report = json.loads(row.report_json)
    transform, gate = report.get("transformation", {}), report.get("gate_report", {})
    body = (
        f"## 변경 내용\n\n사용자가 검토한 AI 변경안입니다.\n\n기준 SHA: `{row.base_sha}`\n"
        f"제공자: `{row.provider}` · 결과: `{report.get('status')}`\n\n"
        f"수정 반영 {len(transform.get('addressed_ids', []))}개 · 보류 {len(transform.get('deferred_ids', []))}개\n"
        f"위험 변경 승인 필요: {bool(transform.get('needs_approval'))} (게시 요청에서 확인)\n\n"
        f"검증: patch/문법 확인, gate `{gate.get('status', 'skipped')}`, pr_eligible=false.\n"
        "대상 앱의 빌드·기동·전체 기능은 검증하지 않았습니다. 기존 데이터 자동 이전은 지원하지 않습니다.\n"
        "이 Draft PR은 검토용 제안이며 배포 승인이나 자동 병합을 의미하지 않습니다.\n\n"
        f"<!-- anyship-analysis:{row.id} -->"
    )
    try:
        pr = github.create_pr(
            token,
            row.repository,
            row.branch,
            row.base_branch,
            "[AnyShip] 배포 준비 변경안 검토",
            body,
        )
    except GitHubFailure:
        pr = github.find_pr(token, row.repository, row.branch, row.base_branch)
        if not pr:
            raise
    return validate(pr)
