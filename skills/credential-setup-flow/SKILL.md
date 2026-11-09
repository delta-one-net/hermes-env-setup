---
name: credential-setup-flow
description: "Use when setting up integrations that need credentials/ENV."
version: 1.0.0
author: Charis
license: MIT
metadata:
  hermes:
    tags: [setup, credentials, env, integrations, env-setup]
---

# Credential Setup Flow — env_setup tools over everything else

## When to Use

Any task where an integration, adapter, provider, or feature needs Zugänge:
API keys, tokens, passwords, SMTP/IMAP credentials, bot tokens — anything that
ends up in an ENV variable / `.env`. Examples:

- "Richte mir die Mailanbindung ein"
- "Verbinde Hermes mit Telegram"
- "Der OpenAI-Key fehlt" / "configure the email gateway"

## The rule

**Trigger IMMEDIATELY, request everything in ONE form, NEVER ask in chat.**

1. The MOMENT the user names an integration or setup task that needs credentials
   ("ich will ein Mailpostfach integrieren", "connect Telegram", "der Key fehlt"),
   call `env_setup_prompt` right away — one short sentence in chat, then the tool
   call. The form pops up as a modal dialog on the user's screen automatically;
   do not explain the mechanism first, do not ask whether they want a form.
2. **Ask for ALL fields the task needs in ONE call** — the form is adaptive to
   the task, that is the point:
   - Mailbox → address, password, IMAP host, ports (everything the integration
     needs, one form, done)
   - API key where you already prepared the rest (you know the service, the
     endpoints, the config) → just the key variable(s), nothing else
   - Derive the ENV names from the integration's documented contract; give
     each field a human `label`, a short `description`, an `example`, and `url`
     (where to get it); `secret: true` for passwords/keys/tokens.
3. Check first when unsure: `env_setup_status` with `variables: ["X", "Y"]`
   tells you which of those are already set — never re-request existing ones.
   Without `variables` it lists missing credentials declared by installed
   plugins (their requires_env).
4. On `resolved`: the values are LIVE in the process. Continue the setup
   immediately — do NOT ask the user what they typed, do NOT read values back.
5. On `cancelled`: accept it, do not re-prompt right away. Offer alternatives
   (user sets them manually in .env later; ask what they want to do).
6. On `timeout`/`cancelled`: the form stays reachable in the desktop app
   (🔐 chip). Call `env_setup_status` later to pick up a late completion before
   re-prompting.

## Common variable contracts (orientation, not a catalog)

- Hermes gateway email adapter: `EMAIL_ADDRESS`, `EMAIL_PASSWORD`,
  `EMAIL_IMAP_HOST`, `EMAIL_IMAP_PORT`, `EMAIL_SMTP_HOST`, `EMAIL_SMTP_PORT`
- Telegram bot: `TELEGRAM_BOT_TOKEN` · Discord bot: `DISCORD_BOT_TOKEN`
- Providers: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`,
  `GITHUB_TOKEN`
For anything else, take the names from the feature's documentation or the
plugin's requires_env — never invent them.

## Never

- Never ask the user to type or paste a password, token, or API key into the
  chat. Even if they offer. Even if it seems faster. Chat input ends up in
  session transcripts and the model context — the whole point of
  `env_setup_prompt` is that values bypass it.
- Never read the `.env` file (via `read_file` or terminal) to "see what's
  there" — `env_setup_status` gives you the same information without ever
  exposing values.
- Never invent ENV names — take them from `env_setup_status` or the
  integration's documented contract.

## After setup

Verify the integration with a non-secret action (send a test query, list
folders, check a port — depending on the feature). If a value the user entered
is wrong, the failure message comes from the feature, not from the secret —
report the error and offer to re-prompt with `env_setup_prompt` for just that
field.

## Reading credentials back (profile scoping — important!)

`get_env_value()` in a gateway session is scope-checked and falls back to the
GATEWAY home's `.env` — it does **not** see profile `.env` files (e.g. values in
`~/.hermes/profiles/<name>/.env`). To check a profile-scoped value, use a
subprocess with the profile home and report only a boolean — never print raw
values:

```bash
HERMES_HOME=~/.hermes/profiles/<name> python3 -c \
  'from hermes_cli.config import load_env; print(load_env().get("KEY") is not None)'
```

## Auth failures: never retry-loop

On `HTTP 401` from a third-party API with formally plausible credentials (right
prefix/length/charset): **one single attempt, then stop.** It means wrong or
inactive credentials on the provider side; retry-loops trigger WAF/Fail2ban
bans on the target host — a permanent 403 then blocks ALL requests from that
IP. Document the failure, ask the user to check the provider admin (e.g.
Shopware: Integration active? regenerate secret), and retest only once after
the fix.