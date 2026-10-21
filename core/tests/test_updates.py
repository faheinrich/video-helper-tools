import threading

import pytest

from video_helper_core import updates

RELEASES = [
    {"tag_name": "analysis-v0.3.0", "html_url": "https://example/analysis-0.3.0"},
    {"tag_name": "archive-v0.0.2", "html_url": "https://example/archive-0.0.2"},
    {"tag_name": "archive-v0.0.10", "html_url": "https://example/archive-0.0.10"},
    {"tag_name": "archive-v9.9.9", "html_url": "https://example/draft", "draft": True},
    {"tag_name": "archive-v8.0.0", "html_url": "https://example/beta", "prerelease": True},
    {"tag_name": "archive-v0.0.1", "html_url": "https://example/archive-0.0.1"},
]


@pytest.mark.parametrize("text,expected", [("0.0.1", (0, 0, 1)), ("1.2", (1, 2, 0)), ("0.0.12", (0, 0, 12)), ("x", (0, 0, 0))])
def test_parse_version(text, expected):
    assert updates.parse_version(text) == expected


def test_versions_are_compared_numerically_not_as_text():
    assert updates.parse_version("0.0.10") > updates.parse_version("0.0.9")


def test_newest_release_of_this_app_only_without_drafts_and_prereleases():
    assert updates.newest_release(RELEASES, "archive-v") == ("0.0.10", "https://example/archive-0.0.10")
    assert updates.newest_release(RELEASES, "analysis-v") == ("0.3.0", "https://example/analysis-0.3.0")
    assert updates.newest_release(RELEASES, "other-v") is None
    assert updates.newest_release([], "archive-v") is None


def test_only_a_newer_release_is_announced():
    assert updates.newer_release(RELEASES, "archive-v", "0.0.1")[0] == "0.0.10"
    assert updates.newer_release(RELEASES, "archive-v", "0.0.10") is None
    assert updates.newer_release(RELEASES, "archive-v", "1.0.0") is None


def run_checker(qapp, monkeypatch, fetch, current="0.0.1"):
    monkeypatch.delenv(updates.DISABLE_ENV)
    monkeypatch.setattr(updates, "fetch_releases", fetch)
    checker = updates.UpdateChecker("archive-v", current)
    found = []
    checker.found.connect(lambda version, url: found.append((version, url)))
    thread = checker.start()
    thread.join(5)
    qapp.processEvents()
    return found


def test_checker_reports_a_newer_release(qapp, monkeypatch):
    assert run_checker(qapp, monkeypatch, lambda: RELEASES) == [("0.0.10", "https://example/archive-0.0.10")]


def test_checker_is_silent_when_up_to_date(qapp, monkeypatch):
    assert run_checker(qapp, monkeypatch, lambda: RELEASES, current="0.0.10") == []


@pytest.mark.parametrize("error", [OSError("offline"), ValueError("not json")])
def test_checker_ignores_network_and_format_errors(qapp, monkeypatch, error):
    def fail():
        raise error

    assert run_checker(qapp, monkeypatch, fail) == []


def test_checker_ignores_unexpected_answers(qapp, monkeypatch):
    assert run_checker(qapp, monkeypatch, lambda: {"message": "rate limit exceeded"}) == []


def test_environment_variable_disables_the_check(monkeypatch):
    monkeypatch.setenv(updates.DISABLE_ENV, "1")
    monkeypatch.setattr(updates, "fetch_releases", lambda: pytest.fail("must not ask GitHub"))
    assert updates.UpdateChecker("archive-v", "0.0.1").start() is None


def test_fetch_releases_asks_github_with_a_user_agent(monkeypatch):
    import io
    import json

    seen = {}

    def fake_urlopen(request, timeout, context):
        seen["url"], seen["agent"], seen["timeout"], seen["context"] = request.full_url, request.get_header("User-agent"), timeout, context
        return io.BytesIO(json.dumps(RELEASES).encode())

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    assert updates.fetch_releases() == RELEASES
    assert seen["url"].startswith("https://api.github.com/repos/faheinrich/video-helper-tools/releases")
    assert seen["agent"] and seen["timeout"] == updates.TIMEOUT_SECONDS and seen["context"] is not None


def test_tls_uses_the_certificate_list_shipped_with_the_app(monkeypatch):
    import certifi

    asked = []
    monkeypatch.setattr(updates.ssl, "create_default_context", lambda **kw: asked.append(kw) or object())
    updates.ssl_context()
    assert asked == [{"cafile": certifi.where()}]


def test_tls_falls_back_to_the_system_certificates_without_certifi(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_certifi(name, *args, **kwargs):
        if name == "certifi":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    asked = []
    monkeypatch.setattr(builtins, "__import__", no_certifi)
    monkeypatch.setattr(updates.ssl, "create_default_context", lambda **kw: asked.append(kw) or object())
    updates.ssl_context()
    assert asked == [{}]


@pytest.mark.parametrize("error", [RuntimeError("odd"), updates.ssl.SSLCertVerificationError("certificate verify failed"), KeyError("x")])
def test_any_failure_is_recorded_not_raised(qapp, monkeypatch, error):
    def fail():
        raise error

    checker = updates.UpdateChecker("archive-v", "0.0.1")
    monkeypatch.delenv(updates.DISABLE_ENV)
    monkeypatch.setattr(updates, "fetch_releases", fail)
    checker.start().join(5)
    assert checker.status == "failed" and type(error).__name__ in checker.detail
    assert "fehlgeschlagen" in checker.describe() and checker.detail in checker.describe()


def test_the_status_tells_what_the_check_found(qapp, monkeypatch):
    monkeypatch.delenv(updates.DISABLE_ENV)
    monkeypatch.setattr(updates, "fetch_releases", lambda: RELEASES)
    newer = updates.UpdateChecker("archive-v", "0.0.1")
    assert newer.status == "waiting" and "läuft noch" in newer.describe()
    newer.start().join(5)
    assert newer.status == "newer" and "0.0.10" in newer.describe() and "verfügbar" in newer.describe()
    current = updates.UpdateChecker("archive-v", "0.0.10")
    current.start().join(5)
    assert current.status == "current" and "neueste Version" in current.describe() and "0.0.10" in current.describe()


def test_a_switched_off_check_says_so(monkeypatch):
    monkeypatch.setenv(updates.DISABLE_ENV, "1")
    checker = updates.UpdateChecker("archive-v", "0.0.1")
    assert checker.start() is None and checker.status == "disabled" and "ausgeschaltet" in checker.describe()
