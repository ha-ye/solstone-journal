# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Top-level `journal install-provider <name>` — install a provider runtime.

Local-system-only: only meaningful on the host that stores the journal. Moved
here from the old journal-access provider-install surface.
"""

from __future__ import annotations

import argparse
import json
import sys

from solstone.think.providers import local_install, parakeet_install
from solstone.think.providers.fit_report import FitReport
from solstone.think.utils import require_solstone

PARAKEET_DOWNLOAD_DISCLOSURE = (
    "parakeet-cpp fetches two external artifacts into this journal's provider "
    "cache before it can run: the parakeet.cpp server binary from github.com "
    "(MIT) and the speech model from huggingface.co (CC-BY-4.0)."
)


def _render_fit_report(report: FitReport) -> None:
    from solstone.think.providers import fit_report

    print(fit_report.render_fit_report(report), file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="journal install-provider",
        description="Install or retry a provider runtime.",
    )
    parser.add_argument("name", help="Provider to install: 'local' or 'parakeet'.")
    args = parser.parse_args()

    require_solstone()

    if args.name not in {"local", "parakeet"}:
        print(
            f"unsupported provider {args.name!r}; supported: local, parakeet",
            file=sys.stderr,
        )
        return 2

    if args.name == "parakeet":
        print(PARAKEET_DOWNLOAD_DISCLOSURE, file=sys.stderr)
        readiness = parakeet_install.inspect_readiness()
        installed = bool(readiness["binary_installed"] and readiness["model_installed"])
        if installed:
            print("parakeet already installed", file=sys.stderr)
        else:
            from solstone.think.providers import fit_report

            _render_fit_report(fit_report.build_parakeet_fit_report())
        try:
            status = parakeet_install.install_parakeet()
        except parakeet_install.ParakeetProviderError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(status, indent=2))
        return 0

    readiness = local_install.inspect_readiness()
    installed = bool(readiness["binary_installed"] and readiness["model_installed"])
    if installed:
        print("local already installed", file=sys.stderr)
    else:
        from solstone.think.providers import fit_report

        _render_fit_report(fit_report.build_local_fit_report(local_install.LOCAL_MODEL))
    try:
        status = local_install.install_local()
    except local_install.LocalProviderError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
