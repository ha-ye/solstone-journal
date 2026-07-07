# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared shell-state builder for Jinja and static shell hydration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING, Any

from flask import g, request

from solstone.convey.config import (
    apply_app_order,
    apply_facet_order,
    get_selected_facet,
    load_convey_config,
    reporting_enabled,
    set_selected_facet,
)
from solstone.convey.icons import APP_LUCIDE_MAP, lucide_svg, resolve_facet_icon_svg
from solstone.convey.provider_readiness import is_blocking_reason, present_for_reason
from solstone.think.talent_runs import AgentFailure, read_unresolved_agent_failures

if TYPE_CHECKING:
    from solstone.apps import AppRegistry

logger = logging.getLogger(__name__)


def _get_version() -> str:
    try:
        return _pkg_version("solstone")
    except PackageNotFoundError:
        return "dev"


def _get_facets_data() -> list[dict]:
    """Get active facets data for shell chrome."""
    from solstone.think.facets import get_facets

    all_facets = get_facets()
    facets_list = []

    for name, data in all_facets.items():
        if data.get("muted", False):
            continue

        facets_list.append(
            {
                "name": name,
                "title": data.get("title", name),
                "color": data.get("color", ""),
                "emoji": data.get("emoji", ""),
                "icon": data.get("icon", ""),
                "icon_svg": resolve_facet_icon_svg(
                    data.get("icon"), data.get("emoji", "")
                ),
            }
        )

    config = load_convey_config()
    return apply_facet_order(facets_list, config)


def _get_selected_facet() -> str | None:
    """Get selected facet from cookie, syncing with config."""
    cookie_facet = request.cookies.get("selectedFacet")
    config_facet = get_selected_facet()

    if cookie_facet == "":
        set_selected_facet(None)
        g.clear_facet_cookie = True
        return None

    facet = cookie_facet if cookie_facet is not None else config_facet

    if facet:
        active_names = {f["name"] for f in _get_facets_data()}
        if facet not in active_names:
            set_selected_facet(None)
            g.clear_facet_cookie = True
            return None

    if cookie_facet is not None and cookie_facet != config_facet:
        set_selected_facet(cookie_facet)

    return facet


@dataclass
class AttentionItem:
    """A system attention item for the chat bar and triage context."""

    placeholder_text: str
    context_lines: list[str]


def _resolve_attention(awareness_current: dict) -> AttentionItem | None:
    """Check attention sources P0-P3, return highest priority or None."""
    try:
        scan = read_unresolved_agent_failures()
        if scan.ok and scan.failures:
            latest_by_name: dict[str, AgentFailure] = {}
            for failure in scan.failures:
                current = latest_by_name.get(failure.name)
                if current is None or failure.ts > current.ts:
                    latest_by_name[failure.name] = failure

            readiness_blockers = []
            for name in sorted(latest_by_name):
                failure = latest_by_name[name]
                reason_code = failure.reason_code
                if not is_blocking_reason(reason_code or ""):
                    continue
                view = present_for_reason(
                    reason_code,
                    provider=failure.provider or "",
                    model=failure.model,
                    status="unhealthy",
                )
                priority = 0 if view.severity == "blocker" else 1
                readiness_blockers.append((priority, name, view))
            if readiness_blockers:
                _priority, name, view = sorted(readiness_blockers)[0]
                placeholder = view.summary
                if len(placeholder) > 90:
                    placeholder = "Provider setup needs attention — ask how to fix it"
                return AttentionItem(
                    placeholder_text=placeholder,
                    context_lines=[
                        (
                            f"System health: {name} is blocked by provider "
                            "readiness. Guide the owner to fix provider setup "
                            "before retrying."
                        ),
                        f"Readiness: {view.summary}.",
                        f"Operator detail: {view.operator_detail}.",
                    ],
                )

            count = len(scan.failures)
            names = sorted({failure.name for failure in scan.failures})
            names_display = ", ".join(names[:3])
            suffix = f" (+{len(names) - 3} more)" if len(names) > 3 else ""
            placeholder = (
                f"{count} agent error{'s' if count != 1 else ''} today"
                " — ask what happened"
            )
            context = [
                f"System health: {count} unresolved agent error(s) today: "
                f"{names_display}{suffix}. If user asks what needs attention, "
                "summarize which agents failed."
            ]
            return AttentionItem(
                placeholder_text=placeholder,
                context_lines=context,
            )
    except Exception:
        logger.warning("failed to resolve chat bar cortex attention", exc_info=True)

    imports = awareness_current.get("imports", {})
    last_completed = imports.get("last_completed")
    last_summary = imports.get("last_result_summary")
    if last_completed and last_summary:
        try:
            from datetime import datetime, timedelta

            completed_dt = datetime.fromisoformat(last_completed)
            if datetime.now() - completed_dt < timedelta(hours=1):
                placeholder = f"Import complete: {last_summary} — ask me about it"
                if len(placeholder) > 90:
                    placeholder = "New import complete — ask me what arrived"
                context = [
                    f"System health: import recently completed — {last_summary}. "
                    "If user asks what needs attention, mention the new import."
                ]
                return AttentionItem(
                    placeholder_text=placeholder,
                    context_lines=context,
                )
        except Exception:
            pass

    journal_state = awareness_current.get("journal", {})
    if journal_state.get("first_daily_ready"):
        try:
            from datetime import datetime
            from pathlib import Path

            from solstone.think.utils import get_journal

            journal = Path(get_journal())
            today = datetime.now().strftime("%Y%m%d")
            agents_dir = journal / today / "talents"
            if agents_dir.is_dir():
                outputs = sorted(p.stem for p in agents_dir.glob("*.md"))
                if outputs:
                    count = len(outputs)
                    placeholder = (
                        f"{count} analysis report{'s' if count != 1 else ''} ready"
                        " — ask about your day"
                    )
                    context = [
                        f"System health: {count} daily analysis report(s) "
                        f"available today: {', '.join(outputs)}. User can ask "
                        "about any of these topics."
                    ]
                    return AttentionItem(
                        placeholder_text=placeholder,
                        context_lines=context,
                    )
        except Exception:
            pass

    voiceprint = awareness_current.get("voiceprint", {})
    if voiceprint.get("status") == "candidate":
        cluster_size = voiceprint.get("cluster_size", 0)
        placeholder = "Voice pattern detected — confirm in Speakers"
        context = [
            f"System detected owner voice pattern from {cluster_size} voice samples. "
            "Direct user to the Speakers app (/app/speakers) to confirm their voiceprint."
        ]
        return AttentionItem(placeholder_text=placeholder, context_lines=context)

    return None


def _resolve_placeholder(awareness_current: dict, day_count: int) -> str:
    """Resolve fallback chat bar placeholder text based on journal state."""
    imports = awareness_current.get("imports", {})
    if not imports.get("has_imported") and day_count < 3:
        return "Bring in past conversations, calendar, or notes to give me context..."
    if awareness_current.get("journal", {}).get("first_daily_ready"):
        if day_count < 2:
            return "Your first daily analysis is ready — ask me what I found..."
        if day_count >= 7:
            return "Ask me about your day, search your journal, or explore insights..."
        return "Your daily analysis is ready — ask about today or anything in your journal..."
    return "observing — your first daily analysis will be ready soon..."


def _build_apps(registry: AppRegistry, config: dict[str, Any]) -> list[dict[str, Any]]:
    apps_dict: dict[str, dict[str, Any]] = {}
    starred_apps = config.get("apps", {}).get("starred", [])
    starred_set = set(starred_apps)

    for app_instance in registry.apps.values():
        name_lucide = APP_LUCIDE_MAP.get(app_instance.name)
        apps_dict[app_instance.name] = {
            "name": app_instance.name,
            "icon": app_instance.icon,
            "icon_svg": lucide_svg(name_lucide) if name_lucide else None,
            "label": app_instance.label,
            "starred": app_instance.name in starred_set,
            "facets_enabled": app_instance.facets_enabled(),
            "date_nav": app_instance.date_nav_enabled(),
            "app_bar": app_instance.app_bar,
            "allow_future_dates": app_instance.allow_future_dates,
            "workspace_url": f"/app/{app_instance.name}/workspace",
            "background_url": (
                f"/app/{app_instance.name}/background"
                if app_instance.get_background_template()
                else None
            ),
        }

    apps_dict = apply_app_order(apps_dict, config)

    if "sol" in apps_dict:
        try:
            from solstone.think.utils import get_config as _get_journal_config

            journal_config = _get_journal_config()
            agent_block = journal_config.get("agent", {})
            if agent_block.get("name_status") in ("chosen", "self-named"):
                agent_name = agent_block.get("name", "").strip()
                if agent_name:
                    apps_dict["sol"]["label"] = agent_name
        except Exception:
            pass

    return list(apps_dict.values())


def _build_chat_bar() -> dict[str, Any]:
    default_chat_bar = {
        "placeholder": "Send a message...",
        "attention": None,
        "sol_request": None,
    }
    chat_bar = dict(default_chat_bar)
    try:
        from solstone.convey.chat_stream import read_chat_events
        from solstone.convey.sol_initiated.state import (
            latest_unresolved_sol_chat_request,
        )
        from solstone.think.awareness import get_current
        from solstone.think.utils import day_dirs

        awareness_current = get_current()
        day_count = len(day_dirs())
        attention = _resolve_attention(awareness_current)
        if attention:
            chat_bar["attention"] = {"placeholder_text": attention.placeholder_text}
        chat_bar["placeholder"] = _resolve_placeholder(awareness_current, day_count)

        today = date.today().strftime("%Y%m%d")
        unresolved_request = latest_unresolved_sol_chat_request(read_chat_events(today))
        if unresolved_request:
            chat_bar["sol_request"] = {**unresolved_request, "day": today}
    except Exception:
        chat_bar = dict(default_chat_bar)
        logger.warning("failed to resolve chat bar shell context", exc_info=True)

    return chat_bar


def build_shell_data(registry: AppRegistry) -> dict[str, Any]:
    """Build the shared shell-state object for Jinja and `/api/shell`."""
    config = load_convey_config()
    return {
        "version": _get_version(),
        "apps": _build_apps(registry, config),
        "facets": _get_facets_data(),
        "selected_facet": _get_selected_facet(),
        "chat_bar": _build_chat_bar(),
        "settings": {"reporting_enabled": reporting_enabled()},
    }
