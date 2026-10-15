import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from .aws_validation import validate_aws_settings

DEFAULT_DATABASE_URL = "postgresql+psycopg://anyship@127.0.0.1:55432/anyship"


@dataclass(frozen=True)
class Settings:
    app_origin: str = "http://localhost:8000"
    database_url: str = field(default=DEFAULT_DATABASE_URL, repr=False)
    github_client_id: str = ""
    github_client_secret: str = field(default="", repr=False)
    github_app_slug: str = ""
    token_key: str = field(default="", repr=False)
    demo: bool = False
    ai_mode: str = "unavailable"
    ai_provider: str = "none"
    ai_timeout: int = 300
    ai_max_calls: int = 12
    ai_workspace: Path = Path(__file__).resolve().parents[1] / "workspaces" / "analysis"
    production: bool = False
    deployment_mode: str = "unavailable"
    mock_step_delay: float = 0.3
    aws_template_url: str = field(default="", repr=False)
    aws_service_role_arn: str = ""
    aws_regions: tuple[str, ...] = ()
    aws_role_name: str = "deploy-service-role"
    aws_verify: str = (
        ""  # "sts"면 환경 등록의 연결 확인에 STSAdapter를 쓴다. 기본은 꺼짐(어댑터 연결 대기)
    )
    # 실제 배포(deployment_mode="real")에서만 쓴다. 비밀이 아닌 값뿐이다(개인 키는 경로만 받고 내용을 읽지 않는다).
    deploy_source: str = "github"  # github: 연결한 저장소를 사용자 토큰으로 받는다. local: 서버 폴더에 받아 둔 소스를 쓴다(시험용)
    deploy_source_dir: Path | None = (
        None  # local일 때만: 받아 둔 소스를 두는 폴더. 저장소 이름과 같은 하위 폴더를 배포한다
    )
    deploy_workspace: Path = (
        Path(__file__).resolve().parents[1] / "workspaces" / "deploy-src"
    )  # github일 때 받은 소스를 잠시 두는 곳
    deploy_ssh_key: Path | None = None  # 호스트 접속용 개인 키 파일. 공개 키는 같은 이름 + ".pub"
    deploy_service_ip: str = ""  # 호스트의 SSH(22)를 이 서비스 서버 주소에만 연다
    deploy_acme_email: str = ""  # Traefik의 Let's Encrypt 연락 주소
    deploy_base_domain: str = ""  # 앱 주소는 <앱>.<환경ID>.aws.<이 도메인>
    deploy_terraform_dir: Path = Path(__file__).resolve().parents[2] / "infra" / "user-account"
    deploy_plugin_cache: Path = Path.home() / ".terraform.d" / "plugin-cache"
    deploy_verify_tls: bool = True  # False는 Let's Encrypt staging 인증서를 시험할 때만
    deploy_dns: bool = True  # False면 앱 주소의 DNS 레코드(*.<환경ID>.aws.<도메인>)를 서비스가 맞추지 않는다(수동으로 관리)
    frontend_dist: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    demo_workspaces: Path = Path(__file__).resolve().parents[1] / "workspaces" / "demo"

    def __post_init__(self):
        if self.deployment_mode not in ("unavailable", "mock", "real"):
            raise ValueError("APP_DEPLOYMENT_MODE must be unavailable, mock or real.")
        if self.deployment_mode == "real":
            self._validate_real_deployment()
        if self.production and self.deployment_mode == "mock":
            raise ValueError("Mock deployments are only available in development.")
        if not 0 <= self.mock_step_delay <= 2:
            raise ValueError("APP_MOCK_STEP_DELAY must be between 0 and 2 seconds.")
        validate_aws_settings(self.aws_template_url, self.aws_service_role_arn, self.aws_regions)
        if not re.fullmatch(
            r"[A-Za-z0-9_+=,.@-]{1,64}", self.aws_role_name.replace("{id}", "0" * 32)
        ):
            raise ValueError(
                "APP_AWS_ROLE_NAME must be an IAM role name, optionally containing {id}."
            )
        if self.aws_verify not in ("", "sts"):
            raise ValueError("APP_AWS_VERIFY must be empty or sts.")
        if self.ai_mode not in ("placeholder", "unavailable", "bronze"):
            raise ValueError("APP_AI_MODE must be placeholder, unavailable or bronze.")
        if self.ai_provider not in ("none", "fake", "bedrock", "anthropic"):
            raise ValueError("APP_AI_PROVIDER must be none, fake, bedrock or anthropic.")
        if not 10 <= self.ai_timeout <= 900 or not 1 <= self.ai_max_calls <= 30:
            raise ValueError("APP_AI_TIMEOUT must be 10..900; APP_AI_MAX_CALLS must be 1..30.")
        if self.ai_mode == "bronze" and (
            self.demo or (self.production and self.ai_provider == "fake")
        ):
            raise ValueError(
                "Bronze AI requires real GitHub authentication; fake is development-only."
            )
        if self.production and self.ai_mode == "placeholder":
            raise ValueError("AI placeholder is only available in development.")
        origin = urlsplit(self.app_origin)
        if (
            origin.scheme not in ("http", "https")
            or not origin.netloc
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("APP_APP_ORIGIN must be an origin without a trailing slash.")
        if self.production and (
            self.demo
            or origin.scheme != "https"
            or make_url(self.database_url).get_backend_name() != "postgresql"
        ):
            raise ValueError("Production requires HTTPS, PostgreSQL and demo disabled.")
        if not self.demo and not self.token_key:
            raise ValueError("Set APP_TOKEN_KEY before starting real authentication.")
        if self.github_app_slug and not re.fullmatch(r"[A-Za-z0-9-]+", self.github_app_slug):
            raise ValueError("APP_GITHUB_APP_SLUG must be a GitHub App slug.")
        if self.production and not self.github_configured:
            raise ValueError("Production requires GitHub App configuration.")

    def _validate_real_deployment(self):
        # 값이 아니라 이름만 알린다. 빠진 설정은 서버가 뜰 때 바로 알려 20분짜리 작업 중에 드러나지 않게 한다.
        if self.demo:
            raise ValueError("APP_DEPLOYMENT_MODE=real cannot be used with APP_DEMO=true.")
        if self.deploy_source not in ("github", "local"):
            raise ValueError("APP_DEPLOY_SOURCE must be github or local.")
        missing = [
            name
            for name, value in (
                ("APP_DEPLOY_SOURCE_DIR", self.deploy_source_dir or self.deploy_source != "local"),
                ("APP_DEPLOY_SSH_KEY", self.deploy_ssh_key),
                ("APP_DEPLOY_SERVICE_IP", self.deploy_service_ip),
                ("APP_DEPLOY_ACME_EMAIL", self.deploy_acme_email),
                ("APP_DEPLOY_BASE_DOMAIN", self.deploy_base_domain),
            )
            if not value
        ]
        if missing:
            raise ValueError("Real deployments require: " + ", ".join(missing) + ".")
        try:
            ipaddress.ip_address(self.deploy_service_ip)
        except ValueError:
            raise ValueError("APP_DEPLOY_SERVICE_IP must be an IP address.") from None
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.deploy_acme_email):
            raise ValueError("APP_DEPLOY_ACME_EMAIL must be an email address.")
        if not re.fullmatch(
            r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}",
            self.deploy_base_domain,
        ):
            raise ValueError("APP_DEPLOY_BASE_DOMAIN must be a lowercase domain name.")

    @property
    def aws_setup_issues(self):
        # Expose only missing setting names, never role, template or credential values.
        required = {
            "template": self.aws_template_url,
            "service_role": self.aws_service_role_arn,
            "regions": self.aws_regions,
            "live_mode": not self.demo,
        }
        return [name for name, value in required.items() if not value]

    @property
    def aws_configured(self):
        return not self.aws_setup_issues

    @property
    def github_configured(self):
        return bool(self.github_client_id and self.github_client_secret and self.github_app_slug)

    @property
    def secure_cookies(self):
        return self.app_origin.startswith("https://")

    @classmethod
    def from_env(cls):
        return cls(
            app_origin=os.getenv("APP_APP_ORIGIN", "http://localhost:8000"),
            database_url=os.getenv("APP_DATABASE_URL") or DEFAULT_DATABASE_URL,
            github_client_id=os.getenv("APP_GITHUB_CLIENT_ID", ""),
            github_client_secret=os.getenv("APP_GITHUB_CLIENT_SECRET", ""),
            github_app_slug=os.getenv("APP_GITHUB_APP_SLUG", ""),
            token_key=os.getenv("APP_TOKEN_KEY", ""),
            demo=os.getenv("APP_DEMO", "false").lower() == "true",
            ai_mode=os.getenv("APP_AI_MODE", "unavailable"),
            ai_provider=os.getenv("APP_AI_PROVIDER", "none"),
            ai_timeout=int(os.getenv("APP_AI_TIMEOUT", "300")),
            ai_max_calls=int(os.getenv("APP_AI_MAX_CALLS", "12")),
            ai_workspace=Path(
                os.getenv("APP_AI_WORKSPACE")
                or Path(__file__).resolve().parents[1] / "workspaces" / "analysis"
            ),
            production=os.getenv("APP_ENV", "development") == "production",
            deployment_mode=os.getenv("APP_DEPLOYMENT_MODE", "unavailable"),
            mock_step_delay=float(os.getenv("APP_MOCK_STEP_DELAY", "0.3")),
            aws_template_url=os.getenv("APP_AWS_TEMPLATE_URL", "").strip(),
            aws_service_role_arn=os.getenv("APP_AWS_SERVICE_ROLE_ARN", "").strip(),
            aws_regions=tuple(
                dict.fromkeys(
                    r.strip() for r in os.getenv("APP_AWS_REGIONS", "").split(",") if r.strip()
                )
            ),
            aws_role_name=os.getenv("APP_AWS_ROLE_NAME", "deploy-service-role").strip(),
            aws_verify=os.getenv("APP_AWS_VERIFY", "").strip().lower(),
            **cls._deploy_from_env(),
        )

    @staticmethod
    def _deploy_from_env():
        def path(name):
            value = os.getenv(name, "").strip()
            return Path(value).expanduser() if value else None

        dns = os.getenv("APP_DEPLOY_DNS", "").strip().lower() or "on"  # 비워 두면 기본값
        if dns not in ("on", "off"):
            raise ValueError("APP_DEPLOY_DNS must be on or off.")
        source = os.getenv("APP_DEPLOY_SOURCE", "").strip().lower() or "github"
        values = {
            "deploy_source": source,
            "deploy_source_dir": path("APP_DEPLOY_SOURCE_DIR"),
            "deploy_dns": dns == "on",
            "deploy_ssh_key": path("APP_DEPLOY_SSH_KEY"),
            "deploy_service_ip": os.getenv("APP_DEPLOY_SERVICE_IP", "").strip(),
            "deploy_acme_email": os.getenv("APP_DEPLOY_ACME_EMAIL", "").strip(),
            "deploy_base_domain": os.getenv("APP_DEPLOY_BASE_DOMAIN", "").strip().lower(),
            "deploy_verify_tls": os.getenv("APP_DEPLOY_VERIFY_TLS", "true").strip().lower()
            != "false",
        }
        for key, name in (
            ("deploy_terraform_dir", "APP_DEPLOY_TERRAFORM_DIR"),
            ("deploy_plugin_cache", "APP_DEPLOY_PLUGIN_CACHE"),
            ("deploy_workspace", "APP_DEPLOY_WORKSPACE"),
        ):
            if path(name):
                values[key] = path(name)
        return values
