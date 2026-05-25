# what solstone sends to your AI provider — and what it doesn't

solstone is local-first. your observers, your audio and screen, and your journal all stay on your machine, in plain files you own. this doc is the plain answer to the question a privacy-motivated owner should be able to *find* rather than *ask*: when solstone uses an AI model, what actually leaves your machine, who it goes to, and under whose terms.

short version: with a local model, nothing leaves. with a hosted provider, only the specific task's text goes — straight from your machine to that provider, under your own key and your own account. sol pbc is never in that path and never sees it.

## with a local model (Ollama): nothing leaves your machine

if you point solstone at a local model through Ollama, model calls go to Ollama on your own machine (`localhost`) and stay there. no API key, no network call to any provider, nothing to sol pbc. this is the maximum-privacy path: the model runs where your data already is.

(transcription is also local — solstone installs a local transcription model during setup, so your audio is turned into text on your machine, not sent out to be transcribed.)

## with a hosted provider (Google / OpenAI / Anthropic): only that task, only to them

if you connect a hosted provider, solstone sends — for each task it runs — that task's prompt plus the journal context relevant to *that task* directly to that provider's API, using **your own API key under your own provider account**.

- it is per task, not a bulk upload. for an analysis task that's the transcript or screen text being analyzed; for chat it's your prompt plus the relevant slice of your journal for that turn. solstone does not ship your whole journal anywhere.
- it goes **straight from your machine to the provider**. solstone does not proxy model calls through any sol pbc server — ever. sol pbc is never in the middle and never sees the request, the content, or the response.
- it uses **your key, your account**. you create the key in the provider's own developer console; solstone just stores it locally and uses it. the relationship is between you and the provider.

## what solstone never sends — to sol pbc or anyone

**what the product collects: nothing extra.** no telemetry, no analytics, no usage tracking, no crash phone-home. there is no sol pbc endpoint in the model path and none anywhere else in the product — this is verifiable in the code.

**what the corporation is bound to, irrevocably.** sol pbc cannot sell, license, share, or otherwise hand your data to any third party — and that holds even for aggregate, anonymized, or de-identified data, which most companies carve out and sol pbc explicitly does not. no targeted advertising. no behavioral profiling of you — ever. this is not a setting you have to find and switch off, and not a policy that can be quietly revised: it's an irrevocable covenant in sol pbc's articles of incorporation (Article 8), binding on sol pbc and any successor. it cannot be weakened by a future release, a policy update, an acquirer, or anyone.

## what happens to it then is governed by *your* agreement with the provider

solstone can tell you exactly what it sends. it cannot change what the provider does with it once it arrives — that's set by the agreement between you and that provider, on the account whose key you used. that is why your-key-your-account matters:

- with your own Anthropic key, the request is governed by **Anthropic's developer API terms** (the console key from `console.anthropic.com`), **not** the consumer `claude.ai` chat terms — and it's your account, so that boundary is yours to set, not ours.
- with your own OpenAI key, it's the **OpenAI platform/API terms** (`platform.openai.com`), not the `chatgpt.com` consumer terms.
- with Google, a key tied to a billing account is the privacy-maximizing hosted path under the **Google AI / Gemini API terms** — see the in-product note when you add a Gemini key.
- with a local model, none of this applies, because nothing leaves.

each provider states its own data-use and retention terms; because you bring your own key, those terms — and any controls the provider offers — are yours to read and set directly:

- Anthropic (developer API): https://www.anthropic.com/legal/commercial-terms and https://privacy.anthropic.com
- OpenAI (platform/API): https://openai.com/policies/row-terms-of-use and https://platform.openai.com/docs/guides/your-data
- Google (Gemini API): https://ai.google.dev/gemini-api/terms

solstone's job is to make the choice — and its consequences — legible. the choice itself is yours. that the provider choice is yours is the whole point of how this is built.

## the deeper story

this data-flow posture isn't a feature decision that could be reversed next quarter — it's downstream of how sol pbc is legally structured. the no-selling, no-sharing, no-targeted-advertising commitments on your data are irrevocable covenants in the articles of incorporation, binding on sol pbc and any successor. if you want the structural-trust story behind why solstone is built this way: https://solpbc.org.

---

*solstone is open source (AGPL-3.0). every claim in this doc is verifiable in the code at https://github.com/solpbc/solstone-journal — the provider call path is `solstone/think/providers/`.*
