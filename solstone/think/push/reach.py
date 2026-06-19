# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Reach relay-token provisioning for push dispatch."""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
import time
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from solstone.think.journal_config import (
    hold_config_lock,
    read_journal_config,
    write_journal_config,
)
from solstone.think.link.ca import LoadedCa, load_or_generate_ca, mint_reach_assertion
from solstone.think.link.paths import LinkState, ca_dir
from solstone.think.services.portal_client import portal_base_url, request_headers

logger = logging.getLogger(__name__)

REACH_REFRESH_MARGIN_SECONDS = 3600
_TIMEOUT_SECONDS = 10


def read_reach_token() -> str | None:
    """Return the stored reach relay token, if present."""
    state = _read_stored_state()
    return _state_token(state)


def ensure_reach_token() -> str | None:
    """Return a usable reach relay token, provisioning or refreshing when needed."""
    now = int(time.time())
    try:
        state = _read_stored_state()
    except Exception as exc:
        logger.warning(
            "reach relay token config read failed: error=%s", type(exc).__name__
        )
        state = None

    if _state_usable(state, now):
        return _state_token(state)

    try:
        link_state = LinkState.load()
    except Exception as exc:
        logger.warning(
            "reach relay token identity read failed: error=%s", type(exc).__name__
        )
        return None
    if link_state is None:
        return None

    try:
        ca = load_or_generate_ca(ca_dir())
        new_state = _request_reach_token(link_state.instance_id, ca)
    except Exception as exc:
        logger.warning("reach relay token refresh failed: error=%s", type(exc).__name__)
        new_state = None

    if new_state is None:
        return _state_unexpired_token(state, now)
    new_token = _state_token(new_state)
    if new_token is None:
        return _state_unexpired_token(state, now)

    try:
        with hold_config_lock():
            config = read_journal_config()
            services = config.setdefault("services", {})
            if not isinstance(services, dict):
                services = {}
                config["services"] = services
            push = services.setdefault("push", {})
            if not isinstance(push, dict):
                push = {}
                services["push"] = push
            push["reach_token"] = new_state
            push.pop("relay_token", None)
            write_journal_config(config)
    except Exception as exc:
        logger.warning(
            "reach relay token persistence failed: error=%s", type(exc).__name__
        )

    return new_token


def _request_reach_token(instance_id: str, ca: LoadedCa) -> dict[str, Any] | None:
    try:
        assertion = mint_reach_assertion(ca, instance_id)
        body = json.dumps(
            {
                "instance_id": instance_id,
                "ca_pubkey": ca.pubkey_spki_pem,
                "assertion": assertion,
            }
        ).encode("utf-8")
        headers = request_headers("push")
        headers.update({"Content-Type": "application/json"})
        request = urllib_request.Request(
            f"{portal_base_url()}/reach/push/relay-token",
            data=body,
            headers=headers,
            method="POST",
        )

        with urllib_request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", response.getcode()))
            raw_body = response.read()
    except HTTPError as exc:
        status = int(exc.code)
        if 400 <= status < 500:
            logger.warning("reach relay token request rejected: status=%s", status)
        else:
            logger.warning("reach relay token request server error: status=%s", status)
        return None
    except (URLError, socket.timeout, TimeoutError) as exc:
        logger.warning(
            "reach relay token transport failure: error=%s", type(exc).__name__
        )
        return None
    except Exception as exc:
        logger.warning(
            "reach relay token transport failure: error=%s", type(exc).__name__
        )
        return None

    if not 200 <= status < 300:
        return None
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    if payload.get("instance_id") != instance_id:
        return None
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        return None
    expires_at = payload.get("expires_at")
    expires_epoch = _parse_expires_epoch(expires_at)
    if expires_epoch is None:
        return None

    return {
        "token": token,
        "instance_id": instance_id,
        "expires_at": expires_at,
        "expires_epoch": expires_epoch,
    }


def _read_stored_state() -> object:
    config = read_journal_config()
    services = config.get("services")
    if not isinstance(services, dict):
        return None
    push = services.get("push")
    if not isinstance(push, dict):
        return None
    return push.get("reach_token")


def _state_token(state: object) -> str | None:
    if not isinstance(state, dict):
        return None
    token = state.get("token")
    return token if isinstance(token, str) and token else None


def _state_usable(state: object, now: int) -> bool:
    token = _state_token(state)
    if token is None or not isinstance(state, dict):
        return False
    expires_epoch = state.get("expires_epoch")
    if not isinstance(expires_epoch, int) or isinstance(expires_epoch, bool):
        return False
    instance_id = state.get("instance_id")
    if not isinstance(instance_id, str) or instance_id != _current_instance_id():
        return False
    return now < expires_epoch - REACH_REFRESH_MARGIN_SECONDS


def _state_unexpired_token(state: object, now: int) -> str | None:
    token = _state_token(state)
    if token is None or not isinstance(state, dict):
        return None
    expires_epoch = state.get("expires_epoch")
    if not isinstance(expires_epoch, int) or isinstance(expires_epoch, bool):
        return None
    return token if now < expires_epoch else None


def _current_instance_id() -> str | None:
    try:
        link_state = LinkState.load()
    except Exception as exc:
        logger.warning(
            "reach relay token identity read failed: error=%s", type(exc).__name__
        )
        return None
    return link_state.instance_id if link_state is not None else None


def _parse_expires_epoch(expires_at: object) -> int | None:
    if not isinstance(expires_at, str) or not expires_at:
        return None
    candidate = expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        return None
    return int(parsed.timestamp())


__all__ = [
    "REACH_REFRESH_MARGIN_SECONDS",
    "ensure_reach_token",
    "read_reach_token",
]
