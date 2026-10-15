"""안전 수정은 요청한 규칙을 해결할 수 있지만, 인접한 업무 동작을 바꾸면 안 된다."""

import hashlib
import json

import pytest

from ai.detectors import RepoView, detect
from ai.llm.fake import FakeLLMClient
from ai.pipeline import run_analysis
from ai.security import SourceMasker
from ai.transform.scope import check_patch_scope, rebase_targets
from ai.transform.service import llm_patch
from ai.transform.workspace import make_diff, source_files

SOURCE = """import logging
from fastapi import FastAPI
app = FastAPI()
logging.basicConfig(filename="app.log", level=logging.INFO, format="%(message)s")
LIMIT = 100
@app.get("/price")
def price(allowed: bool = False):
    if not allowed:
        return {"error": "denied"}
    query = "SELECT amount FROM prices"
    rows = execute(query)
    return {"amount": 100, "rows": rows}
def execute(query):
    return query
"""


def patch(tmp_path, before, after, rule="file_log", *, select=None, reference=None):
    (tmp_path / "main.py").write_text(before, encoding="utf-8")
    view = RepoView(tmp_path)
    targets = [v for v in detect(view) if v.rule == rule]
    if select is not None:
        targets = targets[select : select + 1]
    response = json.dumps(
        {
            "diff": make_diff({"main.py": before}, {"main.py": after}),
            "violation_ids": [v.id for v in targets],
        }
    )
    fake = FakeLLMClient([response] * 4)
    result = llm_patch(
        source_files(view), targets, fake, SourceMasker(view), reference_sources=reference
    )
    return result, fake, targets


@pytest.mark.parametrize(
    "arguments",
    [
        'stream=sys.stdout, level=logging.INFO, format="%(message)s"',
        'level=logging.INFO, stream=sys.stdout, format="%(message)s"',
        'level=logging.INFO, format="%(message)s", stream=sys.stdout',
    ],
)
def test_stdout_keyword_can_be_inserted_without_reordering_existing_arguments(tmp_path, arguments):
    after = "import sys\n" + SOURCE.replace(
        'filename="app.log", level=logging.INFO, format="%(message)s"', arguments
    )
    (result, ids, attempts, warnings), _, targets = patch(tmp_path, SOURCE, after)
    assert result["main.py"] == after and ids == [targets[0].id]
    assert attempts == 1 and not warnings


@pytest.mark.parametrize(
    "arguments",
    [
        'stream=sys.stderr, level=logging.INFO, format="%(message)s"',
        'stream=sys.stdout, level=logging.CRITICAL, format="%(message)s"',
        'stream=sys.stdout, format="%(message)s", level=logging.INFO',
        'stream=sys.stdout, level=logging.INFO, format="%(message)s", force=True',
        'stream=sys.stdout, level=logging.INFO, format="%(message)s", handlers=[]',
    ],
)
def test_stdout_insertion_does_not_allow_other_argument_changes(tmp_path, arguments):
    after = "import sys\n" + SOURCE.replace(
        'filename="app.log", level=logging.INFO, format="%(message)s"', arguments
    )
    (result, ids, _, warnings), _, _ = patch(tmp_path, SOURCE, after)
    assert result["main.py"] == SOURCE and not ids
    assert "[patch_scope_violation]" in warnings[0].message


@pytest.mark.parametrize(
    "extra",
    [
        ('"amount": 100', '"amount": 1'),
        ("if not allowed:", "if False:"),
        ("SELECT amount FROM prices", "DELETE FROM prices"),
        ("execute(query)", 'execute("DELETE FROM prices")'),
        ("LIMIT = 100", "LIMIT = 1"),
        ("allowed: bool = False", "allowed: bool = True"),
        ('@app.get("/price")', '@app.get("/other")'),
        ("return query", 'return "changed"'),
        ("level=logging.INFO", "level=logging.CRITICAL"),
        ('format="%(message)s"', 'format="changed"'),
    ],
)
def test_log_fix_cannot_hide_unrelated_behavior_change(tmp_path, extra):
    after = SOURCE.replace('filename="app.log", ', "").replace(*extra, 1)
    (result, ids, _, warnings), fake, _ = patch(tmp_path, SOURCE, after)
    assert result["main.py"] == SOURCE and not ids
    assert warnings and "patch_scope_violation" in fake.calls[1].user


@pytest.mark.parametrize(
    "addition",
    [
        "\ndef added_helper():\n    return True\n",
        '\nprice = lambda: {"amount": 1}\n',
        "\nimport os as logging\n",
        "\nLIMIT = 1\n",
    ],
)
def test_log_fix_cannot_add_helper_rebind_or_new_assignment(tmp_path, addition):
    after = SOURCE.replace('filename="app.log", ', "") + addition
    (result, ids, _, warnings), _, _ = patch(tmp_path, SOURCE, after)
    assert result["main.py"] == SOURCE and not ids and warnings


@pytest.mark.parametrize(
    "before,after,rule",
    [
        (SOURCE, SOURCE.replace('filename="app.log", ', ""), "file_log"),
        (
            SOURCE,
            "import sys\n"
            + SOURCE.replace('filename="app.log", ', "").replace(
                'format="%(message)s")', 'format="%(message)s", stream=sys.stdout)'
            ),
            "file_log",
        ),
        (
            'import logging\ndef setup():\n    return logging.FileHandler("app.log")\n',
            "import sys\nimport logging\ndef setup():\n"
            "    return logging.StreamHandler(sys.stdout)\n",
            "file_log",
        ),
        (
            "import uvicorn\ndef start():\n"
            '    uvicorn.run("main:app", port=8000, host="0.0.0.0")\n',
            "import os\nimport uvicorn\ndef start():\n"
            '    uvicorn.run("main:app", port=int(os.getenv("PORT", "8080")), host="0.0.0.0")\n',
            "fixed_port",
        ),
        (
            'import uvicorn\nuvicorn.run("main:app", port=8000)\n',
            'import os\nimport uvicorn\nuvicorn.run("main:app", port=int(os.environ["PORT"]))\n',
            "fixed_port",
        ),
        (
            "from sqlalchemy import create_engine\n"
            'engine = create_engine("postgresql://db.invalid/demo", pool_pre_ping=True)\n',
            "import os\nfrom sqlalchemy import create_engine\n"
            'engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)\n',
            "hardcoded_db_url",
        ),
        (
            'SECRET_KEY = "dummy-secret-do-not-use"\n',
            'import os\nSECRET_KEY = os.environ["SECRET_KEY"]\n',
            "hardcoded_secret",
        ),
    ],
)
def test_requested_safe_edits_preserve_scope_and_follow_c_env_policy(tmp_path, before, after, rule):
    (result, ids, attempts, warnings), fake, targets = patch(tmp_path, before, after, rule)
    check_patch_scope({"main.py": before}, {"main.py": after}, {"main.py"}, targets)
    if rule == "hardcoded_db_url":
        # 구조적으로 올바른 추출이어도 보류한다. DATABASE_URL 연결은 배포 쪽이 맡으므로
        # LLM이 이를 일반 앱 설정으로 끼워 넣으면 안 된다.
        assert result["main.py"] == before and not ids
        assert "environment_name_forbidden" in {warning.code for warning in warnings}
        assert "environment_name_forbidden" in fake.calls[1].user
        return
    assert result["main.py"] == after
    assert ids == [v.id for v in targets] and attempts == 1 and not warnings


@pytest.mark.parametrize(
    "port",
    [
        'int(os.getenv("OTHER_PORT", "8080"))',
        'int(os.getenv("PORT", "9000"))',
        'int(os.getenv("PORT", str(LIMIT)))',
    ],
)
def test_port_must_use_the_fixed_contract(tmp_path, port):
    before = 'import os, uvicorn\nLIMIT=8080\nuvicorn.run("main:app", port=8000)\n'
    after = before.replace("port=8000", f"port={port}")
    (result, ids, _, warnings), _, _ = patch(tmp_path, before, after, "fixed_port")
    assert result["main.py"] == before and not ids and warnings


def test_port_fix_cannot_change_another_argument_of_the_same_call(tmp_path):
    before = 'import os, uvicorn\nuvicorn.run("main:app", port=8000, host="127.0.0.1")\n'
    after = before.replace("port=8000", 'port=int(os.getenv("PORT", "8080"))')
    after = after.replace('host="127.0.0.1"', 'host="0.0.0.0"')
    (result, ids, _, warnings), _, _ = patch(tmp_path, before, after, "fixed_port")
    assert result["main.py"] == before and not ids and warnings


def test_db_url_extraction_preserves_engine_options(tmp_path):
    before = (
        "import os\nfrom sqlalchemy import create_engine\n"
        'engine=create_engine("postgresql://db.invalid/demo", pool_pre_ping=True)\n'
    )
    after = before.replace('"postgresql://db.invalid/demo"', 'os.environ["DATABASE_URL"]')
    after = after.replace("pool_pre_ping=True", "pool_pre_ping=False")
    (result, ids, _, warnings), _, targets = patch(tmp_path, before, after, "hardcoded_db_url")
    with pytest.raises(ValueError, match="patch_scope_violation"):
        check_patch_scope({"main.py": before}, {"main.py": after}, {"main.py"}, targets)
    assert result["main.py"] == before and not ids and warnings


def test_only_selected_occurrence_is_editable(tmp_path):
    before = (
        'import logging\nlogging.basicConfig(filename="first.log")\n'
        'handler=logging.FileHandler("second.log")\n'
    )
    after = "import sys\n" + before.replace('filename="first.log"', "")
    after = after.replace('logging.FileHandler("second.log")', "logging.StreamHandler(sys.stdout)")
    (result, ids, _, warnings), _, _ = patch(tmp_path, before, after, select=0)
    assert result["main.py"] == before and not ids and warnings


def test_pipeline_rebases_pending_target_after_template_adds_import(tmp_path):
    repo = tmp_path / "original"
    repo.mkdir()
    text = (
        "import logging\nfrom fastapi import FastAPI\napp=FastAPI()\n"
        'SECRET_KEY="dummy-secret-do-not-use"\nlogging.basicConfig(filename="app.log")\n'
    )
    (repo / "main.py").write_text(text)
    (repo / "requirements.txt").write_text("fastapi==0.115.0\nuvicorn==0.30.0\n")
    (repo / "local.db").write_bytes(b"\0dummy-db-do-not-use")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in repo.iterdir()}

    def response(system, user):
        payload, _ = json.JSONDecoder().raw_decode(user)
        current = payload["source"]["main.py"]
        target = payload["violations"][0]
        assert current.splitlines()[target["line"] - 1].startswith("logging.basicConfig")
        return json.dumps(
            {
                "diff": make_diff(
                    {"main.py": current}, {"main.py": current.replace('filename="app.log"', "")}
                ),
                "violation_ids": [target["id"]],
            }
        )

    fake = FakeLLMClient(
        {"diagnose": ['{"explanations":[],"candidates":[]}'], "transform": [response]}
    )
    result = run_analysis(repo, out_dir=tmp_path / "out", llm=fake, log=lambda *_: None)
    assert result.transformation.llm_attempts == 1
    assert not result.transformation.deferred_ids
    assert result.gate_report.status == "skipped" and not result.gate_report.pr_eligible
    assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in repo.iterdir()}


def test_scope_feedback_can_regenerate_a_valid_patch(tmp_path):
    (tmp_path / "main.py").write_text(SOURCE)
    repo = RepoView(tmp_path)
    target = next(v for v in detect(repo) if v.rule == "file_log")
    good = SOURCE.replace('filename="app.log", ', "")
    bad = good.replace('"amount": 100', '"amount": 1')
    responses = [
        json.dumps(
            {
                "diff": make_diff({"main.py": SOURCE}, {"main.py": text}),
                "violation_ids": [target.id],
            }
        )
        for text in (bad, good)
    ]
    fake = FakeLLMClient(responses)
    result, ids, attempts, warnings = llm_patch(
        source_files(repo), [target], fake, SourceMasker(repo)
    )
    assert result["main.py"] == good and ids == [target.id] and attempts == 2 and not warnings
    assert "patch_scope_violation" in fake.calls[1].user


def test_missing_environment_import_is_not_a_valid_port_fix(tmp_path):
    before = 'import uvicorn\nuvicorn.run("main:app", port=8000)\n'
    after = before.replace("port=8000", 'port=int(os.getenv("PORT", "8080"))')
    (result, ids, _, warnings), _, _ = patch(tmp_path, before, after, "fixed_port")
    assert result["main.py"] == before and not ids and warnings


def test_added_standard_import_cannot_shadow_an_existing_binding(tmp_path):
    before = "sys = None\n" + SOURCE
    after = "import sys\n" + before.replace('filename="app.log", ', "")
    (result, ids, _, warnings), _, _ = patch(tmp_path, before, after)
    assert result["main.py"] == before and not ids and warnings


def test_multiline_log_only_removes_file_options(tmp_path):
    before = (
        'import logging\nlogging.basicConfig(\n    filename="app.log",\n'
        '    filemode="a",\n    level=logging.INFO,\n)\n'
    )
    after = before.replace('    filename="app.log",\n', "").replace('    filemode="a",\n', "")
    (result, ids, _, warnings), _, targets = patch(tmp_path, before, after)
    assert result["main.py"] == after and ids == [targets[0].id] and not warnings


def test_individual_retry_rebases_second_target_after_first_adds_import(tmp_path):
    before = (
        'import logging\nimport uvicorn\nlogging.basicConfig(filename="app.log")\n'
        'uvicorn.run("main:app", port=8000)\n'
    )
    (tmp_path / "main.py").write_text(before)
    view = RepoView(tmp_path)
    targets = [v for v in detect(view) if v.auto_fixable]

    def individual_response(system, user):
        payload, _ = json.JSONDecoder().raw_decode(user)
        current = payload["source"]["main.py"]
        target = payload["violations"][0]
        if target["rule"] == "file_log":
            after = "import sys\n" + current.replace('filename="app.log"', "stream=sys.stdout")
        else:
            assert current.splitlines()[target["line"] - 1].startswith("uvicorn.run")
            after = "import os\n" + current.replace(
                "port=8000", 'port=int(os.getenv("PORT", "8080"))'
            )
        return json.dumps(
            {
                "diff": make_diff({"main.py": current}, {"main.py": after}),
                "violation_ids": [target["id"]],
            }
        )

    fake = FakeLLMClient(
        [
            *[
                json.dumps({"diff": f"bad{i}", "violation_ids": [t.id for t in targets]})
                for i in range(3)
            ],
            individual_response,
            individual_response,
        ]
    )
    result, ids, attempts, warnings = llm_patch(
        source_files(view), targets, fake, SourceMasker(view)
    )
    assert ids == [t.id for t in targets] and attempts == 5 and not warnings
    assert "stream=sys.stdout" in result["main.py"] and "int(os.getenv" in result["main.py"]


def test_missing_or_ambiguous_target_is_deferred_instead_of_switching_occurrence(tmp_path):
    before = 'import logging\nlogging.basicConfig(filename="app.log")\n' * 2
    (tmp_path / "main.py").write_text(before)
    target = next(v for v in detect(RepoView(tmp_path)) if v.rule == "file_log")
    current = before.replace('logging.basicConfig(filename="app.log")\n', "", 1)
    with pytest.raises(ValueError, match="patch_target_not_found"):
        rebase_targets({"main.py": before}, {"main.py": current}, [target])


def test_risky_finding_never_enters_safe_llm_transform_even_if_marked_fixable(tmp_path):
    text = 'import sqlite3\nconnection=sqlite3.connect("demo.db")\n'
    (tmp_path / "main.py").write_text(text)
    repo = RepoView(tmp_path)
    target = next(v for v in detect(repo) if v.rule == "sqlite_usage")
    fake = FakeLLMClient([])
    result, ids, attempts, warnings = llm_patch(
        source_files(repo),
        [target.model_copy(update={"auto_fixable": True})],
        fake,
        SourceMasker(repo),
    )
    assert result == source_files(repo) and not ids and attempts == 0 and not warnings
    assert not fake.calls and not (tmp_path / "demo.db").exists()


@pytest.mark.parametrize(
    "before_cookie,after_cookie",
    [
        ("", "# coding: latin-1\n"),
        ("", "# coding: utf-8\n"),
        ("# coding: utf-8\n", "# coding: latin-1\n"),
        ("# coding: utf-8\n", ""),
        ("#!/usr/bin/python\n", "#!/usr/bin/python\n# coding: latin-1\n"),
    ],
)
def test_encoding_cookie_change_is_rejected_even_with_identical_unicode_ast(
    tmp_path, before_cookie, after_cookie
):
    body = SOURCE + 'message = "한글"\n'
    before = before_cookie + body
    after = after_cookie + body.replace('filename="app.log", ', "")
    (result, ids, _, warnings), _, _ = patch(tmp_path, before, after)
    assert result["main.py"] == before and not ids
    assert "[patch_scope_violation]" in warnings[0].message
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == before


def test_first_two_line_cookie_cannot_hide_inside_target_comment_exception(tmp_path):
    before = 'import logging; logging.basicConfig(\n    filename="app.log",\n)\n'
    after = "import logging; logging.basicConfig(\n    # coding: latin-1\n)\n"
    (tmp_path / "main.py").write_text(before)
    target = next(v for v in detect(RepoView(tmp_path)) if v.rule == "file_log")
    with pytest.raises(ValueError, match="^patch_scope_violation$"):
        check_patch_scope({"main.py": before}, {"main.py": after}, {"main.py"}, [target])


def test_unchanged_encoding_cookie_with_korean_text_is_allowed(tmp_path):
    before = "# coding: utf-8\n" + SOURCE + 'message="한글"\n'
    after = before.replace('filename="app.log", ', "")
    (result, ids, _, warnings), _, targets = patch(tmp_path, before, after)
    assert result["main.py"] == after and ids == [targets[0].id] and not warnings


@pytest.mark.parametrize(
    "change",
    [
        ("# keep policy", "# changed policy"),
        ("# keep policy\n", ""),
        ("LIMIT = 100", "# new unrelated comment\nLIMIT = 100"),
        ("LIMIT = 100", "LIMIT = 100 # noqa"),
        ("LIMIT = 100", "LIMIT = 100 # type: ignore"),
        ("# keep policy", "# keep policy\n# extra policy"),
    ],
)
def test_comments_outside_target_cannot_be_added_changed_or_deleted(tmp_path, change):
    before = SOURCE.replace("LIMIT = 100", "# keep policy\nLIMIT = 100")
    after = before.replace('filename="app.log", ', "").replace(*change)
    (result, ids, _, warnings), _, _ = patch(tmp_path, before, after)
    assert result["main.py"] == before and not ids
    assert "[patch_scope_violation]" in warnings[0].message


@pytest.mark.parametrize("comment", ["", " # stdout", " # updated explanation"])
def test_target_line_comment_edits_are_allowed(tmp_path, comment):
    before = SOURCE.replace('format="%(message)s")', 'format="%(message)s") # old note')
    after = before.replace('filename="app.log", ', "").replace(" # old note", comment)
    (result, ids, _, warnings), _, targets = patch(tmp_path, before, after)
    assert result["main.py"] == after and ids == [targets[0].id] and not warnings


def test_multiline_target_comments_and_shifted_outside_comments_are_allowed(tmp_path):
    before = (
        "# module note\nimport logging\nlogging.basicConfig(\n"
        '    filename="app.log", # file sink\n    level=logging.INFO,\n)\n'
        '# keep following note\nmessage="# this is a string, not a comment"\n'
    )
    after = (
        before.replace("import logging", "import sys\nimport logging")
        .replace('    filename="app.log", # file sink\n', "")
        .replace(
            "    level=logging.INFO,\n",
            "    level=logging.INFO,\n    stream=sys.stdout, # stdout sink\n",
        )
    )
    (result, ids, _, warnings), _, targets = patch(tmp_path, before, after)
    assert result["main.py"] == after and ids == [targets[0].id] and not warnings


def test_unselected_occurrence_comment_remains_protected(tmp_path):
    before = (
        'import logging\nlogging.basicConfig(filename="app.log") # selected\n'
        'handler=logging.FileHandler("other.log") # keep this comment\n'
    )
    after = before.replace('filename="app.log"', "").replace(
        "# keep this comment", "# unrelated edit"
    )
    (result, ids, _, warnings), _, _ = patch(tmp_path, before, after, select=0)
    assert result["main.py"] == before and not ids
    assert "[patch_scope_violation]" in warnings[0].message


def test_warning_retains_scope_reason_when_model_repeats_rejected_patch(tmp_path):
    after = SOURCE.replace('filename="app.log", ', "").replace('"amount": 100', '"amount": 1')
    (_, ids, _, warnings), _, _ = patch(tmp_path, SOURCE, after)
    assert not ids and warnings[0].code == "llm_transform_deferred"
    assert "[patch_scope_violation]" in warnings[0].message
    assert "amount" not in warnings[0].message


def test_missing_target_warning_has_fixed_reason_and_makes_no_llm_call(tmp_path):
    (tmp_path / "main.py").write_text(SOURCE)
    view = RepoView(tmp_path)
    target = next(v for v in detect(view) if v.rule == "file_log")
    current = SOURCE.replace('filename="app.log", ', "")
    fake = FakeLLMClient([])
    result, ids, attempts, warnings = llm_patch(
        {"main.py": current},
        [target],
        fake,
        SourceMasker(view),
        reference_sources=source_files(view),
    )
    assert result["main.py"] == current and not ids and attempts == 0 and not fake.calls
    assert "[patch_target_not_found]" in warnings[0].message


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, SyntaxError])
def test_unknown_error_text_never_enters_warning_or_retry_code(tmp_path, error_type):
    (tmp_path / "main.py").write_text(SOURCE)
    view = RepoView(tmp_path)
    target = next(v for v in detect(view) if v.rule == "file_log")

    def fail(system, user):
        raise error_type('private source: return {"amount": 100}')

    fake = FakeLLMClient([fail] * 4)
    _, ids, _, warnings = llm_patch(source_files(view), [target], fake, SourceMasker(view))
    assert not ids and "[llm_patch_failed]" in warnings[0].message
    assert "private source" not in warnings[0].message and "amount" not in warnings[0].message
    for call in fake.calls[1:]:
        payload, _ = json.JSONDecoder().raw_decode(call.user)
        assert payload["validation_error"] == "llm_patch_failed"
