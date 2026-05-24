# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from solstone.think import utils as think_utils
from solstone.think.link import list_cli

OLD = "2026-01-01T00:00:00Z"
NEW = "2026-05-01T00:00:00Z"
SAME = "2026-03-01T00:00:00Z"
_MISSING = object()


def _args(**overrides: bool) -> argparse.Namespace:
    values = {"observers": False, "json": False}
    values.update(overrides)
    return argparse.Namespace(**values)


def _set_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    think_utils._journal_path_cache = None
    return journal


def _write_bundle(bundle_dir: Path, **fields: Any) -> dict[str, Any]:
    peer: dict[str, Any] = {
        "label": bundle_dir.name,
        "instance_id": bundle_dir.name,
        "home_label": "solstone",
        "paired_at": OLD,
        "fingerprint": "sha256:abcdef123456",
    }
    for key, value in fields.items():
        if value is _MISSING:
            peer.pop(key, None)
        else:
            peer[key] = value
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "peer.json").write_text(
        json.dumps(peer, sort_keys=True),
        encoding="utf-8",
    )
    return peer


def test_no_peers_directory_prints_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_journal(tmp_path, monkeypatch)

    assert list_cli.main(_args()) == 0

    out = capsys.readouterr()
    assert out.out == "No peers paired yet.\n"
    assert out.err == ""


def test_empty_peers_directory_prints_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    (journal / "peers").mkdir(parents=True)

    assert list_cli.main(_args()) == 0

    out = capsys.readouterr()
    assert out.out == "No peers paired yet.\n"
    assert out.err == ""


def test_one_peer_human_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    _write_bundle(
        journal / "peers" / "inst-1",
        label="alpha",
        instance_id="inst-1",
        home_label="home-a",
        fingerprint="sha256:0123456789abcdef",
    )

    assert list_cli.main(_args()) == 0

    out = capsys.readouterr()
    assert out.err == ""
    assert "Peers:\n" in out.out
    assert "  alpha (inst-1)\n" in out.out
    assert "home: home-a" in out.out
    assert "fingerprint: sha256:0123456789abcdef" in out.out


def test_multiple_peers_output_is_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    _write_bundle(journal / "peers" / "inst-b", label="beta", instance_id="inst-b")
    _write_bundle(journal / "peers" / "inst-a", label="alpha", instance_id="inst-a")

    assert list_cli.main(_args()) == 0
    out1 = capsys.readouterr()
    assert list_cli.main(_args()) == 0
    out2 = capsys.readouterr()

    assert out1.out == out2.out
    assert out1.err == out2.err


def test_sort_newest_first_then_basename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    _write_bundle(
        journal / "peers" / "z-old",
        label="old",
        instance_id="z-old",
        paired_at=OLD,
    )
    _write_bundle(
        journal / "peers" / "b-same",
        label="same-b",
        instance_id="b-same",
        paired_at=SAME,
    )
    _write_bundle(
        journal / "peers" / "a-same",
        label="same-a",
        instance_id="a-same",
        paired_at=SAME,
    )
    _write_bundle(
        journal / "peers" / "m-new",
        label="new",
        instance_id="m-new",
        paired_at=NEW,
    )

    assert list_cli.main(_args()) == 0

    out = capsys.readouterr().out
    assert out.index("new (m-new)") < out.index("same-a (a-same)")
    assert out.index("same-a (a-same)") < out.index("same-b (b-same)")
    assert out.index("same-b (b-same)") < out.index("old (z-old)")


def test_missing_paired_at_renders_never_and_sorts_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    _write_bundle(
        journal / "peers" / "inst-valid",
        label="valid",
        instance_id="inst-valid",
        paired_at=OLD,
    )
    _write_bundle(
        journal / "peers" / "inst-missing",
        label="missing",
        instance_id="inst-missing",
        paired_at=_MISSING,
    )

    assert list_cli.main(_args()) == 0

    out = capsys.readouterr().out
    assert "missing (inst-missing)\n    paired never" in out
    assert out.index("valid (inst-valid)") < out.index("missing (inst-missing)")


def test_empty_home_label_renders_dash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    bundle = journal / "peers" / "inst-1"
    _write_bundle(
        bundle,
        label="alpha",
        instance_id="inst-1",
        home_label="",
    )

    assert list_cli.main(_args()) == 0
    human = capsys.readouterr()
    assert "home: —" in human.out

    assert list_cli.main(_args(json=True)) == 0
    json_out = capsys.readouterr()
    records = json.loads(json_out.out)
    assert records == [
        {
            "kind": "peer",
            "label": "alpha",
            "instance_id": "inst-1",
            "home_label": None,
            "paired_at": OLD,
            "fingerprint": "sha256:abcdef123456",
            "bundle_dir": str(bundle.resolve()),
        }
    ]


def test_malformed_peer_json_warns_and_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    bad = journal / "peers" / "bad"
    bad.mkdir(parents=True)
    (bad / "peer.json").write_text("{", encoding="utf-8")
    _write_bundle(journal / "peers" / "good", label="good", instance_id="good")

    assert list_cli.main(_args()) == 0

    out = capsys.readouterr()
    assert out.err == (
        f"warning: skipping peer bundle {bad.resolve()}: peer.json is not valid JSON\n"
    )
    assert "good (good)" in out.out
    assert "bad" not in out.out


def test_all_malformed_peers_returns_empty_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    bad_json = journal / "peers" / "bad-json"
    bad_json.mkdir(parents=True)
    (bad_json / "peer.json").write_text("{", encoding="utf-8")
    missing_label = journal / "peers" / "missing-label"
    missing_instance = journal / "peers" / "missing-instance"
    _write_bundle(missing_label, label=_MISSING)
    _write_bundle(missing_instance, instance_id=_MISSING)

    assert list_cli.main(_args()) == 0

    out = capsys.readouterr()
    assert out.out == "No peers paired yet.\n"
    assert (
        f"warning: skipping peer bundle {bad_json.resolve()}: "
        "peer.json is not valid JSON\n"
    ) in out.err
    assert (
        f"warning: skipping peer bundle {missing_label.resolve()}: "
        "peer.json missing required field 'label'\n"
    ) in out.err
    assert (
        f"warning: skipping peer bundle {missing_instance.resolve()}: "
        "peer.json missing required field 'instance_id'\n"
    ) in out.err


def test_missing_label_warns_and_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    bad = journal / "peers" / "bad"
    _write_bundle(bad, label=_MISSING)
    _write_bundle(journal / "peers" / "good", label="good", instance_id="good")

    assert list_cli.main(_args()) == 0

    out = capsys.readouterr()
    assert out.err == (
        f"warning: skipping peer bundle {bad.resolve()}: "
        "peer.json missing required field 'label'\n"
    )
    assert "good (good)" in out.out
    assert "bad" not in out.out


def test_missing_instance_id_warns_and_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    bad = journal / "peers" / "bad"
    _write_bundle(bad, instance_id=_MISSING)
    _write_bundle(journal / "peers" / "good", label="good", instance_id="good")

    assert list_cli.main(_args()) == 0

    out = capsys.readouterr()
    assert out.err == (
        f"warning: skipping peer bundle {bad.resolve()}: "
        "peer.json missing required field 'instance_id'\n"
    )
    assert "good (good)" in out.out
    assert "bad" not in out.out


def test_missing_peer_json_silent_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    (journal / "peers" / "inst-empty").mkdir(parents=True)

    assert list_cli.main(_args()) == 0

    out = capsys.readouterr()
    assert out.out == "No peers paired yet.\n"
    assert out.err == ""


def test_peers_root_file_warns_and_treats_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    peers = journal / "peers"
    peers.parent.mkdir(parents=True)
    peers.write_text("not a directory", encoding="utf-8")

    assert list_cli.main(_args()) == 0

    out = capsys.readouterr()
    assert out.out == "No peers paired yet.\n"
    assert out.err == (
        f"warning: peer bundle root {peers.resolve()} exists but is not a "
        "directory; treating as empty\n"
    )


def test_json_outputs_flat_single_line_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    bundle = journal / "peers" / "inst-1"
    _write_bundle(
        bundle,
        label="alpha",
        instance_id="inst-1",
        home_label="home-a",
        paired_at=NEW,
        fingerprint="sha256:0123456789abcdef",
    )
    expected = [
        {
            "kind": "peer",
            "label": "alpha",
            "instance_id": "inst-1",
            "home_label": "home-a",
            "paired_at": NEW,
            "fingerprint": "sha256:0123456789abcdef",
            "bundle_dir": str(bundle.resolve()),
        }
    ]

    assert list_cli.main(_args(json=True)) == 0

    out = capsys.readouterr()
    assert out.out == json.dumps(expected) + "\n"
    assert out.out.count("\n") == 1
    assert out.err == ""


def test_observers_flag_walks_peer_and_xdg_observer_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = _set_journal(tmp_path, monkeypatch)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    peer_bundle = journal / "peers" / "inst-1"
    observer_bundle = xdg / "solstone-observer" / "spl" / "observer-a"
    _write_bundle(
        peer_bundle,
        label="peer-a",
        instance_id="inst-1",
        paired_at=SAME,
    )
    _write_bundle(
        observer_bundle,
        label="observer-a",
        instance_id="obs-1",
        paired_at=SAME,
    )

    assert list_cli.main(_args(observers=True)) == 0
    human = capsys.readouterr()
    assert "Peers:\n" in human.out
    assert "  peer-a (inst-1)\n" in human.out
    assert "\nObservers:\n" in human.out
    assert "  observer-a (obs-1)\n" in human.out

    assert list_cli.main(_args(observers=True, json=True)) == 0
    json_out = capsys.readouterr()
    assert json_out.out.count("\n") == 1
    records = json.loads(json_out.out)
    assert [record["kind"] for record in records] == ["peer", "observer"]
    assert records[0]["bundle_dir"] == str(peer_bundle.resolve())
    assert records[1]["bundle_dir"] == str(observer_bundle.resolve())
