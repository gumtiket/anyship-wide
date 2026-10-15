"""알려진 형태의 비밀만 마스킹한다. 일반 비밀 탐지기가 아니다."""

import ast
import json
import re

import yaml

from ai.credentials import (
    DUMMY_SECRET as DUMMY_SECRET,
)
from ai.credentials import (
    credential_name as credential_name,
)
from ai.credentials import (
    credential_urls,
    secret_literals,
)
from ai.detectors.repo import RepoView, aliases, qualified


def deployment_secret_flags(text: str) -> set[int]:
    """env[].secret의 불리언 메타데이터 위치를 찾되, 따옴표 문자열과 자격 증명은 보존한다.

    YAML 노드는 사용자 정의 객체를 만들지 않고도 타입과 소스 위치를 유지한다.
    별칭(alias) 때문에 문서의 다른 곳에 있는 자격 증명 같은 줄이 예외가 되면 안 된다.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError):
        return set()
    if not isinstance(root, yaml.MappingNode):
        return set()
    flags = set()
    for key, env in root.value:
        if (
            key.value != "env"
            or not isinstance(env, yaml.SequenceNode)
            or env.start_mark.index <= key.start_mark.index
        ):
            continue
        for item in env.value:
            if not isinstance(item, yaml.MappingNode) or not (
                env.start_mark.index <= item.start_mark.index < env.end_mark.index
            ):
                continue
            if not any(
                name.value == "name"
                and isinstance(value, yaml.ScalarNode)
                and value.tag == "tag:yaml.org,2002:str"
                for name, value in item.value
            ):
                continue
            for name, value in item.value:
                if (
                    name.value == "secret"
                    and isinstance(value, yaml.ScalarNode)
                    and value.tag == "tag:yaml.org,2002:bool"
                    and value.style is None
                    and value.value.lower() in {"true", "false"}
                    and name.start_mark.line == value.start_mark.line
                ):
                    flags.add(name.start_mark.index)
    return flags


def node_span(source: str, node: ast.AST) -> tuple[int, int]:
    """AST 열 번호는 한국어 리터럴을 포함해 UTF-8 바이트 단위로 센다."""
    lines = source.splitlines(keepends=True)
    start = sum(len(line.encode("utf-8")) for line in lines[: node.lineno - 1]) + node.col_offset
    end = (
        sum(len(line.encode("utf-8")) for line in lines[: node.end_lineno - 1])
        + node.end_col_offset
    )
    return start, end


def edit_nodes(source: str, edits: list[tuple[ast.AST, str]]) -> str:
    ranges = sorted([(node_span(source, node), text) for node, text in edits], reverse=True)
    content = source.encode("utf-8")
    last = len(content) + 1
    for (start, end), replacement in ranges:
        if end > last:
            raise ValueError("overlapping_edits")
        content = content[:start] + replacement.encode("utf-8") + content[end:]
        last = start
    return content.decode("utf-8")


class SourceMasker:
    def __init__(self, repo: RepoView) -> None:
        self._values: set[str] = set()
        self.blocked_files: set[str] = set()
        self._nodes: dict[str, list[ast.Constant]] = {}
        for file, tree in repo.modules():
            secrets = secret_literals(tree)
            imports = aliases(tree)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names = [
                        t.id
                        if isinstance(t, ast.Name)
                        else t.attr
                        if isinstance(t, ast.Attribute)
                        else ""
                        for t in targets
                    ]
                    value = node.value
                    if (
                        any(credential_name(name) for name in names)
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and value.value
                    ):
                        secrets.append(value)
                if isinstance(node, ast.Call) and qualified(node.func, imports) in {
                    "os.getenv",
                    "os.environ.get",
                }:
                    if (
                        len(node.args) > 1
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and credential_name(node.args[0].value)
                        and isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)
                        and node.args[1].value
                    ):
                        secrets.append(node.args[1])
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values, strict=True):
                        if (
                            isinstance(key, ast.Constant)
                            and isinstance(key.value, str)
                            and credential_name(key.value)
                            and isinstance(value, ast.Constant)
                            and isinstance(value.value, str)
                            and value.value
                        ):
                            secrets.append(value)
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and re.match(
                        r"(?:postgresql|postgres|mysql|mariadb|mssql|oracle)(?:\+\w+)?://",
                        node.value,
                    )
                    and "@" in node.value
                ):
                    secrets.append(node)
            unique = {(n.lineno, n.col_offset): n for n in secrets}
            self._nodes[file] = list(unique.values())
            for node in unique.values():
                self._values.add(node.value)
                if node.value != DUMMY_SECRET:
                    self.blocked_files.add(file)
        # 설정 값은 LLM에 보내지 않지만, 자격 증명처럼 보이는 설정은 diff에도 넣지 않는다.
        for file in repo.files():
            text = repo.read(file)
            urls = credential_urls(text)
            if urls:
                self.blocked_files.add(file)
                self._values.update(urls)
            if file.endswith(".json"):
                try:

                    def collect(value, file=file):
                        if isinstance(value, dict):
                            for key, item in value.items():
                                if credential_name(key) and isinstance(item, str) and item:
                                    self._values.add(item)
                                    self.blocked_files.add(file)
                                collect(item)
                        elif isinstance(value, list):
                            for item in value:
                                collect(item)

                    collect(json.loads(text))
                except (ValueError, RecursionError):
                    pass
            if file.endswith((".yaml", ".yml", ".toml", ".ini", ".cfg")):
                values = list(
                    re.finditer(
                        r"(?im)^\s*(?P<key>[\w.-]*(?:secret|password|token|key)[\w.-]*)"
                        r"\s*[:=]\s*['\"]?(?P<value>[^\r\n]+)",
                        text,
                    )
                )
                if values or re.search(r"\w+://[^\s]+@", text):
                    self.blocked_files.add(file)
                flags = deployment_secret_flags(text) if file == "deploy-spec.yaml" else set()
                for match in values:
                    value = match["value"].strip("'\" ")
                    if value and match.start("key") not in flags:
                        self._values.add(value)

    def source(self, repo: RepoView, file: str) -> str:
        text = repo.read(file)
        edits = []
        for node in self._nodes.get(file, []):
            literal = ast.get_source_segment(text, node) or ""
            replacement = (
                '"""[REDACTED]' + "\n" * literal.count("\n") + '"""'
                if "\n" in literal
                else '"[REDACTED]"'
            )
            edits.append((node, replacement))
        return self.text(edit_nodes(text, edits))

    def text(self, text: str, *, allow_dummy: bool = False) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            if not (allow_dummy and value == DUMMY_SECRET):
                forms = {
                    value,
                    json.dumps(value, ensure_ascii=False)[1:-1],
                    json.dumps(value, ensure_ascii=True)[1:-1],
                }
                for form in sorted(forms, key=len, reverse=True):
                    text = text.replace(form, "[REDACTED]")
        for url in credential_urls(text):
            text = text.replace(url, "[REDACTED]")
        return text

    def contains_sensitive(self, text: str) -> bool:
        return any(value != DUMMY_SECRET and value in text for value in self._values)

    def summaries(self, repo: RepoView, *, budget: int = 20000) -> dict[str, str]:
        result = {}
        for file, _ in repo.modules():
            source = self.source(repo, file)  # 파일을 고르거나 자르기 전에 파일 전체를 마스킹한다.
            if budget <= 0:
                break
            result[file] = source[:budget]
            budget -= len(result[file])
        return result
