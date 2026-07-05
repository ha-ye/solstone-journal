# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from urllib.error import HTTPError, URLError

import pytest

from scripts import release_models_gate as gate


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.body


def test_decide_models_publish_excludes_present_version():
    index = gate.ReleaseIndex("released", versions=frozenset({"1.0.0"}))

    assert gate.decide_models_publish("1.0.0", index) is False


def test_decide_models_publish_includes_absent_version():
    index = gate.ReleaseIndex("released", versions=frozenset({"0.0.0.dev0"}))

    assert gate.decide_models_publish("1.0.0", index) is True


def test_decide_models_publish_includes_not_found():
    index = gate.ReleaseIndex("not_found")

    assert gate.decide_models_publish("1.0.0", index) is True


def test_decide_models_publish_raises_on_error():
    index = gate.ReleaseIndex("error", detail="network unavailable")

    with pytest.raises(gate.ReleaseIndexError):
        gate.decide_models_publish("1.0.0", index)


def test_fetch_release_index_normalizes_200(monkeypatch):
    def fake_urlopen(url, timeout):
        assert url == "https://pypi.org/pypi/solstone-journal-models/json"
        assert timeout == 10
        return FakeResponse(b'{"releases": {"0.0.0.dev0": [], "1.0.0": []}}')

    monkeypatch.setattr(gate, "urlopen", fake_urlopen)

    index = gate.fetch_release_index("solstone-journal-models", test=False)

    assert index.kind == "released"
    assert index.versions == frozenset({"0.0.0.dev0", "1.0.0"})


def test_fetch_release_index_normalizes_404(monkeypatch):
    def fake_urlopen(url, timeout):
        raise HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(gate, "urlopen", fake_urlopen)

    index = gate.fetch_release_index("solstone-journal-models", test=True)

    assert index.kind == "not_found"


@pytest.mark.parametrize(
    "error",
    [
        URLError("offline"),
        None,
    ],
)
def test_fetch_release_index_normalizes_errors(monkeypatch, error):
    def fake_urlopen(url, timeout):
        if error is not None:
            raise error
        return FakeResponse(b"{not-json")

    monkeypatch.setattr(gate, "urlopen", fake_urlopen)

    index = gate.fetch_release_index("solstone-journal-models", test=False)

    assert index.kind == "error"
