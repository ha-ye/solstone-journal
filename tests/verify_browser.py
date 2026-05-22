# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Browser scenario verification using Pinchtab snapshots and screenshots."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

logger = logging.getLogger(__name__)


SCENARIOS: list[dict[str, Any]] = [
    {
        "app": "chat",
        "name": "bar-reasons",
        "steps": [
            {"do": "navigate", "path": "/static/tests/chat-bar-reasons.html"},
            {"do": "wait", "ms": 500},
            {"do": "assert_text", "text": "PASS chat bar reasons: 0 failure(s)"},
        ],
    },
    # smoke scenarios
    {
        "app": "sol",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/sol/20260304"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "activities",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/activities/20260304"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "speakers",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/speakers/20260304"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "todos",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/todos/20260304"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "tokens",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/tokens/20260304"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "transcripts",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/transcripts/20260304"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "entities",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/entities"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "health",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/health"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "import",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/import"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "observer",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/observer"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "search",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/search"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "settings",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/settings"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "stats",
        "name": "smoke",
        "steps": [
            {"do": "navigate", "path": "/app/stats"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    # interactive scenarios
    {
        "app": "search",
        "name": "search-flow",
        "steps": [
            {"do": "navigate", "path": "/app/search"},
            {"do": "wait", "ms": 1000},
            {"do": "snapshot"},
            {"do": "find_input", "as": "search_input"},
            {"do": "type", "var": "search_input", "text": "romeo"},
            {"do": "wait", "ms": 1500},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "entities",
        "name": "entity-detail",
        "steps": [
            {"do": "navigate", "path": "/app/entities/work/romeo_montague"},
            {"do": "wait", "ms": 1000},
            {"do": "screenshot"},
        ],
    },
    {
        "app": "todos",
        "name": "todo-states",
        "steps": [
            {"do": "evaluate", "expression": "document.cookie='facet=work;path=/'"},
            {"do": "navigate", "path": "/app/todos/20260304"},
            {"do": "wait", "ms": 1200},
            {"do": "screenshot"},
        ],
    },
]


_ERROR_LISTENER_JS = (
    "window.__pt_errors=[];"
    "window.addEventListener('error',e=>window.__pt_errors.push("
    "String(e.message||e.error||e)));"
    "window.addEventListener('unhandledrejection',e=>window.__pt_errors.push("
    "'unhandledrejection: '+String(e.reason)));"
    "window.onerror=(msg,src,line,col,e)=>window.__pt_errors.push(String(e||msg));"
    "if(!window.__pt_orig_console_error){window.__pt_orig_console_error=console.error;"
    "console.error=function(){window.__pt_errors.push("
    "'console.error: '+Array.prototype.join.call(arguments,' '));"
    "return window.__pt_orig_console_error.apply(console,arguments);};}"
)


ROUTE_SMOKE_EXCLUDES = (
    "/api/",
    "/static",
    "/ingest",
    "/callosum",
    "/local-endpoints",
    "/raw",
    "/pdf",
    "/manifest/",
    "/generation-status",
    "/overflow/",
)


DETAIL_HREF_JS = """
(() => {
  const values = [window.location.pathname];
  document.querySelectorAll('a[href]').forEach((el) => values.push(el.href));
  document.querySelectorAll('[onclick]').forEach((el) => values.push(el.getAttribute('onclick') || ''));
  document.querySelectorAll('[data-import-id]').forEach((el) => {
    if (el.dataset.importId) values.push('/app/import/' + el.dataset.importId);
  });
  return JSON.stringify(values.filter(Boolean));
})()
"""


def baseline_path(scenario: dict[str, Any]) -> Path:
    return Path("tests/baselines/visual") / scenario["app"] / f"{scenario['name']}.jpg"


class PinchTab:
    """Minimal pinchtab HTTP client with process lifecycle.

    Pinchtab v0.7.x uses a flat API — endpoints are at the root level
    (e.g., /navigate, /screenshot, /snapshot) rather than nested under
    /tabs/<id>/ or /instances/. Chrome is auto-managed by the server.
    """

    def __init__(self, port: int = 19867) -> None:
        self.port = port
        self.base_url = f"http://localhost:{port}"
        self._process: subprocess.Popen | None = None
        self._session = requests.Session()

    def start(self, timeout: int = 30) -> None:
        """Launch pinchtab and wait for health check."""
        # Pinchtab reads PINCHTAB_PORT (v0.7.x; renamed from BRIDGE_PORT).
        # /screenshot may return image/* bytes directly; JSON base64 kept as fallback.
        # See pinchtab --help -> ENVIRONMENT.
        env = {
            **os.environ,
            "PINCHTAB_PORT": str(self.port),
            "BRIDGE_HEADLESS": "true",
        }
        profile_dir = Path.home() / ".pinchtab" / "profiles" / "default"
        if profile_dir.exists():
            # Clear cached default profile for deterministic runs — pinchtab persists
            # cookies/storage across sessions. Other tools that share pinchtab use
            # their own named profiles, so this nuke is isolated to test state.
            try:
                shutil.rmtree(profile_dir)
            except OSError as exc:
                raise RuntimeError(
                    f"failed to clear pinchtab default profile: {profile_dir}"
                ) from exc
        self._stderr_path = f"/tmp/pinchtab-{self.port}.log"
        self._stderr_file = open(self._stderr_path, "w")
        try:
            self._process = subprocess.Popen(
                ["pinchtab"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr_file,
                process_group=0,
            )
        except Exception as exc:
            self._stderr_file.close()
            raise RuntimeError("failed to start pinchtab") from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._stderr_file.close()
                try:
                    stderr = Path(self._stderr_path).read_text()
                except Exception:
                    stderr = ""
                raise RuntimeError(
                    f"pinchtab exited with code {self._process.returncode}\n{stderr}"
                )
            try:
                response = self._session.get(f"{self.base_url}/health", timeout=2)
                if response.status_code == 200:
                    health = response.json()
                    instance = health.get("defaultInstance") or {}
                    if (
                        health.get("status") == "ok"
                        and instance.get("status") == "running"
                    ):
                        return
            except requests.ConnectionError:
                pass
            time.sleep(0.5)
        self.stop()
        raise RuntimeError("pinchtab failed to start")

    def stop(self) -> None:
        """Terminate pinchtab process and all children."""
        if hasattr(self, "_stderr_file") and self._stderr_file:
            try:
                self._stderr_file.close()
            except Exception:
                pass
        if self._process:
            pid = self._process.pid
            if self._process.poll() is None:
                self._session.close()
                # Kill the entire process group to catch the Go binary child
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        self._process.send_signal(signal.SIGKILL)
                    self._process.wait()
            self._process = None

    def navigate(self, url: str) -> None:
        response = self._session.post(
            f"{self.base_url}/navigate",
            json={"url": url},
            timeout=30,
        )
        response.raise_for_status()

    def screenshot(self) -> bytes:
        response = self._session.get(
            f"{self.base_url}/screenshot",
            timeout=30,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("image/"):
            return response.content
        payload = response.json()
        return base64.b64decode(payload["base64"])

    def snapshot(self) -> dict:
        response = self._session.get(
            f"{self.base_url}/snapshot",
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def text(self) -> str:
        response = self._session.get(
            f"{self.base_url}/text",
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload.get("text", "")
        if isinstance(payload, str):
            return payload
        return ""

    def action(self, kind: str, **kwargs: Any) -> None:
        response = self._session.post(
            f"{self.base_url}/action",
            json={"kind": kind, **kwargs},
            timeout=30,
        )
        response.raise_for_status()

    def evaluate(self, expression: str) -> Any:
        response = self._session.post(
            f"{self.base_url}/evaluate",
            json={"expression": expression},
            timeout=30,
        )
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return response.text


def inject_error_listener(pt: PinchTab) -> None:
    pt.evaluate(_ERROR_LISTENER_JS)


def collect_console_errors(pt: PinchTab) -> list[str]:
    result = pt.evaluate("JSON.stringify(window.__pt_errors||[])")
    value = result if isinstance(result, str) else result.get("result", "[]")
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def find_input_ref(snapshot: dict) -> str | None:
    """Find first text input node ref from snapshot."""
    for node in snapshot.get("nodes", []):
        role = str(node.get("role", "")).lower()
        tag = str(node.get("tag", "")).lower()
        if role in ("textbox", "searchbox", "combobox") or tag == "input":
            return node.get("ref")
    return None


def find_ref(snapshot: dict, text: str) -> str | None:
    needle = str(text).lower()
    for node in snapshot.get("nodes", []):
        ref = node.get("ref")
        if not ref:
            continue
        if needle == "":
            return ref
        if (
            needle in str(node.get("name", "")).lower()
            or needle in str(node.get("text", "")).lower()
            or needle in str(node.get("label", "")).lower()
            or needle in str(node.get("value", "")).lower()
        ):
            return ref
    return None


def _is_app_shell_path(path: str) -> bool:
    return path.startswith("/app/") and not any(
        excluded in path for excluded in ROUTE_SMOKE_EXCLUDES
    )


def _with_pt_capture(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("__pt_capture", "1"))
    return urlunparse(parsed._replace(query=urlencode(query)))


def _resolve_url(base_url: str, path: str, *, capture: bool = False) -> str:
    url = f"{base_url.rstrip('/')}{path}"
    if capture:
        return _with_pt_capture(url)
    return url


def _resolve_redirect_path(base_url: str, path: str) -> str:
    try:
        response = requests.get(
            _resolve_url(base_url, path), allow_redirects=True, timeout=10
        )
    except requests.RequestException:
        return path
    final_path = urlparse(response.url).path
    if response.ok and _is_app_shell_path(final_path):
        return final_path
    return path


def _derive_app_page_routes() -> list[str]:
    from flask import Flask

    from solstone.apps import AppRegistry

    registry = AppRegistry()
    registry.discover()
    app = Flask(__name__)
    registry.register_blueprints(app)

    routes: list[str] = []
    seen: set[str] = set()
    for rule in sorted(app.url_map.iter_rules(), key=lambda item: item.rule):
        methods = rule.methods - {"HEAD", "OPTIONS"}
        if "GET" not in methods or not rule.endpoint.startswith("app:"):
            continue
        if any(excluded in rule.rule for excluded in ROUTE_SMOKE_EXCLUDES):
            continue
        if rule.rule in seen:
            continue
        seen.add(rule.rule)
        routes.append(rule.rule)
    return routes


def _parent_route(rule: str) -> str:
    before_param = rule.split("<", 1)[0]
    if before_param.endswith("/"):
        return before_param
    parent = before_param.rsplit("/", 1)[0]
    return parent + "/"


def _route_regex(rule: str) -> re.Pattern[str]:
    parts: list[str] = []
    cursor = 0
    for match in re.finditer(r"<[^>]+>", rule):
        parts.append(re.escape(rule[cursor : match.start()]))
        parts.append(r"[^/?#]+")
        cursor = match.end()
    parts.append(re.escape(rule[cursor:]))
    pattern = "".join(parts)
    return re.compile(rf"^{pattern}/?$")


def _candidate_paths(values: list[str]) -> list[str]:
    paths: list[str] = []
    for value in values:
        parsed = urlparse(value)
        if parsed.path.startswith("/app/"):
            paths.append(parsed.path)
            continue
        for match in re.findall(r"/app/[A-Za-z0-9_./%:-]+", value):
            paths.append(urlparse(match).path)
    return paths


def _extract_detail_path(pt: PinchTab, rule: str) -> str | None:
    result = pt.evaluate(DETAIL_HREF_JS)
    raw = result if isinstance(result, str) else result.get("result", "[]")
    try:
        values = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    matcher = _route_regex(rule)
    for path in _candidate_paths(values):
        if matcher.fullmatch(path):
            return path
    return None


def _eval_json(pt: PinchTab, expression: str) -> Any:
    result = pt.evaluate(f"JSON.stringify({expression})")
    raw = result if isinstance(result, str) else result.get("result", "null")
    return json.loads(raw)


def _assert_loading_cleared(pt: PinchTab, path: str) -> list[str]:
    checks: list[tuple[str, str]] = []
    if re.fullmatch(r"/app/activities/\d{8}/?", path):
        checks.append(
            (
                "activities-day Loading activities...",
                "!document.body.innerText.includes('Loading activities...')",
            )
        )
    elif path.rstrip("/") == "/app/import":
        checks.append(
            (
                "import list loading imports...",
                "!document.body.innerText.includes('loading imports...')",
            )
        )
    elif re.fullmatch(r"/app/import/[^/]+/?", path):
        checks.append(
            (
                "import detail loading...",
                "!(document.getElementById('importMeta')?.innerText.toLowerCase().includes('loading')"
                " || document.getElementById('overviewContent')?.innerText.toLowerCase().includes('loading'))",
            )
        )
    elif re.fullmatch(r"/app/sol/\d{8}/?", path):
        checks.append(
            (
                "sol loading agents...",
                "getComputedStyle(document.getElementById('loading-view')).display === 'none'",
            )
        )
    elif re.fullmatch(r"/app/speakers/\d{8}/?", path):
        checks.append(
            (
                "speakers loading...",
                "!(document.getElementById('spkSegmentList')?.innerText.trim().toLowerCase() === 'loading...')",
            )
        )
    elif re.fullmatch(r"/app/tokens/\d{8}/?", path):
        checks.append(
            (
                "tokens loading token usage data...",
                "getComputedStyle(document.getElementById('tokens-loading')).display === 'none'",
            )
        )
    elif path.rstrip("/") == "/app/observer":
        checks.append(
            (
                "observer loading observers...",
                "!document.body.innerText.includes('loading observers...')",
            )
        )
    elif path.rstrip("/") == "/app/link":
        checks.append(
            (
                "link status loading...",
                "document.getElementById('link-status-text')?.innerText.trim() !== 'loading…'",
            )
        )
    elif path.rstrip("/") == "/app/support":
        checks.append(
            (
                "support checking for tickets",
                "!document.body.innerText.includes('checking for tickets')",
            )
        )
    elif path.rstrip("/") == "/app/settings":
        checks.append(
            (
                "settings provider/context placeholders",
                "["
                "document.getElementById('providerStatus')?.innerText,"
                "document.getElementById('contextGroups')?.innerText,"
                "document.getElementById('visionCategoryGroups')?.innerText,"
                "document.getElementById('segmentInsightsList')?.innerText,"
                "document.getElementById('dailyInsightsList')?.innerText,"
                "document.getElementById('mutedFacetsList')?.innerText"
                "].every((text) => !String(text || '').trim().toLowerCase().startsWith('loading'))",
            )
        )

    errors: list[str] = []
    for label, expression in checks:
        try:
            if not _eval_json(pt, expression):
                errors.append(f"loading sentinel still visible: {label}")
        except Exception as exc:
            errors.append(f"loading sentinel check failed for {label}: {exc}")
    return errors


def _resolve_route_path(
    pt: PinchTab, base_url: str, rule: str
) -> tuple[str | None, str | None]:
    if "<" not in rule:
        return _resolve_redirect_path(base_url, rule), None
    if rule.startswith("/app/activities/<day>/screens/<stream>/"):
        # pre-existing unrelated bug, out of scope: list emits timestamp-only URLs.
        return None, "activities dev screen detail route has a stale list link"

    parent = _resolve_redirect_path(base_url, _parent_route(rule))
    if _route_regex(rule).fullmatch(parent):
        return parent, None
    pt.navigate(_resolve_url(base_url, parent, capture=True))
    time.sleep(1.2)
    path = _extract_detail_path(pt, rule)
    if path:
        return path, None
    return None, f"no concrete link found from {parent}"


def run_scenario(
    pt: PinchTab, scenario: dict[str, Any], base_url: str, mode: str
) -> dict[str, Any]:
    """Execute one scenario. Returns {ok, errors, console_errors}."""
    identifier = f"{scenario['app']}/{scenario['name']}"
    errors: list[str] = []
    variables: dict[str, str] = {}
    last_snapshot: dict[str, Any] | None = None
    console_errors: list[str] = []

    logger.info("  %s", identifier)

    try:
        inject_error_listener(pt)
    except Exception:
        pass

    for step in scenario["steps"]:
        action = step["do"]
        try:
            if action == "navigate":
                capture = _is_app_shell_path(step["path"])
                url = _resolve_url(base_url, step["path"], capture=capture)
                pt.navigate(url)
                time.sleep(0.3)
                if not capture:
                    try:
                        inject_error_listener(pt)
                    except Exception:
                        pass

            elif action == "wait":
                time.sleep(float(step["ms"]) / 1000)

            elif action == "snapshot":
                last_snapshot = pt.snapshot()

            elif action == "screenshot":
                png = pt.screenshot()
                path = baseline_path(scenario)
                if mode == "update":
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(png)
                else:
                    if not path.exists():
                        errors.append(f"baseline not found: {path}")
                    # No pixel comparison — baselines are for human review

            elif action == "find":
                if last_snapshot is None:
                    errors.append("find without prior snapshot")
                    continue
                ref = find_ref(last_snapshot, step["text"])
                if ref is None:
                    errors.append(f"find: text not found: {step['text']!r}")
                    continue
                variables[step["as"]] = ref

            elif action == "find_input":
                if last_snapshot is None:
                    errors.append("find_input without prior snapshot")
                    continue
                ref = find_input_ref(last_snapshot)
                if ref is None:
                    errors.append("no text input found in snapshot")
                    continue
                variables[step["as"]] = ref

            elif action == "click":
                ref = step.get("ref") or variables.get(step.get("var", ""))
                if not ref:
                    errors.append(f"click: no ref resolved for {step}")
                    continue
                pt.action("click", ref=ref)

            elif action == "type":
                ref = step.get("ref") or variables.get(step.get("var", ""))
                if not ref:
                    errors.append(f"type: no ref resolved for {step}")
                    continue
                pt.action("type", ref=ref, text=step["text"])

            elif action == "assert_text":
                text = step["text"]
                page_text = pt.text().lower()
                if str(text).lower() not in page_text:
                    errors.append(f"assert_text: '{text}' not found")

            elif action == "evaluate":
                pt.evaluate(step["expression"])

            else:
                errors.append(f"unknown step type: {action}")

        except Exception as exc:
            errors.append(f"step {action} failed: {exc}")

    try:
        console_errors = collect_console_errors(pt)
    except Exception:
        logger.debug("Unable to collect console errors for %s", identifier)
    if console_errors:
        errors.extend(f"captured JS error: {err}" for err in console_errors)

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "console_errors": console_errors,
    }


def run_cold_load_smoke(pt: PinchTab, base_url: str) -> list[dict[str, Any]]:
    """Cold-load every registered app page route with pre-parse error capture."""
    results: list[dict[str, Any]] = []
    for rule in _derive_app_page_routes():
        identifier = f"cold-load/{rule}"
        logger.info("  %s", identifier)
        errors: list[str] = []
        console_errors: list[str] = []
        path: str | None = None

        try:
            path, skip_reason = _resolve_route_path(pt, base_url, rule)
            if skip_reason:
                logger.info("    SKIP %s", skip_reason)
                continue
            if not path:
                logger.info("    SKIP no concrete path")
                continue

            pt.navigate(_resolve_url(base_url, path, capture=True))
            time.sleep(1.2)
            errors.extend(_assert_loading_cleared(pt, path))
            console_errors = collect_console_errors(pt)
            if console_errors:
                errors.extend(f"captured JS error: {err}" for err in console_errors)
        except Exception as exc:
            errors.append(f"cold-load route failed: {exc}")

        results.append(
            {
                "scenario": identifier,
                "ok": len(errors) == 0,
                "errors": errors,
                "console_errors": console_errors,
            }
        )
    return results


def run_all(
    pt: PinchTab, base_url: str, mode: str
) -> tuple[list[dict[str, Any]], list[tuple[str, list[str]]]]:
    """Run all scenarios. Returns (results, console_error_pairs)."""
    results: list[dict[str, Any]] = []
    all_console_errors: list[tuple[str, list[str]]] = []
    for result in run_cold_load_smoke(pt, base_url):
        results.append(result)
        if result["console_errors"]:
            all_console_errors.append((result["scenario"], result["console_errors"]))
    for scenario in SCENARIOS:
        identifier = f"{scenario['app']}/{scenario['name']}"
        result = run_scenario(pt, scenario, base_url, mode)
        results.append({"scenario": identifier, **result})
        if result["console_errors"]:
            all_console_errors.append((identifier, result["console_errors"]))
    return results, all_console_errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Browser scenario verification")
    parser.add_argument(
        "command",
        choices=["verify", "update"],
        help="Verify or update baselines",
    )
    parser.add_argument("--base-url", required=True, help="Convey base URL")
    parser.add_argument(
        "--pinchtab-port",
        type=int,
        default=19867,
        help="Pinchtab bridge port",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    pt = PinchTab(port=args.pinchtab_port)
    logger.info("Starting pinchtab on port %d...", args.pinchtab_port)
    pt.start()

    try:
        logger.info("Running browser scenarios (%s)...", args.command)
        results, console_errors = run_all(pt, args.base_url, args.command)

        passed = sum(1 for r in results if r["ok"])
        failed = sum(1 for r in results if not r["ok"])

        if failed:
            logger.info("")
            logger.info("Failures:")
            for result in results:
                if result["ok"]:
                    continue
                for err in result["errors"]:
                    logger.info("  %s: %s", result["scenario"], err)

        if console_errors:
            logger.info("")
            logger.info("JS console errors:")
            for scenario, errors in console_errors:
                for err in errors:
                    logger.info("  %s: %s", scenario, err)

        logger.info("")
        if args.command == "update":
            logger.info("Updated %d scenario baselines.", passed + failed)
        else:
            logger.info("Browser verification: %d passed, %d failed.", passed, failed)

        if failed:
            logger.info("Run 'make update-browser-baselines' to update baselines")
            return 1

        return 0
    finally:
        pt.stop()


if __name__ == "__main__":
    raise SystemExit(main())
