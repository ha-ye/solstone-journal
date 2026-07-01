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
from solstone.think.utils import require_solstone

PARAKEET_DOWNLOAD_DISCLOSURE = (
    "parakeet-cpp fetches two external artifacts into this journal's provider "
    "cache before it can run: the parakeet.cpp server binary from github.com "
    "(MIT) and the speech model from huggingface.co (CC-BY-4.0)."
)


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
        print(json.dumps(parakeet_install.install_parakeet(), indent=2))
        return 0

    print(json.dumps(local_install.install_local(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
