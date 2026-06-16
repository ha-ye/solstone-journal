# Changelog

All notable changes to solstone (the Python package) will be documented in this file.

Format adapted from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), aligned with `cmo/brand/changelog-voice.md`.

## [0.6.5] - 2026-06-16

### Changed
- the local web interface no longer has its own password, login page, session cookie, or localhost-trust switch. once setup is complete, the local interface serves directly on the journal machine; linked devices continue to use their paired-device identity through link.

## [0.6.4] - 2026-06-15

### Added
- `sol link serve --direct` keeps a linked machine's traffic on your private link's direct path and never falls back to the relay — useful when the journal's machine is reachable on the same network, so there's no dependency on the relay.

### Fixed
- an observer on a machine you've linked to your journal can now send everything it takes in over your private link, no matter the size. before, anything larger than a small message stalled partway and never arrived, so those observers couldn't finish uploading and uploads timed out; now they go through reliably. a separate cleanup issue that could leave a stale connection behind after a reconnect is also resolved.

## [0.6.3] - 2026-06-15

### Fixed
- a machine that reaches your journal over your private link now stays connected. before, that connection would go quiet after a minute or two and not recover on its own, so observers on that machine stopped reaching your journal; it now sends a steady heartbeat to stay alive and reconnects automatically if it ever drops, so they keep streaming without interruption.

## [0.6.2] - 2026-06-15

### Changed
- an observer running on another machine you've linked now streams to your journal over your private link. each observer is identified by its own handle, kept fully separate from the machine's link identity, so the two can change independently and one never stands in for the other. observers running on the same machine as your journal are unaffected. what your observers take in, and where your journal lives, are unchanged.

## [0.6.1] - 2026-06-15

### Changed
- desktop observers now use the journal's current callosum connection path,
  with the observer key kept in the authorization header.

## [0.6.0] - 2026-06-14

### Added
- you can now run sol from one machine against a journal that lives on another. once you've paired the two over your solstone private link, `sol chat` and `sol call` reach the remote journal the same way they reach a local one, with live progress and answers streaming back as sol works. a machine set up this way runs its own observers and its own `sol`, while its journal stays on the machine that holds it.
- managing your paired devices now has its own home in `journal link`: list what's paired, check a device's status, and unpair one when you're done with it. `sol link` stays the device-side command for joining and serving a link.

### Changed
- installing solstone now gives you the lightweight `sol` client by default. `pip install solstone` (or `uv tool install solstone`, or `uvx solstone`) installs a small client that talks to a journal running elsewhere, without the full set of components a journal host needs. to run a journal on this machine, install `solstone[journal]` for the full stack, or `solstone[journal-cuda]` for the version that uses your graphics card for transcription. the macOS app is unaffected; it still includes everything to run a journal.
- your journal's local web and api address is now reachable only from the same machine. the older option that opened it to your network behind a password is gone. to use a journal from another device, you now pair that device over your solstone private link, the same pairing flow as before, rather than opening a port. this is a tightening of how a journal can be reached; what your observers take in, where your journal lives, and what leaves the machine are all unchanged.

## [0.5.6] - 2026-06-14

### Added
- the backup page now lets you choose where your encrypted backup lives. keep using your own bucket as before, or pick solstone hosted, which is on the way and shown as coming later with a clear path back to your own bucket in the meantime.

### Changed
- the backup page got a clearer pass over its layout and wording, the routine actions sit apart from the one that erases a backup, and the page now carries the same naming as the rest of solstone, including the `journal backup` terminal command's help text.
- the link page now matches the rest of solstone's look. the pairing button and links on that surface read in the warm palette instead of an off-tone blue, so the page reads as one piece.

### Fixed
- pairing a new device over your solstone private link now completes instead of stalling. before, the join would reach your journal but hang for about half a minute and then give up; it now connects cleanly, with a plain one-line message if anything goes wrong. what travels and where your journal lives are unchanged; this is the connection itself finishing the way it should.
- the recovery-key Copy button on the backup page now copies your key, where before it could copy a stray bit of internal text. some loose setup labels that were never meant to show are gone too.
- when sol's background thinking is cut short at a budget or step limit, that run now closes honestly and its cost shows up on your spending view like any other. before, a cut-short run could sit in limbo and its spend never appeared, so your spending view was lower than what was actually used. a cut-short run now reads plainly as cut short rather than as a clean finish, and keeps whatever partial work it had.

## [0.5.5] - 2026-06-14

### Added
- before sol reaches solstone support on your behalf, it now asks first. reaching support is the one moment something may leave your machine, so sol offers it in the chat bar and waits for your yes; decline and the conversation stays entirely local. when you do say yes, the chat bar shows the reach-out plainly as it happens, and the offer survives a reload so you never lose your place.
- there's a new Thinking app in your journal that owns how sol thinks. provider, key, and on-device setup all live there now, with three clear lanes: become a solstone scout, bring your own key for Claude, Gemini, or GPT, or run local thinking models on your own device. first-run setup now routes you straight into Thinking to pick your lane instead of asking for a single key up front.
- you can now point the local (on-device) option at your own OpenAI-compatible server instead of the bundled engine. when you configure an endpoint, sol's generation and background thinking run there, including screen content for vision; transcription always stays on your machine, and there is no silent fallback to the cloud. it's your server, by your choice, and the local card spells out plainly what goes where.
- linux machines with an arm64 GPU (like the NVIDIA DGX Spark / GB10) can now run the local on-device option, and on-device transcription defaults to whisper on those boxes. previously these setups had no on-device path for sol's thinking.

### Changed
- pairing a new device over your home network is smoother and clearer. a brand-new device on your local network now reaches the pairing step to earn its credentials, the same step it always had to pass; it still has to present your one-shot pairing code, can only talk to the pairing endpoint, and your journal stays local-only until you pair. the pairing command now hands you a ready-to-use link to paste on the other device, and a refused or mistyped attempt now tells you exactly what went wrong instead of a generic error. a paired device also keeps both the name you give it and its own hostname, so they no longer overwrite each other in the list.
- setting up how sol reaches the world is reorganized around the apps that own each piece. Thinking owns scout and your provider setup, Link owns your solstone private link, and the old "your services" switchboard is gone; Settings now opens to a simple guide that points you to each app. the Thinking app also reads scout's real status directly (requested, invited, on), with a "check now" option, instead of guessing from local state.

### Fixed
- the web app's search summary now counts results correctly, reading "1 result" or "2 results" as it should.
- internal stability improvements.

## [0.5.4] - 2026-06-12

### Added
- you can now back up your journal to your own cloud storage, encrypted before it ever leaves your machine. set up a destination you own (an S3 or B2 bucket), and solstone keeps an encrypted copy there on a schedule, with restore and prune built in. you hold the keys: a daily key that runs the backup and a separate recovery key you save somewhere safe, and you need the recovery key to restore. nothing routes through sol pbc, ever; the backup goes straight from your machine to your bucket and back. set it up from the new backup page in your journal, or from the terminal with `journal backup`.
- your journal now has a "your services" page that gathers the optional extras you can turn on, like scout and your private link, in one place. each one shows whether it's on and lets you turn it on or off from inside your journal, with backup linked from there too.
- on-device transcription can now tell voices apart on its own. when the local engine handles your audio, it now labels who's speaking the same way the hosted option already did, so a conversation reads as a back-and-forth rather than one undivided block. this runs entirely on your machine; nothing new leaves it.
- the curation page now suggests when two speaker names look like the same person, alongside the facet and entity merges it already offered, so tidying up who's who has one clear place to happen. it only ever suggests; you decide.

### Changed
- solstone no longer includes todos, skills, routines, or the daily digest. these came out as a deliberate narrowing of what solstone does. anything you'd already saved under them stays untouched on disk; nothing is deleted.
- on Linux, the on-device (local) option now runs on your graphics card instead of the processor. it needs a usable GPU; a Linux machine without one uses a hosted option instead. on macOS, the local option runs on Apple's MLX, as before.
- turning on solstone scout is now a single sign-in link that works the same on every machine, including headless ones. the older code-based path could stall or report a confusing failure on some setups; that path is gone.

### Fixed
- a computer running more than one observer no longer blends them into a single stream. if you ran, say, a desktop observer and a terminal observer on the same machine, they could collapse into one and overwrite each other; each observer now registers itself and keeps its own identity. this happens locally on your machine; nothing new leaves it.

## [0.5.3] - 2026-06-10

### Added
- whether your journal is reachable over your local network is now a setting in journal settings, alongside everything else. it used to live only behind a command-line flag. the default is unchanged: your journal stays local-only until you turn this on.

### Changed
- your journal's own config is now the single source for your managed provider keys. a key set only in your shell environment, and not in your journal config, is no longer picked up. this keeps where your keys come from predictable, and your journal config the one place that decides. nothing about what leaves your machine changes.
- the words across health, settings, and sol now read "talents" where they used to say "agents," with a few labels clarified along the way (disk usage, error counts, and a stray "None" that no longer shows up on the command line).

### Fixed
- search in the web app works again. every search was failing with a generic error; this resolves the underlying cause, and results come back as they should.
- importing a transfer or peer archive now refuses anything that would write outside your journal folder. a malformed or hostile archive is turned away whole, writing nothing — your journal folder stays the only place an import can land.
- marking a todo done or cancelled when it was already finished is now refused with a clear message, instead of leaving it recorded as both at once.
- an entity active right now no longer shows "Last active: Tomorrow" in the evening. the day is computed on your local time, so it reads correctly.
- the chat box no longer shows a provider-setup notice as its placeholder on every page, and a day with nothing in it now gets the same friendly empty state as the other timeline views.
- on macOS, installing or upgrading the journal service no longer fails on a slow machine. some upgrades could stop on an input/output error while the old service was still unloading; this now waits and retries instead of giving up.

## [0.5.2] - 2026-06-05

### Changed
- terminal `sol chat` now runs through the same chat that the web app uses, so you get the same answer whichever way you ask. it shows its progress as it works and prints the final answer at the end.

### Fixed
- on-device transcription no longer splits words apart. transcripts from the local engine were coming through with letters scattered ("Tak ing out the m one y" instead of "Taking out the money"); new transcriptions now read as natural words and sentences. anything already stored garbled needs a fresh re-transcription to clean up.
- asking sol about your own journal now gets an answer. questions that look something up in your memory, like past conversations, your notes, a quote, or your name, used to get declined; sol answers them now. reflection-style questions are unchanged.
- on macOS, setup and upgrade no longer stall at the readiness check. a diagnostic step could take long enough to time out on a fresh or cold machine; it now runs in a fraction of a second.
- syncing a folder of audio now tells you what it couldn't read. `sol import --sync audio` used to fold an unreadable file in with the ones it intentionally skipped; an unreadable file is now called out on its own line, with a reason, and the per-file list shows up with `-v`. `--auto` guidance also works alongside `--sync --save` now.
- on Linux, installing or reinstalling the journal service no longer removes other services you set up. if you had your own solstone-related units, like a desktop observer or your own backup job, an upgrade could delete them. that cleanup step is gone.

## [0.5.1] - 2026-06-05

### Fixed
- macOS installs now get the same clean journal package as every other platform. the Apple Silicon wheel no longer carries retired internal talent prompts from an earlier build, so provider readiness stays available after install.

## [0.5.0] - 2026-06-05

### Added
- you can now point solstone at a folder of audio files and keep it in sync with your journal. `sol import --sync audio --path <folder>` shows what is new, and `--save` imports only what has not already been brought in.
- sol now surfaces suggested facets and entity merges for review, so organizing the journal has a clear place to happen.

### Changed
- provider readiness is now one signal across Settings, Health, support diagnostics, chat, and the command line. when a provider or model is blocking work, solstone shows the affected task and the next step instead of scattering the reason across pages.
- on Apple Silicon, the local setup path now matches the on-device engine the journal actually runs, including the right memory requirement before activation.
- transcription setup is more careful about memory. when the local model is a poor fit for the machine, solstone says so up front and points to a hosted option instead of trying to push through.

### Fixed
- provider settings load cleanly from a direct link and show each task's defaults.
- audio folder sync retires missing skipped files when the source folder changes, so a dry run does not keep warning about files that are no longer there.

## [0.4.10] - 2026-06-02

### Added
- pdfs and still images that come in with your day are now read into your journal and made searchable, the same way a transcript is. sol can find what a document or an image actually contains, not just that one was there. on a scanned pdf with no selectable text, sol reads the page itself. nothing new leaves your machine, and what's kept on disk is unchanged.

### Fixed
- if you run the on-device option on a Mac, it works again. on apple silicon, on-device thinking and vision had started turning away every request, so the day's processing stalled; this resolves it. and if the on-device engine ever restarts, your journal now catches up on anything it missed while it was down.
- solstone no longer tells you an update is available when there isn't one. if you were already on the latest build, or running a pre-release or development build that's ahead of it, the check could nudge you for nothing. it now points to an update only when a genuinely newer published version exists.

## [0.4.9] - 2026-06-02

### Fixed
- upgrading on macos is steadier when the journal service is already running. setup waits for the old service to finish unloading and uses the service's real start time, so a healthy upgrade does not stall at readiness.
- solstone raises the file limit for the installed journal service, giving long-running journals more room for observers, local providers, and background work.
- revealing a provider key in settings is now only visual. clicking the eye button no longer triggers an unwanted save, validation, or clear.

## [0.4.8] - 2026-06-01

### Fixed
- macos setup now tolerates a launchd race where the journal service starts and becomes healthy even though `launchctl kickstart` reports a transient error. setup trusts the supervisor readiness marker, so an upgrade can continue to observer registration instead of stopping at "service up failed".

## [0.4.7] - 2026-05-31

### Fixed
- upgrading over an older install no longer stops because the `sol` or `journal` shortcut in your shell points somewhere stale. setup now repairs the shortcuts it owns and keeps going, whether solstone came from the macos app or from the terminal.
- sol's background thinking can ask the journal for identity, routines, health, and talent context again. those approved journal tools were being turned away before sol could use them; now they work without widening what sol is allowed to run.
- fresh installs from PyPI resolve cleanly when pip chooses the dependencies. solstone now pins the matching telemetry packages used by sol's thinking runtime, so install no longer lands on an incompatible mix.

## [0.4.6] - 2026-05-31

### Added
- you can now re-run sol's thinking on any day from the page, either "process now" to pick up where it left off or "redo from scratch" to start the day over. the same is available in the terminal with `sol reprocess <day>`.

### Changed
- your journal now tells you plainly whether it's caught up. the stats and health pages show an honest "is my journal caught up?" answer plus a "days that need a hand" list for any day it can't finish on its own, like one with corrupted data or a step that keeps failing. catch-up runs on its own in the background, never leaves older days behind, and `journal doctor` reports the same answer from the terminal.
- the on-device option is now a single "Local (on-device)" choice on both macOS and linux. on a Mac it now runs entirely on your machine, including sol's thinking, so the local-only path covers more of your journal without anything leaving your machine.
- on linux, the default on-device transcription now works the moment you install, with no extra to add. the runtime ships with the install and `journal setup` fetches the model. owners with an NVIDIA GPU can still opt into a GPU-accelerated build, and `journal doctor` now reports whether the default transcription runtime and model are ready.
- local journal commands now live with the journal service. things like navigating, routines, identity, on-device provider install, health, and stats moved from `sol` to `journal` (for example `journal navigate`, `journal health`, `journal reprocess`). the old `sol` forms now point you to the new one.

### Fixed
- your journal no longer shows finished work as still pending. days that had an earlier error but later completed were being counted as outstanding, so the backlog looked larger than it was. the count now reflects what's actually still incomplete.

## [0.4.5] - 2026-05-30

### Added
- you can now reach your journal from your phone or laptop even when they aren't on the same network as your home machine. setup lives at the connections page, which is now the single home for how you connect, your network access, and your paired devices. pairing shows a fresh code, lets you name each device, and lets you see and remove any device with one tap.
- the local model that runs entirely on your machine can now take in images as well as text, so the on-device option works on more of what's in your journal. nothing new leaves your machine.

### Changed
- the local model is now kept running for you in the background instead of starting up on demand, so it's ready the moment sol needs it. fresh installs launch it reliably the first time, and a model download now shows real progress instead of sitting at 0 percent through several gigabytes.
- diagnostics are now two clearer commands. `sol doctor` checks that the `sol` command itself is working, from anywhere. the new `journal doctor` checks the health of your journal and its background service. each asks only the question that fits where you run it, so neither raises a false alarm.
- the entities and devices views read more clearly: plain empty states when there's nothing yet, a retry when something fails to load, and detaching a facet now spells out what will happen and offers a one-tap way to undo it.

### Fixed
- your journal now shows when a moment has been transcribed but not yet thought through, instead of looking finished, and catches those moments up on its own. day-by-day status and the transcripts view reflect this honestly, so nothing sits half-processed without you knowing.

## [0.4.4] - 2026-05-27

### Changed
- when sol is catching up on a backlog, today's thinking no longer waits in line behind it. on a busy journal, or right after an install, the day's catch-up work and sol's thinking on fresh observations now run alongside each other, so new moments get attended to in seconds instead of waiting hours.

### Fixed
- transcripts come through on every audio format again. if you run a transcription backend other than whisper, some audio was making it into your journal but quietly producing no transcript. this resolves it, so the moments you spoke are written down the way you'd expect.
- upgrading from an older install no longer trips a setup check. if you first installed solstone a different way and then moved to the current method, `sol doctor` now adjusts the older `sol` and `journal` shortcuts for you instead of stopping. if you hit this, this resolves it.

## [0.4.3] - 2026-05-27

### Added
- a dedicated reader for facet newsletters at `/app/news/`. reverse-chrono index across all your facets, per-day detail with a copy button and a pdf download, and a sample newsletter so you can see the shape before your first one lands. newsletters sit next to reflections in the sidebar.

### Changed
- the participation tab on an activity now shows a structured list of people, grouped into attendees and mentioned, with a short note next to each name about how that person showed up in the activity. low-confidence entries appear muted with a "less certain" tag, and empty or unavailable states read in plain language instead of raw json.

### Fixed
- weekly reflection writes a full reflection to your journal again. on busy journals it was running out of room mid-gather and either saving nothing or saving only a short summary; both paths are resolved, and the reflections page renders again.
- attendee lists are stricter about who counts as an attendee. someone whose name only appeared in a transcript, without other corroboration, is now demoted to mentioned rather than surfaced as an attendee. reported by Ryan during a walkthrough.
- background work sol runs through google (morning briefings and other scheduled talents) no longer fails silently on a size limit. a request-budget calculation was landing one over the supported maximum, rejecting every call on the default settings; the calculation is corrected.
- sidebar labels in the expanded menu no longer truncate. entities, transcripts, and other longer labels show in full at narrower window widths. reported by Ryan.

## [0.4.2] - 2026-05-26

### fixed
- on a fresh install, `journal setup` could stop on a doctor check that flagged the `sol` command on your machine as out of place — even when it was the one journal had just put there. if you hit this setting up 0.4.1, this resolves it.

## [0.4.1] - 2026-05-26

### fixed
- some 0.4.0 installs didn't come back up after upgrading — sol wouldn't start, and journal commands timed out. this resolves it.

## [0.4.0] — 2026-05-26

### changed
- **service commands moved fully to `journal`.** Service commands (supervisor, cortex, heartbeat, setup, transcribe, services, etc.) are no longer surfaced under `sol` — they live exclusively under `journal`. Your existing solstone service migrates itself automatically on the next service restart; no action needed.
- `journal start` is now the canonical run command (replaces `journal supervisor` as the service-unit entry point — old units self-migrate).
- the `sol` CLI continues to be your day-to-day surface (chat, call, top, import, search across the journal).
- cogitate is now baseline — the openhands-sdk runtime that powers sol's tool-calling agents ships in the wheel, so a fresh install with a hosted provider key runs cogitate immediately with no extra install step. wheel size grows by about 337 MB on install to carry openhands-sdk, litellm, and their transitive dependencies.
- minimum python is now 3.12 (was 3.11) — required by the openhands-sdk runtime that ships baseline. if you installed solstone with a 3.11 interpreter, reinstall under 3.12+ before updating.

### removed
- `sol <service-cmd>` paths typed by a human now redirect to `journal <cmd>` with a clear error and exit non-zero. Service units still pointing at the old paths self-migrate; nothing on disk breaks.
- the built-in `sol observer install` command is gone. linux and tmux observers now install from their own published packages: `pipx install solstone-linux` (or `solstone-tmux`), `solstone-linux install-service` (or `solstone-tmux install-service`), then `sol observer create <name>` mints a key you give the observer. the macOS observer continues to come from the signed app bundle at solstone.app/observers.
- the bundled per-provider install commands are gone — `sol call settings providers install` now accepts `local` only (cogitate runs out of the box for hosted providers with a key set), and `uninstall`/`disable`/`enable`/`validate-key` are removed entirely. local install continues to work via `sol call settings providers install local`.

## [0.3.10] — 2026-05-26

### Added
- **journal CLI** — `solstone` now installs two CLI binaries: `sol` (the day-to-day surface for talking to your journal — chat, call, top, import, etc.) and `journal` (host operations — supervisor, setup, install-models, the daemons that tend your journal). `sol --help` shows both surfaces; `journal --help` shows just the host commands. Existing `sol <cmd>` invocations all keep working. Internal docs and scripts use `journal <cmd>` for host operations going forward.

## [0.3.9] - 2026-05-25

### Added
- solstone now has a "services" layer for the optional cloud-backed extras sol pbc offers alongside your local solstone. today that means solstone scout, the alpha-tester program that provisions a Google Gemini key for you and unlocks scout-only features. services are off by default; you turn them on from `services.solstone.app` or `sol services enable scout`, and solstone itself still runs entirely on your machine.
- you can now move days or whole journals between your own machines, and connect an observer on one machine to a journal on another, over a direct private link between your devices. `sol link join` pairs them; `sol transfer send --to <peer>` and `sol export --to <peer>` push from one to the other. revoking a paired device at the `/link` dashboard, with `sol observer revoke`, or with `sol call link unpair` cuts the connection at TLS the moment you revoke.
- a new "Local (on-device)" provider runs sol from a bundled `llama-server` on your own machine with a pinned Qwen model. zero-egress: when sol is set to local, it never falls through to a cloud provider.
- a new daily `journal/identity/health.md` surface tells you whether solstone is OK at a glance. sol reads its own signals, auto-recovers from things like stuck transcripts, and the home page and morning briefing now read its summary.

### Changed
- a few of the surfaces you touch most are now more direct. creating a facet lands you on a real detail page that confirms what you just made and offers next steps. clicking a "needs you" item on the home page opens a fresh chat with editable starter text already in the box (not as ghost placeholder), and sol knows which item you came from. each modality on segment-detail pages has its own "analyze now" button so you can re-run analysis on one part of a day without dropping to a terminal. the health, tokens, and service-log pages were rebuilt around a glance row that answers the first question (is solstone OK, is this costing too much, where did the pipeline fail) with the detail kept under progressive disclosure; service log lines now carry severity colors with screen-reader announcements on errors.

### Fixed
- segments that were already analyzed sometimes painted as still-pending on the day timeline; they now render correctly. audio playback on segment pages now shows the real duration and the right format, transcript lines no longer carry a doubled timestamp, the day view scrolls naturally on short windows, and a cold-load race on transcripts pages is resolved. internal stability improvements across providers install, background tasks, and the convey wizard.

## [0.3.8] - 2026-05-22

### Added
- you can now run sol's on-screen analysis fully on your own Mac. on Apple Silicon with at least 16 GB of memory, "MLX (Local, Apple Silicon)" appears in Settings under Providers; choose it once, sol downloads a local model in the background, and from then on the part of sol that makes sense of your screen runs on your machine, with nothing sent to a cloud provider. it's opt-in and covers vision today; the rest of sol stays on whichever provider you've chosen.
- you can now power sol with Anthropic or OpenAI without installing anything extra. choose the provider in Settings and solstone sets it up for you, with no separate command-line tool to install first. running on a hosted Google key needs no extra setup either.
- `sol setup --clean-uninstall` removes the pieces setup added to your machine, behind a confirmation that lists exactly what it will remove. your journal is never touched.

### Changed
- the timeline view is rebuilt. it opens straight into your real journal, fits any window from a phone-width pane to a wide desktop, and every entry shows which AI produced it with a link to that day. when sol finishes summarizing a new day, the view updates on its own.
- long todo lists now load fast and stay readable: solstone shows the most recent items first with a "show more" control for the rest, instead of rendering everything at once.
- api keys in setup and Settings are now masked as you type, and the validate button tells you plainly whether the key connected or failed.
- on Linux, bringing an observer online no longer needs git or a build step on the host; observers now install straight from their published packages.

### Fixed
- video and audio in your journal that showed "format not supported" now play. some entries with video or audio hit this; it's resolved.
- on installs from PyPI, sol's meeting-screen analysis was coming back as freeform notes instead of the structured entries it was built to produce. the missing piece now ships with the package, so meeting frames return to their intended shape.
- transcription that gave up on a long, dense stretch of audio now retries and recovers, so days that previously failed to transcribe complete. this also recovered a backlog of past days that had errored.
- pages that occasionally didn't finish loading now load cleanly.
- on some machines the background service could stop overnight and not restart; it now restarts as intended.
- pairing a phone by QR code now works in Safari on iPhone and Mac, where the code could previously render too small to scan.
- internal stability improvements, plus quieter local logs.

## [0.3.6] - 2026-05-18

### Changed
- solstone now uses each provider's current models, and the structured results sol asks providers for are validated the same way across every provider, including the backup one. this makes the AI features more reliable, with no change to how you use solstone.

### Fixed
- in some cases what sol wrote to your journal from a screen could be off. a frame with little on it could pick up names from your own contacts as if they'd been on screen, and an occasional runaway from the model could write a long block of repeated text into an entry. both are now caught before anything is written, so your journal reflects what was actually there.
- when sol fell back to a backup AI provider for a task that involved an image, the image could be left out of the request, so the result was a confident guess instead of something grounded in what was on screen. images are now sent correctly on every provider, and structured results from the backup provider are read correctly.
- upgrading solstone over an existing install now works cleanly. before, an upgrade could stop partway: setup could wrongly report that port 5015 was in use when it was solstone's own running service, and re-registering this machine's observer could fail as "already exists." if an upgrade left you stuck, this resolves it.

## [0.3.5] - 2026-05-17

### Added
- a new data-flow page explains, in plain language, what solstone sends to your chosen AI provider and what never leaves your machine. it covers local-first processing, that each task is scoped (not your whole journal), that the keys and the account are yours, and the things sol pbc is bound never to do with your data. it's linked from setup, the install guide, and the readme so you can read it before you connect a provider.
- the install guide now has a section on how to power sol: starting with a hosted provider key is recommended, running fully local is a real supported goal but not yet the default daily experience, with a heads-up on the hardware that takes. setup and the api-key settings now also tell you, per provider, to use a developer api key from the provider's console rather than your consumer chat login, with the right console link for each.

### Changed
- in-app support and feedback now point to support.solstone.app, and that's the default in support settings for new journals. if your settings still point at the old support address, nothing breaks and you can leave it as is. setup, the install guide, and the readme now also lead with following and tagging @solstone.app on Bluesky for feedback, then GitHub issues, then the support site.

## [0.3.4] - 2026-05-16

### Added
- a fresh journal now opens with a useful set of starred apps in the nav rail instead of a blank one. if you've already arranged your own starred apps, your choices are left exactly as they are.

### Changed
- the deprecated `precision` setting for parakeet transcription has been removed. `quantization` (auto, fp32, or int8) is the setting to use. if your journal config still carries the old `precision` line it's now simply ignored, with no change to how transcription runs.

### Fixed
- browsing back from the all-facets entity edit view now returns you to the entity you were looking at, in the same facet. before, back could land you on a different view.
- the bundled `transcripts read` documentation now shows the correct options. the previous example listed the wrong units for `--start` and `--length`, so following it as written would have failed.

## [0.3.3] - 2026-05-16

### Added
- a validate button now sits next to the gemini api key on the setup page, so you can confirm the key works before finalizing.

### Changed
- the setup page is reworked: cleaner typography, retention preferences as three explicit choices (always keep, keep for a set number of days, don't retain), enter-to-submit from any field, and your journal version and path surfaced up top.
- a fresh `sol setup` now installs the solstone bundle into all three coding-agent configs (claude, codex, gemini) at once, and lands the per-talent skill files in your journal so sol's sub-agents can find them.

### Fixed
- the setup page works end-to-end on a fresh install. earlier builds had a silent javascript bug that left the validate button, retention radios, and finalize submit unresponsive.
- on macos, your local timezone now resolves correctly on first setup. earlier installs could land in utc because the resolver missed where macos stores zone data.
