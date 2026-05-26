# L7 Plan: scout device-code enable + disable

Ship a headless device-code enable flow for `journal services enable scout`, add `journal services disable scout`, and remove the `headless_no_browser` token.

## D1 - Disable-verification fingerprint

- Decision: choose (a), full-hex `key_fingerprint_sha256`.
- Rationale: it lets disable remove only the portal-provisioned key without persisting or comparing the secret itself; full hex avoids truncation collisions and keeps tests simple.
- Storage key: `key_fingerprint_sha256` under `config["services"]["scout"]`.
- Computed at provision time in `scout.provision_scout_handoff()` from the handoff `google_api_key`.
- Verified at disable time in `scout.disable_scout()` before deleting `config["env"]["GOOGLE_API_KEY"]`.
- Format: lowercase 64-char hex from `sha256(google_api_key.encode("utf-8")).hexdigest()`.

## D2 - Device-code mint helper

- Decision: add `portal_client.mint_device_code(base_url) -> DeviceCodeOutcome`.
- Shape:
  - `DeviceCodeOutcome(kind: Literal["success", "failed"], nonce: str | None = None, code: str | None = None, expires_in: int | None = None, reason: str | None = None, detail: str | None = None)`
- Endpoint shape: POST to the services portal device-code mint endpoint for scout, using `request_headers("cli")`.
- Success payload contract: JSON object with `nonce`, `code`, and `expires_in`.
- Validate `nonce` with `NONCE_REGEX`; validate `code` with `DEVICE_CODE_REGEX`; validate `expires_in` as a positive int.
- Add dedicated mint-status mapper `_handle_mint_status(status)`.
- Do not modify `handle_http_status()`: it is poll-specific, and `410` only has handoff-poll meaning.
- Mint status mapping:
  - `400 -> nonce_invalid`
  - `429 -> rate_limited`
  - all other HTTP statuses -> `unexpected_payload`
- Transport mapping mirrors poll helper:
  - TLS errors -> `tls_verification_failed`
  - timeout / non-TLS `URLError` -> `portal_unreachable`
  - malformed payload -> `unexpected_payload`

## D3 - Error tokens and exit codes

- Add `rate_limited`: exit 1.
- Copy: `too many enable attempts from this network — wait an hour and try again.`
- Add `already_disabled`: exit 0.
- Copy: `solstone scout is not enabled on this machine.`
- Remove `headless_no_browser` from `ERROR_MESSAGES` and `EXIT_CODES`.
- New copies do not match `BLOCKED_COPY_RE`: no sign/log/account/linked/authenticate phrases.
- Final `ERROR_MESSAGES` key set:
  - `consent_link_expired`
  - `consent_timeout`
  - `portal_unreachable`
  - `tls_verification_failed`
  - `nonce_invalid`
  - `unexpected_payload`
  - `write_failed`
  - `already_enabled`
  - `manual_key_present`
  - `rate_limited`
  - `already_disabled`
  - `journal_not_initialized`
  - `unknown_service`
- Final explicit `EXIT_CODES`:
  - `already_enabled: 0`
  - `manual_key_present: 0`
  - `already_disabled: 0`
  - `unknown_service: 2`
- All other tokens return 1 through `EXIT_CODES.get(token, 1)`.

## D4 - Disable verb structure

- Add `scout.DisableOutcome` dataclass:
  - `was_enabled: bool`
  - `env_key_preserved: bool`
- Add `scout.disable_scout() -> DisableOutcome`.
- Locking: use the same `_lock_path()` and `fcntl.LOCK_EX` pattern as `provision_scout_handoff()`.
- Config mutation rules:
  - Require initialized journal config via `_require_journal_config()`.
  - Read config under lock.
  - If no `services.scout` block: no write; return `DisableOutcome(was_enabled=False, env_key_preserved=False)`.
  - Remove `config["services"]["scout"]`; remove empty `services` dict if left empty.
  - Compare current `env.GOOGLE_API_KEY` fingerprint to stored `key_fingerprint_sha256`.
  - If current env key is missing or fingerprint mismatch: leave env untouched and return `env_key_preserved=True`.
  - If current env key matches stored fingerprint: delete `env["GOOGLE_API_KEY"]`; remove empty `env` dict only if local convention permits. Prefer preserving empty `env` unless tests establish otherwise.
  - Write mutated config atomically via `write_journal_config()`.
- Precedent for idempotent noop output is `_enable_scout()` at `solstone/think/services/cli.py:183-185`:
  - `if not args.force and is_scout_enabled():`
  - `_print_error("already_enabled")`
  - `return EXIT_CODES["already_enabled"]`
- Decision for `already_disabled`: emit `_print_error("already_disabled")` to STDERR and exit 0, matching `already_enabled` / `manual_key_present`.
- Add `_disable_scout(args)` CLI handler:
  - Catch `scout.JournalNotInitializedError` and emit `journal_not_initialized`.
  - Catch other exceptions from storage as `write_failed`.
  - `not outcome.was_enabled -> _print_error("already_disabled"); return 0`
  - `outcome.was_enabled and outcome.env_key_preserved -> print(STDOUT_DISABLE_PRESERVED_MANUAL_KEY); return 0`
  - `outcome.was_enabled and not outcome.env_key_preserved -> print(STDOUT_DISABLE_SUCCESS); return 0`
- Parser wiring:
  - Add sibling `disable` parser under root.
  - Add nested service subparsers with `dest="service"`, `metavar="{scout}"`, `parser_class=_ServicesArgumentParser`.
  - Add only `disable scout`, with `set_defaults(handler=_disable_scout)`.
  - Keep invalid service choices using `_ServicesArgumentParser.error()` so `disable foo` emits `unknown_service`.
- New stdout constants:
  - `STDOUT_DISABLE_SUCCESS = "Scout disabled."`
  - `STDOUT_DISABLE_PRESERVED_MANUAL_KEY = "Scout disabled — your manually-pasted key was preserved."`

## D5 - Long-poll reuse

- Device-code path calls `_poll_handoff(base_url, nonce, args.wait)` unchanged.
- No new wait/timeout constants; `--wait` still defaults to `portal_client.DEFAULT_WAIT_SECONDS` (`900`) and clamps through `_wait_seconds()`.
- Add `STDOUT_DEVICE_CODE_TEMPLATE` with `{url}` and `{code}` placeholders.
- Template content:
  - `Open this URL in any browser:`
  - `{url}`
  - blank line
  - `Then enter this code when prompted:`
  - `{code}`
  - blank line
  - `Waiting for you to finish in the browser (up to 15 minutes)...`
- The wait line is baked into the template; device-code branch must not print `STDOUT_WAITING` again.
- Device-code URL placeholder: use the services portal's device-code entry URL from the worker contract. If no helper exists, derive it locally in CLI from `base_url` without adding another public symbol.

## Symbol additions

- `solstone/think/services/constants.py`
  - `DEVICE_CODE_PREFIX`
  - `DEVICE_CODE_REGEX`
  - `DEVICE_CODE_TTL_MS = 900_000`
- `solstone/think/services/portal_client.py`
  - `DeviceCodeOutcome`
  - `mint_device_code(base_url)`
  - `_handle_mint_status(status)`
- `solstone/think/services/scout.py`
  - `KEY_FINGERPRINT_FIELD = "key_fingerprint_sha256"`
  - `_fingerprint_key(key: str) -> str`
  - `DisableOutcome`
  - `disable_scout() -> DisableOutcome`
  - Do not update `_HANDOFF_FIELDS`; it remains the worker-payload contract.
- `solstone/think/services/cli.py`
  - `STDOUT_DEVICE_CODE_TEMPLATE`
  - `STDOUT_DISABLE_SUCCESS`
  - `STDOUT_DISABLE_PRESERVED_MANUAL_KEY`
  - `_print_device_code_instructions(url, code)`
  - `_disable_scout(args)`
  - Prefer `import solstone.think.services.scout as scout` and update existing storage calls to `scout.*`, so tests can monkeypatch storage through the module.

## Code-shape sketches

- `portal_client.mint_device_code(base_url)`
  - Build POST request to scout device-code mint endpoint.
  - Headers: `request_headers("cli")`.
  - On HTTPError: return `_handle_mint_status(exc.code)`.
  - On TLS/network: return failed `DeviceCodeOutcome` with existing canonical reason.
  - On 200: parse JSON object; validate `nonce`, `code`, `expires_in`; return success outcome.
  - On malformed response: return failed `unexpected_payload`.

- `scout.provision_scout_handoff(payload)`
  - Existing validation unchanged.
  - Add `KEY_FINGERPRINT_FIELD: _fingerprint_key(values["google_api_key"])` to persisted scout block.

- `scout.disable_scout()`
  - `_require_journal_config()`.
  - Acquire `_lock_path()` lock.
  - Re-check config exists.
  - Read config.
  - Read `scout_block = config.get("services", {}).get("scout")`.
  - If not dict: return `DisableOutcome(False, False)`.
  - Remove scout block.
  - Compare current env key fingerprint with stored field.
  - Delete env key only on match.
  - Write config.
  - Return `DisableOutcome(True, preserved)`.

- `_enable_scout(args)` changed headless/browser-false branch
  - When `_is_headless()` is true, call mint helper instead of printing browser URL and returning 2.
  - When `_open_browser(browser_url)` returns false, fall back to the same device-code helper.
  - Device-code success: print instructions, poll existing handoff, provision payload, print `STDOUT_SUCCESS`.
  - Device-code failed: `_print_error(outcome.reason or "unexpected_payload")`, return mapped exit.
  - Browser success branch remains: print `STDOUT_OPENING`, open browser, print `STDOUT_WAITING`, poll, provision, print `STDOUT_SUCCESS`.

- `_disable_scout(args)`
  - Call `scout.disable_scout()`.
  - Map `JournalNotInitializedError -> journal_not_initialized`.
  - Map storage exceptions -> `write_failed`.
  - Map outcomes exactly as D4.

## Test diff enumeration

- `tests/services/test_constants.py`
  - Delete `test_device_code_constants_are_not_defined`.
  - Add `test_device_code_constants_match_worker_contract`: assert prefix, regex pattern source, and `DEVICE_CODE_TTL_MS == 900_000`.
  - Add `test_device_code_regex_rejects_ambiguous_chars`: reject `I`, `L`, `O`, `U`, `0`, `1` in the random code portion.

- `tests/services/test_short_token_envelope.py`
  - Drop `headless_no_browser`.
  - Add `rate_limited`.
  - Add `already_disabled`.
  - Keep equality assertion against `set(cli.ERROR_MESSAGES)` / `.keys()`; the current test uses `assert set(cli.ERROR_MESSAGES) == CANONICAL_TOKENS`.

- `tests/services/test_cli_namespace.py`
  - Delete `test_headless_prints_url_and_exits_2`.
  - Add `test_headless_mints_device_code_and_polls`: monkeypatch `_is_headless=True`, `portal_client.mint_device_code` success, `portal_client.poll_handoff_once` success payload, storage provision no-op or assert saved config, exit 0, URL printed, code printed.
  - Delete `test_open_browser_false_maps_to_headless`.
  - Add `test_open_browser_false_falls_back_to_device_code`: `_is_headless=False`, `_open_browser=False`, same mint/poll assertions.
  - Add `test_mint_device_code_rate_limited`: mint failed `rate_limited`, exit 1, stderr starts `rate_limited: `.
  - Add `test_disable_scout_when_enabled_clears_block_and_env_key`.
  - Add `test_disable_scout_when_manually_keyed_preserves_env_key`: assert stdout contains `preserved`.
  - Add `test_disable_scout_when_not_enabled_emits_already_disabled`: exit 0, stderr starts `already_disabled: `.
  - Add `test_disable_scout_when_journal_not_initialized_emits_token`.
  - Add `test_disable_unknown_service_emits_unknown_service`.

- `tests/services/test_brand_canon.py`
  - Remove `"headless_no_browser"` branch.
  - Add `"device_code_happy"`: monkeypatch `_is_headless=True`, mint success, poll success, provision no-op.
  - Add `"device_code_rate_limited"`: mint failed `rate_limited`.
  - Add `"device_code_portal_unreachable"`: mint failed `portal_unreachable`.
  - Add `"device_code_unexpected_payload"`: mint failed `unexpected_payload`.
  - Add `"disable_happy"`: provision then disable.
  - Add `"disable_already_disabled"`: no scout block, run disable.
  - Add `"disable_manual_preserved"`: provision, replace env key, run disable.
  - Add `"disable_rotated_stale_key"`: scout block with stale fingerprint plus different env key, run disable.
  - Template mental regex check: `Open this URL in any browser`, `Then enter this code when prompted`, and `Waiting for you to finish in the browser` do not match `BLOCKED_COPY_RE`.

- `tests/services/test_scout_storage.py`
  - Update `test_provision_scout_handoff_round_trip_preserves_config` to assert `key_fingerprint_sha256 == sha256(test_key.encode("utf-8")).hexdigest()`.
  - Add `test_disable_scout_when_enabled_returns_outcome_clears_block_and_env_key`.
  - Add `test_disable_scout_preserves_env_key_when_fingerprint_mismatches`.
  - Add `test_disable_scout_when_not_enabled_returns_was_enabled_false`.
  - Add `test_disable_scout_uses_journal_config_lock`: mirror existing parallel-write lock pattern if practical; otherwise assert lock path creation/chmod through a direct disable.

## Risks / open questions

- Worker-contract exact values for `DEVICE_CODE_PREFIX`, `DEVICE_CODE_REGEX.pattern`, device-code POST path, and device-code entry URL are not present in this repo; implement from the L7 scope/portal contract, not from local inference.
- `unknown_service` copy currently says `Use journal services enable scout.`; acceptable for current token but may read oddly for `disable foo`. Keep unless product copy explicitly changes.
- Preserve empty `env` / `services` sections conservatively unless tests or existing config conventions require cleanup.
