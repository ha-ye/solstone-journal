# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ipaddress
import subprocess
from pathlib import Path

import scripts.check_channel_adapter_scrub as scrub


def _parts(*pieces: str) -> str:
    return "".join(pieces)


def _line_findings(line: str) -> list[scrub.Finding]:
    return scrub.scan_line("scratch.txt", 1, line)


def _components(findings: list[scrub.Finding]) -> set[str]:
    return {finding.component for finding in findings}


def _port_value(findings: list[scrub.Finding]) -> str:
    values = [
        finding.value for finding in findings if finding.component == "ssh-scp-port"
    ]
    assert len(values) == 1
    return values[0]


def test_tier1_literal_rejected() -> None:
    for sensitive in scrub.TIER1_VALUES:
        findings = _line_findings(f"stream = {sensitive!r}")

        assert [(finding.tier, finding.component) for finding in findings] == [
            ("Tier-1", "literal")
        ]


def test_tier2_ip_literal_uses_range_and_documented_exclusions() -> None:
    public_host = ".".join(["71", "19", "22", "17"])

    findings = _line_findings(f"host = {public_host!r}")

    assert [
        (finding.tier, finding.component, finding.value) for finding in findings
    ] == [("Tier-2", "ip-literal", public_host)]
    assert "documented IP literal exclusion list" in findings[0].detail
    assert _line_findings("version = '1.2.0.1'") == []
    assert _line_findings("bind = '0.0.0.0'") == []
    assert _line_findings("fixture = '192.0.2.44'") == []
    assert _line_findings("text = 'v1.2.3.4'") == []
    assert _line_findings("text = '1.2.3.4.5'") == []


def test_tier2_user_host_rejects_bare_reachable_literal() -> None:
    host = ".".join(["10", "0", "0", "7"])
    reachable = f"deploy@{host}"
    with_port = f"{reachable}:2222"
    with_path = f"{reachable}:/var/tmp/proof"
    public_host = ".".join(["buildbox", "solpbc", "org"])
    public_reachable = f"{_parts('deploy')}@{public_host}"

    findings = _line_findings(reachable)

    assert [
        (finding.tier, finding.component, finding.value) for finding in findings
    ] == [("Tier-2", "user-host", reachable)]
    assert [
        (finding.tier, finding.component, finding.value)
        for finding in _line_findings(with_port)
    ] == [("Tier-2", "user-host", with_port)]
    assert [
        (finding.tier, finding.component, finding.value)
        for finding in _line_findings(with_path)
    ] == [("Tier-2", "user-host", with_path)]
    assert [
        (finding.tier, finding.component, finding.value)
        for finding in _line_findings(public_reachable)
    ] == [("Tier-2", "user-host", public_reachable)]
    assert _line_findings("mail = 'deploy@example.com'") == []
    assert _line_findings("pkg = 'qrcode-generator@1.4.4'") == []


def test_tier2_ssh_scp_port_rejected() -> None:
    ssh = _parts("s", "sh")
    scp = _parts("s", "cp")
    ssh_flag = _parts("-", "p")
    scp_flag = _parts("-", "P")
    option_compact = _parts("-", "o", "Port", "=", "2222")
    option_split = _parts("-", "o", " ", "Port", "=", "2222")

    shell_findings = _line_findings(f"{ssh} {ssh_flag} 2222 build-host.example")
    argv_findings = _line_findings(
        f"cmd = [{scp!r}, {scp_flag!r}, '2222', 'src', 'dest']"
    )
    shell_option_findings = _line_findings(f"{ssh} {option_compact} host")
    shell_split_option_findings = _line_findings(f"{ssh} {option_split} host")
    argv_option_findings = _line_findings(
        f"cmd = [{ssh!r}, {_parts('-', 'o')!r}, {_parts('Port', '=', '2222')!r}, 'h']"
    )

    assert _port_value(shell_findings) == "-p/-P"
    assert _port_value(argv_findings) == "argv -p/-P"
    assert _port_value(shell_option_findings) == "-oPort=/-o Port="
    assert _port_value(shell_split_option_findings) == "-oPort=/-o Port="
    assert _port_value(argv_option_findings) == "argv -oPort=/argv -o Port="
    assert _line_findings("$(UV) build --wheel -C--build-option=--plat-name=x") == []
    assert _line_findings('<div data-speaker-option="voice">') == []


def test_tier2_ipv6_literal_rejected_with_documented_exclusions() -> None:
    global_v6 = str(
        ipaddress.IPv6Address(
            int(ipaddress.IPv6Network(_parts("2000", "::", "/3")).network_address) + 1
        )
    )

    findings = _line_findings(f"host = {global_v6!r}")

    assert [
        (finding.tier, finding.component, finding.value) for finding in findings
    ] == [("Tier-2", "ipv6-literal", global_v6)]
    assert _line_findings("loopback = '::1'") == []
    assert _line_findings("fixture = '2001:db8::1'") == []
    assert _line_findings("clock = '12:34:56'") == []
    assert _line_findings("path = 'std::fmt'") == []


def test_tier3_components_reject_reach_contexts() -> None:
    term = scrub.TIER3_TERMS[1]
    ssh = _parts("s", "sh")

    cases = {
        "ssh-argv": f"cmd = [{ssh!r}, {term!r}]",
        "ssh-shell": f"{ssh} {term}",
        "user-host": f"{term}@host",
        "host-user": f"user@{term}.local",
        "config-value": f"remote_host = {term!r}",
        "remote-call": f"ssh_run(lane, {term!r})",
    }

    for component, line in cases.items():
        assert component in _components(_line_findings(line))


def test_config_key_detector_covers_remote_run_wrapper() -> None:
    term = scrub.TIER3_TERMS[0]

    findings = _line_findings(f"remote_run_wrapper = {term!r}")

    assert "config-value" in _components(findings)


def test_channel_adapter_scrub_scans_repository_sources_including_itself() -> None:
    result = scrub.scan_paths(scrub.ROOT, scrub.tracked_entries(scrub.ROOT))

    assert result.findings == ()


def test_channel_adapter_scrub_falsification_plants_each_component(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    ssh = _parts("s", "sh")
    global_v6 = str(
        ipaddress.IPv6Address(
            int(ipaddress.IPv6Network(_parts("2000", "::", "/3")).network_address) + 1
        )
    )
    plants = [
        *scrub.TIER1_VALUES,
        f"deploy@{'.'.join(['10', '0', '0', '7'])}",
        f"deploy@{'.'.join(['10', '0', '0', '7'])}:2222",
        f"{_parts('deploy')}@{'.'.join(['buildbox', 'solpbc', 'org'])}",
        f"{ssh} {_parts('-', 'o', 'Port', '=', '2222')} host",
        f"{ssh} {_parts('-', 'o', ' ', 'Port', '=', '2222')} host",
        f"cmd = [{ssh!r}, {_parts('-', 'o')!r}, {_parts('Port', '=', '2222')!r}, 'h']",
        global_v6,
        f"{ssh} {scrub.TIER3_TERMS[0]}",
        f"cmd = [{_parts('s', 'sh')!r}, {scrub.TIER3_TERMS[1]!r}]",
        f"{scrub.TIER3_TERMS[2]}@host",
        f"user@{scrub.TIER3_TERMS[3]}.local",
        f"remote_host = {scrub.TIER3_TERMS[4]!r}",
        f"ssh_run(lane, {scrub.TIER3_TERMS[5]!r})",
    ]
    scratch = repo / "scratch.txt"
    scratch.write_text("\n".join(plants) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "scratch.txt"], cwd=repo, check=True)

    result = scrub.scan_paths(repo, scrub.tracked_entries(repo))

    assert len(result.findings) >= len(plants)
    assert {finding.path for finding in result.findings} == {"scratch.txt"}


def test_channel_adapter_scrub_scans_raw_symlink_targets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    link = repo / "leaky-link"
    link.symlink_to(scrub.TIER1_VALUES[0])
    subprocess.run(["git", "add", "leaky-link"], cwd=repo, check=True)

    result = scrub.scan_paths(repo, scrub.tracked_entries(repo))

    assert [
        (finding.path, finding.line, finding.component) for finding in result.findings
    ] == [("leaky-link", 1, "literal")]


def test_channel_adapter_scrub_reports_unreadable_tracked_regular_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    tracked = repo / "regular.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "regular.txt"], cwd=repo, check=True)
    tracked.unlink()
    tracked.mkdir()

    result = scrub.scan_paths(repo, scrub.tracked_entries(repo))

    assert [(finding.path, finding.component) for finding in result.findings] == [
        ("regular.txt", "tracked-io")
    ]
    assert "errno" in result.findings[0].detail


def test_channel_adapter_scrub_skips_deleted_tracked_regular_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    tracked = repo / "regular.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "regular.txt"], cwd=repo, check=True)
    tracked.unlink()

    result = scrub.scan_paths(repo, scrub.tracked_entries(repo))

    assert result.findings == ()
