"""로그인 경로: 데모 로그인, GitHub OAuth(PKCE), 현재 사용자, 로그아웃."""

import base64
import hashlib
import logging
import secrets
import time
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select

from .db import LoginSession, Membership, OAuthAttempt, User, Workspace
from .github_api import GitHubFailure

auth_logger = logging.getLogger("anyship.auth")

SESSION_COOKIE = "app_session"
OAUTH_COOKIE = "app_oauth"
OAUTH_COOKIE_PATH = "/api/auth/github"
MAX_SESSION_SECONDS = 8 * 3600


def digest(value):
    """쿠키·state 같은 원문 대신 DB에 저장하는 SHA-256 값."""
    return hashlib.sha256(value.encode()).hexdigest()


def router(settings, github, cipher, db, current, mutation, origin_check):
    routes = APIRouter(prefix="/api")

    def create_login(session, profile, token, ttl, response):
        """GitHub 프로필로 사용자·워크스페이스를 찾거나 만들고 로그인 세션 쿠키를 붙인다."""
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
        expires = int(time.time()) + max(1, min(int(ttl), MAX_SESSION_SECONDS))
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
            SESSION_COOKIE,
            raw,
            max_age=expires - int(time.time()),
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            path="/",
        )
        return response

    @routes.post("/auth/demo")
    def demo_login(request: Request, session=Depends(db)):
        if not settings.demo:
            raise HTTPException(404)
        origin_check(request)
        # 브라우저마다 데모 로그인할 때 별도의 사용자와 워크스페이스를 만든다.
        profile = {"id": -secrets.randbelow(2**52) - 1, "login": "demo-user", "name": "데모 사용자"}
        return create_login(session, profile, "demo", 3600, JSONResponse({"ok": True}))

    @routes.get("/auth/github/start")
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
            OAUTH_COOKIE,
            browser,
            max_age=600,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            path=OAUTH_COOKIE_PATH,
        )
        return response

    @routes.get("/auth/github/callback")
    def callback(
        request: Request, state: str = "", code: str = "", error: str = "", session=Depends(db)
    ):
        def failed(reason):
            auth_logger.warning("GitHub login failed: %s", reason)
            response = RedirectResponse("/?" + urlencode({"auth_error": reason}), status_code=302)
            response.delete_cookie(OAUTH_COOKIE, path=OAUTH_COOKIE_PATH)
            return response

        if settings.demo or not settings.github_configured:
            return failed("not_configured")
        attempt = session.get(OAuthAttempt, digest(state))
        binding = request.cookies.get(OAUTH_COOKIE, "")
        if (
            not attempt
            or attempt.expires_at <= int(time.time())
            or not binding
            or not secrets.compare_digest(attempt.browser_hash, digest(binding))
        ):
            return failed("invalid_state")
        verifier = attempt.verifier
        # state는 한 번만 쓸 수 있다. 동시에 들어온 콜백 중 하나만 삭제에 성공한다.
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
            success.delete_cookie(OAUTH_COOKIE, path=OAUTH_COOKIE_PATH)
            old = request.cookies.get(SESSION_COOKIE)
            if old:
                session.execute(delete(LoginSession).where(LoginSession.id == digest(old)))
            return create_login(
                session, profile, data["access_token"], data.get("expires_in", 28800), success
            )
        except GitHubFailure as failure:
            return failed(failure.code)
        except (KeyError, ValueError):
            return failed("invalid_response")

    @routes.get("/me")
    def me(login=Depends(current), session=Depends(db)):
        user = session.get(User, login.user_id)
        workspace = session.get(Workspace, login.workspace_id)
        return {
            "user": {"login": user.login, "name": user.name},
            "workspace": {"id": workspace.id, "name": workspace.name},
            "csrf_token": login.csrf,
        }

    @routes.post("/auth/logout")
    def logout(login=Depends(mutation), session=Depends(db)):
        session.execute(delete(LoginSession).where(LoginSession.id == login.id))
        session.commit()
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return routes
