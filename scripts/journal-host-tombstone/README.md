# solstone-journal-host (tombstone)

Retired distribution. The journal moved into its own packages:

    pip install solstone-journal          # the journal (CPU)
    pip install solstone-journal-cuda     # the journal on NVIDIA CUDA

This project is a build-fails-by-design tombstone: any install/build/metadata
operation exits nonzero with a migration message. It is **not** a uv workspace
member and is published exactly once, at the split release, with:

    SOLSTONE_TOMBSTONE_ALLOW_BUILD=1 python3 setup.py sdist

so that PyPI carries a final `solstone-journal-host==0.7.0` that fails loudly for
anyone still on the old spelling.
