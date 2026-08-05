---
sidebar_position: 9
sidebar_label: "Migrate to another host"
title: "Migrating an instance to another host"
description: "Move a whole Hermes instance to another machine over SSH: host profiles, preflight, the migration stages, and what to do when one fails"
---

# Migrating an instance to another host

`hermes migrate host` moves a **whole instance** — config, credentials, sessions,
memories, skills, cron jobs — to another machine over SSH, and installs Hermes
there first if the target does not have it. The same thing is available in the
dashboard under **Backup & Migration** (`/migrate`).

## What it does, and what it deliberately does not

**It moves, it does not clone.** Exactly one instance stays live. Two instances
holding the same `auth.json` and `cron/jobs.json` would authenticate as the same
user and fire every scheduled job twice, so there is no "run both" mode and no
repeatable sync to a warm standby.

**The source is only ever stopped, never modified.** That single property is why
every failure is recoverable: if anything goes wrong, you start the source again
and you are back where you began.

**It stops at a target that is ready but not started.** Migration ends with a
verified, idle instance on the target and prints the command to start it.
Promotion is a decision for you, not for the tool — starting it automatically
would mean discovering a broken target *after* service had already moved.

**It does not erase the source.** After a successful migration the source disk
still holds `.env`, `auth.json` and `state.db`. Keeping them is a free rollback
path; clearing them is your call, by hand.

**It does not carry the deployment substrate.** A containerised source migrates
to a *native* target: `docker-compose.yml`, image builds and systemd units do
not come along.

## Prerequisites

- **SSH key access to the target**, already working. Hermes connects with
  `BatchMode=yes`, so a missing or wrong key fails immediately instead of
  hanging on a password prompt. Password authentication is not supported at all
  — it would mean storing a plaintext secret to save you one `ssh-copy-id`.
- **`python3` on the target.** The installer and Hermes itself both need it.
- **Windows targets need OpenSSH Server enabled.** Hermes does not install it
  for you. Linux and macOS targets need nothing beyond sshd.

:::caution Windows targets are unverified
The Windows branch exists — OS detection maps to `install.ps1` — but it has not
been exercised end to end, and the probe and stage commands assume a POSIX
shell. Treat a Windows target as experimental and keep the source instance
until you have confirmed the migrated one works.
:::
- Free disk on the target of at least **twice** the estimated archive size — the
  archive has to fit alongside the unpacked result.

## Adding a target host

Dashboard: **Backup & Migration → Add target host**. Fields are `id`, `label`,
`host`, `user`, `port` (default 22), `identity file` and `target HERMES_HOME`
(default `~/.hermes`). The `id` is a slug: lowercase letters, digits, `-` and
`_`.

Profiles are stored in `$HERMES_HOME/migration_targets.json` at mode 0600, not
in `config.yaml` — a migration target is an operator asset, not agent
configuration.

**Only the *path* to a private key is stored.** Never key material, never a
password; sending a `password` field to the API is rejected outright. Because
the file holds no secrets, the dashboard can show it verbatim.

Targets are stored per profile, and the dashboard passes the profile through
when it launches a migration, so a target added while managing profile `work`
is the one that runs.

### Host keys are pinned on first contact

The first connection accepts the target's host key and **records its
fingerprint** in the profile — trust on first use. Every later connection
verifies against that pin and fails hard if it does not match:

```
host key verification failed for '10.0.0.5': pinned SHA256:abc…,
known_hosts now holds SHA256:xyz… This channel carries plaintext .env and
auth.json — resolve this by hand.
```

The dashboard shows the pinned fingerprint under each target, or "Host key not
pinned yet" before the first preflight.

Host keys live in `$HERMES_HOME/migration_known_hosts`, **not** in your
`~/.ssh/known_hosts`. That file is shared with every other ssh use on the
account and gets edited by hand; a fingerprint read out of it would prove
nothing about what this feature saw on first contact. The pin is checked both
by ssh (`StrictHostKeyChecking=yes`) and independently before ssh is launched,
so an edited `migration_known_hosts` is caught too.

A mismatch is never something to clear by deleting the entry. It means either
the target was rebuilt — in which case remove and re-add the target
deliberately — or something is sitting between you and it, waiting to be handed
plaintext credentials.

## Preflight

Preflight is **read-only** — it never modifies the target — and returns two
tiers of verdict. If the connection itself cannot be established (bad key,
unreachable host, changed host key), you get that ssh error instead of a
verdict list; nothing was sent.

It doubles as the connection test: the first successful preflight is also what
pins the target's host key.

**Blocking** (migration cannot start):

| Check | Passes when |
|---|---|
| `os` | The target's OS and architecture are identified (Linux, macOS or Windows), which is what selects the installer |
| `python3` | `python3` is on the target's `PATH` |
| `disk_space` | Free space ≥ 2× the estimated archive size |
| `target_home` | The target `HERMES_HOME` is safe to overwrite — see below |

**Warnings** (shown in amber, do not stop anything):

| Check | Meaning |
|---|---|
| `clock_skew` | The target's clock differs from the source's by more than 120s |
| `hermes_version` | Hermes is absent (it will be installed), or installed at a different version than the source |

Clock skew is worth its own line. `auth.json` carries expiry-sensitive OAuth
tokens, so a target with a skewed clock does not present as a clock problem — it
presents as "login expired" after the migration, which is near-impossible to
diagnose after the fact and trivial to check before.

### "Safe to overwrite"

A target that already runs Hermes has a populated `HERMES_HOME` from its own
first run, so "non-empty" cannot mean "blocked". The check is:

- absent or empty → passes
- **pristine** — files exist but none of `auth.json`, `sessions`, `state.db`,
  `.env`, `skills` → passes
- anything carrying real user state → **blocks**

The block is cleared only by an explicit **confirm overwrite** acknowledgement,
unchecked by default, which passes `--confirm-overwrite` to the run. This is the
branch that destroys someone else's instance, so it is deliberately not a
checkbox that defaults on — and it waives *only* the `target_home` block. A
failed `python3` or `disk_space` check cannot be waived by consenting to
overwrite unrelated data.

## Running the migration

From the dashboard, press **Start migration**; from a shell:

```bash
hermes migrate host <target-id> [--confirm-overwrite]
```

Both run the same code — the dashboard launches the CLI and tails its log, so
the feature works identically headless. Progress lines have the form
`[<stage>] <status> <detail>`.

Six stages, in this order:

1. **`install`** — installs Hermes on the target if `hermes` is not already on
   its `PATH`, using the project's own installer (`install.sh`, or
   `install.ps1` on Windows).
2. **`stop_source`** — stops the source gateway. **Downtime starts here.**
   Hermes then re-checks that the process is actually gone before continuing,
   rather than trusting the stop command's exit code — backing up a still-live
   instance would snapshot SQLite mid-write.
3. **`backup`** — a **full** `hermes backup` on the source, written at 0600.
   Not `--quick`: quick captures only critical state and would silently drop
   `skills/`, memories, plans and projects, which are user content a migration
   must carry.
4. **`transfer`** — streams the archive to `/tmp/hermes-migration.zip` on the
   target through a single ssh invocation, then chmods it to 600.
5. **`restore`** — `hermes import` on the target.
6. **`verify`** — read-only assertions on the target: `config.yaml` is present,
   `auth.json` is mode 0600 if it exists, and every SQLite store opens and
   answers a query. Any failure aborts the stage rather than being reported as
   a passing detail.

**Install comes before the stop on purpose.** It depends on no data and is the
step most likely to fail — network, dependencies, permissions — so it should
fail while the source is still serving, where the cost is nearly zero. It also
shortens the downtime window for free.

There is no start stage. The run halts here and reports **ready, not started**.

### Temporary archives

The archive is plaintext: it contains `.env` and `auth.json`. Both copies — the
local one under `HERMES_HOME` and `/tmp/hermes-migration.zip` on the target —
are created at 0600 and removed on every exit path, including failures.

If the *remote* copy cannot be removed, the run emits a `[cleanup] warn` line
naming the file. That is not cosmetic: it means plaintext credentials are
sitting in the target's `/tmp`. Delete it by hand.

## When a stage fails

Recovery follows from the one invariant — the source is only stopped, never
modified — so it depends on nothing but *which* stage failed.

| Failed at | Source | What to do |
|---|---|---|
| Preflight | Untouched, still serving | The target was not modified. Fix the profile or the target and run preflight again. |
| `install` | Untouched, still serving | Nothing to undo. Fix the cause and run again. |
| `stop_source` | Still serving, or stopping | Nothing was transferred. Start the source if it stopped. |
| `backup` | Stopped, data intact | Start the source to roll back. |
| `transfer` | Stopped, data intact | Start the source to roll back. |
| `restore` | Stopped, data intact | Start the source to roll back. `hermes import` overwrites, so the target home is now partially populated — **clear it before retrying**. |
| `verify` | Stopped, data intact | Start the source to roll back, then investigate the target. |

The CLI prints the applicable recovery sentence itself when it aborts, and the
dashboard shows the same text.

## After a successful migration

The completion panel gives you three things:

1. **The target is ready but not started.** Start it when you are ready:

   ```bash
   ssh [-p PORT] [-i IDENTITY] USER@HOST "HERMES_HOME=<target home> hermes gateway start"
   ```

   The dashboard renders this line filled in from the target profile.

2. **Credentials remain on the source.** `.env`, `auth.json` and `state.db` are
   still on the old machine. Keep them as a rollback path or remove them
   yourself — Hermes will not delete user data on your behalf.

3. **Do not start both.** Once the target is up, the source must stay down. Two
   live instances share one identity and one cron schedule.

## See also

- [`hermes backup` / `hermes import`](../reference/cli-commands.md#hermes-backup)
  — the packaging migration reuses rather than re-implementing
- [Profiles](./profiles.md) — running several independent instances on one
  machine
- [Security](./security.md)
