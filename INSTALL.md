# installing solstone

these instructions are for a coding agent and human working together. solstone is the platform: sol is the app, and the journal is the memory it keeps. sol lives on your devices, experiences your day with you, and keeps it all in your journal — always private, only yours. open source, made by sol pbc.

**supported platforms:** linux (primary), macOS. windows is not yet supported.

the latest version of these instructions is at https://solstone.app/install.

## before you begin

### check whether solstone is already installed

```bash
sol --version 2>&1 && journal service status 2>&1
```

if `sol` isn't on PATH, the install hasn't been done yet — proceed.
if solstone is running and healthy, skip to [install sol on your devices](#install-sol-on-your-devices).

### prerequisites

linux: install `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and `ripgrep` (`rg`) from your distro package manager.

macOS: install xcode command line tools (`xcode-select --install`) and homebrew (https://brew.sh), then `brew install uv ripgrep`.

## install

most people install solstone to **run the journal here** — the full host that
transcribes, makes sense of your day, and holds everything:

```bash
pip install solstone-journal                                    # includes sol on PATH
uv tool install solstone-journal && uv tool install solstone   # journal tool + sol tool
pipx install solstone-journal && pipx install solstone         # same two-tool story
```

Pick one installer.

A pip install of solstone-journal puts `sol`, `solstone`, `journal`, and
`mlx-vlm-server` on PATH (`~/.local/bin/` for uv tool and pipx, which most
shells already include). uv tool and pipx expose each installed tool's own
commands — the journal tool provides `journal` and `mlx-vlm-server`, and the
thin solstone tool provides `sol` and `solstone`, which is why their install
lines above are two commands.

NVIDIA GPU owners who want GPU-accelerated transcription install
`solstone-journal-cuda` **instead of** `solstone-journal` with the same
installer command shape.

### just the `sol` client

`pip install solstone` (no extras) installs only the thin `sol` access client —
talk to a journal running **elsewhere** (a second machine, or a journal you
reach over your private link). it carries none of the journal host's AI/media
stack, so it's small and fast:

```bash
uv tool install solstone        # the sol client, on PATH
uvx solstone --help             # or ephemerally — no install, one-shot
```

Running the journal? Check the machine first: `uvx solstone check` renders a one-shot readiness verdict (GPU, memory, free disk) for the bundled local models — no install required. It exits 0 when ready, and prints exactly what's missing when not.

A thin/no-extras install carries only `sol` and `solstone`; `journal setup`,
`journal start`, and `mlx-vlm-server` require a `solstone-journal` host install.

## set up

```bash
journal setup
```

this runs the setup readiness doctor battery, confirms the journal directory at `~/journal`, installs the local transcription model (~2.5 GB on linux), installs the `sol` skill for claude code, codex, and gemini, installs the journal-side `sol` and `journal` router skills so sol can tend the journal, and starts a background service (systemd on linux, launchd on macOS) listening on http://localhost:5015.

let your human know: **open http://localhost:5015 in a browser**. the first-run wizard walks them through setting their identity and connecting a gemini API key.

a `solstone-journal` install bundles everything a journal host needs — PDF rendering, whisper, and the default CPU transcription stack are all included; `journal setup` downloads the transcription model. there are no separate à-la-carte extras to add. if the readiness doctor step (`journal doctor --readiness`) finds missing system libraries, it will tell you the exact install command to run for your platform.

Pick one of `solstone-journal` or `solstone-journal-cuda` — the CPU and GPU ONNX runtimes share the same files and must not both be installed. `journal doctor` reports whether the transcription runtime and model are ready.

This CUDA extra is only for transcription. The Linux local model provider uses Vulkan for screen analysis, so a hardware Vulkan GPU from AMD, NVIDIA, or Intel can work; CPU/software Vulkan devices are rejected instead of falling back silently. On AMD, the local model path runs through Mesa/RADV Vulkan, while transcription stays on the bundled CPU runtime.

if the service fails to start, check `journal service logs`.

## choosing how to power sol

sol is powered by an AI model, and you choose which. the choice has real privacy and hardware trade-offs worth understanding before you invest time in a path.

- **a hosted provider key is the recommended way to start.** point solstone at Google (Gemini), OpenAI, or Anthropic with **your own developer API key**, created in that provider's developer console — *not* the consumer chat product (gemini.google.com / chatgpt.com / claude.ai). this is the fastest path to a working sol and what the first-run wizard sets up. cogitate (sol's tool-calling agent loop, used by chat/digest/morning_briefing/etc.) works out of the box as soon as you set a provider key — no extra install step.
- **a local model via the local provider is a real, supported goal, but not the default daily experience yet.** running sol fully locally means nothing leaves your machine. it's the maximum-privacy path, but it needs capable hardware and a local model with strong "thinking" support; smaller models on constrained machines (for example a base Mac mini) struggle on the reasoning-heavy work. treat local as a goal to grow into, not the recommended starting point.
- **on Apple Silicon, you can run sol's screen analysis on-device today.** macs with Apple Silicon and at least 16 GB of memory can turn on the local provider in settings → providers; journal downloads a local model once, then does the work of making sense of your screen entirely on your machine, with nothing sent to a cloud provider. it's opt-in and covers screen analysis for now; the rest of sol stays on whichever provider you chose above.

a hardware heads-up: local transcription alone installs a ~2.5 GB model, and a capable local *thinking* model needs meaningfully more memory and compute on top of that. if your machine is constrained, start with a hosted key and revisit local later; you can switch any time in settings → providers.

what actually leaves your machine differs sharply between these paths: with a local model, nothing leaves; with a hosted provider, only that task's prompt plus the relevant journal context goes, directly to that provider under your own key. solstone is never a proxy, and sol pbc is never in that path and never sees it. for the full picture of what's sent, to whom, and under whose terms, see [what solstone sends](DATA-FLOW.md).

## install sol on your devices

your journal needs sol alongside it — sol takes in your day on each device and keeps it in your journal. each platform ships its own package (the engineering docs call these observers); install one for each machine you want sol on.

**macOS:** download the signed app bundle from https://solstone.app/observers and drag it to Applications. it pairs itself with the running journal on first launch.

**linux:**

```bash
pipx install solstone-linux
solstone-linux install-service
journal observer create laptop      # mint a key for this observer
```

`solstone-linux install-service` walks you through pointing the observer at the key you just minted. swap `laptop` for any name you'd like to identify this machine by.

**tmux terminal sessions:**

```bash
pipx install solstone-tmux
solstone-tmux install-service
journal observer create tmux-laptop
```

(for observer packages, `uv tool install solstone-tmux` is also fine if you prefer uv.)

## migrating from a pre-split install

If you installed the journal before the package split, run the one-time migration
line for your installer family, then run `journal setup`.

```bash
pip uninstall solstone-journal-host && pip install solstone-journal
pipx uninstall solstone && pipx install solstone-journal && pipx install solstone
uv tool uninstall solstone && uv tool install solstone-journal && uv tool install solstone
```

## upgrading

```bash
pip install --upgrade solstone-journal && journal setup
uv tool install --upgrade solstone-journal && uv tool install --upgrade solstone && journal setup
pipx upgrade solstone-journal && journal setup
```

Use the same installer family you used for install. The `uv tool` form upgrades both
the journal package and the thin `solstone` client, while the pipx form upgrades
the journal tool. For GPU transcription, upgrade `solstone-journal-cuda` instead
of `solstone-journal`. The `journal setup` step refreshes runtime artifacts and
reconciles the systemd/launchd unit if anything has changed.

## uninstall

1. remove setup-managed runtime files: `journal setup --clean-uninstall`
   this removes the user service, managed `~/.local/bin/sol` wrapper, user config, and setup manifest. it does not remove your journal.
2. optional: remove the installed `sol` agent skill: `sol skills uninstall`.
3. uninstall the python packages: `uv tool uninstall solstone-journal && uv tool uninstall solstone` (or `pipx uninstall solstone-journal`).
4. macOS only: drag `/Applications/solstone.app` to Trash.
5. macOS only, optional: remove observer app data and the parakeet model cache:
   ```bash
   rm -rf ~/Library/Application\ Support/solstone/
   ```
   this evicts the ~2.5 GB parakeet cache; reinstall will re-download it.
6. macOS only, optional: reset privacy permissions:
   ```bash
   tccutil reset Microphone app.solstone.observer && tccutil reset ScreenCapture app.solstone.observer
   ```
   or use System Settings → Privacy & Security.

## done

once it's running, sol experiences your day along with you and keeps it all in your journal — conversations transcribed, people and projects surfaced, a knowledge graph built, everything searchable at http://localhost:5015. your journal is one folder per day, always private, only yours.

source code: https://github.com/solpbc/solstone-journal
company: https://solpbc.org

## feedback

questions, feedback, or a bug? **follow and tag [@solstone.app](https://bsky.app/profile/solstone.app) on Bluesky** for discussion and updates, open an issue at https://github.com/solpbc/solstone-journal/issues for bugs, or reach support at https://support.solstone.app. you don't need to know anyone — those are the front doors.

(running into trouble or want to develop on solstone yourself? see [CONTRIBUTING.md](CONTRIBUTING.md).)
