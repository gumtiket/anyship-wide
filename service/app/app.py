"""FastAPI 앱을 조립한다: 실행기, 공통 의존성(세션·로그인·CSRF), 라우터, 정적 화면."""

import secrets
import time
import uuid
from contextlib import asynccontextmanager

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import (
    analysis_api,
    auth_api,
    aws_onboarding,
    code_changes_api,
    demo_changes_api,
    deploy_api,
    mock_deployments,
    onboarding,
    onprem_api,
    projects_api,
)
from .auth_api import SESSION_COOKIE, digest
from .aws_adapter import AwsAdapter
from .config import Settings
from .db import LoginSession, Membership, User, database
from .github_api import DemoGitHub, GitHubAPI, GitHubFailure
from .project_access import ProjectAccess


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
            # 지원하지 않는 입력의 값을 응답에 다시 싣지 않는다.
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
        value = request.cookies.get(SESSION_COOKIE, "")
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

    access = ProjectAccess(settings, github, access_token)
    app.include_router(
        auth_api.router(settings, github, cipher, db, current, mutation, origin_check)
    )
    app.include_router(onboarding.router(settings, github, db, current, mutation, access_token))
    app.include_router(projects_api.router(github, access, db, current, mutation, access_token))
    app.include_router(aws_onboarding.router(settings, aws_adapter, db, current, mutation))
    app.include_router(mock_deployments.router(settings, mock_runner, db, current, mutation))
    app.include_router(
        deploy_api.router(settings, deploy_runner, db, current, mutation, access_token)
    )
    app.include_router(onprem_api.router(settings, deploy_runner, db, current, mutation))
    app.include_router(
        analysis_api.router(
            settings,
            analysis_runner,
            github,
            db,
            current,
            mutation,
            access.owned_project,
            access.real_project,
            access_token,
        )
    )
    app.include_router(
        code_changes_api.router(settings, github, access, db, current, mutation, access_token)
    )
    app.include_router(demo_changes_api.router(settings, access, db, current, mutation))

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
