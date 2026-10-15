"""진단과 마스킹이 함께 쓰는, 알려진 자격 증명 형태(완전한 탐지기는 아니다)."""

import ast
import re
from urllib.parse import parse_qsl, urlsplit

from ai.source_names import aliases, qualified

DUMMY_SECRET = "dummy-secret-do-not-use"
SECRET_WORDS = {
    "secret",
    "password",
    "passwd",
    "pwd",
    "key",
    "token",
    "authorization",
    "auth",
    "credential",
    "credentials",
    "cookie",
}
URL_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")


def credential_name(name: str) -> bool:
    words = re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower().split("_")
    return bool(set(words) & SECRET_WORDS) or name.lower() in {"apikey", "accesstoken"}


def credential_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        return bool(
            url.scheme
            and (
                url.username is not None
                or url.password is not None
                or any(credential_name(key) for key, _ in parse_qsl(url.query))
            )
        )
    except ValueError:
        # user-info가 들어간 잘못된 형식의 URL도 기본값으로 공개하기에는 안전하지 않다.
        return "://" in value and "@" in value


def credential_urls(text: str) -> list[str]:
    return [match.group() for match in URL_PATTERN.finditer(text) if credential_url(match.group())]


def secret_literals(tree: ast.AST) -> list[ast.Constant]:
    found = {}
    imports = aliases(tree)

    def retain(node: ast.AST | None) -> None:
        if isinstance(node, (ast.Tuple, ast.List)):
            for item in node.elts:
                retain(item)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
            found[(node.lineno, node.col_offset)] = node

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(credential_name(getattr(t, "id", getattr(t, "attr", ""))) for t in targets):
                retain(node.value)
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and credential_name(key.value)
                ):
                    retain(value)
        if isinstance(node, ast.Call):
            if qualified(node.func, imports).split(".")[-1] in {"HTTPBasicAuth", "BasicAuth"}:
                for argument in node.args[:2]:
                    retain(argument)
            for keyword in node.keywords:
                if keyword.arg and credential_name(keyword.arg):
                    retain(keyword.value)
            # getenv 별칭은 SourceMasker가 해석한다. 여기서는 일반 기본값도 탐지한다.
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and credential_name(node.args[0].value)
            ):
                if qualified(node.func, imports) in {"os.getenv", "os.environ.get"}:
                    default = (
                        node.args[1]
                        if len(node.args) > 1
                        else next((k.value for k in node.keywords if k.arg == "default"), None)
                    )
                    retain(default)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if credential_urls(node.value) or re.match(r"(?i)^Bearer\s+\S+", node.value):
                retain(node)
    return list(found.values())
