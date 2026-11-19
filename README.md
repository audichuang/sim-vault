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
uv tool install --force git+https://github.com/audichuang/sim-vault@<tag>
```

### Roll back

Install the previous tag the same way (`--force ...@<previous tag>`).

## Commands

| Command   | What it does |
|-----------|--------------|
| `ensure`  | Get a simulator by name: use the local one, restore the archive, or create it (`--create`); optional `--boot` and `--lease` |
| `restore` | Restore an archived simulator and print its UDID |
| `park`    | Archive a simulator to the vault, verify the read-back, then delete the local device |
| `release` | Give up a lease taken by `ensure --lease` (optionally shut down) |
| `shutdown`| Shut a device down under its lock |
| `status`  | Show the local device for a name |
| `list`    | List archives in the vault |
| `gc`      | Remove what interrupted runs left behind (use `--dry-run` first) |

Every command takes `--json`. `sim-vault --help` is the reference for subcommands, flags,
environment variables (`SIM_VAULT_DIR`, `SIM_VAULT_HISTORY`) and the exit-code table that
tells a caller what to do next; `sim-vault <command> --help` has the per-command flags.

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests run against a fake `xcrun simctl` and a vault under `/tmp`; they never touch real
simulators.

## License

MIT
