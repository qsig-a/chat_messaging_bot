"""Deployed-version resolution."""

from sms_bridge.version import DEFAULT_VERSION, resolve_version


def test_reports_dev_when_no_file(tmp_path):
    assert resolve_version(tmp_path) == DEFAULT_VERSION


def test_reads_the_version_file(tmp_path):
    (tmp_path / "VERSION").write_text("v1.2.3\n")
    assert resolve_version(tmp_path) == "v1.2.3"


def test_strips_surrounding_whitespace(tmp_path):
    (tmp_path / "VERSION").write_text("  v0.3.0 \n")
    assert resolve_version(tmp_path) == "v0.3.0"


def test_blank_file_falls_back_to_dev(tmp_path):
    (tmp_path / "VERSION").write_text("   \n")
    assert resolve_version(tmp_path) == DEFAULT_VERSION


def test_accepts_a_string_root(tmp_path):
    (tmp_path / "VERSION").write_text("v9.9.9\n")
    assert resolve_version(str(tmp_path)) == "v9.9.9"


def test_default_root_returns_a_nonempty_string():
    # Whatever the checkout state, it must not raise and must be usable in a log line.
    version = resolve_version()
    assert isinstance(version, str) and version
