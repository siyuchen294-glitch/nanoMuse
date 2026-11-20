# The Android prompt assembly, and why not to patch it in the dex

Written while diagnosing a slow and expensive 1.0.9 install. Kept so the next
person does not repeat the same dead end.

## What the compiled app contains

classes.dex, in the obfuscated class that builds the system prompt, holds this
sequence:

    const-string v3, "\n  Runtime context:\n  - Current date: "
    const-string v4, " ("
    const-string v5, ")\n  - Device language: "

Those three constants are the runtimeContext string from
ChatViewModel.systemPrompt: current date and timezone, device language, and the
number of models the model-use tool can reach.

That string is handed to PromptCacheLayout.build as the runtimeContext argument,
which appends it after every stable and volatile section. The clock is the last
thing in the system message, not the first. A byte prefix cache therefore still
covers the whole stable head.

## The dead end

A patch that nulls the date and the timezone operands, and rewrites the header
into an instruction to run date in a shell, compiles, installs, and looks like a
fix, because the prompt no longer changes every minute. On a 1.0.9 build it buys
nothing: the cached prefix ended at the same byte either way. What it does cost
is the assistant's sense of time, which then depends on a tool call.

Byte offsets in an obfuscated dex also do not survive an upstream release, so
such a patch has to be redone for every version, which is exactly the kind of
maintenance a project should not accept.

## What to do instead

* Confirm the layout before changing anything: read PromptCacheLayout.kt and
  PromptCacheLayoutTest.kt in this repository.
* If a build really does place volatile text first, fix it in
  ChatViewModel.systemPrompt, stable sections first and runtimeContext last,
  and ship it. Do not patch the compiled app.
* For a build that cannot be rebuilt, put nmgate.py in front of the provider and
  leave the app alone.

## Reverting such a patch

Uninstall the patched build and install the release APK for the same version.
The user data directory survives only if the signature matches, so a re-signed
build loses app private state on the way out; the memory directory of the phone
sandbox is separate and can be exported first.
