# sim-vault

Keep resident iOS simulators parked as tar archives on another volume (for example a NAS
share) and get them back later, app data and logins included. It also serializes work on a
device with `flock` locks and per-owner leases, so several agents or scripts can share one
simulator without shutting it down under each other.

Pure Python standard library, a single module, no runtime dependencies.

## Requirements

- macOS with Xcode (`xcrun simctl`)
- Python 3.9 or newer
- An archive directory on a **different volume** than `~/Library/Developer/CoreSimulator`.
  The default is `/Volumes/WorkSpace/ios-sim`; set `SIM_VAULT_DIR` to use another one. An
  unmounted share is refused instead of silently filling the local disk.

## Install

```sh
uv tool install git+https://github.com/audichuang/sim-vault@v3.1.0
sim-vault --version
```

`pipx install git+https://github.com/audichuang/sim-vault@v3.1.0` works the same way.

### Upgrade

```sh
TAG=v3.1.0   # the tag you want
uv tool install --force "git+https://github.com/audichuang/sim-vault@$TAG"
```

### Roll back

Install the previous tag the same way (set `TAG` to it). A previous tag exists only from the
second release on; v3.1.0 is the first.

## Commands

| Command   | What it does |
|-----------|--------------|
| `ensure`  | Get a simulator by name: use the local one, restore the archive, or create it (`--create`); optional `--boot` and `--lease` |
| `run`     | Acquire a simulator as in `ensure` and run a command under a held lease |
| `restore` | Restore an archived simulator and print its UDID |
| `park`    | Archive a simulator to the vault, verify the read-back, then delete the local device |
| `release` | Give up a lease taken by `ensure --lease` (optionally shut down) |
| `shutdown`| Shut a device down under its lock |
| `status`  | Show the local device, archive and leases for a name |
| `list`    | List archives in the vault |
| `gc`      | Remove what interrupted runs left behind (use `--dry-run` first) |

Commands other than `run` take `--json`. `sim-vault --help` is the reference for subcommands, flags,
environment variables (`SIM_VAULT_DIR`, `SIM_VAULT_HISTORY`) and the exit-code table that
tells a caller what to do next; `sim-vault <command> --help` has the per-command flags.

### Run a command with a held lease

```sh
sim-vault run "My Test Device" --lease project:test --boot --deny-other-leases -- ./run-tests
```

The command inherits `SIM_VAULT_NAME`, `SIM_VAULT_UDID`, the full owner in
`SIM_VAULT_LEASE` and the lock descriptor in `SIM_VAULT_LEASE_FD`. With
`--deny-other-leases` or `--exclusive`, the held lease blocks other owners'
`ensure` calls. `--allow-lease` applies during acquisition.

Pass `int(os.environ["SIM_VAULT_LEASE_FD"])` in Python's `subprocess.Popen`
`pass_fds` tuple for any launcher that needs to keep the lease alive. Close the
descriptor for long-lived helpers. In bash, use numeric redirection for macOS
`/bin/bash` 3.2. Put the background operator inside the `eval` string.

```bash
eval "helper ... ${SIM_VAULT_LEASE_FD}>&- &"
```

The final stderr line and optional `--result-file` JSON give the stage, exit
code, child exit code and teardown result. Teardown leaves the device running.
If an escaped process still holds the lock, use the reported `lsof <lock path>`
command to find candidates and verify ownership before using `kill <pid>`.
The next command that takes the device lock removes the lease after the last
holder exits.

## Security

An archive contains the simulator's app data, logins and keychain. Restrict the share that
holds the archive directory (ACL) so that only your own account can read it. sim-vault
creates new vault directories with mode `0700` and new archive files (`.tar`, `.json` and
their `.partial` files) with mode `0600`; existing vault directories and archives are not
changed (`chmod` them yourself if needed).
On an SMB share (`smbfs`) the modes shown by macOS are synthesized by the client and the
server's ACL is what actually controls access, so set the ACL there.

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests run against a fake `xcrun simctl` and a vault under `/tmp`; they never touch real
simulators.

## License

MIT
