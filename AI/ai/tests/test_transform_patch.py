"""LLM diff 전송이 원본 소스 승인을 약하게 하거나 비밀을 노출하면 안 된다."""

import json
import re

import pytest

from ai.detectors import RepoView, detect
from ai.llm.fake import FakeLLMClient
from ai.security import SourceMasker
from ai.transform.patch import canonical_patch
from ai.transform.service import llm_patch
from ai.transform.workspace import Workspace, make_diff, source_files

SOURCE = """# coding: utf-8
# keep module policy
import logging
import uvicorn
from fastapi import FastAPI
app = FastAPI()
SECRET_KEY = "dummy-secret-do-not-use"
logging.basicConfig(filename="app.log", level=logging.INFO, format="%(message)s")
uvicorn.run("main:app", port=8000, host="127.0.0.1")
# keep business policy
@app.get("/price")
def price(allowed: bool = False):
    if not allowed:
        return {"error": "denied"}
    query = "SELECT amount FROM prices"
    return {"amount": 100, "query": query, "message": "한글"}
"""


def setup_patch(tmp_path, before=SOURCE, rules=None):
    (tmp_path / "main.py").write_bytes(before.encode("utf-8"))
    view = RepoView(tmp_path)
    targets = [v for v in detect(view) if v.auto_fixable and (rules is None or v.rule in rules)]
    return view, SourceMasker(view), targets


def attempt(view, masker, targets, diff):
    response = json.dumps({"diff": diff, "violation_ids": [v.id for v in targets]})
    fake = FakeLLMClient([response] * (3 + len(targets)))
    return llm_patch(source_files(view), targets, fake, masker)


def transport_error(diff):
    # 모델이 틀리게 낸 헤더 수·좌표와 빠진 경로 접두사를 재현한다.
    diff = re.sub(r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", "@@ -999,1 +888,1 @@", diff)
    return diff.replace("--- a/main.py", "--- main.py").replace("+++ b/main.py", "+++ main.py")


def test_masked_three_rule_response_becomes_valid_original_diff(tmp_path):
    view, masker, targets = setup_patch(tmp_path)
    visible = masker.source(view, "main.py")
    after = (
        visible.replace("import logging", "import os\nimport sys\nimport logging")
        .replace('"[REDACTED]"', 'os.environ["SECRET_KEY"]')
        .replace('filename="app.log", ', "")
        .replace('format="%(message)s")', 'format="%(message)s", stream=sys.stdout)')
        .replace("port=8000", 'port=int(os.getenv("PORT", "8080"))')
    )
    diff = transport_error(make_diff({"main.py": visible}, {"main.py": after})).rstrip("\n")
    candidate, ids, attempts, warnings = attempt(view, masker, targets, diff)
    assert ids == [v.id for v in targets] and attempts == 1 and not warnings
    assert "[REDACTED]" not in candidate["main.py"]
    assert candidate["main.py"] == after
    with Workspace(source_files(view)) as workspace:
        workspace.apply(make_diff(source_files(view), candidate))
        assert workspace.compile()
    assert (tmp_path / "main.py").read_text() == SOURCE


def test_unchanged_masked_line_keeps_original_without_replacing_markers(tmp_path):
    view, masker, targets = setup_patch(tmp_path, rules={"file_log"})
    visible = masker.source(view, "main.py")
    after = visible.replace('filename="app.log", ', "")
    candidate, ids, _, warnings = attempt(
        view, masker, targets, make_diff({"main.py": visible}, {"main.py": after})
    )
    assert ids == [targets[0].id] and not warnings
    assert candidate["main.py"] == SOURCE.replace('filename="app.log", ', "")
    assert 'SECRET_KEY = "dummy-secret-do-not-use"' in candidate["main.py"]


@pytest.mark.parametrize(
    "extra",
    [
        ('"amount": 100', '"amount": 1'),
        ("if not allowed:", "if False:"),
        ("SELECT amount FROM prices", "DELETE FROM prices"),
        ("# keep business policy", "# changed policy"),
        ("# coding: utf-8", "# coding: latin-1"),
    ],
)
def test_canonicalization_never_approves_unrelated_changes(tmp_path, extra):
    view, masker, targets = setup_patch(tmp_path, rules={"file_log"})
    visible = masker.source(view, "main.py")
    after = visible.replace('filename="app.log", ', "").replace(*extra)
    candidate, ids, _, warnings = attempt(
        view, masker, targets, transport_error(make_diff({"main.py": visible}, {"main.py": after}))
    )
    assert candidate == source_files(view) and not ids
    assert "[patch_scope_violation]" in warnings[0].message


def test_modified_line_with_unresolved_marker_is_deferred(tmp_path):
    before = (
        'import logging\nSECRET_KEY="dummy-secret-do-not-use"; '
        'logging.basicConfig(filename="app.log")\n'
    )
    view, masker, targets = setup_patch(tmp_path, before, {"file_log"})
    visible = masker.source(view, "main.py")
    after = visible.replace('filename="app.log"', "")
    candidate, ids, _, warnings = attempt(
        view, masker, targets, make_diff({"main.py": visible}, {"main.py": after})
    )
    assert candidate == source_files(view) and not ids
    assert "[masked_patch_unresolved]" in warnings[0].message


def test_new_marker_is_not_restored_to_original_dummy(tmp_path):
    view, masker, targets = setup_patch(tmp_path, rules={"file_log"})
    visible = masker.source(view, "main.py")
    after = visible.replace('filename="app.log", ', "") + 'OTHER_VALUE="[REDACTED]"\n'
    candidate, ids, _, warnings = attempt(
        view, masker, targets, make_diff({"main.py": visible}, {"main.py": after})
    )
    assert candidate == source_files(view) and not ids
    assert "[masked_patch_unresolved]" in warnings[0].message


def test_real_credential_file_stays_blocked_before_any_llm_call(tmp_path):
    fictional = "fabricated-" + "credential-do-not-use"
    before = SOURCE.replace("dummy-secret-do-not-use", fictional)
    view, masker, targets = setup_patch(tmp_path, before, {"file_log"})
    fake = FakeLLMClient([])
    result, ids, attempts, warnings = llm_patch(source_files(view), targets, fake, masker)
    assert result == source_files(view) and not ids and attempts == 0 and not warnings
    assert "main.py" in masker.blocked_files and not fake.calls


def test_separate_import_and_port_hunks_apply_against_same_before():
    before = 'import uvicorn\nuvicorn.run("main:app", port=8000)\n'
    diff = """--- a/main.py
+++ b/main.py
@@ -1,999 +1,999 @@
 import uvicorn
+import os
@@ -2,999 +3,999 @@
-uvicorn.run("main:app", port=8000)
+uvicorn.run("main:app", port=int(os.environ["PORT"]))
"""
    patch = canonical_patch({"main.py": before}, diff, {"main.py"}, {})
    with Workspace({"main.py": before}) as workspace:
        workspace.apply(patch)
        assert workspace.compile()
        assert workspace.read({"main.py"})["main.py"].startswith("import uvicorn\nimport os\n")


@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
def test_line_endings_and_source_eof_marker(ending):
    before = "x=1" + ending
    after = "x=2" + ending
    diff = make_diff({"main.py": before}, {"main.py": after})
    patch = canonical_patch({"main.py": before}, diff, {"main.py"}, {})
    with Workspace({"main.py": before}) as workspace:
        workspace.apply(patch)
        # Workspace.read의 줄바꿈 정책과 무관하게 전송 바이트를 확인한다.
        assert (workspace.root / "main.py").read_bytes().decode("utf-8") == after


@pytest.mark.parametrize(
    "old,new",
    [
        ("../main.py", "main.py"),
        ("/main.py", "main.py"),
        ("other.py", "other.py"),
        ("a/main.py", "b/other.py"),
        ("a/main.py", "/dev/null"),
        ("/dev/null", "b/main.py"),
    ],
)
def test_header_normalization_does_not_allow_traversal_rename_or_creation(old, new):
    diff = f"--- {old}\n+++ {new}\n@@ -1,1 +1,1 @@\n-x=1\n+x=2\n"
    with pytest.raises(ValueError):
        canonical_patch({"main.py": "x=1\n"}, diff, {"main.py"}, {})


@pytest.mark.parametrize(
    "metadata",
    ["new mode 100755", "old mode 100644", "rename from main.py", "GIT binary patch"],
)
def test_transport_rejects_file_metadata(metadata):
    diff = f"{metadata}\n--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-x=1\n+x=2\n"
    with pytest.raises(ValueError, match="^patch_metadata_forbidden$"):
        canonical_patch({"main.py": "x=1\n"}, diff, {"main.py"}, {})


def test_transport_rejects_duplicate_file_sections():
    section = "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-x=1\n+x=2\n"
    with pytest.raises(ValueError, match="^patch_duplicate_file$"):
        canonical_patch({"main.py": "x=1\n"}, section * 2, {"main.py"}, {})


def test_transport_rejects_unaligned_masked_source():
    diff = (
        '--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-SECRET_KEY="[REDACTED]"\n'
        '+SECRET_KEY=os.environ["SECRET_KEY"]\n'
    )
    with pytest.raises(ValueError, match="^masked_source_unaligned$"):
        canonical_patch(
            {"main.py": 'SECRET_KEY="dummy-secret-do-not-use"\n'},
            diff,
            {"main.py"},
            {"main.py": '\nSECRET_KEY="[REDACTED]"\n'},
        )


@pytest.mark.parametrize(
    "source,diff,code",
    [
        (
            "x=1\nx=1\n",
            "--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,1 @@\n-x=1\n+x=2\n",
            "patch_context_ambiguous",
        ),
        (
            "x=1\n",
            "--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,1 @@\n-y=1\n+x=2\n",
            "patch_context_not_found",
        ),
        (
            "x=1\ny=1\n",
            "--- a/main.py\n+++ b/main.py\n@@ -1,2 +1,2 @@\n-x=1\n+x=2\n y=1\n"
            "@@ -2,1 +2,1 @@\n-y=1\n+y=2\n",
            "patch_overlapping_hunks",
        ),
        (
            "x=1\n",
            "--- a/main.py\n+++ b/main.py\n@@ -1,0 +1,1 @@\n+import os\n",
            "patch_invalid_hunk",
        ),
    ],
)
def test_context_is_exact_unique_nonoverlapping_and_required(source, diff, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        canonical_patch({"main.py": source}, diff, {"main.py"}, {})
