import base64
import hashlib
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from . import (
    analyses,
    analysis_api,
    aws_onboarding,
    code_changes,
    demo_changes,
    deploy_api,
    deployments,
    mock_deployments,
    onboarding,
    onprem_api,
)
from .aws_adapter import AwsAdapter
from .config import Settings
from .db import (
    CodeChange,
    DemoChange,
    LoginSession,
    Membership,
    OAuthAttempt,
    Project,
    RepositoryConnection,
    User,
    Workspace,
    database,
)
from .github_api import DemoGitHub, GitHubAPI, GitHubFailure

auth_logger = logging.getLogger("anyship.auth")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ProjectInput(BaseModel):
    repository_url: str = Field(default="", max_length=2048)
    installation_id: int | None = Field(default=None, gt=0)
    repository_id: int | None = Field(default=None, gt=0)
    branch: str = Field(min_length=1, max_length=255)


class RepositoryInput(BaseModel):
    repository_url: str = Field(min_length=1, max_length=2048)


class AnalysisInput(BaseModel):
    restart: bool = False


class DemoReviewInput(BaseModel):
    review_hash: str = Field(min_length=64, max_length=64)


def create_app(
    settings: Settings,
    gateway=None,
    aws_adapter: AwsAdapter | None = None,
    deploy_runner=None,
    ai_provider=None,
):
    engine, sessions = database(settings.database_url)
    cipher = Fernet(settings.token_key.encode() if settings.token_key else Fernet.generate_key())
    github = gateway or (DemoGitHub() if settings.demo else GitHubAPI())
    analysis_runner = None
    if settings.ai_mode == "bronze":
        from .analysis_runner import AnalysisRunner

        analysis_runner = AnalysisRunner(settings, sessions, github, provider=ai_provider)
    mock_runner = (
        mock_deployments.MockRunner(settings, sessions)
        if settings.deployment_mode == "mock"
        else None
    )
    if deploy_runner is None and settings.deployment_mode == "real":
        from .deploy_runner import DeployRunner  # 실제 모드에서만 불러온다

        deploy_runner = DeployRunner.from_settings(settings, sessions)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if mock_runner:
                mock_runner.start()
            if deploy_runner:
                deploy_runner.start()
            if analysis_runner:
                analysis_runner.start()
            yield
        finally:
            if analysis_runner:
                analysis_runner.close()
            if deploy_runner:
                deploy_runner.close()
            if mock_runner:
                mock_runner.close()
            engine.dispose()

    app = FastAPI(title="AnyShip", lifespan=lifespan)
    app.state.engine = engine
    app.state.sessions = sessions
    app.state.mock_runner = mock_runner
    app.state.deploy_runner = deploy_runner
    app.state.analysis_runner = analysis_runner

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        if request.url.path.startswith("/api/onprem/"):
            return JSONResponse(
                {"detail": "환경 등록 요청의 입력값을 확인해 주세요."}, status_code=422
            )
        if "/mock-deployment" in request.url.path:
            # Reject unsupported secret inputs without echoing their values.
            return JSONResponse(
                {
                    "detail": "모의 작업의 입력값을 확인해 주세요. 지원하는 환경·작업과 7~40자리 16진수 버전을 사용하세요."
                },
                status_code=422,
            )
        if request.method == "POST" and request.url.path.endswith("/deployment/jobs"):
            # 이 본문에는 사용자 비밀이 들어 있다. 기본 422 응답은 입력값을 그대로 돌려주므로 값을 싣지 않는다.
            return JSONResponse(
                {
                    "detail": "배포 요청의 입력값을 확인해 주세요. 비밀의 이름은 대문자·숫자·밑줄만 쓰고 값은 4096자 이하여야 합니다."
                },
                status_code=422,
            )
        return await request_validation_exception_handler(request, error)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(GitHubFailure)
    async def github_error(request, error):
        messages = {
            401: "GitHub 인증이 만료되었습니다. 다시 로그인해 주세요.",
            403: "저장소 권한 또는 GitHub 요청 제한을 확인해 주세요.",
            404: "접근할 수 없는 GitHub 리소스입니다.",
            429: "GitHub 요청 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.",
        }
        return JSONResponse(
            {
                "detail": messages.get(
                    error.status, "GitHub 연결에 실패했습니다. 잠시 후 다시 시도해 주세요."
                )
            },
            status_code=error.status,
        )

    def db():
        with sessions() as session:
            yield session

    def origin_check(request):
        if request.headers.get("origin") != settings.app_origin:
            raise HTTPException(403, "허용되지 않은 요청 출처입니다.")

    def current(request: Request, session=Depends(db)):
        value = request.cookies.get("app_session", "")
        login = session.get(LoginSession, digest(value)) if value else None
        if not login or login.expires_at <= int(time.time()):
            raise HTTPException(401, "로그인이 필요합니다.")
        if not session.get(Membership, (login.user_id, login.workspace_id)):
            raise HTTPException(403, "워크스페이스 접근 권한이 없습니다.")
        user = session.get(User, login.user_id)
        if (user.github_id < 0) != settings.demo:
            raise HTTPException(401, "이 실행 모드에서 사용할 수 없는 세션입니다.")
        return login

    def mutation(request: Request, login=Depends(current)):
        origin_check(request)
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), login.csrf):
            raise HTTPException(403, "요청 확인에 실패했습니다. 페이지를 새로고침해 주세요.")
        return login

    def access_token(login):
        try:
            return cipher.decrypt(login.token_cipher.encode()).decode()
        except InvalidToken:
            raise HTTPException(401, "다시 로그인해 주세요.") from None

    app.include_router(onboarding.router(settings, github, db, current, mutation, access_token))
    app.include_router(aws_onboarding.router(settings, aws_adapter, db, current, mutation))
    app.include_router(mock_deployments.router(settings, mock_runner, db, current, mutation))
    app.include_router(
        deploy_api.router(settings, deploy_runner, db, current, mutation, access_token)
    )
    app.include_router(onprem_api.router(settings, deploy_runner, db, current, mutation))

    def installations(login):
        return github.installations(access_token(login))

    def repositories(login, installation):
        if not any(item["id"] == installation for item in installations(login)):
            raise HTTPException(404, "접근할 수 없는 GitHub 연결입니다.")
        return github.repositories(access_token(login), installation)

    def repository(login, installation, repository_id):
        for item in repositories(login, installation):
            if item["id"] == repository_id:
                if item.get("archived") or not item.get("permissions", {}).get("push"):
                    raise HTTPException(
                        403, "프로젝트 등록에는 활성 저장소의 쓰기 권한이 필요합니다."
                    )
                return item
        raise HTTPException(404, "접근할 수 없는 저장소입니다.")

    def create_login(session, profile, token, ttl, response):
        user = session.scalar(select(User).where(User.github_id == profile["id"]))
        if user is None:
            user = User(
                id=str(uuid.uuid4()),
                github_id=profile["id"],
                login=profile["login"],
                name=profile.get("name") or profile["login"],
            )
            workspace = Workspace(id=str(uuid.uuid4()), name=f"{profile['login']}'s workspace")
            session.add_all([user, workspace])
            session.flush()
            session.add(Membership(user_id=user.id, workspace_id=workspace.id, role="owner"))
            workspace_id = workspace.id
        else:
            user.login = profile["login"]
            user.name = profile.get("name") or profile["login"]
            workspace_id = session.scalar(
                select(Membership.workspace_id).where(Membership.user_id == user.id)
            )
        raw = secrets.token_urlsafe(32)
        expires = int(time.time()) + max(1, min(int(ttl), 8 * 3600))
        session.add(
            LoginSession(
                id=digest(raw),
                user_id=user.id,
                workspace_id=workspace_id,
                token_cipher=cipher.encrypt(token.encode()).decode(),
                csrf=secrets.token_urlsafe(32),
                expires_at=expires,
            )
        )
        session.commit()
        response.set_cookie(
            "app_session",
            raw,
            max_age=expires - int(time.time()),
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        return {
            "demo": settings.demo,
            "github_configured": settings.github_configured,
            "onprem_available": getattr(deploy_runner, "onprem", None) is not None,
            "aws_available": settings.aws_configured,
            "aws_verification_available": settings.aws_configured and aws_adapter is not None,
            "aws_regions": list(settings.aws_regions),
            "aws_setup_issues": settings.aws_setup_issues,
            "ai_available": settings.ai_mode == "bronze",
            "ai_mode": settings.ai_mode,
            "ai_provider": settings.ai_provider,
            "demo_changes_available": settings.demo,
            "deployment_mode": settings.deployment_mode,
            "callback_url": settings.app_origin + "/api/auth/github/callback",
        }

    @app.post("/api/auth/demo")
    def demo_login(request: Request, session=Depends(db)):
        if not settings.demo:
            raise HTTPException(404)
        origin_check(request)
        # Each browser demo login gets a separate identity and workspace.
        profile = {"id": -secrets.randbelow(2**52) - 1, "login": "demo-user", "name": "데모 사용자"}
        return create_login(session, profile, "demo", 3600, JSONResponse({"ok": True}))

    @app.get("/api/auth/github/start")
    def start(session=Depends(db)):
        if settings.demo or not settings.github_configured:
            raise HTTPException(
                503, "지금은 GitHub 로그인을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."
            )
        state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        now = int(time.time())
        session.execute(delete(OAuthAttempt).where(OAuthAttempt.expires_at < now))
        session.execute(delete(LoginSession).where(LoginSession.expires_at < now))
        session.add(
            OAuthAttempt(
                id=digest(state),
                browser_hash=digest(browser),
                verifier=verifier,
                expires_at=now + 600,
            )
        )
        session.commit()
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        query = urlencode(
            {
                "client_id": settings.github_client_id,
                "redirect_uri": settings.app_origin + "/api/auth/github/callback",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        response = RedirectResponse(
            "https://github.com/login/oauth/authorize?" + query, status_code=302
        )
        response.set_cookie(
            "app_oauth",
            browser,
            max_age=600,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            path="/api/auth/github",
        )
        return response

    @app.get("/api/auth/github/callback")
    def callback(
        request: Request, state: str = "", code: str = "", error: str = "", session=Depends(db)
    ):
        def failed(reason):
            auth_logger.warning("GitHub login failed: %s", reason)
            response = RedirectResponse("/?" + urlencode({"auth_error": reason}), status_code=302)
            response.delete_cookie("app_oauth", path="/api/auth/github")
            return response

        if settings.demo or not settings.github_configured:
            return failed("not_configured")
        attempt = session.get(OAuthAttempt, digest(state))
        binding = request.cookies.get("app_oauth", "")
        if (
            not attempt
            or attempt.expires_at <= int(time.time())
            or not binding
            or not secrets.compare_digest(attempt.browser_hash, digest(binding))
        ):
            return failed("invalid_state")
        verifier = attempt.verifier
        consumed = session.execute(delete(OAuthAttempt).where(OAuthAttempt.id == attempt.id))
        session.commit()
        if consumed.rowcount != 1:
            return failed("invalid_state")
        if error == "access_denied":
            return failed("access_denied")
        if not code:
            return failed("missing_code")
        try:
            data = github.exchange(settings, code, verifier)
            profile = github.profile(data["access_token"])
            success = RedirectResponse("/", status_code=302)
            success.delete_cookie("app_oauth", path="/api/auth/github")
            old = request.cookies.get("app_session")
            if old:
                session.execute(delete(LoginSession).where(LoginSession.id == digest(old)))
            return create_login(
                session, profile, data["access_token"], data.get("expires_in", 28800), success
            )
        except GitHubFailure as failure:
            return failed(failure.code)
        except (KeyError, ValueError):
            return failed("invalid_response")

    @app.get("/api/me")
    def me(login=Depends(current), session=Depends(db)):
        user = session.get(User, login.user_id)
        workspace = session.get(Workspace, login.workspace_id)
        return {
            "user": {"login": user.login, "name": user.name},
            "workspace": {"id": workspace.id, "name": workspace.name},
            "csrf_token": login.csrf,
        }

    @app.post("/api/auth/logout")
    def logout(login=Depends(mutation), session=Depends(db)):
        session.execute(delete(LoginSession).where(LoginSession.id == login.id))
        session.commit()
        response = JSONResponse({"ok": True})
        response.delete_cookie("app_session", path="/")
        return response

    @app.get("/api/github/install")
    def install(login=Depends(current)):
        return RedirectResponse("/#connect", status_code=302)

    @app.get("/api/github/installations")
    def installation_list(login=Depends(current)):
        return [
            {"id": item["id"], "account": item["account"]["login"]} for item in installations(login)
        ]

    def resolve_repository(login, url):
        result = onboarding.inspect_access(
            github, access_token(login), onboarding.repository_name(url), settings.demo
        )
        if result["status"] != "ready":
            raise HTTPException(
                404 if result["status"] == "authorization_required" else 403, result["message"]
            )
        return result["installation_id"], result["repository"]

    @app.post("/api/github/resolve")
    def resolve(body: RepositoryInput, login=Depends(mutation)):
        installation_id, repo = resolve_repository(login, body.repository_url)
        return {
            "installation_id": installation_id,
            "id": repo["id"],
            "full_name": repo["full_name"],
            "private": repo["private"],
            "default_branch": repo["default_branch"],
            "branches": [
                {"name": b["name"], "sha": b["commit"]["sha"]}
                for b in github.branches(access_token(login), repo["full_name"])
            ],
        }

    @app.get("/api/github/installations/{installation_id}/repositories")
    def repository_list(installation_id: int, login=Depends(current)):
        return [
            {
                "id": item["id"],
                "full_name": item["full_name"],
                "description": item.get("description"),
                "private": item["private"],
                "default_branch": item["default_branch"],
                "language": item.get("language"),
                "can_write": bool(item.get("permissions", {}).get("push"))
                and not item.get("archived", False),
            }
            for item in repositories(login, installation_id)
        ]

    @app.get("/api/github/installations/{installation_id}/repositories/{repository_id}/branches")
    def branch_list(installation_id: int, repository_id: int, login=Depends(current)):
        repo = repository(login, installation_id, repository_id)
        return [
            {"name": item["name"], "sha": item["commit"]["sha"]}
            for item in github.branches(access_token(login), repo["full_name"])
        ]

    def project_json(item):
        return {
            "id": item.id,
            "full_name": item.full_name,
            "branch": item.branch,
            "base_sha": item.base_sha,
            "created_at": item.created_at,
            "status": "connected",
        }

    @app.get("/api/projects")
    def projects(login=Depends(current), session=Depends(db)):
        rows = session.scalars(
            select(Project)
            .where(Project.workspace_id == login.workspace_id)
            .order_by(Project.created_at.desc())
        )
        return [project_json(row) for row in rows]

    @app.get("/api/projects/{project_id}")
    def project_detail(project_id: str, login=Depends(current), session=Depends(db)):
        row = session.scalar(
            select(Project).where(
                Project.id == project_id, Project.workspace_id == login.workspace_id
            )
        )
        if not row:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        return project_json(row)

    @app.delete("/api/projects/{project_id}", status_code=204)
    def delete_project(project_id: str, login=Depends(mutation), session=Depends(db)):
        project = session.scalar(
            select(Project)
            .where(
                Project.id == project_id,
                Project.workspace_id == login.workspace_id,
            )
            .with_for_update()
        )
        if project is None:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        mock_deployments.clear_targets(session, project_id=project_id)
        deployments.clear_real_targets(session, project_id=project_id)
        analyses.clear_project(session, project_id)
        # Conditional deletes compete with workflow claims on the same rows.
        # Keep all changes in one transaction so a busy child rolls back cleanup.
        session.execute(
            delete(CodeChange).where(
                CodeChange.project_id == project_id,
                CodeChange.lease_until <= int(time.time()),
            )
        )
        session.execute(
            delete(DemoChange).where(
                DemoChange.project_id == project_id,
                DemoChange.status.notin_(("applying", "publishing")),
            )
        )
        if session.scalar(
            select(CodeChange.id).where(CodeChange.project_id == project_id)
        ) or session.scalar(select(DemoChange.id).where(DemoChange.project_id == project_id)):
            raise HTTPException(409, "작업 처리 중입니다. 완료 후 저장소 연결을 삭제해 주세요.")
        session.delete(project)
        session.commit()
        return Response(status_code=204)

    @app.post("/api/projects", status_code=201)
    def add_project(body: ProjectInput, login=Depends(mutation), session=Depends(db)):
        if body.repository_url:
            installation_id, repo = resolve_repository(login, body.repository_url)
        elif body.installation_id and body.repository_id:
            installation_id = body.installation_id
            repo = repository(login, installation_id, body.repository_id)
        else:
            raise HTTPException(422, "GitHub 저장소 URL을 입력해 주세요.")
        branch = github.branch(access_token(login), repo["full_name"], body.branch)
        row = Project(
            id=str(uuid.uuid4()),
            workspace_id=login.workspace_id,
            repository_id=repo["id"],
            installation_id=installation_id,
            full_name=repo["full_name"],
            branch=branch["name"],
            base_sha=branch["commit"]["sha"],
            created_by=login.user_id,
            created_at=int(time.time()),
        )
        session.add(row)
        try:
            session.execute(
                delete(RepositoryConnection).where(
                    RepositoryConnection.user_id == login.user_id,
                    RepositoryConnection.workspace_id == login.workspace_id,
                    func.lower(RepositoryConnection.repository_url)
                    == f"https://github.com/{repo['full_name']}".lower(),
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(
                409, "이미 연결한 저장소입니다. 프로젝트 목록에서 확인해 주세요."
            ) from None
        return project_json(row)

    def owned_project(project_id, login, session):
        row = session.scalar(
            select(Project).where(
                Project.id == project_id, Project.workspace_id == login.workspace_id
            )
        )
        if not row:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        return row

    def real_project(project_id, login, session):
        if settings.demo:
            raise HTTPException(409, "실제 GitHub 로그인 모드로 실행해 주세요.")
        project = owned_project(project_id, login, session)
        repo = repository(login, project.installation_id, project.repository_id)
        if repo["full_name"].lower() != project.full_name.lower():
            raise HTTPException(
                409, "저장소 주소가 변경되었습니다. 프로젝트 연결 정보를 확인해 주세요."
            )
        installation = next(
            (i for i in installations(login) if i["id"] == project.installation_id), None
        )
        if not installation:
            raise HTTPException(404, "GitHub App 연결이 해제되었습니다.")
        permissions = installation.get("permissions", {})
        if installation.get("suspended_at") or any(
            permissions.get(key) != "write" for key in ("contents", "pull_requests")
        ):
            raise HTTPException(
                403,
                "GitHub App의 Contents와 Pull requests를 Read and write로 설정하고 설치 권한을 승인해 주세요.",
            )
        return project

    app.include_router(
        analysis_api.router(
            settings,
            analysis_runner,
            github,
            db,
            current,
            mutation,
            owned_project,
            real_project,
            access_token,
        )
    )

    def change_json(row):
        if not row:
            return None
        return {
            "id": row.id,
            "status": row.status,
            "provider": "placeholder",
            "file": code_changes.FILE_NAME,
            "title": code_changes.TITLE,
            "content": row.content,
            "diff": row.diff,
            "review_hash": row.review_hash,
            "base_sha": row.base_sha,
            "branch": row.branch,
            "commit_sha": row.commit_sha,
            "pr_url": row.pr_url,
            "pr_number": row.pr_number,
            "busy": row.lease_until > int(time.time()),
            "error": row.error,
        }

    def require_placeholder():
        if settings.ai_mode != "placeholder":
            raise HTTPException(
                503,
                "AI 분석 모듈이 아직 연결되지 않았습니다. 테스트에는 APP_AI_MODE=placeholder 설정이 필요합니다.",
            )

    def get_change(project_id, session):
        row = session.scalar(select(CodeChange).where(CodeChange.project_id == project_id))
        if not row:
            raise HTTPException(409, "먼저 분석 요청으로 수정 항목을 등록해 주세요.")
        return row

    @app.get("/api/projects/{project_id}/changes")
    def changes(project_id: str, login=Depends(current), session=Depends(db)):
        owned_project(project_id, login, session)
        return change_json(
            session.scalar(select(CodeChange).where(CodeChange.project_id == project_id))
        )

    @app.post("/api/projects/{project_id}/analysis")
    def analyze(project_id: str, body: AnalysisInput, login=Depends(mutation), session=Depends(db)):
        require_placeholder()
        project = real_project(project_id, login, session)
        row = session.scalar(select(CodeChange).where(CodeChange.project_id == project_id))
        if row and (not body.restart or row.status == "pr_created"):
            return change_json(row)
        if row:
            # The same lease protects refresh, apply and publish from each other.
            claimed = session.execute(
                update(CodeChange)
                .where(
                    CodeChange.id == row.id,
                    CodeChange.lease_until <= int(time.time()),
                    CodeChange.status != "pr_created",
                )
                .values(lease_until=int(time.time()) + 600)
            )
            session.commit()
            if claimed.rowcount != 1:
                raise HTTPException(409, "작업 처리 중입니다. 완료 후 다시 시도해 주세요.")
        try:
            if row and row.commit_sha:
                # Recover a PR whose response was lost before allowing a restart.
                existing = github.find_pr(
                    access_token(login), project.full_name, row.branch, project.branch
                )
                if existing:
                    code_changes.validate_pr(existing, project, row)
                    row.pr_url, row.pr_number, row.status = (
                        existing["html_url"],
                        existing["number"],
                        "pr_created",
                    )
                    row.lease_until = 0
                    session.commit()
                    return change_json(row)
            sha, tree = code_changes.snapshot(github, access_token(login), project)
        except Exception:
            if row:
                row.lease_until = 0
                session.commit()
            raise
        if row is None:
            row = CodeChange(id=str(uuid.uuid4()), project_id=project.id)
            session.add(row)
        row.base_sha, row.base_tree = sha, tree
        row.branch = "anyship/ai-placeholder-" + uuid.uuid4().hex[:16]
        row.content, row.status = code_changes.CONTENT, "proposed"
        row.diff = row.review_hash = row.tree_sha = row.commit_sha = row.error = ""
        row.lease_until = 0
        project.base_sha = sha
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            row = get_change(project_id, session)
        return change_json(row)

    @app.post("/api/projects/{project_id}/changes/apply")
    def apply_change(project_id: str, login=Depends(mutation), session=Depends(db)):
        require_placeholder()
        project = real_project(project_id, login, session)
        row = get_change(project_id, session)
        if row.status in ("applied", "pr_created"):
            return change_json(row)
        claimed = session.execute(
            update(CodeChange)
            .where(
                CodeChange.id == row.id,
                CodeChange.status == "proposed",
                CodeChange.lease_until <= int(time.time()),
            )
            .values(lease_until=int(time.time()) + 600)
        )
        session.commit()
        if claimed.rowcount != 1:
            raise HTTPException(409, "작업 처리 중입니다. 잠시 후 다시 확인해 주세요.")
        try:
            code_changes.apply(row, project)
        finally:
            row.lease_until = 0
            session.commit()
        return change_json(row)

    @app.post("/api/projects/{project_id}/changes/pr")
    def publish_change(
        project_id: str, body: DemoReviewInput, login=Depends(mutation), session=Depends(db)
    ):
        require_placeholder()
        project = real_project(project_id, login, session)
        row = get_change(project_id, session)
        if (
            not row.review_hash
            or row.content != code_changes.CONTENT
            or row.diff != code_changes.make_diff(row.content)
            or not secrets.compare_digest(body.review_hash, row.review_hash)
            or not secrets.compare_digest(row.review_hash, code_changes.review_hash(row, project))
        ):
            raise HTTPException(409, "변경 내용을 다시 검토한 뒤 PR 생성을 요청해 주세요.")
        if row.status == "pr_created":
            return change_json(row)
        claimed = session.execute(
            update(CodeChange)
            .where(
                CodeChange.id == row.id,
                CodeChange.status.in_(["applied", "publishing"]),
                CodeChange.lease_until <= int(time.time()),
            )
            .values(status="publishing", lease_until=int(time.time()) + 600, error="")
        )
        session.commit()
        if claimed.rowcount != 1:
            raise HTTPException(409, "PR 생성 요청을 처리 중입니다. 잠시 후 다시 확인해 주세요.")
        try:
            session.refresh(row)
            if not secrets.compare_digest(body.review_hash, code_changes.review_hash(row, project)):
                raise HTTPException(
                    409, "검토 이후 작업이 갱신되었습니다. 현재 변경 내용을 다시 확인해 주세요."
                )
            pr = code_changes.publish(github, access_token(login), project, row, session.commit)
            row.pr_url, row.pr_number, row.status = pr["html_url"], pr["number"], "pr_created"
        except (GitHubFailure, HTTPException) as error:
            row.status = "applied"
            row.error = (
                error.detail
                if isinstance(error, HTTPException)
                else "GitHub 게시에 실패했습니다. App 권한·브랜치 규칙·연결을 확인하고 다시 시도해 주세요. 생성된 브랜치와 PR은 재시도 시 확인합니다."
            )
            raise
        finally:
            row.lease_until = 0
            session.commit()
        return change_json(row)

    def demo_project(project_id, login, session):
        if not settings.demo:
            raise HTTPException(404, "테스트 모드에서만 사용할 수 있습니다.")
        row = session.scalar(
            select(Project).where(
                Project.id == project_id, Project.workspace_id == login.workspace_id
            )
        )
        if not row:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        return row

    def demo_json(row):
        if not row:
            return None
        return {
            "id": row.id,
            "status": row.status,
            "mode": "demo",
            "file": demo_changes.FILE_NAME,
            "content": row.content,
            "diff": row.diff,
            "review_hash": row.review_hash,
            "branch": f"demo/placeholder-{row.id[:8]}",
            "commit_sha": row.commit_sha,
            "error": row.error,
            "github_pr_url": None,
            "pr_title": "[테스트] AI 분석 모듈 미연결 안내 추가",
        }

    @app.get("/api/projects/{project_id}/demo-change")
    def get_demo_change(project_id: str, login=Depends(current), session=Depends(db)):
        demo_project(project_id, login, session)
        return demo_json(
            session.scalar(select(DemoChange).where(DemoChange.project_id == project_id))
        )

    @app.post("/api/projects/{project_id}/demo-change")
    def propose_demo_change(project_id: str, login=Depends(mutation), session=Depends(db)):
        demo_project(project_id, login, session)
        row = session.scalar(select(DemoChange).where(DemoChange.project_id == project_id))
        if row:
            return demo_json(row)
        row = DemoChange(id=str(uuid.uuid4()), project_id=project_id, content=demo_changes.CONTENT)
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            row = session.scalar(select(DemoChange).where(DemoChange.project_id == project_id))
        return demo_json(row)

    def demo_change(project_id, login, session):
        demo_project(project_id, login, session)
        row = session.scalar(select(DemoChange).where(DemoChange.project_id == project_id))
        if not row:
            raise HTTPException(409, "먼저 테스트 수정 항목을 등록해 주세요.")
        return row

    @app.post("/api/projects/{project_id}/demo-change/apply")
    def apply_demo_change(project_id: str, login=Depends(mutation), session=Depends(db)):
        row = demo_change(project_id, login, session)
        if row.status in ("applied", "pr_preview"):
            return demo_json(row)
        claimed = session.execute(
            update(DemoChange)
            .where(DemoChange.id == row.id, DemoChange.status.in_(["proposed", "apply_failed"]))
            .values(status="applying", error="")
        )
        session.commit()
        if claimed.rowcount != 1:
            raise HTTPException(409, "현재 테스트 작업을 처리 중입니다.")
        try:
            row.diff, row.review_hash = demo_changes.apply(
                settings.demo_workspaces, row.id, row.content
            )
            row.status = "applied"
        except (demo_changes.DemoFailure, OSError):
            row.status = "apply_failed"
            row.error = "임시 파일 생성에 실패했습니다. Git 설치와 작업 폴더를 확인한 뒤 다시 실행해 주세요."
            session.commit()
            raise HTTPException(409, row.error) from None
        session.commit()
        return demo_json(row)

    @app.post("/api/projects/{project_id}/demo-change/pr")
    def preview_demo_pr(
        project_id: str, body: DemoReviewInput, login=Depends(mutation), session=Depends(db)
    ):
        row = demo_change(project_id, login, session)
        if not row.review_hash or not secrets.compare_digest(body.review_hash, row.review_hash):
            raise HTTPException(409, "현재 변경 내용을 확인한 뒤 다시 요청해 주세요.")
        if row.status == "pr_preview":
            return demo_json(row)
        claimed = session.execute(
            update(DemoChange)
            .where(DemoChange.id == row.id, DemoChange.status == "applied")
            .values(status="publishing", error="")
        )
        session.commit()
        if claimed.rowcount != 1:
            raise HTTPException(409, "수정 실행과 검토를 먼저 완료해 주세요.")
        try:
            row.commit_sha = demo_changes.commit(
                settings.demo_workspaces, row.id, row.diff, row.content
            )
            row.status = "pr_preview"
        except (demo_changes.DemoFailure, OSError):
            row.status = "applied"
            row.error = "검토한 변경과 작업 파일이 일치하는지, Git 작업이 가능한지 확인해 주세요."
            session.commit()
            raise HTTPException(409, row.error) from None
        session.commit()
        return demo_json(row)

    if (settings.frontend_dist / "assets").is_dir():
        app.mount(
            "/assets", StaticFiles(directory=settings.frontend_dist / "assets"), name="assets"
        )

    @app.get("/dev/aws", include_in_schema=False)
    def legacy_aws_page(environment: uuid.UUID | None = None):
        route = "/#aws" + (f"/{environment}" if environment else "")
        return RedirectResponse(route, status_code=307)

    @app.get("/")
    def index():
        if (settings.frontend_dist / "index.html").is_file():
            return FileResponse(settings.frontend_dist / "index.html")
        return JSONResponse(
            {"message": "Frontend build required. See docs/LOCAL_DEVELOPMENT.md."}, status_code=503
        )

    return app
