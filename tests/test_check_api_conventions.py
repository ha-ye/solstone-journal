# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-test for scripts/check_api_conventions.py.

Drives the conventions check against throwaway good/bad fixture trees and
asserts the detector's classification and the script's pass/fail exit codes.
The bad fixture deliberately includes a violation only the return-style
classifier catches — a JSON handler (returns ``jsonify(...)``) whose decorator
carries no ``/api/`` segment, mirroring the real ``apps/observer`` bare
``jsonify([...])`` shape — so this proves the detector handles the repo's real
route topology rather than a URL/substring shortcut.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_api_conventions.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_api_conventions", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cac = _load_checker()


# A clean route module: collections through respond_collection, creates through
# created, errors through error_response, pages through render_template/redirect.
GOOD_ROUTES = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from flask import Blueprint, render_template, redirect
from solstone.convey.utils import respond_collection, created, error_response
from solstone.convey.reasons import INVALID_DAY

good_bp = Blueprint("app:goodapp", __name__, url_prefix="/app/goodapp")


@good_bp.route("/api/items")
def list_items():
    return respond_collection([{"id": 1}], total=1)


@good_bp.route("/api/items", methods=["POST"])
def create_item():
    return created({"id": 2}, location="/app/goodapp/api/items/2")


@good_bp.route("/api/items/<item_id>")
def get_item(item_id):
    if not item_id:
        return error_response(INVALID_DAY, detail="bad id")
    return respond_collection([{"id": item_id}])


@good_bp.route("/<day>")
def page(day):
    if not day:
        return "", 404
    return render_template("app.html")


@good_bp.route("/old")
def old():
    return redirect("/app/goodapp/")
"""

# A module with no Blueprint registration. Discovery must skip it even though it
# contains a violation-shaped expression, proving discovery is registration-led.
GOOD_HELPERS = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from flask import jsonify


def build_payload():
    return jsonify([])
"""

# A route module exercising every forbidden escape hatch.
BAD_ROUTES = """\
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from flask import Blueprint, jsonify, render_template, abort

bad_bp = Blueprint("app:badapp", __name__, url_prefix="/app/badapp")


@bad_bp.route("/segments/<day>")
def list_segments(day):
    # JSON handler, NO /api/ in the decorator: only the return-style classifier
    # catches this bare top-level array (mirrors apps/observer.ingest_segments).
    result = []
    result.append({"key": day})
    return jsonify(result)


@bad_bp.route("/empty")
def list_empty():
    return jsonify([])


@bad_bp.route("/<day>")
def page(day):
    # Page route: render_template + bare "", 404 -> must NOT be flagged.
    if not day:
        return "", 404
    return render_template("app.html")


@bad_bp.route("/api/thing/<thing_id>")
def get_thing(thing_id):
    if not thing_id:
        abort(404)
    if thing_id == "x":
        return "", 400
    return jsonify({"error": "nope"}), 500
"""


def _write_tree(root: Path, app_name: str, files: dict[str, str]) -> None:
    app_dir = root / "solstone" / "apps" / app_name
    app_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (app_dir / name).write_text(content, encoding="utf-8")


@pytest.fixture
def good_root(tmp_path) -> Path:
    root = tmp_path / "good"
    _write_tree(root, "goodapp", {"routes.py": GOOD_ROUTES, "helpers.py": GOOD_HELPERS})
    return root


@pytest.fixture
def bad_root(tmp_path) -> Path:
    root = tmp_path / "bad"
    _write_tree(root, "badapp", {"routes.py": BAD_ROUTES})
    return root


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
    )


def test_good_fixture_exits_zero(good_root):
    result = _run(good_root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "api-conventions: pass" in result.stdout


def test_bad_fixture_exits_one(bad_root):
    result = _run(bad_root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "NEW violations" in result.stderr


def test_discovery_is_blueprint_led(good_root):
    # helpers.py registers no Blueprint -> it is not scanned.
    modules = {p.as_posix() for p in cac.discover_modules(good_root)}
    assert modules == {"solstone/apps/goodapp/routes.py"}


def test_classifier_flags_non_api_json_array(bad_root):
    findings = cac.scan_file(bad_root / "solstone/apps/badapp/routes.py")
    kinds_by_func: dict[str, set[str]] = {}
    for _lineno, kind, func in findings:
        kinds_by_func.setdefault(func, set()).add(kind)

    # The no-/api/ JSON handlers are flagged for their bare arrays.
    assert "bare-array" in kinds_by_func["list_segments"]
    assert "bare-array" in kinds_by_func["list_empty"]
    # The page route's bare "", 404 is NOT flagged.
    assert "page" not in kinds_by_func
    # The JSON RPC handler's every escape hatch is flagged.
    assert kinds_by_func["get_thing"] == {"abort", "bare-return", "inline-error"}


def test_ratchet_by_file_kind_count(bad_root):
    # No allowlist -> every violation is new.
    new, tracked = cac.evaluate(bad_root, {})
    assert new
    assert tracked == []

    # Allowlist the exact counts -> green, all tracked.
    counts = cac.count_violations(bad_root)
    new_exact, tracked_exact = cac.evaluate(bad_root, counts)
    assert new_exact == []
    assert tracked_exact

    # Lower a single (file, kind) count below its actual occurrences -> fails.
    key = next(iter(counts))
    ratcheted = dict(counts)
    ratcheted[key] = counts[key] - 1
    new_over, _ = cac.evaluate(bad_root, ratcheted)
    assert new_over


def test_repo_tree_is_green():
    # The committed allowlist keeps the real tree green.
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
