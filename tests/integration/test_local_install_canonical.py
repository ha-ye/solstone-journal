# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json

import pytest

from solstone.think.models import LOCAL_FLASH
from solstone.think.providers import local_install
from solstone.think.providers.install_state import read_install_status
from solstone.think.providers.local import LocalModelSpec


@pytest.mark.integration
def test_install_model_canonical_end_to_end_with_mocked_httpx(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"providers": {}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    content = b"model-bytes"
    spec = LocalModelSpec(
        model_id=LOCAL_FLASH,
        repo="example/model",
        filename="model.gguf",
        revision="main",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        min_ram_bytes=1,
    )
    monkeypatch.setitem(local_install.LOCAL_MODEL_SPECS, LOCAL_FLASH, spec)

    class FakeResponse:
        headers = {"content-length": str(len(content))}

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self):
            yield content[:5]
            yield content[5:]

    class FakeStream:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    import httpx

    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: FakeStream())

    result = local_install.install_model(LOCAL_FLASH)

    assert result["install_state"] == "installed"
    status = read_install_status(scope="bundled", name="local")
    assert status["install_state"] == "installed"
    saved = json.loads((config_dir / "journal.json").read_text(encoding="utf-8"))
    slot = saved["providers"]["bundled"]["local"]
    assert slot["install_state"] == "installed"
    assert slot["model_id"] == LOCAL_FLASH
    assert slot["model_sha256"] == spec.sha256
    assert (tmp_path / "cache" / "providers" / "local" / "models").is_dir()
    assert local_install.model_path(LOCAL_FLASH).read_bytes() == content
