import json

import pytest

from tcc_pipeline.config import ProjectConfig
from tcc_pipeline.context import build_context, redact_text
from tcc_pipeline.models import Issue


def project(tmp_path, **kwargs):
    return ProjectConfig(
        name="fixture", repo=str(tmp_path), test_commands=[["python", "-m", "pytest"]], **kwargs
    )


def issue(path="module.py", line=1):
    return Issue(key="one", rule="python:S1481", path=path, line=line, message="Unused variable")


def test_context_preserves_crlf_and_includes_nearest_tests(tmp_path):
    source = b"def run():\r\n    unused = 1  # DEMO_UNUSED\r\n    return 2\r\n"
    (tmp_path / "module.py").write_bytes(source)
    (tmp_path / "test_module.py").write_text("def test_run():\n    assert True\n")
    context = build_context(tmp_path, issue(line=2), project(tmp_path), 4000)
    assert context["files"]["module.py"] == source.decode()
    assert "test_module.py" in context["files"]
    assert context["file_metadata"]["module.py"]["start_line"] == 1
    assert context["issue"]["line"] == 2


def test_context_is_json_bounded_and_keeps_target_near_end(tmp_path):
    text = "prefix = 'text with \\\" escape'\n" * 1000 + "target = 1\n" + "tail = 0\n" * 100
    (tmp_path / "module.py").write_text(text, encoding="utf-8")
    context = build_context(tmp_path, issue(line=1001), project(tmp_path), 1200)
    assert len(json.dumps(context, ensure_ascii=False)) <= 1200
    assert "target = 1" in context["files"]["module.py"]
    assert context["file_metadata"]["module.py"]["truncated"]
    assert context["file_metadata"]["module.py"]["start_line"] > 900


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../outside.py",
        "C:/private.py",
        "/private.py",
        "..\\outside.py",
        ".env",
        "secrets.json",
        ".envrc",
        "module.py:secret",
        "bad\x00.py",
    ],
)
def test_rejects_unsafe_target_paths(tmp_path, name):
    with pytest.raises(ValueError):
        build_context(tmp_path, issue(name), project(tmp_path), 3000)


def test_explicit_secrets_and_reference_labels_are_excluded(tmp_path):
    (tmp_path / "module.py").write_text("answer = 42\n")
    for name in (".env", "credentials.json", "reference_labels.csv", "manual-labels.json"):
        (tmp_path / name).write_text("private")
    (tmp_path / "helper.py").write_text("VALUE = 1\n")
    config = project(
        tmp_path,
        context_files=[
            ".env",
            "credentials.json",
            "reference_labels.csv",
            "manual-labels.json",
            "helper.py",
        ],
    )
    context = build_context(tmp_path, issue(), config, 4000)
    assert set(context["files"]) == {"module.py", "helper.py"}
    assert "private" not in json.dumps(context)


def test_source_secret_literals_are_redacted(tmp_path):
    (tmp_path / "module.py").write_text('API_KEY = "highly-sensitive-value"\nanswer = 42\n')
    context = build_context(tmp_path, issue(line=2), project(tmp_path), 3000)
    assert "highly-sensitive-value" not in json.dumps(context)
    assert "[REDACTED]" in context["files"]["module.py"]


def test_outside_symlink_is_not_read(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 'PRIVATE'\n")
    try:
        (repo / "linked.py").symlink_to(outside)
    except OSError:
        pytest.skip("Criação de symlink indisponível nesta conta Windows")
    with pytest.raises(ValueError, match="Link fora"):
        build_context(repo, issue("linked.py"), project(repo), 3000)


def test_long_target_line_fails_instead_of_providing_invalid_edit_text(tmp_path):
    (tmp_path / "module.py").write_text("x" * 5000)
    with pytest.raises(ValueError, match="limite"):
        build_context(tmp_path, issue(), project(tmp_path), 1000)


def test_redaction_handles_pem_bearer_and_known_values():
    text = "-----BEGIN PRIVATE KEY-----\nhello\n-----END PRIVATE KEY-----\nBearer abcdefgh123456\nknown-value"
    redacted = redact_text(text, ("known-value",))
    assert all(value not in redacted for value in ["hello", "abcdefgh123456", "known-value"])


def test_multiline_secret_redaction_preserves_source_line_locations(tmp_path):
    source = 'KEY = """-----BEGIN PRIVATE KEY-----\r\nprivate\r\n-----END PRIVATE KEY-----"""\r\ntarget = 42\r\n'
    (tmp_path / "module.py").write_bytes(source.encode())
    context = build_context(tmp_path, issue(line=4), project(tmp_path), 3000)
    actual = context["files"]["module.py"]
    assert actual.splitlines()[3] == "target = 42"
    assert actual.count("\r\n") == source.count("\r\n")
    assert "private" not in actual


def test_inside_symlink_cannot_alias_excluded_secret_file(tmp_path):
    (tmp_path / "secrets.py").write_text("PRIVATE = 'sensitive'\n")
    try:
        (tmp_path / "module.py").symlink_to(tmp_path / "secrets.py")
    except OSError:
        pytest.skip("Criação de symlink indisponível nesta conta Windows")
    with pytest.raises(ValueError, match="Link"):
        build_context(tmp_path, issue(), project(tmp_path), 3000)


@pytest.mark.parametrize("payload", [b"binary\x00content", b"\xff\xfe\x00"])
def test_binary_or_non_utf8_target_rejected(tmp_path, payload):
    (tmp_path / "module.py").write_bytes(payload)
    with pytest.raises(ValueError):
        build_context(tmp_path, issue(), project(tmp_path), 3000)
