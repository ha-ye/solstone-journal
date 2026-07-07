# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regression tests for pair-modal presentation modes."""

from __future__ import annotations

import re

from solstone.apps.network import copy


def _body(link_env) -> str:
    env = link_env()
    response = env.client.get("/app/network/workspace")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _function_body(body: str, start_marker: str, end_marker: str) -> str:
    start = body.index(start_marker)
    end = body.index(end_marker, start)
    return body[start:end]


def _link_copy(env) -> dict[str, object]:
    response = env.client.get("/app/network/api/state")
    assert response.status_code == 200
    return response.get_json()["link_copy"]


def test_pair_presentation_selector_markup_and_copy(link_env) -> None:
    env = link_env()
    response = env.client.get("/app/network/workspace")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    payload = _link_copy(env)

    title_idx = body.index('id="link-pair-modal-title"')
    selector_idx = body.index('id="link-present-selector"')
    pair_code_idx = body.index('id="link-pair-code"')
    assert title_idx < selector_idx < pair_code_idx

    selector = body[selector_idx:pair_code_idx]
    assert 'role="radiogroup"' in selector
    assert 'data-copy-attr="aria-label:PRESENTATION_SELECTOR_LABEL"' in selector
    assert payload["PRESENTATION_SELECTOR_LABEL"] == copy.PRESENTATION_SELECTOR_LABEL
    for button_id, mode, label, checked in (
        ("link-present-phone", "phone", copy.PRESENTATION_PHONE_LABEL, "true"),
        (
            "link-present-computer",
            "computer",
            copy.PRESENTATION_COMPUTER_LABEL,
            "false",
        ),
        ("link-present-glasses", "glasses", copy.PRESENTATION_GLASSES_LABEL, "false"),
    ):
        assert f'id="{button_id}"' in selector
        assert 'role="radio"' in selector
        assert f'aria-checked="{checked}"' in selector
        assert f'data-presentation-mode="{mode}"' in selector
        assert (
            payload[
                {
                    "phone": "PRESENTATION_PHONE_LABEL",
                    "computer": "PRESENTATION_COMPUTER_LABEL",
                    "glasses": "PRESENTATION_GLASSES_LABEL",
                }[mode]
            ]
            == label
        )

    input_idx = body.index('id="link-pair-link-input"')
    assert "readonly" in body[input_idx : input_idx + 200]
    assert 'aria-labelledby="link-pair-link-label"' in body[input_idx : input_idx + 200]
    assert 'id="link-pair-link-copy"' in body
    assert 'data-copy="PAIR_LINK_FIELD_LABEL"' in body
    assert 'data-copy="PAIR_LINK_COPY_LABEL"' in body
    assert payload["PAIR_LINK_FIELD_LABEL"] == copy.PAIR_LINK_FIELD_LABEL
    assert payload["PAIR_LINK_COPY_LABEL"] == copy.PAIR_LINK_COPY_LABEL
    assert "pair_url" not in body


def test_pair_presentation_css_modes_keep_phone_grid(link_env) -> None:
    body = _body(link_env)

    assert ".link-pair-grid { display: grid; grid-template-columns: auto 1fr;" in body
    assert ".link-pair-qr-cell { position: relative; grid-row: span 4; }" in body
    assert ".link-pair-computer-panel { display: none; grid-column: 1 / -1; }" in body
    assert ".link-pair-grid.is-computer { grid-template-columns: 1fr; }" in body
    assert ".link-pair-grid.is-computer .link-pair-qr-cell" in body
    assert (
        ".link-pair-grid.is-computer .link-pair-computer-panel { display: flex; }"
        in body
    )
    assert ".link-pair-grid.is-glasses { grid-template-columns: 1fr; }" in body
    assert (
        ".link-pair-grid.is-glasses .link-pair-qr-cell { grid-row: auto; width: 100%; }"
        in body
    )
    assert ".link-pair-grid.is-glasses .link-qr-container svg," in body
    assert "max-width: 100%" in body
    assert ".link-pair-grid.is-glasses .link-pair-label-edit" in body
    assert ".link-pair-grid.is-glasses .link-pair-details" in body


def test_pair_render_seam_uses_current_pair_without_label_clobber(link_env) -> None:
    body = _body(link_env)

    assert "let currentPair = null;" in body
    assert "let presentationMode = 'phone';" in body
    assert "let viewedMode = null;" in body
    assert "let lastPosture = null;" in body
    assert "let reachRevealed = false;" in body

    render_body = _function_body(
        body,
        "function renderPairPresentation()",
        "function setPresentationMode",
    )
    assert "if (!currentPair) return;" in render_body
    assert (
        "pairCodeBox.classList.toggle('is-computer', presentationMode === 'computer');"
        in render_body
    )
    assert (
        "pairCodeBox.classList.toggle('is-glasses', presentationMode === 'glasses');"
        in render_body
    )
    assert "renderQr(currentPair.pair_link);" in render_body
    assert "caFpEl.textContent = currentPair.ca_fingerprint;" in render_body
    assert "pairLinkInput.value = currentPair.pair_link;" in render_body
    assert "setPairCopy();" in render_body
    assert "updatePresentationSelector();" in render_body
    assert "deviceLabelInput.value" not in render_body

    request_body = _function_body(
        body,
        "async function requestPairCode",
        "function handleExpiry",
    )
    assert "currentPair = {" in request_body
    assert "pair_link: data.pair_link" in request_body
    assert "ca_fingerprint: data.ca_fingerprint" in request_body
    assert "device_label: data.device_label" in request_body
    assert "expires_in: Number(data.expires_in) || 300" in request_body
    assert "deviceLabelInput.value = data.device_label;" in request_body
    assert "renderPairPresentation();" in request_body
    assert body.count("deviceLabelInput.value = data.device_label;") == 1


def test_pair_mode_switch_does_not_request_new_pair_code(link_env) -> None:
    body = _body(link_env)

    set_mode_body = _function_body(
        body,
        "function setPresentationMode",
        "function selectedPresentationIndex",
    )
    assert "presentationMode = mode;" in set_mode_body
    assert "renderPairPresentation();" in set_mode_body
    assert "requestPairCode" not in set_mode_body

    open_body = _function_body(
        body, "function openPairModal", "function closePairModal"
    )
    assert "resetPairCodeState();" in open_body
    assert "presentationMode = 'phone';" in open_body
    assert open_body.index("presentationMode = 'phone';") < open_body.index(
        "requestPairCode({ restart: true })"
    )

    reset_body = _function_body(
        body,
        "function resetPairCodeState",
        "function renderQr",
    )
    assert "currentPair = null;" in reset_body
    assert "if (presentSelector) presentSelector.hidden = false;" in reset_body
    assert "pairCodeBox.classList.remove('is-computer', 'is-glasses');" in reset_body

    complete_body = _function_body(
        body, "function handlePairComplete", "function showPairError"
    )
    error_body = _function_body(
        body, "function showPairError", "function openPairModal"
    )
    assert "if (presentSelector) presentSelector.hidden = true;" in complete_body
    assert "if (presentSelector) presentSelector.hidden = true;" in error_body


def test_pair_selector_keyboard_and_clipboard_only_copy(link_env) -> None:
    body = _body(link_env)

    key_body = _function_body(
        body,
        "function handlePresentationSelectorKey",
        "function selectPairLinkInput",
    )
    for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"):
        assert key in key_body
    assert "event.preventDefault();" in key_body
    assert "setPresentationMode(next.dataset.presentationMode);" in key_body
    assert "next.focus();" in key_body

    clipboard_body = _function_body(
        body,
        "async function clipboardWriteText",
        "function setPairCopy",
    )
    assert "navigator.clipboard.writeText" in clipboard_body
    assert "return false;" in clipboard_body
    assert "document.execCommand" not in clipboard_body

    copy_link_body = _function_body(
        body,
        "async function copyCurrentPairLink",
        "async function selectAndCopyPairLink",
    )
    assert "clipboardWriteText(currentPair.pair_link)" in copy_link_body
    assert "PAIR_LINK_COPY_SUCCESS_TOAST" in copy_link_body
    assert "PAIR_LINK_COPY_FAIL_TOAST" in copy_link_body

    assert re.search(
        r"pairLinkInput\?\.addEventListener\('focus', selectAndCopyPairLink\);",
        body,
    )
    assert re.search(
        r"pairLinkInput\?\.addEventListener\('click', selectAndCopyPairLink\);",
        body,
    )
    assert "pairLinkCopy?.addEventListener('click'" in body

    fingerprint_body = _function_body(
        body,
        "async function copyFingerprint",
        "function replaceRenameInput",
    )
    assert "document.execCommand('copy')" in fingerprint_body
