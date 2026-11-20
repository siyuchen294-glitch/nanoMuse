# Cache friendly prompts, and small tools for phones and gateways

Notes and scripts collected while making nanoMuse cheaper to run against a
provider that bills a cached prefix at a lower rate (DeepSeek, and any OpenAI
compatible endpoint that reports prompt_cache_hit_tokens).

Nothing here changes nanoMuse itself. Every piece is optional.

## Why a prefix cache matters

Providers compare a request against the previous one byte by byte and stop at
the first difference. Everything after that point is charged as new input. If a
clock, a timestamp or a per turn recall list sits near the top of the system
message, the first difference is at byte zero and the whole history is charged
again on every step of every turn.

The Android and iOS apps handle this already: PromptCacheLayout keeps the safety
policy and the Hands ladder in a stable prefix, and appends memory, session
addenda and the runtime context afterwards. The Python runtime is the piece that
still puts a clock inside the template body.

## What is here

| File | What it does |
| --- | --- |
| nm_surgery_one.py | Runs inside the Python runtime. Splits the rendered system prompt into a stable head and a volatile tail, moves the tail into the last user message, replaces the front trimming of fit_context with block compaction, and records prompt_cache_hit_tokens. One file, standard library only, install and uninstall. |
| nmgate.py | An OpenAI compatible gateway for clients that cannot be patched. It reorders the request the same way, collapses duplicate messages and echoed sync blocks, and keeps a hit and miss ledger at /stats. SSE streams pass through unchanged. |
| phone-browser/ | Scripts that run a real Chromium inside an Android PRoot sandbox and expose it over CDP, for sites that reject headless fingerprints. Includes the environment quirks such a setup needs. |

## The Python runtime case

nanomuse/prompts.py has the line Current date/time, filled from
Agent.build_system_prompt on every call, inside SYSTEM_PROMPT, ahead of the
workspace, the tool list, the profile and the memory section. Agent then calls
context_messages, which keeps the newest N messages, so the oldest message drops
out each turn and the history shifts as well.

Two consequences, both visible in the ledger after the patch:

* between the clock and the trimming, almost nothing in a long conversation is
  ever a byte match;
* raising max_context_tokens does not help, because the extra history is charged
  at full price.

nm_surgery_one.py moves the clock and the per turn recall into a context block
inside the last user message, keeps the head byte stable between memory writes,
and replaces front trimming with a coarse block digest, so the request keeps
growing at the end instead of being rewritten at the front.

Commands, from inside the runtime environment:

    python3 nm_surgery_one.py --install     # writes site-packages/nm_surgery_one.py and a .pth
    python3 nm_surgery_one.py --check       # Agent mounted: True
    python3 nm_surgery_one.py --stats       # hit and miss ledger, per conversation state
    python3 nm_surgery_one.py --uninstall

The runtime keeps its own memory, sessions and config.toml untouched; the patch
only reads them. Removing the .pth file restores the original behaviour.

## The gateway case

nmgate.py is for a client whose request layout cannot be changed, an app build
without source, for example. Point the client's base URL at the gateway and it
does the same reordering on the wire:

    python3 nmgate.py --serve               # 127.0.0.1:8790, upstream https://api.deepseek.com/v1
    python3 nmgate.py --stats
    python3 nmgate.py --selftest            # stable head across three turns, SSE passthrough, ledger

Over USB, adb reverse tcp:8790 tcp:8790 lets a phone reach it on 127.0.0.1.

## The phone browser case

phone-browser/ covers the other reason a phone agent feels limited: the in app
browser is a WebView, which cannot be driven, and a headless Chromium is
fingerprinted by some sites. The scripts start Xvfb, run a non headless Chromium
with a persistent profile, and expose it over CDP, plus a helper that saves a
screenshot even when the MCP server refuses a file path.

Three things this environment needs, all measured:

* TMPDIR must point at /tmp. The default points into Android app storage that
  does not exist inside PRoot, and Chromium exits with Failed to create
  ProcessSingleton.
* There is no /dev/shm, so Chromium needs --disable-dev-shm-usage.
* x11vnc needs -noshm and -noxdamage, because PRoot does not implement SysV
  shared memory and the process exits at shmget.

## Android: already handled upstream

1.0.9 builds with PromptCacheLayout place the runtime context last, so patching
the prompt assembly in the dex is not needed on those builds.
android-smalipatch.md records what the prompt looks like in the compiled app and
why patching it there is the wrong lever.

## Licence

GPL-3.0-or-later, the same as the rest of the repository.
