# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

from solstone.think import install_models, provider_cache_seed

ProviderFile = tuple[str, str, bytes]


def _write_provider_files(root: Path, files: tuple[ProviderFile, ...]) -> Path:
    cache = root / "journal" / "cache" / "providers"
    for provider, relpath, content in files:
        target = cache / provider / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return cache


def _regular_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def _configure_seed(
    monkeypatch: pytest.MonkeyPatch,
    current_root: Path,
    *,
    sibling_roots: list[Path] | None = None,
    packaged: bool = False,
) -> None:
    monkeypatch.delenv("SOLSTONE_PROVIDER_CACHE_SEED_SOURCE", raising=False)
    monkeypatch.setattr(
        provider_cache_seed, "get_journal", lambda: str(current_root / "journal")
    )
    monkeypatch.setattr(
        provider_cache_seed, "get_project_root", lambda: str(current_root)
    )
    monkeypatch.setattr(provider_cache_seed, "is_packaged_install", lambda: packaged)
    monkeypatch.setattr(
        provider_cache_seed,
        "_sibling_worktrees",
        lambda _root: list(sibling_roots or []),
    )


def _assert_hardlink(source: Path, target: Path) -> None:
    source_stat = os.stat(source)
    target_stat = os.stat(target)
    assert target_stat.st_ino == source_stat.st_ino
    assert target_stat.st_nlink >= 2


def test_seed_provider_cache_success_hardlinks_sibling_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    current_root = tmp_path / "current"
    sibling_root = tmp_path / "sibling"
    files = (
        ("rerank", "rev/onnx/model.onnx", b"rerank-model"),
        ("rerank", "rev/tokenizer.json", b"tokenizer"),
        ("rfdetr", "engine/65c0ffcc/rfdetr-cli", b"rfdetr-cli"),
        ("rfdetr", "model/rev/rfdetr-nano-f16.gguf", b"rfdetr-model"),
    )
    sibling_cache = _write_provider_files(sibling_root, files)
    _configure_seed(monkeypatch, current_root, sibling_roots=[sibling_root])

    result = provider_cache_seed.seed_provider_cache()

    assert result.seeded is True
    assert result.file_count == len(files)
    assert result.byte_count == sum(
        len(content) for _provider, _relpath, content in files
    )
    assert result.source == sibling_cache
    current_cache = current_root / "journal" / "cache" / "providers"
    for provider, relpath, _content in files:
        _assert_hardlink(
            sibling_cache / provider / relpath, current_cache / provider / relpath
        )
    assert "provider cache: seeded 4 files" in capsys.readouterr().out


def test_seed_provider_cache_cross_device_skips_without_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    current_root = tmp_path / "current"
    sibling_root = tmp_path / "sibling"
    _write_provider_files(
        sibling_root,
        (("rerank", "rev/onnx/model.onnx", b"rerank-model"),),
    )
    _configure_seed(monkeypatch, current_root, sibling_roots=[sibling_root])

    def raise_exdev(_src: Path, _dst: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(provider_cache_seed.os, "link", raise_exdev)

    result = provider_cache_seed.seed_provider_cache()

    assert result.seeded is False
    assert result.reason == "cross-device"
    assert _regular_files(current_root / "journal" / "cache" / "providers") == []
    assert "is on a different filesystem, will download" in capsys.readouterr().out


def test_seed_provider_cache_no_sibling_skips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    current_root = tmp_path / "current"
    _configure_seed(monkeypatch, current_root, sibling_roots=[])

    result = provider_cache_seed.seed_provider_cache()

    assert result.seeded is False
    assert result.reason == "no-source"
    assert _regular_files(current_root / "journal" / "cache" / "providers") == []
    assert "no sibling checkout with a populated cache" in capsys.readouterr().out


def test_seed_provider_cache_partial_cache_hardlinks_available_providers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    current_root = tmp_path / "current"
    sibling_root = tmp_path / "sibling"
    files = (
        ("rerank", "rev/onnx/model.onnx", b"rerank-model"),
        ("ced", "v0.1.0/.ced-install.json", b"{}"),
    )
    sibling_cache = _write_provider_files(sibling_root, files)
    _configure_seed(monkeypatch, current_root, sibling_roots=[sibling_root])

    result = provider_cache_seed.seed_provider_cache()

    assert result.seeded is True
    assert result.file_count == len(files)
    current_cache = current_root / "journal" / "cache" / "providers"
    for provider, relpath, _content in files:
        _assert_hardlink(
            sibling_cache / provider / relpath, current_cache / provider / relpath
        )
    assert not (current_cache / "rfdetr").exists()


def test_seed_provider_cache_local_already_populated_skips_before_sibling_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    current_root = tmp_path / "current"
    existing = (
        _write_provider_files(
            current_root,
            (("rerank", "existing.txt", b"local"),),
        )
        / "rerank"
        / "existing.txt"
    )
    monkeypatch.delenv("SOLSTONE_PROVIDER_CACHE_SEED_SOURCE", raising=False)
    monkeypatch.setattr(
        provider_cache_seed, "get_journal", lambda: str(current_root / "journal")
    )
    monkeypatch.setattr(
        provider_cache_seed,
        "_sibling_worktrees",
        lambda _root: pytest.fail("sibling lookup should not run"),
    )

    result = provider_cache_seed.seed_provider_cache()

    assert result.seeded is False
    assert result.reason == "local-populated"
    assert existing.read_bytes() == b"local"
    assert "local cache already populated" in capsys.readouterr().out


def test_seed_provider_cache_env_override_precedes_git_worktrees(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    current_root = tmp_path / "current"
    override_root = tmp_path / "override"
    sibling_root = tmp_path / "sibling"
    override_cache = _write_provider_files(
        override_root,
        (("rerank", "override.bin", b"override"),),
    )
    _write_provider_files(
        sibling_root,
        (("rfdetr", "sibling.bin", b"sibling"),),
    )
    _configure_seed(monkeypatch, current_root, sibling_roots=[sibling_root])
    monkeypatch.setenv("SOLSTONE_PROVIDER_CACHE_SEED_SOURCE", str(override_root))

    result = provider_cache_seed.seed_provider_cache()

    assert result.seeded is True
    assert result.source == override_cache
    current_cache = current_root / "journal" / "cache" / "providers"
    _assert_hardlink(
        override_cache / "rerank" / "override.bin",
        current_cache / "rerank" / "override.bin",
    )
    assert not (current_cache / "rfdetr" / "sibling.bin").exists()


def test_seed_provider_cache_never_raises_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    current_root = tmp_path / "current"
    sibling_root = tmp_path / "sibling"
    _write_provider_files(
        sibling_root,
        (("rerank", "rev/onnx/model.onnx", b"rerank-model"),),
    )
    _configure_seed(monkeypatch, current_root, sibling_roots=[sibling_root])
    monkeypatch.setattr(
        provider_cache_seed,
        "_hardlink_tree",
        lambda _src, _dst: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = provider_cache_seed.seed_provider_cache()

    assert result.seeded is False
    assert result.reason == "error"
    assert "unexpected error: boom" in capsys.readouterr().out


def test_install_models_seeds_only_default_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls: list[str] = []
    paths = {
        "binary_cpu": tmp_path / "cpu",
        "binary_vulkan": tmp_path / "vulkan",
        "model": tmp_path / "model",
    }
    monkeypatch.delenv(install_models.JOURNAL_VARIANT_ENV, raising=False)
    monkeypatch.setattr(install_models, "_platform_info", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(install_models, "_detect_linux_variant", lambda: "cpu")
    monkeypatch.setattr(install_models, "_verify_bundled_assets", lambda: None)
    monkeypatch.setattr(
        install_models, "_install_rerank_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_ced_assets", lambda *, check, force: 0
    )
    monkeypatch.setattr(
        install_models, "_install_rfdetr_model", lambda *, check, force: 0
    )
    monkeypatch.setattr(install_models, "_check_linux_cpp_ready", lambda: paths)
    monkeypatch.setattr(
        install_models, "seed_provider_cache", lambda: calls.append("seed")
    )

    monkeypatch.setattr(sys, "argv", ["sol install-models"])
    assert install_models.main() == 0
    assert calls == ["seed"]

    calls.clear()
    monkeypatch.setattr(sys, "argv", ["sol install-models", "--check"])
    assert install_models.main() == 0
    assert calls == []
