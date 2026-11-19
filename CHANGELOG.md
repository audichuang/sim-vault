# Changelog

## 4.0.0

- Held leases use a process-held lock to protect a simulator across commands. A live
  lock still protects the device if an older `release` removes the lease's JSON.
- `run` acquires a held lease for a command and reports the command and teardown results.
- Manual sessions can use `ensure --lease auto` and `release --lease auto` with
  `--lease-scope`. `whoami` looks up the current session's token. Status shows session
  lease age and anchor liveness, and history records the lease kind.
  If the process list cannot be read, `ensure --lease auto` fails instead of guessing the
  session, and `release`/`whoami --lease auto` report `no-lease` and leave the lease in place.
- Newly created vault directories use mode `0700`, and newly created vault files (`.tar`,
  `.json` and their `.partial` files) are opened with mode `0600` from the start. Existing
  vault directories and archives, as well as state files under
  `~/Library/Developer/CoreSimulator/sim-vault/`, keep their current permissions.
- Known issue [#1](https://github.com/audichuang/sim-vault/issues/1). A signal during
  a `run` hand-off can leave a simulator booted or affect child signal forwarding and exit reporting.

## 3.1.0

- Packaged as a standalone repository: `src/sim_vault/`, installable with
  `uv tool install git+https://github.com/audichuang/sim-vault@v3.1.0`. Behaviour of all
  subcommands is unchanged.
- New `sim-vault --version`.
- New environment variable `SIM_VAULT_HISTORY` overrides the path of `history.jsonl`
  (default unchanged).
- Tests moved to `tests/`; CI runs them on macOS with Python 3.9 and 3.12.

## v3 (2026-11-19)

- Read-back is meant to hit the vault: the checksum read after `park`, and during `restore`
  extraction or pairing of tar/JSON, sets `F_NOCACHE` before the first read (reported by the
  maintainer; not independently verified at release time). If `park`
  cannot set it, it exits 1 and keeps the device; `restore` only warns (checksum still checked).
- `history.jsonl` under `~/Library/Developer/CoreSimulator/sim-vault/`: one line per
  ensure/restore/park/release/shutdown call (ts, cmd, name, udid, rc, kind, action, bytes,
  posterMB, cwd, pid, seconds, phases), also for failures, usage errors and
  SIGTERM/SIGHUP/SIGINT. Lines are written with O_APPEND plus flock; a write failure only warns.
  `--json` results gain `seconds` and `phases`.
- Error lines are now `error (exit N, <kind>): <message>`, followed by `next:`; signals and
  unexpected exceptions use the same format.
- PosterBoard over 1 GiB: `park` excludes the two system PosterBoard directories
  (`data/Library/Application Support/{PRBPosterExtensionDataStore,com.apple.PosterKit}`,
  anchored member paths) by default, prints one line, exits 0, and records
  `posterboardTrimmed`/`excluded` in the metadata. `--keep-posterboard` restores the old
  behaviour (exit 9, device untouched); `--trim-posterboard` is kept as a no-op alias.
  Reported by the maintainer: a trimmed archive restores and boots, and PosterBoard rebuilds
  itself (not independently verified at release time).
- `park --exclude-symbol-cache` (off by default) excludes
  `data/Library/Caches/com.apple.coresymbolicationd`.
- New subcommand `shutdown <name> [--busy-lock F] [--wait S]`: shuts down under the device
  lock; exits 8 and lists owners when any lease exists; already shut down or absent exits 0.
  `simctl shutdown` inside park/release is now done under the device lock too, so a shutdown
  that outlives a killed sim-vault keeps new `ensure` calls out.
- `park` killed between its tar and JSON renames: `gc` no longer deletes the only
  `.json.partial` that matches the new tar; it lists it, and the next `park`/`restore`
  pairs it when the checksum matches.
- `gc` skips non-directories under `leases/` (`.DS_Store`) and leases released during the listing.
- `park` partial files carry a per-run token (`<name>.tar.<pid>-<hex>.partial`); cleanup only
  removes what this run created. A pending `.json.partial` that cannot be paired exits 1
  (`pending-pair`) without overwriting or cleaning.
- `gc` rechecks existence, inode/mtime, age and pending state after taking the name lock.
- `park`, `shutdown`, `release --shutdown`, `ensure --deny-other-leases` read leases strictly:
  an unreadable or non-lease file exits 1 (`lease-unreadable`) and nothing is shut down or deleted.
- History lock waits at most 5 seconds, then warns and skips the line.
- Archive with a tar but no JSON: restore/ensure exit 1 (`archive-incomplete`), `--create`
  does not create; `park` exits 7 and needs `--replace`.
- `ensure --deny-other-leases [--allow-lease <owner>]`: exits 8 when another owner holds a
  lease, takes no lease itself; unlike `--exclusive`, later `ensure` calls by others are unaffected.
- An undecidable pairing (failure, vault listing failed, tar stat error) is treated as
  unknown: restore/ensure/park exit 1 (`pending-pair`) and never create a device or decide
  3/4/10 from stale metadata; `gc` keeps the candidate `.json.partial` and reports it.
- Known risks, not fixed: two Macs sharing one vault keep locks in their own CoreSimulator
  directories, so simultaneous `park` of the same name may both exit 0 and keep the later
  write; `ensure` after a brief vault outage can create a device from stale metadata (needs
  three conditions at once); restoring the staging JSON on a filesystem returning ENOTSUP
  exits `pending-pair` and needs a manual look.
- Tests: 90 to 133.

## v2 (2026-11-17)

- Fetching and parking decisions moved into the tool. Locks are fixed-path flock files (not
  `$TMPDIR`); SIGTERM cleans up leftovers; restore clones under a temporary name and records
  `restoredAs` before renaming; a local device with the same name that is not the archived
  one exits 10; a device booted during packaging exits 8 without replacing the archive or
  deleting the device; Shutting Down is waited out; `list` survives a broken JSON; no tracebacks.
- New subcommands `status`, `ensure` (`--create`, `--boot`, `--lease`, `--exclusive`),
  `release`, `gc`. `park` gained lease checks, `--busy-lock`, PosterBoard over 1 GiB exits 9,
  `--missing-ok`. Every subcommand takes `--json`, and failures are a single object too.
- Compatibility: `park <name> [--shutdown] [--replace]` can only refuse more (8, 9), never
  delete more. `restore <name>` still prints only the UDID, but a local same-name device that
  is not the archived one now exits 10 instead of 0. `restore` ignores leases; callers that
  use leases call `ensure`.
- Tar gets `--no-xattrs --mac-metadata` (bsdtar cannot read back a large xattr stored in a
  pax header). `park` runs `sync` first; a device that stays Shutdown but is written during
  packaging is repacked once automatically.
- Known risks, not fixed: boot-record reconciliation window when `ensure` is SIGKILLed in
  `simctl boot`; empty `{udid}.lock` files from `--busy-lock` accumulate; `--busy-lock`
  directory missing makes `park` exit 1; lock files are mode 0644; TERM and HUP arriving
  together can print a Python traceback; the fake-xcrun counters in tests are occasionally
  flaky under parallel runs.

## v1 (2026-11-17)

- `park|restore|list`: pack a whole simulator (apps, logins, keychain) into `$SIM_VAULT_DIR`,
  verify by reading back before deleting. Restore takes about 10 seconds, boot 3 seconds, and
  the app opens still logged in.
- Three pitfalls handled: a copy placed back under `Devices` is invisible to the service
  (clone from a staging set instead); a clone changes the UDID and invalidates app paths in
  the LaunchServices database (delete the csstore so it rebuilds); packing right after
  shutdown can produce a corrupt tar with exit 0 and a matching checksum (wait for the files
  to settle, then verify with `tar -t`).
- Same day, folded in here (no separate version numbers were recorded):
  - `park` measures PosterBoard data first: a bloated store would otherwise be packed whole.
  - Exit 5 (vault not mounted) no longer means "create a fresh device": that could replace an
    archive holding a login with an empty device. Only 3 and 4 fall back to creating.
  - `restore` records `restoredAs` in the metadata; `park` exits 7 when the archived `udid`
    and `restoredAs` are both not this device; `--replace` overrides.
