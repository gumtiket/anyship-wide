"""앱 코드를 import하거나 실행하지 않고 배포 자원을 해석한다."""

import ast

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from ai.detectors.repo import RepoView, aliases, qualified
from ai.models import TransformReport
from ai.security import SourceMasker
from ai.spec.env_policy import EXTERNAL_URLS
from ai.spec.models import BackingService, Release

POSTGRES_DRIVERS = {"psycopg", "psycopg2", "psycopg2-binary", "asyncpg", "pg8000"}
OTHER_DRIVERS = {"pymysql", "mysqlclient", "mysql-connector-python", "aiomysql", "asyncmy"}


def _test_module(path: str) -> bool:
    return any(
        part in {"test", "tests", "conftest.py"} or part.startswith("test_")
        for part in path.split("/")
    )


def _test_only_variables(repo: RepoView | None) -> set[str]:
    runtime, tests = set(), set()
    if repo is not None:
        for path, tree in repo.modules():
            imports = aliases(tree)
            found = tests if _test_module(path) else runtime
            for node in ast.walk(tree):
                key = None
                if (
                    isinstance(node, ast.Subscript)
                    and qualified(node.value, imports) == "os.environ"
                ):
                    key = node.slice
                elif (
                    isinstance(node, ast.Call)
                    and node.args
                    and qualified(node.func, imports) in {"os.getenv", "os.environ.get"}
                ):
                    key = node.args[0]
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.add(key.value)
    return tests - runtime


class ResourceContractError(ValueError):
    """이 모듈 밖으로는 고정된 코드·메시지만 나간다. 소스 텍스트나 파서 예외는 나가지 않는다."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code, self.message = code, message


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_yaml_key")
        return super().construct_mapping(node, deep=deep)


def _existing(repo: RepoView | None) -> tuple[bool, Release | None]:
    if repo is None or "deploy-spec.yaml" not in repo.files():
        return False, None
    source = repo.read("deploy-spec.yaml")
    if SourceMasker(repo).contains_sensitive(source):
        raise ResourceContractError(
            "existing_spec_sensitive",
            "기존 배포 명세에 민감 값이 있어 명세 재생성을 보류했습니다. "
            "비밀 값을 분리한 뒤 다시 분석해 주세요.",
        )
    try:
        document = yaml.load(source, Loader=_UniqueLoader)
        if not isinstance(document, dict):
            raise ValueError("spec_mapping_required")
        services = document.get("backing_services", [])
        if not isinstance(services, list):
            raise ValueError("backing_services_list_required")
        bindings = [
            BackingService.model_validate(
                {**item, "bind_as": "DATABASE_URL"}
                if isinstance(item, dict)
                and item.get("type") == "postgres"
                and item.get("bind_as") is None
                else item
            )
            for item in services
        ]
        if len(bindings) > 1 or any(item.type != "postgres" for item in bindings):
            raise ValueError("unsupported_or_duplicate_binding")
        release = document.get("release")
        release = Release.model_validate(release) if release is not None else None
        if release is not None and not release.migrate.strip():
            raise ValueError("empty_migration")
        return bool(bindings), release
    except (ValueError, TypeError, yaml.YAMLError, RecursionError):
        raise ResourceContractError(
            "existing_spec_invalid",
            "기존 배포 명세의 DB 연결·마이그레이션 설정을 확인할 수 없어 재생성을 "
            "보류했습니다. 지원 형식으로 수정한 뒤 다시 분석해 주세요.",
        ) from None


def _postgres_evidence(repo: RepoView | None) -> bool:
    if repo is None:
        return False
    drivers = set()
    if "requirements.txt" in repo.files():
        for line in repo.read("requirements.txt").splitlines():
            try:
                drivers.add(canonicalize_name(Requirement(line).name))
            except InvalidRequirement:
                continue
    postgres = bool(drivers & POSTGRES_DRIVERS)
    other = bool(drivers & OTHER_DRIVERS)
    for path, tree in repo.modules():
        # 테스트 전용 드라이버나 고정 데이터 URL은 운영 의존성이 아니다.
        if _test_module(path):
            continue
        imports = aliases(tree)
        postgres |= any(name.split(".")[0] in POSTGRES_DRIVERS for name in imports.values())
        other |= any(name.split(".")[0] in OTHER_DRIVERS for name in imports.values())
        documentation = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Constant)
                or not isinstance(node.value, str)
                or id(node) in documentation
            ):
                continue
            postgres |= node.value.startswith(("postgresql://", "postgresql+", "postgres://"))
            other |= node.value.startswith(("mysql://", "mysql+", "mssql://", "mssql+"))
    return postgres and not other


def _migration(repo: RepoView | None) -> str | None:
    """기존 SQLAlchemy 진입점을 인식한다. 파일 이름만으로는 부족하다."""
    if repo is None:
        return None
    modules = dict(repo.modules())
    tree = modules.get("app/migrate.py")
    if tree is None or "app/db.py" not in modules:
        return None
    imports = aliases(tree)
    main = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"),
        None,
    )
    if main is None or not any(
        isinstance(node, ast.Call)
        and qualified(node.func, imports) == "app.db.Base.metadata.create_all"
        and node.args
        and qualified(node.args[0], imports) == "app.db.engine"
        for node in ast.walk(main)
    ):
        return None
    for node in tree.body:
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if (
            isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "main"
                for statement in node.body
                for call in ast.walk(statement)
            )
        ):
            return "python -m app.migrate"
    return None


def resolve_resources(
    repo: RepoView | None, transformation: TransformReport, *, converted_postgres: bool
) -> tuple[bool, str | None]:
    existing_postgres, release = _existing(repo)
    test_only = _test_only_variables(repo)
    variables = {item.name: item for item in transformation.env_vars if item.name not in test_only}
    postgres = (
        existing_postgres
        or converted_postgres
        or ("DATABASE_URL" in variables and _postgres_evidence(repo))
    )
    if "DATABASE_URL" in variables and not postgres:
        raise ResourceContractError(
            "postgres_binding_unresolved",
            "DATABASE_URL을 사용하는 앱의 DB 종류·연결 선언을 확인하지 못했습니다. "
            "PostgreSQL 의존성 또는 기존 배포 명세의 DB 연결을 확인해 주세요.",
        )
    if any(
        name in EXTERNAL_URLS - {"DATABASE_URL"} and item.required
        for name, item in variables.items()
    ):
        raise ResourceContractError(
            "external_binding_unresolved",
            "필수 외부 자원 환경변수를 공급할 수 없어 명세 생성을 보류했습니다. "
            "지원하는 외부 자원 연결을 확인해 주세요.",
        )
    migrate = release.migrate if release else transformation.migrate_command
    if migrate is None and postgres:
        migrate = _migration(repo)
    # 생성된 명령에도 generate_spec의 기존 길이·줄 검증이 그대로 적용된다.
    return postgres, migrate
