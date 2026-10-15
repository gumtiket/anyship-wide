from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    login: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))


class Membership(Base):
    __tablename__ = "memberships"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="owner")


class LoginSession(Base):
    __tablename__ = "login_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    token_cipher: Mapped[str] = mapped_column(Text)
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)


class OAuthAttempt(Base):
    __tablename__ = "oauth_attempts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    verifier: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("workspace_id", "repository_id", name="uq_project_workspace_repository"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    repository_id: Mapped[int] = mapped_column(BigInteger)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    full_name: Mapped[str] = mapped_column(String(512))
    branch: Mapped[str] = mapped_column(String(255))
    base_sha: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[int] = mapped_column(BigInteger)


class RepositoryConnection(Base):
    __tablename__ = "repository_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "workspace_id", name="uq_connection_user_workspace"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    repository_url: Mapped[str] = mapped_column(String(2048))
    expires_at: Mapped[int] = mapped_column(BigInteger)
    install_state_hash: Mapped[str] = mapped_column(String(64), default="")
    install_expires_at: Mapped[int] = mapped_column(BigInteger, default=0)


class AwsEnvironment(Base):
    __tablename__ = "aws_environments"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "created_by", "request_id", name="uq_aws_environment_request"
        ),
        UniqueConstraint("workspace_id", "role_arn", name="uq_aws_environment_role"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    template_url: Mapped[str] = mapped_column(Text)
    service_role_arn: Mapped[str] = mapped_column(String(2048))
    stack_name: Mapped[str] = mapped_column(String(128))
    role_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    submitted_role_arn: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    role_arn: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    aws_account_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    verified_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    verification_token: Mapped[str] = mapped_column(String(36), default="")
    lease_until: Mapped[int] = mapped_column(BigInteger, default=0)
    # 실제 배포용. 공용 기반이 만들어지면 채워진다. 모두 비밀이 아니다(DB 비밀번호는 Secrets Manager에만 있다).
    env_id: Mapped[str | None] = mapped_column(
        String(21), unique=True, nullable=True
    )  # DNS와 Terraform 이름에 쓰는 고정 값
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    db_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    db_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    db_secret_arn: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    state_bucket: Mapped[str | None] = mapped_column(
        String(63), nullable=True
    )  # Terraform state를 둔 사용자 계정의 S3 버킷


class CodeChange(Base):
    __tablename__ = "code_changes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    base_sha: Mapped[str] = mapped_column(String(64))
    base_tree: Mapped[str] = mapped_column(String(64))
    branch: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    diff: Mapped[str] = mapped_column(Text, default="")
    review_hash: Mapped[str] = mapped_column(String(64), default="")
    tree_sha: Mapped[str] = mapped_column(String(64), default="")
    commit_sha: Mapped[str] = mapped_column(String(64), default="")
    pr_url: Mapped[str] = mapped_column(Text, default="")
    pr_number: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    lease_until: Mapped[int] = mapped_column(BigInteger, default=0)


class AnalysisSlot(Base):
    __tablename__ = "analysis_slots"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    active_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[int] = mapped_column(BigInteger, default=0)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (UniqueConstraint("project_id", "request_id", name="uq_analysis_request"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    runtime_id: Mapped[str] = mapped_column(String(36))
    repository_id: Mapped[int] = mapped_column(BigInteger)
    repository: Mapped[str] = mapped_column(String(512))
    base_branch: Mapped[str] = mapped_column(String(255))
    base_sha: Mapped[str] = mapped_column(String(40))
    base_tree: Mapped[str] = mapped_column(String(40), default="")
    provider: Mapped[str] = mapped_column(String(16))
    target_env: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    logs_json: Mapped[str] = mapped_column(Text, default="[]")
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    files_json: Mapped[str] = mapped_column(Text, default="[]")
    diff: Mapped[str] = mapped_column(Text, default="")
    review_hash: Mapped[str] = mapped_column(String(64), default="")
    publish_status: Mapped[str] = mapped_column(String(16), default="proposed")
    branch: Mapped[str] = mapped_column(String(255))
    tree_sha: Mapped[str] = mapped_column(String(40), default="")
    commit_sha: Mapped[str] = mapped_column(String(40), default="")
    pr_url: Mapped[str] = mapped_column(Text, default="")
    pr_number: Mapped[int] = mapped_column(BigInteger, default=0)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    lease_until: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class MockDeployment(Base):
    __tablename__ = "mock_deployments"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(36))
    aws_environment_id: Mapped[str | None] = mapped_column(
        ForeignKey("aws_environments.id"), nullable=True, index=True
    )
    label: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(12))
    adapter_env_id: Mapped[str] = mapped_column(String(21), unique=True)
    set_name: Mapped[str] = mapped_column(String(24))
    checked_runtime_id: Mapped[str] = mapped_column(String(36), default="")
    active_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class MockJob(Base):
    __tablename__ = "mock_jobs"
    __table_args__ = (UniqueConstraint("project_id", "request_id", name="uq_mock_job_request"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    runtime_id: Mapped[str] = mapped_column(String(36))
    adapter_env_id: Mapped[str] = mapped_column(String(21))
    target_label: Mapped[str] = mapped_column(String(100))
    set_name: Mapped[str] = mapped_column(String(24))
    action: Mapped[str] = mapped_column(String(16))
    scenario: Mapped[str] = mapped_column(String(24))
    image_tag: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    logs_json: Mapped[str] = mapped_column(Text, default="[]")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[int] = mapped_column(BigInteger)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class OnpremEnvironment(Base):
    __tablename__ = "onprem_environments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "created_by", "request_id", name="uq_onprem_request"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(254))
    env_id: Mapped[str] = mapped_column(String(21), unique=True)
    connection_kind: Mapped[str] = mapped_column(String(24), default="ssh", server_default="ssh")
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_user: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ssh_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_seen_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    public_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="ISSUED")
    error_code: Mapped[str] = mapped_column(String(64), default="")
    active_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[int] = mapped_column(BigInteger)
    deleted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class OnpremRegistrationToken(Base):
    __tablename__ = "onprem_registration_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey("onprem_environments.id"), index=True)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    used_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class OnpremJob(Base):
    """Environment operations exist before a project selects this environment."""

    __tablename__ = "onprem_jobs"
    __table_args__ = (
        UniqueConstraint("environment_id", "request_id", name="uq_onprem_job_request"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey("onprem_environments.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    runtime_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    logs_json: Mapped[str] = mapped_column(Text, default="[]")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[int] = mapped_column(BigInteger)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Deployment(Base):
    """프로젝트의 실제 AWS 또는 온프레미스 대상(모의 배포 `MockDeployment`와 별개)."""

    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(
            "(aws_environment_id IS NOT NULL AND onprem_environment_id IS NULL) OR "
            "(aws_environment_id IS NULL AND onprem_environment_id IS NOT NULL)",
            name="ck_deployment_environment",
        ),
    )
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    aws_environment_id: Mapped[str | None] = mapped_column(
        ForeignKey("aws_environments.id"), index=True, nullable=True
    )
    onprem_environment_id: Mapped[str | None] = mapped_column(
        ForeignKey("onprem_environments.id"), index=True, nullable=True
    )
    set_name: Mapped[str] = mapped_column(String(24))
    app_name: Mapped[str] = mapped_column(
        String(63), default=""
    )  # 온프레미스는 실패 후 정리를 위해 배포 시작 전에 저장한다
    image_tag: Mapped[str] = mapped_column(String(40), default="")  # 지금 실행 중인 버전
    url: Mapped[str] = mapped_column(String(512), default="")
    active_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[int] = mapped_column(
        BigInteger, default=0
    )  # 작업 선점의 만료(초). 20분짜리 작업을 덮는 TTL
    # 환경 이전(`migrate`) 뒤 원래 환경에 멈춘 채 남은 앱. 사용자가 확인하고 지울 때까지 기록해 둔다(비어 있으면 없다).
    previous_kind: Mapped[str] = mapped_column(
        String(8), default="", server_default=""
    )  # "aws" 또는 "onprem"
    previous_environment_id: Mapped[str] = mapped_column(String(36), default="", server_default="")
    previous_app_name: Mapped[str] = mapped_column(String(63), default="", server_default="")
    previous_url: Mapped[str] = mapped_column(String(512), default="", server_default="")


class DeployJob(Base):
    __tablename__ = "deploy_jobs"
    __table_args__ = (UniqueConstraint("project_id", "request_id", name="uq_deploy_job_request"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    runtime_id: Mapped[str] = mapped_column(
        String(36)
    )  # 어느 서버 실행에서 시작했는지(재시작 때 중단 처리)
    action: Mapped[str] = mapped_column(String(16))
    set_name: Mapped[str] = mapped_column(String(24))
    image_tag: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    stage: Mapped[str] = mapped_column(
        String(16), default=""
    )  # 실패했을 때 어느 단계였는지(spec, build, foundation, check, deploy)
    logs_json: Mapped[str] = mapped_column(Text, default="[]")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[int] = mapped_column(BigInteger)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class DemoChange(Base):
    __tablename__ = "demo_changes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    content: Mapped[str] = mapped_column(Text)
    diff: Mapped[str] = mapped_column(Text, default="")
    review_hash: Mapped[str] = mapped_column(String(64), default="")
    commit_sha: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")


def database(url):
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine, sessionmaker(engine, expire_on_commit=False)
