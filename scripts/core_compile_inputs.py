#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Discover non-test compile-time inputs for the shipping solstone-core bins."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

ROOT_PACKAGE = "solstone-core"
INCLUDE_MACROS = frozenset({"include_str", "include_bytes"})


class CoreCompileInputError(RuntimeError):
    """A shipping Rust compile-time input could not be derived safely."""


@dataclass(frozen=True)
class CoreCompileInputAsset:
    source_file: Path
    macro: Literal["include_str", "include_bytes"]
    line: int
    column: int
    raw_argument: str
    resolved_path: Path
    sdist_path: str


@dataclass(frozen=True)
class _Package:
    name: str
    member: str
    manifest: Path
    data: dict[str, Any]


def discover_core_compile_inputs(root: Path) -> tuple[CoreCompileInputAsset, ...]:
    root = root.resolve()
    packages = _workspace_packages(root)
    closure = _shipping_closure(root, packages)
    source_files = _shipping_source_files(root, packages, closure)
    records: list[CoreCompileInputAsset] = []
    for source_file in source_files:
        text = _read_text(source_file)
        filtered = _without_cfg_test_items(source_file, text)
        records.extend(_include_records(root, source_file, filtered))
    return tuple(
        sorted(records, key=lambda item: (item.sdist_path, item.source_file, item.line))
    )


def core_compile_input_sdist_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for asset in discover_core_compile_inputs(root):
        try:
            data = asset.resolved_path.read_bytes()
        except OSError as exc:
            raise CoreCompileInputError(
                f"compile-input-read-failed: {asset.resolved_path}: {exc}"
            ) from None
        existing = files.get(asset.sdist_path)
        if existing is not None and existing != data:
            raise CoreCompileInputError(
                f"compile-input-conflict: {asset.sdist_path} has multiple byte sources"
            )
        files[asset.sdist_path] = data
    return files


def _workspace_packages(root: Path) -> dict[str, _Package]:
    workspace_manifest = root / "core" / "Cargo.toml"
    data = _read_toml(workspace_manifest, label="core workspace")
    members = data.get("workspace", {}).get("members")
    if not isinstance(members, list) or not members:
        raise CoreCompileInputError(
            "workspace-members-invalid: core workspace has no members"
        )
    packages: dict[str, _Package] = {}
    for member in members:
        if not isinstance(member, str) or not member:
            raise CoreCompileInputError(
                f"workspace-member-invalid: invalid workspace member {member!r}"
            )
        manifest = root / "core" / member / "Cargo.toml"
        member_data = _read_toml(manifest, label=f"workspace member {member}")
        name = member_data.get("package", {}).get("name")
        if not isinstance(name, str) or not name:
            raise CoreCompileInputError(
                f"package-name-missing: {manifest.relative_to(root).as_posix()}"
            )
        if name in packages:
            raise CoreCompileInputError(f"package-name-duplicate: {name}")
        packages[name] = _Package(name, member, manifest, member_data)
    if ROOT_PACKAGE not in packages:
        raise CoreCompileInputError(f"root-package-missing: {ROOT_PACKAGE}")
    return packages


def _read_toml(path: Path, *, label: str) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CoreCompileInputError(f"manifest-missing: {label}: {path}") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CoreCompileInputError(f"manifest-invalid: {label}: {exc}") from None


def _shipping_closure(root: Path, packages: dict[str, _Package]) -> tuple[str, ...]:
    workspace_data = _read_toml(root / "core" / "Cargo.toml", label="core workspace")
    workspace_deps = workspace_data.get("workspace", {}).get("dependencies", {})
    if not isinstance(workspace_deps, dict):
        raise CoreCompileInputError("workspace-dependencies-invalid")
    seen: set[str] = set()
    ordered: list[str] = []

    def visit(name: str) -> None:
        if name in seen:
            return
        package = packages.get(name)
        if package is None:
            raise CoreCompileInputError(f"closure-package-missing: {name}")
        seen.add(name)
        ordered.append(name)
        dependencies = package.data.get("dependencies", {})
        if not isinstance(dependencies, dict):
            raise CoreCompileInputError(f"dependencies-invalid: {package.name}")
        for dep_name in _normal_workspace_dependency_names(
            root, package, dependencies, workspace_deps
        ):
            if dep_name in packages:
                visit(dep_name)

    visit(ROOT_PACKAGE)
    return tuple(ordered)


def _normal_workspace_dependency_names(
    root: Path,
    package: _Package,
    dependencies: dict[str, Any],
    workspace_deps: dict[str, Any],
) -> tuple[str, ...]:
    names: list[str] = []
    for key, value in dependencies.items():
        dep_manifest: Path | None = None
        if isinstance(value, dict) and value.get("workspace") is True:
            workspace_value = workspace_deps.get(key)
            if isinstance(workspace_value, dict) and isinstance(
                workspace_value.get("path"), str
            ):
                dep_manifest = root / "core" / workspace_value["path"] / "Cargo.toml"
        elif isinstance(value, dict) and isinstance(value.get("path"), str):
            dep_manifest = package.manifest.parent / value["path"] / "Cargo.toml"
        if dep_manifest is None:
            continue
        dep_data = _read_toml(dep_manifest, label=f"dependency {key}")
        dep_name = dep_data.get("package", {}).get("name")
        if not isinstance(dep_name, str) or not dep_name:
            raise CoreCompileInputError(
                f"dependency-package-name-missing: {dep_manifest}"
            )
        names.append(dep_name)
    return tuple(names)


def _shipping_source_files(
    root: Path, packages: dict[str, _Package], closure: tuple[str, ...]
) -> tuple[Path, ...]:
    pending: list[Path] = []
    root_package = packages[ROOT_PACKAGE]
    pending.extend(_root_bin_targets(root_package))
    for name in closure:
        if name == ROOT_PACKAGE:
            continue
        pending.append(_lib_target(packages[name]))

    seen: set[Path] = set()
    ordered: list[Path] = []
    while pending:
        source = pending.pop()
        source = source.resolve()
        if source in seen:
            continue
        if not source.is_file():
            raise CoreCompileInputError(f"source-file-missing: {source}")
        seen.add(source)
        ordered.append(source)
        text = _read_text(source)
        filtered = _without_cfg_test_items(source, text)
        pending.extend(_module_files(source, filtered))
    return tuple(sorted(ordered))


def _root_bin_targets(package: _Package) -> tuple[Path, ...]:
    targets: list[Path] = []
    for raw_bin in package.data.get("bin", []):
        if not isinstance(raw_bin, dict):
            raise CoreCompileInputError(f"bin-target-invalid: {package.name}")
        path = raw_bin.get("path")
        if isinstance(path, str) and path:
            targets.append(package.manifest.parent / path)
    if package.data.get("package", {}).get("autobins") is not False:
        implicit = package.manifest.parent / "src" / "main.rs"
        if implicit.is_file():
            targets.append(implicit)
    if not targets:
        raise CoreCompileInputError(f"root-bin-target-missing: {package.name}")
    return tuple(dict.fromkeys(targets))


def _lib_target(package: _Package) -> Path:
    raw_lib = package.data.get("lib")
    if isinstance(raw_lib, dict) and isinstance(raw_lib.get("path"), str):
        return package.manifest.parent / raw_lib["path"]
    return package.manifest.parent / "src" / "lib.rs"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CoreCompileInputError(f"source-file-missing: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise CoreCompileInputError(f"source-file-unreadable: {path}: {exc}") from None


def _module_files(source: Path, text: str) -> tuple[Path, ...]:
    masked = _masked_rust(source, text)
    modules: list[Path] = []
    pattern = re.compile(
        r"(?:(?:pub)(?:\s*\([^)]*\))?\s+)?mod\s+"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<tail>[;{])",
        re.MULTILINE,
    )
    for match in pattern.finditer(masked):
        if match.group("tail") != ";":
            continue
        path_attr = _path_attribute_before(source, text, masked, match.start())
        if path_attr is not None:
            relative = _parse_string_literal(
                path_attr, source, _line_for_offset(text, match.start())
            )
            target = source.parent / relative
            if not target.is_file():
                raise CoreCompileInputError(
                    f"path-module-missing: {source}:{_line_for_offset(text, match.start())}: {relative}"
                )
            modules.append(target)
            continue
        name = match.group("name")
        base = (
            source.parent
            if source.stem in {"lib", "main", "mod"}
            else source.parent / source.stem
        )
        candidates = (base / f"{name}.rs", base / name / "mod.rs")
        for candidate in candidates:
            if candidate.is_file():
                modules.append(candidate)
                break
        else:
            raise CoreCompileInputError(
                f"module-file-missing: {source}:{_line_for_offset(text, match.start())}: mod {name}"
            )
    return tuple(modules)


def _path_attribute_before(
    source: Path, text: str, masked: str, offset: int
) -> str | None:
    index = offset
    while index > 0 and masked[index - 1].isspace():
        index -= 1
    if index == 0 or masked[index - 1] != "]":
        return None
    attr_start = masked.rfind("#", 0, index)
    if attr_start == -1:
        return None
    attr_masked = masked[attr_start:index]
    if not re.fullmatch(r"#\s*\[\s*path\s*=\s*[\s]*\]", attr_masked, re.DOTALL):
        return None
    attr_text = text[attr_start:index]
    match = re.fullmatch(r"#\s*\[\s*path\s*=\s*(?P<path>.+)\s*\]", attr_text, re.DOTALL)
    if match is None:
        raise CoreCompileInputError(
            f"path-attribute-invalid: {source}:{_line_for_offset(text, attr_start)}"
        )
    return match.group("path").strip()


def _include_records(
    root: Path, source: Path, text: str
) -> tuple[CoreCompileInputAsset, ...]:
    masked = _masked_rust(source, text)
    records: list[CoreCompileInputAsset] = []
    pattern = re.compile(r"\b(?P<macro>include_str|include_bytes)\s*!\s*\(")
    for match in pattern.finditer(masked):
        macro = match.group("macro")
        close = _matching_delimiter(masked, match.end() - 1, "(", ")")
        raw_argument = text[match.end() : close].strip()
        line = _line_for_offset(text, match.start())
        column = _column_for_offset(text, match.start())
        include_path = _parse_string_literal(raw_argument, source, line)
        resolved = (source.parent / include_path).resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise CoreCompileInputError(
                f"compile-input-outside-repo: {source}:{line}: {include_path}"
            )
        if resolved.is_symlink() or not resolved.is_file():
            raise CoreCompileInputError(
                f"compile-input-missing: {source}:{line}: {include_path}"
            )
        records.append(
            CoreCompileInputAsset(
                source_file=source.resolve(),
                macro=macro,  # type: ignore[arg-type]
                line=line,
                column=column,
                raw_argument=raw_argument,
                resolved_path=resolved,
                sdist_path=resolved.relative_to(root).as_posix(),
            )
        )
    return tuple(records)


def _without_cfg_test_items(source: Path, text: str) -> str:
    masked = _masked_rust(source, text)
    output = list(text)
    pattern = re.compile(r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]")
    for match in reversed(tuple(pattern.finditer(masked))):
        start = match.start()
        end = _cfg_test_item_end(source, text, masked, match.end())
        for index in range(start, end):
            if output[index] != "\n":
                output[index] = " "
    return "".join(output)


def _cfg_test_item_end(source: Path, text: str, masked: str, offset: int) -> int:
    index = _skip_ws(masked, offset)
    while index < len(masked) and masked[index] == "#":
        attr_end = masked.find("]", index)
        if attr_end == -1:
            raise CoreCompileInputError(
                f"cfg-test-excision-failed: {source}:{_line_for_offset(text, index)}: unterminated attribute"
            )
        index = _skip_ws(masked, attr_end + 1)
    item_start = _skip_visibility(masked, index)
    if _starts_word(masked, item_start, "mod") or _starts_word(
        masked, item_start, "fn"
    ):
        brace = masked.find("{", item_start)
        semicolon = masked.find(";", item_start)
        if brace == -1 or (semicolon != -1 and semicolon < brace):
            raise CoreCompileInputError(
                f"cfg-test-excision-failed: {source}:{_line_for_offset(text, item_start)}: expected item body"
            )
        return _matching_delimiter(masked, brace, "{", "}") + 1
    raise CoreCompileInputError(
        f"cfg-test-excision-failed: {source}:{_line_for_offset(text, item_start)}: unsupported cfg(test) item"
    )


def _skip_visibility(masked: str, index: int) -> int:
    if not _starts_word(masked, index, "pub"):
        return index
    index = _skip_ws(masked, index + 3)
    if index < len(masked) and masked[index] == "(":
        index = _matching_delimiter(masked, index, "(", ")") + 1
    return _skip_ws(masked, index)


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _starts_word(text: str, index: int, word: str) -> bool:
    end = index + len(word)
    if text[index:end] != word:
        return False
    before = index == 0 or not (text[index - 1].isalnum() or text[index - 1] == "_")
    after = end == len(text) or not (text[end].isalnum() or text[end] == "_")
    return before and after


def _matching_delimiter(text: str, offset: int, open_char: str, close_char: str) -> int:
    if offset >= len(text) or text[offset] != open_char:
        raise CoreCompileInputError("delimiter-match-failed: opening delimiter missing")
    depth = 0
    for index in range(offset, len(text)):
        char = text[index]
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index
    raise CoreCompileInputError("delimiter-match-failed: unbalanced delimiters")


def _masked_rust(source: Path, text: str) -> str:
    chars = list(text)
    index = 0
    while index < len(chars):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if char == "/" and next_char == "/":
            index = _mask_line_comment(chars, text, index)
        elif char == "/" and next_char == "*":
            index = _mask_block_comment(source, chars, text, index)
        elif _raw_string_prefix(text, index) is not None:
            index = _mask_raw_string(source, chars, text, index)
        elif (char == "b" and next_char == '"') or char == '"':
            index = _mask_quoted(
                source,
                chars,
                text,
                index + (1 if char == "b" else 0),
                '"',
                allow_newline=True,
            )
        elif char == "b" and next_char == "'":
            index = _mask_quoted(
                source,
                chars,
                text,
                index + 1,
                "'",
                allow_newline=False,
            )
        elif char == "'" and _looks_like_char_literal(text, index):
            index = _mask_quoted(source, chars, text, index, "'", allow_newline=False)
        else:
            index += 1
    return "".join(chars)


def _mask_line_comment(chars: list[str], text: str, index: int) -> int:
    while index < len(chars) and text[index] != "\n":
        chars[index] = " "
        index += 1
    return index


def _mask_block_comment(source: Path, chars: list[str], text: str, index: int) -> int:
    depth = 0
    while index < len(chars):
        pair = text[index : index + 2]
        if pair == "/*":
            depth += 1
            chars[index] = chars[index + 1] = " "
            index += 2
            continue
        if pair == "*/":
            depth -= 1
            chars[index] = chars[index + 1] = " "
            index += 2
            if depth == 0:
                return index
            continue
        if text[index] != "\n":
            chars[index] = " "
        index += 1
    raise CoreCompileInputError(
        f"unterminated-rust-comment: {source}:{_line_for_offset(text, len(text))}"
    )


def _raw_string_prefix(text: str, index: int) -> tuple[int, str] | None:
    if text.startswith("br", index):
        index += 2
    elif text.startswith("r", index):
        index += 1
    else:
        return None
    hashes_start = index
    while index < len(text) and text[index] == "#":
        index += 1
    if index >= len(text) or text[index] != '"':
        return None
    return index + 1, "#" * (index - hashes_start)


def _mask_raw_string(source: Path, chars: list[str], text: str, index: int) -> int:
    prefix = _raw_string_prefix(text, index)
    if prefix is None:
        return index + 1
    content_start, hashes = prefix
    terminator = '"' + hashes
    end = text.find(terminator, content_start)
    if end == -1:
        raise CoreCompileInputError(
            f"unterminated-rust-string: {source}:{_line_for_offset(text, index)}"
        )
    end += len(terminator)
    for cursor in range(index, end):
        if text[cursor] != "\n":
            chars[cursor] = " "
    return end


def _mask_quoted(
    source: Path,
    chars: list[str],
    text: str,
    quote_index: int,
    quote: str,
    *,
    allow_newline: bool,
) -> int:
    index = quote_index
    chars[index] = " "
    index += 1
    while index < len(chars):
        char = text[index]
        if char == "\n":
            if not allow_newline:
                raise CoreCompileInputError(
                    f"unterminated-rust-string: {source}:{_line_for_offset(text, quote_index)}"
                )
            index += 1
            continue
        chars[index] = " "
        if char == "\\":
            index += 1
            if index < len(chars):
                chars[index] = " "
            index += 1
            continue
        if char == quote:
            return index + 1
        index += 1
    raise CoreCompileInputError(
        f"unterminated-rust-string: {source}:{_line_for_offset(text, quote_index)}"
    )


def _looks_like_char_literal(text: str, index: int) -> bool:
    if index + 1 >= len(text) or text[index + 1] == "\n":
        return False
    if (text[index + 1].isalnum() or text[index + 1] == "_") and (
        index + 2 >= len(text) or text[index + 2] != "'"
    ):
        return False
    cursor = index + 1
    while cursor < len(text) and text[cursor] != "\n":
        if text[cursor] == "\\":
            cursor += 2
            continue
        if text[cursor] == "'":
            return cursor - index <= 16
        cursor += 1
    return False


def _parse_string_literal(raw: str, source: Path, line: int) -> str:
    raw = raw.strip()
    raw_match = re.fullmatch(
        r'r(?P<hashes>#*)"(?P<body>.*)"(?P=hashes)', raw, re.DOTALL
    )
    if raw_match is not None:
        return raw_match.group("body")
    if not (raw.startswith('"') and raw.endswith('"')):
        raise CoreCompileInputError(
            f"unsupported-include-argument: {source}:{line}: {raw}"
        )
    body = raw[1:-1]
    if "\n" in body:
        raise CoreCompileInputError(
            f"unsupported-include-argument: {source}:{line}: multiline string literal"
        )
    try:
        return bytes(body, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError as exc:
        raise CoreCompileInputError(
            f"unsupported-include-argument: {source}:{line}: invalid escape {exc}"
        ) from None


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _column_for_offset(text: str, offset: int) -> int:
    line_start = text.rfind("\n", 0, offset) + 1
    return offset - line_start + 1
