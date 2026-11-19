#!/usr/bin/env python3
"""Keep resident iOS simulators parked as tar archives on another volume, and get them back.

The archive keeps the whole device (installed apps, keychain, logins, settings), so a
restored simulator is ready to use. Only the device is archived; the iOS runtime is shared
and must still be installed when restoring.

Project scripts should call `ensure` to get a device and `park` to put it away, and pass the
exit code on. They should not branch on 3/4/5 themselves: `ensure --create` already does that.

commands (see `sim-vault <command> --help` for flags):
  status <name>    what is local, what is archived, who uses it; waits for no lock
                   (probes the device lock for an instant); 0 unless simctl fails
  ensure <name>    use the local device, else restore it, else (3/4 with --create) create it;
                   optionally --boot and register a --lease (--deny-other-leases: 8 while
                   other owners hold one); prints the UDID
  run <name>       acquire a held lease and run a command; see `run --help`
  restore <name>   `ensure` without policy, for people and old scripts: never creates or
                   boots, ignores leases; prints the UDID. Callers using leases use ensure
  park <name>      archive, verify, then delete the device (refuses while it is in use);
                   PosterBoard data over 1 GiB is left out by default (--keep-posterboard:
                   exit 9 instead); --exclude-symbol-cache leaves another system cache out
  shutdown <name>  shut the device down under the lock unless a lease is held (8); for
                   project `down` scripts, after releasing their own lease
  release <name>   drop a lease; with --shutdown the last lease shuts down what ensure booted
  whoami <name>    show this session's anchor and lease token without taking the device lock
  list             archives in the vault
  gc               remove what interrupted runs left behind (dry-run first)
Held leases protect a device while a process holds their lock and expire when it exits.
For manual sessions, use ensure and release with --lease auto --lease-scope DIR.
Ensure also takes --lease-prefix PREFIX for the token. Use whoami --lease-scope DIR to find it.
Commands other than run take --json: stdout is then exactly one JSON object, also on failure:
{"ok": false, "code": 5, "kind": "vault-unavailable", "message": "...", "next": "..."}.
Messages go to stderr; a failure ends with "error (exit N, <kind>): <message>" and a
"next:" line. --json results of ensure/restore/park/release/shutdown also carry "seconds"
(total) and "phases" (seconds per step: lockWait, shutdownQuiesce, write, readBack, delete,
extract, clone, boot, shutdown; steps not reached are left out).

The vault is $SIM_VAULT_DIR (default /Volumes/WorkSpace/ios-sim). It must sit on a different
volume than ~/Library/Developer/CoreSimulator, so an unmounted share is refused instead of
silently filling the local disk. Locks, leases and staging live in
~/Library/Developer/CoreSimulator/sim-vault/ (fixed paths, not $TMPDIR). Every ensure,
restore, park, release, shutdown and run appends one JSON line (ts, cmd, name, udid, rc,
kind, leaseKind, action, bytes, posterMB, cwd, pid, seconds, phases) to history.jsonl, also on failure
and on SIGTERM/SIGHUP/SIGINT; a history that cannot be written only warns. $SIM_VAULT_HISTORY
overrides the path of history.jsonl (default: that directory).

exit codes and what the caller does next:
  0  ok
  1  error (simctl, tar, read-back mismatch, several devices with the name): stop, look, do
     not recreate the device. Kinds worth knowing: archive-incomplete (restore/ensure: the
     vault has the tar but no metadata; never treated as "no archive", --create does not
     create), lease-unreadable (park, shutdown, release --shutdown, ensure
     --deny-other-leases: a lease file cannot be read, so nothing was shut down or
     deleted), pending-pair (park: an
     interrupted park left a tar without matching metadata and pairing failed)
  2  usage
  3  no archive (restore/ensure without --create): project policy - create the device or ask
  4  runtime of the archive not installed: same as 3; the new device's first park returns 7,
     and needs --replace once you have checked the old archive is not needed
  5  vault unavailable (share not mounted): wait for it and retry; never create a same-name
     device meanwhile - parking it later would replace the logged-in archive
  6  park: the device is booted and --shutdown was not given: shut it down or pass --shutdown
  7  park would replace the archive of another device, or a tar without metadata
     (archive-incomplete): do not add --replace automatically
  8  busy: device lock not free within --wait, another owner's lease (for ensure: an
     exclusive one, or any with --deny-other-leases), a --busy-lock held, an
     exclusive ensure on a device that is running, the device still being written
     120 s after shutdown, or the device was booted or written while being archived
     (nothing deleted). The message names the holder; wait for it, or release it if it is
     stale. If the same holder returns 8 twice, stop and report it
  9  park --keep-posterboard: PosterBoard data is over 1 GiB; the device was not shut
     down or deleted; park without --keep-posterboard to archive without the PosterBoard
     store, or rebuild the PosterBoard store and park again. Without the
     flag park leaves that data out instead (the system rebuilds it on the first boot
     after a restore)
  10 restore/ensure: the local device with this name is not the archived one, and the
     archive can still be restored: stop and report; do not delete or park on your own
"""
import argparse
from contextlib import ExitStack
import ctypes
from datetime import datetime
import fcntl
import hashlib
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

__version__ = "4.0.0"
TOOL_VERSION = "3"
DEFAULT_VAULT = "/Volumes/WorkSpace/ios-sim"
SIM_ROOT = os.path.expanduser("~/Library/Developer/CoreSimulator")
DEFAULT_SET = os.path.join(SIM_ROOT, "Devices")
# Fixed paths, not $TMPDIR: two sessions with different TMPDIRs must see the same locks.
STATE_ROOT = os.path.join(SIM_ROOT, "sim-vault")
LOCK_DIR = os.path.join(STATE_ROOT, "locks")
LEASE_DIR = os.path.join(STATE_ROOT, "leases")
RECORD_DIR = os.path.join(STATE_ROOT, "archives")
STAGING_ROOT = os.path.join(STATE_ROOT, "staging")
LEGACY_STAGING_ROOT = os.path.join(SIM_ROOT, "sim-vault-staging")
HISTORY_PATH = (os.environ.get("SIM_VAULT_HISTORY")
                or os.path.join(STATE_ROOT, "history.jsonl"))
HISTORY_COMMANDS = ("ensure", "restore", "park", "release", "shutdown", "run")
HISTORY_LOCK_TIMEOUT = 5
POSTER_STORE = os.path.join("data", "Library", "Application Support",
                            "PRBPosterExtensionDataStore")
# What park leaves out when PosterBoard data is over the limit (unless --keep-posterboard), relative to the device directory: the store
# measured above and its sibling the PosterBoard repair also renames away.
POSTER_TRIM = ("data/Library/Application Support/PRBPosterExtensionDataStore",
               "data/Library/Application Support/com.apple.PosterKit")
SYMBOL_CACHE = "data/Library/Caches/com.apple.coresymbolicationd"
# fcntl only has F_NOCACHE on Darwin; older Pythons lack the constant there (value 48).
F_NOCACHE = getattr(fcntl, "F_NOCACHE", 48 if sys.platform == "darwin" else None)
POSTER_LIMIT_KB = 1024 * 1024
CHUNK = 8 * 1024 * 1024
QUIET_SECONDS = 10
QUIET_TIMEOUT = 120
STATE_TIMEOUT = 120
BOOT_TIMEOUT = 300
POLL = 1.0
DEFAULT_WAIT = 60
HELD_RELEASE_TIMEOUT = 5
RUN_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
STALE_AFTER = 3600
RESTORING = " [sim-vault restoring "

(EXIT_ERROR, EXIT_USAGE, EXIT_NO_ARCHIVE, EXIT_NO_RUNTIME, EXIT_NO_VAULT, EXIT_BOOTED,
 EXIT_OVERWRITE, EXIT_BUSY, EXIT_POSTER, EXIT_NOT_ARCHIVED) = 1, 2, 3, 4, 5, 6, 7, 8, 9, 10

# File descriptors of the locks this process holds. Every child gets them, so a tar or
# simctl still running after this process dies keeps the device locked until it stops.
HELD_FDS = []


class Fail(Exception):
    def __init__(self, message, code=EXIT_ERROR, kind="error", next_step=None):
        super().__init__(message)
        self.code = code
        self.kind = kind
        self.next_step = next_step or ("stop and look into the message above; do not "
                                       "recreate or delete the device to get past it")


class Interrupted(BaseException):
    """SIGTERM/SIGHUP turned into an exception so `finally` blocks clean up."""

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def ignore_during_cleanup(_signum, _frame):
    pass


def on_signal(signum, _frame):
    # A caught handler shields this process during cleanup and resets on exec,
    # so subprocesses launched for rollback keep their usual signal handling.
    for other in RUN_SIGNALS:
        signal.signal(other, ignore_during_cleanup)
    raise Interrupted(signum)


# What this CLI call did, for history.jsonl and --json: fields set along the way (udid,
# action, bytes, ...) and seconds per phase, summed when a phase runs twice.
RUN = {"fields": {}, "phases": {}, "started": time.monotonic()}


def note(**fields):
    RUN["fields"].update(fields)


class phase:
    """Add the monotonic time spent in the block to RUN["phases"][name]."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.started = time.monotonic()
        return self

    def __exit__(self, *exc):
        phases = RUN["phases"]
        phases[self.name] = phases.get(self.name, 0.0) + time.monotonic() - self.started


def phase_text(phases):
    return ", ".join(f"{k} {v:.1f}s" for k, v in phases.items())


# Cleanup steps that failed; the final message then points at gc instead of "cleaned up".
CLEANUP_FAILURES = []


def cleanup(what, fn):
    try:
        fn()
    except BaseException as exc:
        CLEANUP_FAILURES.append(f"{what}: {exc}")
        log(f"warning: could not {what}: {exc}")


def write_stderr(message):
    try:
        print(message, file=sys.stderr, flush=True)
    except BrokenPipeError:
        # Keep stderr's buffered write from failing again at interpreter exit.
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), sys.stderr.fileno())


def log(message):
    write_stderr(f"sim-vault: {message}")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def simctl(*args, device_set=None, timeout=None, keep_lock=False, lease_fd=None):
    """keep_lock: the child inherits the device lock, so if this process dies it keeps
    others out until it finishes. Only for steps whose half-done state others must not
    see (clone, rename, delete, shutdown); boot/bootstatus/list could hold it without
    bound. A held lease can protect a boot or bootstatus without the device lock."""
    cmd = ["xcrun", "simctl"]
    if device_set:
        cmd += ["--set", device_set]
    cmd += list(args)
    pass_fds = tuple(HELD_FDS) if keep_lock else ()
    if lease_fd is not None:
        pass_fds += (lease_fd,)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                pass_fds=pass_fds)
    except subprocess.TimeoutExpired:
        raise Fail(f"{' '.join(cmd)} did not finish in {timeout}s",
                   next_step="check the device in `xcrun simctl list devices`, then rerun")
    if result.returncode != 0:
        raise Fail(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
    return result.stdout


# -- devices and runtimes -------------------------------------------------------------

def all_devices(device_set=None):
    data = json.loads(simctl("list", "devices", "-j", device_set=device_set))
    out = []
    for runtime, devices in data["devices"].items():
        for device in devices:
            out.append(dict(device, runtime=runtime))
    return out


def resolve(name):
    """Available devices with this exact name, and how many unavailable ones share it."""
    matches = [d for d in all_devices() if d["name"] == name]
    available = [d for d in matches if d.get("isAvailable", True)]
    return available, len(matches) - len(available)


def find_one(name, warnings=None):
    matches, unavailable = resolve(name)
    if unavailable:
        message = f"{unavailable} unavailable simulator(s) named '{name}' ignored"
        log(f"warning: {message}")
        if warnings is not None:
            warnings.append(message)
    if len(matches) > 1:
        listing = ", ".join(f"{d['udid']} ({d['state']})" for d in matches)
        raise Fail(f"{len(matches)} simulators are named '{name}': {listing}",
                   kind="duplicate-name",
                   next_step="names must be unique; delete the extra one after checking "
                             "it holds nothing you need, then rerun")
    return matches[0] if matches else None


def device_state(udid):
    for device in all_devices():
        if device["udid"] == udid:
            return device["state"]
    return None


def wait_for_shutdown(udid, timeout=STATE_TIMEOUT):
    deadline = time.time() + timeout
    while True:
        state = device_state(udid)
        if state in ("Shutdown", None):
            return state
        if time.time() >= deadline:
            raise Fail(f"{udid} is still {state} after {timeout}s",
                       next_step="check `xcrun simctl list devices`, then rerun")
        time.sleep(POLL)


def shutdown_device(udid):
    state = device_state(udid)
    if state in ("Booted", "Booting"):
        # keep_lock: a shutdown still running after this process is killed must keep the
        # device locked, or a new ensure could boot it only to see it go down.
        simctl("shutdown", udid, timeout=STATE_TIMEOUT, keep_lock=True)
    return wait_for_shutdown(udid)


def list_runtimes():
    return json.loads(simctl("list", "runtimes", "-j"))["runtimes"]


def runtime_available(identifier, runtimes=None):
    if runtimes is None:
        runtimes = list_runtimes()
    return any(r["identifier"] == identifier and r.get("isAvailable") for r in runtimes)


def version_key(text):
    parts = [int(p) for p in re.findall(r"\d+", text or "")[:3]]
    return tuple(parts + [0] * (3 - len(parts)))


def pick_runtime(device_type, runtimes):
    """Newest available runtime that supports this device type."""
    packed = lambda v: (v[0] << 16) | (v[1] << 8) | v[2]
    candidates = []
    for r in runtimes:
        if not r.get("isAvailable"):
            continue
        supported = r.get("supportedDeviceTypes")
        if supported is not None:
            ok = any(t.get("identifier") == device_type["identifier"] for t in supported)
        else:
            version = packed(version_key(r.get("version")))
            ok = (device_type.get("minRuntimeVersion", 0) <= version
                  <= device_type.get("maxRuntimeVersion", 0xFFFFFFFF))
        if ok:
            candidates.append(r)
    if not candidates:
        raise Fail(f"no installed runtime supports {device_type['name']}", kind="no-runtime",
                   next_step="install a runtime in Xcode > Settings > Components")
    return max(candidates, key=lambda r: version_key(r.get("version")))["identifier"]


def create_device(name, wanted_type):
    types = json.loads(simctl("list", "devicetypes", "-j"))["devicetypes"]
    matches = [t for t in types if wanted_type in (t["name"], t["identifier"])]
    if not matches:
        raise Fail(f"unknown device type {wanted_type!r}", EXIT_USAGE, "usage",
                   "use a name or identifier from `xcrun simctl list devicetypes`")
    runtime = pick_runtime(matches[0], list_runtimes())
    log(f"creating {name} ({matches[0]['name']}, {runtime})")
    return simctl("create", name, matches[0]["identifier"], runtime).strip()


def poster_kb(udid):
    """Allocated size of the PosterBoard store, like `du -sk`."""
    total = 0
    for root, dirs, files in os.walk(os.path.join(DEFAULT_SET, udid, POSTER_STORE)):
        for entry in dirs + files:
            try:
                total += os.lstat(os.path.join(root, entry)).st_blocks * 512
            except OSError:
                pass
    return total // 1024


# -- vault ------------------------------------------------------------------------------

def same_volume(a, b):
    return os.stat(a).st_dev == os.stat(b).st_dev


def vault_dir():
    path = os.path.abspath(os.environ.get("SIM_VAULT_DIR") or DEFAULT_VAULT)
    parent = os.path.dirname(path)
    retry = "wait for the share to mount again and rerun; do not create a same-name device"
    if not os.path.isdir(parent):
        raise Fail(f"vault parent {parent} does not exist (share not mounted?)", EXIT_NO_VAULT,
                   "vault-unavailable", retry)
    try:
        same = same_volume(parent, SIM_ROOT)
    except OSError as exc:
        raise Fail(f"cannot stat vault: {exc}", EXIT_NO_VAULT, "vault-unavailable", retry)
    if same:
        raise Fail(f"{parent} is on the same volume as CoreSimulator (share not mounted?); "
                   "parking there would not free any space", EXIT_NO_VAULT,
                   "vault-unavailable", retry)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise Fail(f"cannot create {path}: {exc}", EXIT_NO_VAULT, "vault-unavailable", retry)
    if not os.access(path, os.W_OK):
        raise Fail(f"{path} is not writable (launchd jobs may lack network-volume access)",
                   EXIT_NO_VAULT, "vault-unavailable", retry)
    return path


def archive_paths(vault, name):
    # Test devices (SVTEST ...) must never touch the real vault.
    if name.startswith("SVTEST") and os.path.realpath(vault) == os.path.realpath(DEFAULT_VAULT):
        raise Fail(f"'{name}' is a test device but the vault is the real one ({vault})",
                   kind="test-guard", next_step="set SIM_VAULT_DIR to a test vault")
    if "/" in name or name.startswith(".") or RESTORING.strip() in name:
        raise Fail(f"unsupported simulator name for a file name: {name!r}", kind="bad-name",
                   next_step="rename the simulator")
    return os.path.join(vault, f"{name}.tar"), os.path.join(vault, f"{name}.json")


def read_meta(meta_path):
    """The archive's metadata, or None when there is none. Raises Fail when unreadable."""
    try:
        with open(meta_path) as handle:
            meta = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Fail(f"cannot read {meta_path}: {exc}", kind="bad-metadata",
                   next_step="fix or remove the metadata file by hand")
    if not isinstance(meta, dict) or "udid" not in meta:
        raise Fail(f"{meta_path} is not sim-vault metadata", kind="bad-metadata",
                   next_step="fix or remove the metadata file by hand")
    return meta


def open_private(path, mode="w"):
    """Create path exclusively with mode 0600, refusing symlinks or existing files."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        return os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)
        remove_file(path)
        raise


def write_json(path, data, *, private=False):
    if not private:
        with open(path + ".partial", "w") as handle:
            json.dump(data, handle, indent=2)
        os.replace(path + ".partial", path)
        return
    partial = f"{path}{partial_token()}.partial"
    created = False
    try:
        with open_private(partial, "w") as handle:
            created = True
            json.dump(data, handle, indent=2)
        os.replace(partial, path)
    except BaseException:
        if created:
            remove_file(partial)
        raise


def safe_key(name):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]
    return f"{safe}-{hashlib.sha1(name.encode()).hexdigest()[:8]}"


def record_path(name):
    return os.path.join(RECORD_DIR, safe_key(name) + ".json")


def save_record(name, meta):
    """Local copy of the archive's identity, so a same-name blank device is still caught
    (10) while the share is unmounted."""
    try:
        os.makedirs(RECORD_DIR, exist_ok=True)
        write_json(record_path(name), {k: meta.get(k) for k in ("name", "udid", "restoredAs",
                                                                "runtime", "parkedAt")})
    except OSError as exc:
        log(f"warning: could not keep a local record of the archive: {exc}")


def archive_info(name, runtimes=None):
    """(meta, info) of the archive. meta is None when there is none or it cannot be read;
    info carries vaultError when the vault is unavailable, and source=local-record when
    the identity comes from this Mac's record instead."""
    try:
        vault = vault_dir()
        tar_path, meta_path = archive_paths(vault, name)
        meta = read_meta(meta_path)
        info = {"source": "vault"}
        if meta is not None and not os.path.exists(tar_path):
            info.update(error="metadata without a tar", udid=meta.get("udid"),
                        restoredAs=meta.get("restoredAs"))
            meta = None
        elif meta is None and os.path.exists(tar_path):
            info.update(error="tar without metadata (archive-incomplete)")
    except Fail as exc:
        if exc.code != EXIT_NO_VAULT:
            return None, {"error": str(exc)}
        info = {"vaultError": str(exc), "source": "local-record"}
        try:
            with open(record_path(name)) as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            meta = None
    if meta is None:
        return None, dict(info, exists=False)
    info.update(exists=True, udid=meta.get("udid"), restoredAs=meta.get("restoredAs"),
                runtime=meta.get("runtime"),
                runtimeAvailable=runtime_available(meta.get("runtime"), runtimes),
                parkedAt=meta.get("parkedAt"), bytes=meta.get("bytes"), host=meta.get("host"))
    return meta, info


def matches_archive(udid, meta):
    return None if meta is None else udid in (meta.get("udid"), meta.get("restoredAs"))


# -- locks -----------------------------------------------------------------------------

def lock_path(name):
    return os.path.join(LOCK_DIR, safe_key(name) + ".lock")


def read_holder(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError):
        return True
    return True


def describe_holder(holder):
    if not holder:
        return "another process"
    text = (f"pid {holder.get('pid')} ({' '.join(holder.get('argv', []))}) "
            f"in {holder.get('cwd')} since {holder.get('startedAt')}")
    if not pid_alive(holder.get("pid")):
        text += (" - that process has exited; a tar or simctl it started still holds "
                 "the lock and releases it when it finishes")
    return text


def lock_holder(name):
    """Holder info when the device lock is taken, else None. Does not keep the lock."""
    path = lock_path(name)
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return read_holder(path) or {}
    finally:
        os.close(fd)   # closing drops a lock we may just have taken
    return None


class DeviceLock:
    """Per-name flock. The kernel drops it when the holder and every child given the fd are
    gone, so there is nothing to reclaim. The file is never deleted: a waiter holding the old
    inode would get in alongside a newcomer on the new one."""

    def __init__(self, name, wait=DEFAULT_WAIT, *, key=None):
        self.name = name or key
        self.wait = wait
        self.path = lock_path(name) if key is None else os.path.join(LOCK_DIR, key + ".lock")
        self.fd = None

    def __enter__(self):
        os.makedirs(LOCK_DIR, exist_ok=True)
        # Not "w": opening with O_TRUNC would wipe the holder's details for everyone.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.time() + self.wait
        with phase("lockWait"):
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() >= deadline:
                        holder = read_holder(self.path)
                        os.close(fd)
                        raise Fail(f"'{self.name}' is in use by {describe_holder(holder)}; "
                                   f"waited {self.wait:g}s", EXIT_BUSY, "busy",
                                   "wait for that process to finish, or pass a longer "
                                   "--wait; if the same holder returns 8 twice, stop and "
                                   "report it")
                    time.sleep(0.2)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "argv": sys.argv, "cwd": os.getcwd(),
                                 "startedAt": now_iso()}).encode())
        self.fd = fd
        HELD_FDS.append(fd)
        return self

    def __exit__(self, *exc):
        # Close only, never LOCK_UN: flock belongs to the open file description, which a
        # child given the fd shares, and LOCK_UN would release it for that child too.
        # Children are waited for or killed before this runs; one still alive after an
        # abrupt death of this process keeps the lock until it exits.
        HELD_FDS.remove(self.fd)
        os.close(self.fd)


def try_lock(name, *, key=None):
    """Non-blocking DeviceLock for gc: an entered lock (leave it with __exit__), or None
    when someone holds it."""
    lock = DeviceLock(name, wait=0, key=key)
    try:
        return lock.__enter__()
    except Fail:
        return None


def hold_busy_locks(templates, udid):
    """Take each project lock file (flock) without waiting and keep it until park is done.
    A missing file is created (empty, never truncated or written): a project that creates
    it later to flock it must find it taken. The lock of a freshly restored device (new
    UDID) never exists yet, so skipping it would leave the whole park unguarded."""
    held = []
    try:
        for template in templates:
            path = template.replace("{udid}", udid)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
            except OSError as exc:
                raise Fail(f"cannot open or create --busy-lock {path}: {exc}; not parking",
                           kind="busy-lock-unavailable",
                           next_step="fix the path (does its directory exist?) and rerun")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise Fail(f"{path} is held; the device is in use", EXIT_BUSY, "busy",
                           "wait for that run to finish, then park again")
            held.append(fd)
            HELD_FDS.append(fd)
    except BaseException:
        release_fds(held)
        raise
    return held


def release_fds(fds):
    for fd in fds:
        if fd in HELD_FDS:
            HELD_FDS.remove(fd)
        os.close(fd)


# -- leases ----------------------------------------------------------------------------
# A lease says "an owner is using this device across commands". Its file lives in
# leases/<name key>/<owner sha1>.json. _boot.json in the same directory records that
# `ensure --boot --lease` booted the device: the last lease released with --shutdown shuts
# it down, whichever owner that is.

def lease_dir(name):
    return os.path.join(LEASE_DIR, safe_key(name))


def lease_path(name, owner):
    return os.path.join(lease_dir(name), lease_filename(owner))


def lease_filename(owner):
    return hashlib.sha1(owner.encode()).hexdigest() + ".json"


def held_lock_path(path):
    return os.path.splitext(path)[0] + ".lock"


def boot_record_path(name):
    return os.path.join(lease_dir(name), "_boot.json")


def lease_entries(directory):
    try:
        return sorted(os.listdir(directory))
    except (FileNotFoundError, NotADirectoryError):
        return []


def unreadable_lease(path, exc):
    return Fail(f"cannot read lease {path}: {exc}; it may be someone's live lease, "
                "so nothing was shut down or deleted", kind="lease-unreadable",
                next_step="fix its permissions or find out whose it is; remove it "
                          "by hand only once you know it is stale")


def read_lease_files(directory, entries, strict=False, quiet=False, unreadable=None):
    """strict: a lease that cannot be read or is not a lease raises Fail (lease-unreadable)
    instead of being skipped, for commands that act on "nobody uses it" (park, shutdown,
    ensure --deny-other-leases): an unreadable lease may be someone's live one."""
    out = []
    for entry in entries:
        if entry.startswith("_") or not entry.endswith(".json"):
            continue
        path = os.path.join(directory, entry)
        try:
            with open(path) as handle:
                lease = json.load(handle)
            if not (isinstance(lease, dict) and isinstance(lease.get("owner"), str)):
                raise ValueError("not a lease object with an owner")
            out.append(dict(lease, file=path))
        except FileNotFoundError:
            continue   # released meanwhile
        except (OSError, ValueError) as exc:
            if strict:
                raise unreadable_lease(path, exc)
            if unreadable is not None:
                unreadable.append(path)
            if not quiet:
                log(f"warning: unreadable lease {entry}: {exc}")
    return out


def read_leases(name, strict=False):
    directory = lease_dir(name)
    return read_lease_files(directory, lease_entries(directory), strict)


def held_alive(path):
    """Probe an exclusive held lease without conflicting with other readers."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def scan_held_leases(directory, entries, leases):
    """Include live and dead held leases, even when an older release removed the JSON."""
    by_lock = {held_lock_path(lease["file"]): lease for lease in leases
               if lease.get("kind") == "held"}
    for entry in entries:
        if entry.endswith(".lock"):
            by_lock.setdefault(os.path.join(directory, entry), {"kind": "held", "mode": "exclusive"})
    held = []
    for path, lease in sorted(by_lock.items()):
        try:
            held.append(dict(lease, lockFile=path, alive=held_alive(path)))
        except OSError as exc:
            held.append(dict(lease, lockFile=path, alive=True, unreadable=str(exc)))
    return held


def lease_snapshot(directory, quiet=False):
    entries = lease_entries(directory)
    unreadable = []
    leases = read_lease_files(directory, entries, quiet=quiet, unreadable=unreadable)
    return leases, scan_held_leases(directory, entries, leases), unreadable


class ProcBsdInfo(ctypes.Structure):
    # struct proc_bsdinfo from Darwin's sys/proc_info.h (PROC_PIDTBSDINFO).
    _fields_ = [("flags", ctypes.c_uint32), ("status", ctypes.c_uint32),
                ("ids", ctypes.c_uint32 * 10), ("comm", ctypes.c_char * 16),
                ("name", ctypes.c_char * 32), ("counts_and_tty", ctypes.c_uint32 * 6),
                ("start_seconds", ctypes.c_uint64), ("start_microseconds", ctypes.c_uint64)]


def process_identity(pid):
    """Return the kernel's microsecond start time and command name for a live PID."""
    if sys.platform != "darwin" or type(pid) is not int or not 0 < pid < 2**31:
        return None
    try:
        proc_pidinfo = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidinfo
    except OSError:
        return None
    proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                             ctypes.c_void_p, ctypes.c_int)
    info = ProcBsdInfo()
    size = ctypes.sizeof(info)
    if proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size or info.status == 5:  # SZOMB
        return None
    start = info.start_seconds * 1_000_000 + info.start_microseconds
    return start, os.fsdecode(info.name or info.comm)


SESSION_ANCESTOR_COMMS = {"claude", "codex", "login", "sshd", "sshd-session", "tmux"}


class ProcessInspectionFailed(Exception):
    pass


def ps_output(*args):
    try:
        result = subprocess.run(["/bin/ps", *args], capture_output=True, text=True,
                                errors="replace", env=dict(os.environ, LC_ALL="C", TZ="UTC"),
                                timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def comm_basename(comm):
    return os.path.basename(comm.strip()).lstrip("-")


def process_ancestors():
    error = "Process inspection failed; could not determine the session anchor"
    output = ps_output("-axww", "-o", "pid=,ppid=,comm=")
    if output is None:
        raise ProcessInspectionFailed(error)
    processes = {}
    for row in output.splitlines():
        parts = row.split(None, 2)
        if len(parts) == 3 and parts[0].isdecimal() and parts[1].isdecimal():
            pid, parent, comm = parts
            processes[int(pid)] = int(parent), comm_basename(comm)
    pid = os.getpid()
    ancestors = {}
    while pid in processes:
        parent, _ = processes[pid]
        if parent == 0:
            return ancestors
        if parent not in processes or parent in ancestors:
            break
        ancestors[parent] = processes[parent][1]
        pid = parent
    raise ProcessInspectionFailed(error)


def env_pid(key):
    try:
        pid = int(os.environ.get(key, ""))
    except ValueError:
        return None
    return pid if 0 < pid < 2**31 else None


def ps_identity(pid):
    output = ps_output("-ww", "-p", str(pid), "-o", "lstart=,comm=")
    parts = output.split(None, 5) if output is not None else []
    if len(parts) == 6:
        return " ".join(parts[:5]), parts[5].strip()
    return None


def anchor_record(pid, found_by, comm=None):
    identity = process_identity(pid)
    source = "proc_pidinfo"
    if identity is None:
        identity = ps_identity(pid)
        source = "ps" if identity is not None else None
    start, observed_comm = identity if identity is not None else (None, None)
    comm = comm or (comm_basename(observed_comm) if observed_comm else None)
    anchor = {"pid": pid, "startedAt": start, "startSource": source,
              "comm": comm, "foundBy": found_by}
    if comm == "codex":
        session_source = "CODEX_THREAD_ID"
    elif comm == "claude" or found_by == "CLAUDE_PID":
        session_source = "CLAUDE_CODE_SESSION_ID"
    else:
        # A shell or a test override can be the process anchor for either harness.
        session_source = "CODEX_THREAD_ID" if os.environ.get("CODEX_THREAD_ID") else "CLAUDE_CODE_SESSION_ID"
    session = os.environ.get(session_source)
    if session:
        anchor.update(session=session, sessionSource=session_source)
    return anchor


def session_anchor():
    override = env_pid("SIM_VAULT_ANCHOR_PID")
    if override is not None:
        return anchor_record(override, "SIM_VAULT_ANCHOR_PID")
    ancestors = process_ancestors()
    claude = env_pid("CLAUDE_PID")
    if claude in ancestors:
        return anchor_record(claude, "CLAUDE_PID", ancestors[claude])
    # Codex's process is found via this allow-list. Its thread ID identifies the session.
    for pid, comm in ancestors.items():
        if comm in SESSION_ANCESTOR_COMMS:
            return anchor_record(pid, "ancestor", comm)
    return None


def same_anchor(stored, caller):
    if stored is None or caller is None:
        return stored is caller
    if (stored["pid"] != caller["pid"] or stored["startedAt"] is None
            or stored["startSource"] != caller["startSource"]
            or stored["startedAt"] != caller["startedAt"]):
        return False
    stored_session = stored.get("session")
    if (stored_session is None and caller.get("sessionSource") == "CLAUDE_CODE_SESSION_ID"
            and stored.get("comm") != "codex"):
        return True
    return stored_session == caller.get("session")


def anchor_alive(anchor):
    if anchor is None:
        return True
    pid = anchor["pid"]
    if not pid_alive(pid):
        return False
    identity = None
    if anchor["startSource"] == "proc_pidinfo":
        identity = process_identity(pid)
    elif anchor["startSource"] == "ps":
        identity = ps_identity(pid)
    return identity is None or identity[0] == anchor["startedAt"]


def canonical_scope(path):
    try:
        if sys.platform != "darwin":
            if os.path.isdir(path):
                return os.path.realpath(path)
            raise NotADirectoryError(path)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            # Darwin's F_GETPATH returns the filesystem's spelling of the directory.
            return os.fsdecode(fcntl.fcntl(fd, 50, bytes(1024)).split(b"\0", 1)[0])
        finally:
            os.close(fd)
    except OSError as exc:
        raise Fail(f"--lease-scope must be an existing directory ({path!r}): {exc}",
                   EXIT_USAGE, "usage", "pass an existing directory with --lease-scope") from exc


def same_scope(stored, scope):
    if stored == scope:
        return True
    if not isinstance(stored, str):
        return False
    # Older leases used realpath, which may have kept an alternate APFS spelling.
    try:
        return os.path.samefile(stored, scope)
    except OSError:
        return False


def scoped_sessions(leases, scope):
    return (lease for lease in leases if lease.get("kind") == "session"
            and same_scope(lease.get("scope"), scope))


def session_lease(leases, scope, anchor):
    return next((lease for lease in scoped_sessions(leases, scope)
                 if same_anchor(lease.get("anchor"), anchor)), None)


def whoami(name, scope, anchor):
    directory = lease_dir(name)
    leases = read_lease_files(directory, lease_entries(directory), quiet=True)
    lease = session_lease(leases, scope, anchor)
    return {"anchor": anchor, "lease": lease["owner"] if lease else None}


def session_busy(name, lease, caller_anchor):
    anchor = lease.get("anchor")
    anchor_state = "unknown" if anchor is None else "alive" if anchor_alive(anchor) else "exited"
    scope_state = "exists" if os.path.isdir(lease["scope"]) else "missing"
    age = lease_age(lease)
    age_text = f"{age:.0f}s" if age is not None else "unknown"
    resume = (" It may be your own lease from before a resume."
              if not same_anchor(anchor, caller_anchor) else "")
    owner = lease["owner"]
    return Fail(f"'{name}' already has session lease {owner} for this scope "
                f"(age {age_text}, anchor {anchor_state}, scope {scope_state}).{resume}",
                EXIT_BUSY, "busy", shlex.join(["sim-vault", "release", name, "--lease", owner]))


def legacy_scope_lease(name, scopes, leases):
    """Keep the old DayDo/DSA owner-file guard during the wrapper transition."""
    if name.startswith("DD "):
        aliases = {"DD iPhone 18 Pro": "iphone", "DD iPad Pro 11": "ipad"}
        project = "daydo"
        target = aliases.get(name, re.sub(r"[^A-Za-z0-9]", "_", name))
    elif name.startswith("DSA ") and "/" not in name:
        project = "dsa"
        target = name.replace(" ", "-")
    else:
        return None
    for scope in scopes:
        scope_key = hashlib.sha1(os.fsencode(scope)).hexdigest()
        path = os.path.join(STATE_ROOT, "owners", f"{project}-{scope_key}-{target}")
        try:
            with open(path, encoding="utf-8", errors="surrogateescape") as handle:
                owner = handle.read().rstrip("\n")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise unreadable_lease(path, exc) from exc
        previous = next((lease for lease in leases if lease["owner"] == owner), None)
        if previous:
            return previous
    return None


def verified_held_holder(lease):
    if not lease["alive"] or lease.get("unreadable") or lease.get("pidStartedAt") is None:
        return None
    pid = lease.get("pid")
    identity = process_identity(pid)
    if identity and identity[0] == lease["pidStartedAt"]:
        return pid, identity[1]
    return None


def held_label(lease):
    return "held lease " + (lease.get("owner") or os.path.basename(lease["lockFile"])[:8])


def describe_held(lease, holder):
    if lease.get("unreadable"):
        return f"{held_label(lease)} is unreadable: {lease['unreadable']}"
    description = f"pid {holder[0]} ({holder[1]})" if holder else "a process that inherited its lock"
    return f"{held_label(lease)} held by {description}"


def require_readable_held(leases):
    for lease in leases:
        if lease.get("unreadable"):
            raise unreadable_lease(lease["lockFile"], lease["unreadable"])


def held_candidate_hint(lease):
    return f"Find candidate processes with `lsof {shlex.quote(lease['lockFile'])}`."


def held_leased_fail(name, leases, *, kind="busy"):
    require_readable_held(leases)
    descriptions, tips = [], []
    for lease in leases:
        holder = verified_held_holder(lease)
        descriptions.append(describe_held(lease, holder))
        if holder:
            tips.append(f"If it is your own leftover process, use `kill {holder[0]}`.")
        else:
            tips.append(held_candidate_hint(lease))
    tips.append("Wait for the holder to finish. The held lease disappears once it exits.")
    return Fail(f"'{name}' is leased by {'; '.join(descriptions)}", EXIT_BUSY, kind, " ".join(tips))


def write_held_lease(name, owner, udid, mode):
    """Under DeviceLock, publish a new held lease and return its open, exclusive fd.

    The caller owns the fd and passes it only to the processes using the lease.
    """
    path = lease_path(name, owner)
    lock_file = held_lock_path(path)
    try:
        already_held = held_alive(lock_file)
    except OSError as exc:
        raise unreadable_lease(lock_file, exc) from exc
    if already_held:
        existing = read_holder(path)
        if not isinstance(existing, dict) or existing.get("kind") != "held":
            existing = {}
        raise held_leased_fail(name, [dict(existing, lockFile=lock_file, alive=True)])
    if os.path.lexists(path):
        raise Fail(f"'{name}' already has a lease for {owner}", EXIT_BUSY, "busy",
                   f"wait for that run to finish; {stale_lease_hint(name)}")
    directory = lease_dir(name)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".held-", dir=directory)
    try:
        # Passive probes only open the final path. Publish an already locked inode.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.replace(temporary, lock_file)
        pid = os.getpid()
        identity = process_identity(pid)
        stamp = now_iso()
        write_json(path, {"owner": owner, "name": name, "udid": udid, "mode": mode, "kind": "held",
                          "pid": pid, "pidStartedAt": identity[0] if identity else None,
                          "createdAt": stamp, "updatedAt": stamp})
    except BaseException:
        os.close(fd)
        # The rename consumed the temporary path if the lock was published.
        remove_file(temporary if os.path.exists(temporary) else lock_file)
        remove_file(path + ".partial")
        raise
    return fd


def read_boot_record(name):
    return read_boot_record_file(boot_record_path(name))


def read_boot_record_file(path):
    try:
        with open(path) as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if isinstance(record.get("by"), str):
        record["by"] = [record["by"]]
    return record


def reconcile_boot_record(name, devices):
    """The boot record is written before `simctl boot`, so it can outlive a boot that never
    happened or a device shut down by hand. Keep it only while that device is running.
    Caller holds the lock."""
    record = read_boot_record(name)
    if not record:
        return None
    state = next((d["state"] for d in devices if d["udid"] == record.get("udid")), None)
    if state in (None, "Shutdown"):
        remove_file(boot_record_path(name))
        return None
    return record


def blocking_leases(leases, lease=None, exclusive=False, deny_other_leases=False, allow_leases=()):
    """Exclusive leases always block other owners, even on the allow list."""
    others = [l for l in leases if not lease or l.get("owner") != lease]
    return [l for l in others if exclusive or l.get("mode") == "exclusive"
            or (deny_other_leases and l.get("owner") not in allow_leases)]


def stale_lease_hint(name):
    return ("release someone else's lease only once its owner's worktree no longer exists or "
            "that session has ended: "
            f"`sim-vault release '{name}' --lease <owner>`")


def leased_fail(name, leases):
    return Fail(f"'{name}' is leased by {describe_leases(leases)}", EXIT_BUSY, "busy",
                f"wait for that run to finish; {stale_lease_hint(name)}")


def remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def drop_stale_leases(name, present_udids):
    """Drop legacy leases and boot records whose devices are gone. Caller holds the lock."""
    for lease in read_leases(name):
        if lease.get("kind") not in ("held", "session") and lease.get("udid") not in present_udids:
            log(f"dropping lease of {lease.get('owner')}: device {lease.get('udid')} is gone")
            remove_file(lease["file"])
    record = read_boot_record(name)
    if record and record.get("udid") not in present_udids:
        remove_file(boot_record_path(name))


def release_boot_record(path, record, owners, remaining):
    if not record:
        return
    if not remaining:
        # A later owner must not inherit shutdown duty for a device it did not boot.
        remove_file(path)
    elif any(owner in owners for owner in record.get("by", [])):
        # A rename can leave leases for two devices in the same directory.
        next_owners = [l["owner"] for l in remaining
                       if l.get("udid") == record.get("udid") and l.get("owner") is not None]
        if next_owners:
            write_json(path, dict(record, by=next_owners))
        else:
            remove_file(path)


def reap_boot_record(directory, leases, live, dead):
    path = os.path.join(directory, "_boot.json")
    record = read_boot_record_file(path)
    if not record:
        return
    udid = record.get("udid")
    states = {d["udid"]: d["state"] for d in all_devices()}
    if states.get(udid) in (None, "Shutdown"):
        remove_file(path)
        return
    owners = {lease["owner"] for lease in dead if lease.get("udid") == udid and "owner" in lease}
    # A v3 release can remove the JSON. In that case the owner hash is all we have.
    orphan_locks = {os.path.basename(l["lockFile"]) for l in dead if "file" not in l}
    owners.update(o for o in record.get("by", [])
                  if held_lock_path(lease_filename(o)) in orphan_locks)
    if owners:
        remaining = live + [l for l in leases if l.get("kind") != "held" and l.get("udid") in states]
        release_boot_record(path, record, owners, remaining)


def reap_held_leases(directory, dry_run=False, *, snapshot=None):
    """Under DeviceLock, drop dead held leases and reconcile their boot duty without shutdown."""
    leases, held, unreadable = snapshot if snapshot is not None else lease_snapshot(directory, quiet=True)
    live = [l for l in held if l["alive"]]
    dead = [l for l in held if not l["alive"]]
    if dead and not dry_run:
        if not unreadable and not any(l.get("unreadable") for l in live):
            reap_boot_record(directory, leases, live, dead)
        for lease in dead:
            if "file" in lease:
                remove_file(lease["file"])
            remove_file(lease["lockFile"])
    return live, dead


def same_held_owner(name, owner, held):
    if owner is None:
        return []
    path = held_lock_path(lease_path(name, owner))
    return [l for l in held if l["lockFile"] == path]


def lease_view(lease):
    return {k: lease.get(k) for k in ("owner", "udid", "mode", "pid", "createdAt",
                                      "updatedAt")}


def describe_leases(leases):
    return "; ".join(describe_held(l, verified_held_holder(l)) if "lockFile" in l else
                     f"{l.get('owner')} ({l.get('mode')}, since {l.get('createdAt')}, "
                     f"pid {l.get('pid')})" for l in leases)


# -- quiet / archive helpers ----------------------------------------------------------

def open_uncached(path, strict=True):
    """Open path for reading with F_NOCACHE set before the first read, so a read-back
    really fetches the bytes from the share: a plain second read of a file just written
    came from the Mac's cache with no network traffic (2026-11). strict: a failure to set
    the flag raises Fail (park's verification would prove nothing); otherwise warn."""
    fd = os.open(path, os.O_RDONLY)
    try:
        if F_NOCACHE is not None:
            try:
                fcntl.fcntl(fd, F_NOCACHE, 1)
            except OSError as exc:
                if strict:
                    raise Fail(f"cannot turn off read caching for {path}: {exc}; the read-back "
                               "would not prove the share holds the archive; the simulator "
                               "was kept", next_step="check the vault share, then rerun")
                log(f"warning: cannot turn off read caching for {path}: {exc}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def stream_into_tar(path, tar_args, strict=True):
    """Read path once, feeding it to `tar <tar_args> -f -`; return (sha256, tar ok)."""
    digest = hashlib.sha256()
    handle = open_uncached(path, strict)
    proc = subprocess.Popen(["tar", *tar_args, "-f", "-"], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            pass_fds=tuple(HELD_FDS))
    try:
        tar_open = True
        with handle:
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                digest.update(chunk)
                if tar_open:
                    try:
                        proc.stdin.write(chunk)
                    except BrokenPipeError:
                        # tar quit on a corrupt stream; keep hashing so the checksum stays a
                        # whole-file check, and let tar's exit code report the failure.
                        tar_open = False
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        stderr = proc.stderr.read().decode(errors="replace").strip()
        ok = proc.wait() == 0
    finally:
        handle.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if not ok:
        log(f"tar: {stderr[-500:]}")
    return digest.hexdigest(), ok


def file_sha256(path):
    digest = hashlib.sha256()
    with open_uncached(path) as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def newest_mtime(device_dir, now):
    """Newest mtime of anything under device_dir, ignoring timestamps in the future (a
    file stamped ahead of the clock would otherwise look busy forever)."""
    latest, path_of = 0.0, None
    for root, dirs, files in os.walk(device_dir):
        for entry in dirs + files:
            path = os.path.join(root, entry)
            try:
                mtime = os.lstat(path).st_mtime
            except OSError:
                continue
            if latest < mtime <= now + 1:
                latest, path_of = mtime, path
    return latest, path_of


def wait_until_quiet(device_dir):
    """After shutdown the device keeps flushing files for a while; archiving then gave a
    structurally broken tar with exit code 0 (2026-11 test). Wait for mtimes to settle."""
    deadline = time.time() + QUIET_TIMEOUT
    while True:
        now = time.time()
        latest, path = newest_mtime(device_dir, now)
        if now - latest >= QUIET_SECONDS:
            return
        if now >= deadline:
            raise Fail(f"{device_dir} is still being written after {QUIET_TIMEOUT}s "
                       f"(latest: {path}); not archiving", EXIT_BUSY, "busy",
                       "something is using the device; find it, stop it, then rerun")
        time.sleep(POLL)


def overwrite_message(name, old, udid, tar_exists=True):
    if not tar_exists:
        return (f"the metadata of '{name}' came from another device ({old.get('udid')}) and "
                "its tar is missing, so that archive cannot be restored any more",
                "after checking nothing of that archive is needed, rerun with --replace")
    if runtime_available(old.get("runtime")):
        return (f"the archive of '{name}' came from another device ({old.get('udid')}), "
                f"not {udid}; parking would replace it",
                "do not add --replace on your own: restore that archive instead (the local "
                "device has to go first; check it holds nothing you need), or pass --replace "
                "only once dropping the archive is decided")
    return (f"the archive of '{name}' came from another device ({old.get('udid')}) and its "
            f"runtime {old.get('runtime')} is gone, so it cannot be restored any more",
            "after checking nothing in that archive is needed, rerun with --replace")


# A park writes <name>.tar.<token>.partial and <name>.json.<token>.partial, the token
# unique per run, so a run cleaning up after itself never removes what an earlier,
# interrupted run left. v2 used <name>.{tar,json}.partial without a token.
PARTIAL_RE = re.compile(r"(?P<stem>.*)\.(?P<ext>tar|json)(?P<token>\.\d+-[0-9a-f]+)?\.partial")


def partial_token():
    return f".{os.getpid()}-{os.urandom(3).hex()}"


def pending_meta_partials(tar_path, meta_path):
    """Metadata .partial files whose park was killed between its two renames: the tar of
    the same run is in place (its .partial is gone), the metadata is not. Files only.
    None when the vault cannot be listed or the tar cannot be stat'ed: unknown is not the
    same as "none"."""
    try:
        os.stat(tar_path)
    except FileNotFoundError:
        return []
    except OSError as exc:
        log(f"warning: cannot stat {tar_path} to look for metadata of an interrupted park: "
            f"{exc}")
        return None
    vault, base = os.path.split(meta_path)
    stem = base[:-len(".json")]
    out = []
    try:
        entries = sorted(os.listdir(vault))
    except OSError as exc:
        log(f"warning: cannot list {vault} to look for metadata of an interrupted park: {exc}")
        return None
    for entry in entries:
        match = PARTIAL_RE.fullmatch(entry)
        if not match or match["stem"] != stem or match["ext"] != "json":
            continue
        token = match["token"] or ""
        if not os.path.exists(f"{tar_path}{token}.partial"):
            out.append(os.path.join(vault, entry))
    return out


def settle_pending(tar_path, meta_path):
    """Under the device lock: finish what a park killed between its renames left.
    Returns "none" (nothing pending, or the metadata in place already matches the tar:
    the leftover .partial files are stale and removed), "paired" (a .partial matching the
    tar became the metadata), or "failed" (pending, but none could be checked or matched;
    everything is left as it is)."""
    pending = pending_meta_partials(tar_path, meta_path)
    if pending is None:
        return "failed"
    if not pending:
        return "none"
    try:
        actual = file_sha256(tar_path)
        current = read_meta(meta_path)
        if current is not None and current.get("sha256") == actual:
            # The pair is settled; a stale .partial that cannot be removed is only clutter.
            for path in pending:
                log(f"removing {path}: the archive is already paired")
                try:
                    remove_file(path)
                except OSError as exc:
                    log(f"warning: could not remove {path}: {exc}")
            return "none"
        for path in pending:
            try:
                with open(path) as handle:
                    wanted = json.load(handle).get("sha256")
            except (OSError, ValueError, AttributeError) as exc:
                log(f"warning: {path} is unreadable ({exc})")
                continue
            if wanted == actual:
                os.replace(path, meta_path)
                log(f"paired {meta_path} with {tar_path} (left by an interrupted park)")
                return "paired"
        log(f"warning: no metadata matches {tar_path} ({', '.join(pending)} left as is)")
    except (OSError, ValueError, Fail) as exc:
        log(f"warning: could not check {', '.join(pending)} against {tar_path} ({exc}); "
            "left as is")
    return "failed"


# -- park ------------------------------------------------------------------------------

def park(name, shutdown=False, replace=False, wait=DEFAULT_WAIT, busy_locks=(),
         missing_ok=False, keep_posterboard=False, exclude_symbol_cache=False):
    vault = vault_dir()
    tar_path, meta_path = archive_paths(vault, name)
    with DeviceLock(name, wait):
        held, _ = reap_held_leases(lease_dir(name))
        if held:
            raise held_leased_fail(name, held)
        if settle_pending(tar_path, meta_path) == "failed":
            raise Fail(f"an interrupted park left the tar of '{name}' without matching "
                       "metadata, and pairing them failed (see the warnings above); not "
                       "parking over it, nothing was changed", kind="pending-pair",
                       next_step="rerun once the vault reads fine; if it keeps failing, stop "
                                 "and report it, do not delete the .partial files")
        device = find_one(name)
        if device is None:
            if missing_ok:
                log(f"no simulator named '{name}'; nothing to park")
                return {"action": "absent", "udid": None}
            raise Fail(f"no simulator named '{name}'", kind="absent",
                       next_step="nothing to park; check the name with `xcrun simctl list "
                                 "devices` (pass --missing-ok if absent is fine)")
        udid = device["udid"]
        note(udid=udid)
        drop_stale_leases(name, {d["udid"] for d in all_devices()})
        leases = read_leases(name, strict=True)
        if leases:
            raise Fail(f"'{name}' is leased by {describe_leases(leases)}", EXIT_BUSY, "busy",
                       f"wait for those runs to finish; {stale_lease_hint(name)}")
        held = hold_busy_locks(busy_locks, udid)
        try:
            result = park_locked(name, device, tar_path, meta_path, shutdown, replace,
                                 keep_posterboard, exclude_symbol_cache)
        finally:
            release_fds(held)
        remove_file(boot_record_path(name))
        return result


def park_locked(name, device, tar_path, meta_path, shutdown, replace, keep_posterboard=False,
                exclude_symbol_cache=False):
    udid = device["udid"]
    old = read_meta(meta_path)
    if old is None and os.path.exists(tar_path) and not replace:
        raise orphan_tar_fail(name, tar_path, EXIT_OVERWRITE)
    if old is not None and not replace and udid not in (old.get("udid"),
                                                        old.get("restoredAs")):
        message, next_step = overwrite_message(name, old, udid, os.path.exists(tar_path))
        raise Fail(message, EXIT_OVERWRITE, "overwrite", next_step)
    state = device["state"]
    if state in ("Booted", "Booting") and not shutdown:
        raise Fail(f"'{name}' is {state}", EXIT_BOOTED, "booted",
                   "shut it down first, or pass --shutdown if the project allows it")
    poster = poster_kb(udid)
    note(posterMB=poster // 1024)
    log(f"PosterBoard data: {poster // 1024} MB")
    excluded = []
    if poster > POSTER_LIMIT_KB:
        if keep_posterboard:
            raise Fail(f"PosterBoard data of '{name}' is {poster // 1024} MB (limit "
                       f"{POSTER_LIMIT_KB // 1024} MB); with --keep-posterboard it would go "
                       "into the archive and come back on every restore", EXIT_POSTER,
                       "posterboard-bloated",
                       "the device was not shut down or deleted; park without "
                       "--keep-posterboard to archive without the PosterBoard store, or "
                       "rebuild the PosterBoard store and park again")
        log(f"PosterBoard data is {poster // 1024} MB, over {POSTER_LIMIT_KB // 1024} MB: "
            "leaving it out of the archive; the system rebuilds it on the first boot after a "
            "restore (pass --keep-posterboard to refuse with 9 instead)")
        excluded += [rel for rel in POSTER_TRIM
                     if os.path.isdir(os.path.join(DEFAULT_SET, udid, rel))]
    if exclude_symbol_cache and os.path.isdir(os.path.join(DEFAULT_SET, udid, SYMBOL_CACHE)):
        excluded.append(SYMBOL_CACHE)
    with phase("shutdownQuiesce"):
        if state in ("Booted", "Booting"):
            log(f"shutting down {name}")
            shutdown_device(udid)
        elif state == "Shutting Down":
            log(f"{name} is shutting down; waiting")
            wait_for_shutdown(udid)
        elif state != "Shutdown":
            raise Fail(f"'{name}' is {state}",
                       next_step="wait until it is Shutdown, then rerun")
        device_dir = os.path.join(DEFAULT_SET, udid)
        # Files the device's processes had mapped keep dirty pages after they exit; the
        # kernel writes them back later, stamping a fresh mtime mid-archive (park then
        # returned 8 on about half of the parks right after a shutdown, 2026-11 test).
        # Flush them first.
        os.sync()
        wait_until_quiet(device_dir)
    # Anchored member paths: tar runs in DEFAULT_SET, so members are <udid>/...; an
    # unanchored pattern would also drop same-named folders inside app containers.
    exclude_args = []
    for rel in excluded:
        exclude_args += ["--exclude", f"^{udid}/{rel}"]
    with open(os.path.join(device_dir, "device.plist"), "rb") as handle:
        plist = plistlib.load(handle)

    token = partial_token()
    partial = f"{tar_path}{token}.partial"
    meta_partial = f"{meta_path}{token}.partial"
    created = set()
    log(f"archiving {name} ({udid}) to {tar_path}")
    proc = None
    try:
        for attempt in (1, 2):
            if attempt == 2:
                token = partial_token()
                partial = f"{tar_path}{token}.partial"
                meta_partial = f"{meta_path}{token}.partial"
            # Wall clock on purpose: compared with file mtimes below.
            started = time.time()
            digest = hashlib.sha256()
            size = 0
            # --no-xattrs --mac-metadata: plain bsdtar puts each xattr in a pax header and its
            # own reader rejects headers over about 1 MB ("Special header too large"); a MobileAsset
            # download left a 3.6 MB com.apple.ResourceFork that way (2026-11). AppleDouble
            # mac-metadata keeps the xattrs, also with COPYFILE_DISABLE=1. Extraction is unchanged,
            # so older archives still restore.
            with phase("write"):
                proc = subprocess.Popen(["tar", "--no-xattrs", "--mac-metadata", *exclude_args,
                                         "-C", DEFAULT_SET, "-cf", "-", udid],
                                        stdout=subprocess.PIPE, pass_fds=tuple(HELD_FDS))
                with open_private(partial, "wb") as out:
                    created.add(partial)
                    for chunk in iter(lambda: proc.stdout.read(CHUNK), b""):
                        digest.update(chunk)
                        out.write(chunk)
                        size += len(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                if proc.wait() != 0:
                    raise Fail("tar failed while archiving; the simulator was kept")
            written = digest.hexdigest()
            # One read-back pass checks both the bytes and that tar can walk the archive:
            # a matching checksum alone passed a broken archive in testing.
            with phase("readBack"):
                read_back, readable = stream_into_tar(partial, ["-t"])
            if os.path.getsize(partial) != size or read_back != written or not readable:
                raise Fail("archive read back from the vault is not intact; "
                           "the simulator was kept")
            # Someone may have booted the device while tar was reading it; the archive would
            # then be torn. Check before it replaces the previous one.
            state = device_state(udid)
            latest, path = newest_mtime(device_dir, time.time())
            if state == "Shutdown" and latest >= started and attempt == 1:
                # Shut down a moment ago and only written meanwhile (2026-11: IconsCache, Biome
                # files of a device whose processes were gone): flush, wait, archive once more.
                log(f"written while archiving ({path}); waiting and archiving once more")
                os.remove(partial)
                created.discard(partial)
                with phase("shutdownQuiesce"):
                    os.sync()
                    wait_until_quiet(device_dir)
                continue
            if state != "Shutdown" or latest >= started:
                raise Fail(f"'{name}' changed while it was being archived (now {state}; "
                           f"latest write {path}, {latest - started:+.1f}s after archiving "
                           "started); the old archive and the device were kept",
                           EXIT_BUSY, "busy", "let whoever uses it finish, then park again")
            if attempt == 2 and [d["udid"] for d in resolve(name)[0]] != [udid]:
                raise Fail(f"'{name}' no longer names only {udid} after archiving again; the "
                           "old archive and the local devices were kept", EXIT_BUSY, "busy",
                           "check `xcrun simctl list devices`, then park again")
            break
        meta = {
            "name": name,
            "udid": udid,
            "runtime": plist.get("runtime"),
            "deviceType": plist.get("deviceType"),
            "sha256": written,
            "bytes": size,
            "parkedAt": now_iso(),
            "host": socket.gethostname(),
            "toolVersion": TOOL_VERSION,
        }
        if excluded:
            meta["excluded"] = excluded
        if any(rel in POSTER_TRIM for rel in excluded):
            meta["posterboardTrimmed"] = True
        with open_private(meta_partial, "w") as handle:
            created.add(meta_partial)
            json.dump(meta, handle, indent=2)
        # Replace the previous archive only after the new one is verified.
        os.replace(partial, tar_path)
        os.replace(meta_partial, meta_path)
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        # Judge from the files, not a flag: a signal can land right after the tar replace
        # returns, before any flag is set. The metadata .partial exists only once the tar
        # is verified, and the tar .partial is gone only once it was renamed into place.
        # Only this run's own files: an earlier run's leftovers are not ours to remove.
        keep = []
        if meta_partial in created and os.path.exists(meta_partial) and not os.path.exists(partial):
            # The new tar is in place: its metadata must follow, or the pair is broken
            # (new tar, old checksum) and no longer restores. Only if they really match;
            # otherwise keep the .partial for the next park/restore or a human.
            def pair():
                with open(meta_partial) as handle:
                    if json.load(handle).get("sha256") != file_sha256(tar_path):
                        raise Fail("new tar does not match the new metadata")
                os.replace(meta_partial, meta_path)
            cleanup("pair the new metadata with the new tar", pair)
            keep = [meta_partial]
        for leftover in (partial, meta_partial):
            if leftover in keep or leftover not in created:
                continue
            if os.path.exists(leftover):
                cleanup(f"remove {leftover}", lambda p=leftover: os.remove(p))
        raise
    save_record(name, meta)
    state = device_state(udid)
    if state != "Shutdown":
        raise Fail(f"'{name}' was archived but is now {state}; the device was not deleted",
                   EXIT_BUSY, "busy", "the new archive is good; park again once it is "
                                      "no longer in use")
    with phase("delete"):
        simctl("delete", udid, keep_lock=True)
    note(bytes=size)
    log(f"parked {name}: {size / 1e9:.2f} GB in {time.monotonic() - RUN['started']:.0f}s "
        f"({phase_text(RUN['phases'])}); device deleted")
    result = {"action": "parked", "udid": udid, "bytes": size, "posterMB": poster // 1024}
    if excluded:
        result.update(excluded=excluded, posterboardTrimmed=bool(meta.get("posterboardTrimmed")))
    return result


# -- restore / ensure ------------------------------------------------------------------

def remove_stale_restoring(name, devices=None):
    """Delete devices left as '<name> [sim-vault restoring <pid>]' by an interrupted restore.
    The caller holds the device lock, and a restore (or a child of a killed one) keeps it
    until it stops, so every such device is a leftover; the pid in the name is only a hint
    and may have been reused."""
    prefix = name + RESTORING
    for device in devices if devices is not None else all_devices():
        if device["name"].startswith(prefix):
            log(f"deleting {device['name']} ({device['udid']}) left by an interrupted restore")
            simctl("delete", device["udid"], keep_lock=True)


def orphan_tar_fail(name, tar_path, code=EXIT_ERROR):
    return Fail(f"{tar_path} has no metadata next to it, so whose device it holds and its "
                "checksum are unknown; it may be the only copy of a logged-in device",
                code, "archive-incomplete",
                "stop and report it; do not create a same-name device or park over it. Once "
                "the owner decides the tar is not needed, move it away (or park with "
                "--replace)")


def not_archived_fail(name, existing, info):
    where = ("this Mac's record of the archive (the vault is unavailable)"
             if info.get("source") == "local-record" else "the archive")
    return Fail(f"'{name}' exists locally ({existing['udid']}) but is not the device in "
                f"{where} ({info.get('udid')}), which can still be restored",
                EXIT_NOT_ARCHIVED, "not-archived-device",
                "stop and report this; do not delete or park anything on your own. "
                "Once the owner decides: to use the archive and keep the local device, "
                f"`xcrun simctl rename {existing['udid']} '<other name>'` and rerun; to "
                "use the archive and drop the local device, `xcrun simctl delete "
                f"{existing['udid']}` and rerun; to keep the local device and drop the "
                f"archive, `sim-vault park '{name}' --replace`")


def restore_locked(name, warnings=None):
    """Restore without taking the lock (the caller holds it). Returns a result dict."""
    remove_stale_restoring(name)
    try:
        tar_path, meta_path = archive_paths(vault_dir(), name)
    except Fail:
        pass   # vault unavailable or a bad name: reported below where it matters
    else:
        # Unsettled, the metadata in place may describe another device than the tar:
        # deciding 3/4/10 or creating a device from it would be deciding on stale data.
        if settle_pending(tar_path, meta_path) == "failed":
            raise Fail(f"an interrupted park left the tar of '{name}' without matching "
                       "metadata, and pairing them failed (see the warnings above); nothing "
                       "was restored or created", kind="pending-pair",
                       next_step="rerun once the vault reads fine; if it keeps failing, stop "
                                 "and report it, do not delete the .partial files")
    existing = find_one(name, warnings)
    if existing is not None:
        note(udid=existing["udid"])
        meta, info = archive_info(name)
        matches = matches_archive(existing["udid"], meta)
        if matches is False and info.get("runtimeAvailable"):
            raise not_archived_fail(name, existing, info)
        log(f"{name} already exists, nothing to restore")
        return {"action": "existing", "udid": existing["udid"], "state": existing["state"],
                "matchesArchive": matches, "archive": info,
                "needsReplaceOnFirstPark": matches is False or (
                    meta is None and info.get("udid") is not None
                    and existing["udid"] not in (info.get("udid"), info.get("restoredAs")))
                    or "archive-incomplete" in (info.get("error") or "")}
    vault = vault_dir()
    tar_path, meta_path = archive_paths(vault, name)
    meta = read_meta(meta_path)
    if meta is None and os.path.exists(tar_path):
        raise orphan_tar_fail(name, tar_path)
    if meta is None or not os.path.exists(tar_path):
        raise Fail(f"no archive for '{name}' in {vault}", EXIT_NO_ARCHIVE, "no-archive",
                   "create the device if the project's policy allows it, or ask")
    if not runtime_available(meta.get("runtime")):
        raise Fail(f"runtime {meta.get('runtime')} is not installed", EXIT_NO_RUNTIME,
                   "runtime-missing", "create the device if the project's policy allows it; "
                                      "its first park then returns 7 and needs --replace")
    host = socket.gethostname()
    if meta.get("host") and meta["host"] != host:
        message = f"this archive was parked on {meta['host']}, not {host}"
        log(f"warning: {message}")
        if warnings is not None:
            warnings.append(message)

    os.makedirs(STAGING_ROOT, exist_ok=True)
    # Staging must be on the CoreSimulator volume so clone is a cheap APFS clone.
    staging = tempfile.mkdtemp(prefix="set-", dir=STAGING_ROOT)
    with open(os.path.join(staging, "owner.json"), "w") as handle:
        json.dump({"pid": os.getpid(), "name": name}, handle)
    temp_name = f"{name}{RESTORING}{os.getpid()}]"
    udid = None
    renamed = False
    clone_phase = None
    try:
        log(f"extracting {tar_path}")
        # Not strict: the checksum below still checks every byte.
        with phase("extract"):
            digest, extracted = stream_into_tar(tar_path, ["-C", staging, "-x"], strict=False)
        if not extracted:
            raise Fail("tar failed while extracting")
        if digest != meta.get("sha256"):
            raise Fail("archive checksum does not match its metadata; not restored")
        # CoreSimulatorService only scans the default set at startup, so a directory
        # copied into it stays invisible. Cloning from a private set goes through the
        # service and does not disturb other running simulators. The clone gets a
        # temporary name: the real name appearing means the restore finished.
        clone_phase = phase("clone").__enter__()
        udid = simctl("clone", meta["udid"], temp_name, DEFAULT_SET,
                      device_set=staging, keep_lock=True).strip()
        note(udid=udid)
        # The clone gets a new UDID, but the LaunchServices database stores app bundle
        # paths with the old one, so every user app fails with "Launchd job spawn failed"
        # (2026-11 test). It is a cache: drop it and the first boot rebuilds it.
        lsd = os.path.join(DEFAULT_SET, udid, "data", "var", "db", "lsd")
        if os.path.isdir(lsd):
            for entry in os.listdir(lsd):
                if entry.endswith(".csstore") and "LaunchServices-" in entry:
                    os.remove(os.path.join(lsd, entry))
        try:
            simctl("delete", meta["udid"], device_set=staging, keep_lock=True)
        except Fail as exc:
            log(f"warning: could not unregister staging copy: {exc}")
        # Without restoredAs the restored device looks foreign to its own archive (park 7,
        # restore 10), so a failed write aborts the restore. It is written before the
        # rename, so a finished restore always has it.
        meta["restoredAs"] = udid
        try:
            write_json(meta_path, meta, private=True)
        except OSError as exc:
            raise Fail(f"could not record the restored UDID in {meta_path}: {exc}; "
                       "the half-restored device was deleted", kind="vault-write",
                       next_step="check the vault is writable, then rerun")
        simctl("rename", udid, name, keep_lock=True)
        renamed = True
    except BaseException:
        if udid and not renamed:
            cleanup(f"delete the half-restored {temp_name}",
                    lambda: simctl("delete", udid, keep_lock=True))
        raise
    finally:
        if clone_phase is not None:
            clone_phase.__exit__()
        shutil.rmtree(staging, ignore_errors=True)
    save_record(name, meta)
    note(bytes=meta.get("bytes"))
    log(f"restored {name} as {udid} in {time.monotonic() - RUN['started']:.0f}s "
        f"({phase_text(RUN['phases'])}; archive kept at {tar_path})")
    return {"action": "restored", "udid": udid, "state": "Shutdown", "matchesArchive": True,
            "needsReplaceOnFirstPark": False,
            "archive": {"source": "vault", "exists": True, "udid": meta.get("udid"),
                        "restoredAs": udid, "runtime": meta.get("runtime"),
                        "runtimeAvailable": True, "parkedAt": meta.get("parkedAt"),
                        "bytes": meta.get("bytes"), "host": meta.get("host")}}


def restore(name, wait=DEFAULT_WAIT):
    warnings = []
    with DeviceLock(name, wait):
        reap_held_leases(lease_dir(name))
        result = restore_locked(name, warnings)
    result["warnings"] = warnings
    return result


def ensure(name, create=None, boot=False, lease=None, exclusive=False, wait=DEFAULT_WAIT,
           deny_other_leases=False, allow_leases=(), *, held_lease=False, expect_udid=None,
           session=None, lease_prefix=None, lease_scope=None):
    warnings = []
    if lease:
        note(leaseKind="session" if session is not None else "held" if held_lease else "legacy")
    with DeviceLock(name, wait), ExitStack() as held_fd:
        fd = None
        if session is not None:
            previous = next(scoped_sessions(read_leases(name, strict=True), session["scope"]), None)
            if previous:
                raise session_busy(name, previous, session["anchor"])
            lease = f"{lease_prefix or 'auto'}:{secrets.token_hex(4)}"
            if os.path.lexists(lease_path(name, lease)):
                raise Fail(f"'{name}' already has a lease for {lease}", EXIT_BUSY, "busy",
                           "rerun to generate a new session token")
        held, _ = reap_held_leases(lease_dir(name))
        same_owner = same_held_owner(name, lease, held)
        if same_owner:
            raise held_leased_fail(name, same_owner)
        if exclusive or deny_other_leases:
            require_readable_held(held)
        blocking = blocking_leases(held, lease, exclusive, deny_other_leases, allow_leases)
        if blocking:
            raise held_leased_fail(name, blocking)
        try:
            result = restore_locked(name, warnings)
        except Fail as exc:
            if exc.code not in (EXIT_NO_ARCHIVE, EXIT_NO_RUNTIME) or not create:
                raise
            # A 3 can still leave metadata behind (its tar is missing): the first park of
            # the new device then meets that metadata and returns 7.
            leftover_meta = exc.code == EXIT_NO_RUNTIME or os.path.exists(
                archive_paths(vault_dir(), name)[1])
            udid = create_device(name, create)
            result = {"action": "created", "udid": udid, "state": "Shutdown",
                      "matchesArchive": None if not leftover_meta else False,
                      "needsReplaceOnFirstPark": leftover_meta,
                      "archive": {"exists": leftover_meta}}
        udid = result["udid"]
        devices = all_devices()
        drop_stale_leases(name, {d["udid"] for d in devices})
        reconcile_boot_record(name, devices)
        manual = [l for l in read_leases(name) if l.get("kind") != "held"]
        mine = [l for l in manual if l.get("owner") == lease] if lease else []
        if mine and mine[0].get("kind") == "session":
            note(leaseKind="session")
            raise leased_fail(name, mine)
        if session is not None:
            # The wrappers' pwd -P can preserve the caller's APFS spelling.
            scopes = dict.fromkeys((session["scope"], os.path.realpath(lease_scope)))
            previous = legacy_scope_lease(name, scopes, manual)
            if previous:
                raise Fail(f"'{name}' has a legacy lease {previous['owner']} recorded for this scope",
                           EXIT_BUSY, "busy",
                           shlex.join(["sim-vault", "release", name, "--lease", previous["owner"]]))
        blocking = blocking_leases(manual, lease, exclusive)
        if blocking:
            raise leased_fail(name, blocking)
        if deny_other_leases:
            # A running device may pass this check. Only other owners' current leases count.
            others = [l for l in read_leases(name, strict=True)
                      if l.get("kind") != "held" and l.get("owner") != lease
                      and l.get("owner") not in allow_leases]
            if others:
                raise leased_fail(name, others)
        state = device_state(udid)
        reentry = bool(mine) and mine[0].get("udid") == udid
        if exclusive and not reentry and state != "Shutdown":
            raise Fail(f"'{name}' is {state} but has no lease: someone outside sim-vault is "
                       "using it", EXIT_BUSY, "busy",
                       "find who booted it (an open xcodebuild, a manual session), let them "
                       "finish and shut it down, then rerun")
        if expect_udid is not None and udid != expect_udid:
            note(udid=udid, action=result["action"])
            raise Fail(f"'{name}' resolved to {udid}, expected {expect_udid}",
                       kind="udid-changed", next_step="check the resolved device UDID")
        lease_file, wrote_new = None, False
        if lease:
            lease_file = lease_path(name, lease)
            stamp = now_iso()
            if reentry:
                record = dict(mine[0], updatedAt=stamp,
                              mode="exclusive" if exclusive else mine[0].get("mode"))
                record.pop("file", None)
            else:
                record = {"owner": lease, "udid": udid, "pid": os.getppid(), "createdAt": stamp,
                          "updatedAt": stamp, "mode": "exclusive" if exclusive else "shared"}
                if session is not None:
                    record.update(kind="session", **session)
                wrote_new = True
            # The lease goes in before booting, so a device we boot always has an owner.
            if held_lease:
                mode = "exclusive" if exclusive or deny_other_leases else "shared"
                fd = write_held_lease(name, lease, udid, mode)
                held_fd.callback(os.close, fd)
            else:
                os.makedirs(lease_dir(name), exist_ok=True)
                write_json(lease_file, record)
        booted_by_us = wrote_record = False
        try:
            if boot:
                state = device_state(udid)
                if state == "Shutting Down":
                    state = wait_for_shutdown(udid)
                if state == "Shutdown":
                    if lease:
                        # Written before booting: a kill during the boot still leaves the
                        # shutdown duty on record (reconciled against the real state later).
                        write_json(boot_record_path(name), {"udid": udid, "by": [lease],
                                                            "at": now_iso()})
                        wrote_record = True
                    log(f"booting {name}")
                    with phase("boot"):
                        simctl("boot", udid, timeout=STATE_TIMEOUT, lease_fd=fd)
                    booted_by_us = True
                # Booting devices too: return only once it is usable.
                with phase("boot"):
                    simctl("bootstatus", udid, "-b", timeout=BOOT_TIMEOUT, lease_fd=fd)
        except BaseException:
            keep_lease = False
            if wrote_record and not booted_by_us:
                # simctl boot itself failed: nothing of ours is running, drop the record.
                remove_file(boot_record_path(name))
            if booted_by_us:
                # Undo our own boot so a retry by the same owner does not find a running
                # device without a lease (8). If that fails too, keep lease and record so
                # `release --shutdown` can finish the job.
                try:
                    shutdown_device(udid)
                    remove_file(boot_record_path(name))
                except BaseException as exc:
                    keep_lease = bool(lease)
                    CLEANUP_FAILURES.append(f"shut down {name}: {exc}")
                    log(f"warning: could not shut down {name} after the failed boot: {exc}; "
                        f"`sim-vault release '{name}' --lease {lease} --shutdown` retries")
            if held_lease and keep_lease:
                # Preserve the v3 recovery via release --shutdown when undo fails.
                write_json(lease_file, dict(record, mode=mode))
            elif wrote_new and not keep_lease:
                remove_file(lease_file)
            if held_lease:
                remove_file(held_lock_path(lease_file))
            raise
        if result.get("matchesArchive") is False and result["action"] == "existing":
            warnings.append("this device is not the archived one (the archive's runtime is "
                            "gone); its first park returns 7 and needs --replace")
        result.update(name=name, state=device_state(udid), bootedByUs=booted_by_us,
                      lease=lease, warnings=warnings)
        if held_lease:
            result["leaseFd"] = fd
            held_fd.pop_all()
        return result


def release(name, lease, shutdown=False, wait=DEFAULT_WAIT, *, session=None):
    if session is not None:
        note(leaseKind="session")
    with DeviceLock(name, wait):
        return release_locked(name, lease, shutdown, session=session)


def release_locked(name, lease, shutdown=False, *, held_udid=None, session=None):
    directory = lease_dir(name)
    snapshot = lease_snapshot(directory, quiet=True)
    if session is not None:
        match = session_lease(snapshot[0], **session)
        lease = match["owner"] if match else None
    path = lease_path(name, lease) if lease is not None else None
    if held_udid is not None:
        # run holds the lease flock here and removes its lock file after release.
        snapshot = (snapshot[0], [l for l in snapshot[1]
                                  if l["lockFile"] != held_lock_path(path)], snapshot[2])
    else:
        existing = next((l for l in snapshot[0] if l["file"] == path), None)
        if existing:
            kind = existing.get("kind")
            note(leaseKind=kind if kind in ("session", "held") else "legacy")
    held = [l for l in snapshot[1] if l["alive"]]
    blocking = same_held_owner(name, lease, held)
    if blocking:
        note(leaseKind="held")
        raise held_leased_fail(name, blocking)
    if shutdown:
        require_readable_held(held)
    reap_held_leases(directory, snapshot=snapshot)
    devices = all_devices()
    device = next((d for d in devices if d["name"] == name), None)
    if device and held_udid is None:
        note(udid=device["udid"])
    drop_stale_leases(name, {d["udid"] for d in devices})
    record = reconcile_boot_record(name, devices)
    existed = path is not None and os.path.exists(path)
    # Strict with --shutdown: an unreadable lease may be someone's live one.
    manual = [l for l in read_leases(name, strict=shutdown) if l.get("kind") != "held"]
    others = [l for l in manual if l.get("owner") != lease] + held
    mine = ({"udid": held_udid} if held_udid is not None
            else next((l for l in manual if l["file"] == path), None))
    same_device = bool(record and mine and mine.get("udid") == record.get("udid"))
    responsible = same_device and lease in record.get("by", [])
    did_shutdown = False
    if shutdown and not others and responsible:
        # The lease stays until the device is down, so a failed shutdown can be rerun.
        log(f"shutting down {name} (booted by ensure for {', '.join(record['by'])})")
        with phase("shutdown"):
            shutdown_device(record["udid"])
        remove_file(boot_record_path(name))
        did_shutdown = True
    elif shutdown and others:
        log(f"not shutting down: still leased by {describe_leases(others)}")
    elif shutdown and not existed:
        log(f"not shutting down: {lease or 'this session'} holds no lease on it")
    elif shutdown:
        log("not shutting down: sim-vault did not boot it under this lease")
    if same_device and not did_shutdown:
        release_boot_record(boot_record_path(name), record, [lease], others)
    if path is not None:
        remove_file(path)
    return {"action": "released" if existed else "no-lease", "lease": lease,
            "shutdown": did_shutdown, "remaining": [lease_view(l) for l in others]}


def run_release(name, owner, udid, fd, wait):
    path = lease_path(name, owner)
    lock_file = held_lock_path(path)
    with ExitStack() as held_fd:
        held_fd.callback(os.close, fd)
        with DeviceLock(name, wait):
            try:
                record = read_holder(path)
                if not isinstance(record, dict) or record.get("kind") != "held":
                    record = {}
                if record.get("pid") == os.getpid():
                    record.pop("pid")
                    record.pop("pidStartedAt", None)
                    write_json(path, record)
            finally:
                held_fd.close()
            with open(lock_file, "rb") as lock:
                deadline = time.monotonic() + HELD_RELEASE_TIMEOUT
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            lease = dict(record, owner=owner, lockFile=lock_file, alive=True)
                            raise held_leased_fail(name, [lease], kind="held-by-other")
                        time.sleep(0.05)
                release_locked(name, owner, held_udid=udid)
                remove_file(lock_file)


def signal_run_child(pid, signum):
    try:
        os.kill(pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def run_command(args, command, outcome):
    owner = f"{args.lease}:{os.getpid()}-{secrets.token_hex(3)}"
    acquired = child = None
    rc, kind = EXIT_ERROR, "child"

    def on_child_signal(signum, frame):
        if child is None or child.returncode is not None:
            on_signal(signum, frame)
        note(signal=signum)
        # The child shares the shell job and receives terminal signals directly.
        if signum == signal.SIGTERM:
            signal_run_child(child.pid, signum)

    try:
        acquired = ensure(args.name, args.create, args.boot, owner, args.exclusive,
                          args.wait, args.deny_other_leases, args.allow_lease,
                          held_lease=True, expect_udid=args.expect_udid)
        udid, fd = acquired["udid"], acquired["leaseFd"]
        note(action=acquired["action"], udid=udid)
        os.set_inheritable(fd, True)
        env = dict(os.environ, SIM_VAULT_NAME=args.name, SIM_VAULT_UDID=udid,
                   SIM_VAULT_LEASE=owner, SIM_VAULT_LEASE_FD=str(fd))
        outcome["stage"] = "child"
        with phase("child"):
            try:
                # The shell manages the existing process group. The DeviceLock is
                # already closed, and every caller fd and the lease fd pass through.
                child = subprocess.Popen(command, env=env, close_fds=False)
            except OSError as exc:
                rc = outcome["childRc"] = 127 if isinstance(exc, FileNotFoundError) else 126
                log(f"cannot start {command[0]!r}: {exc}")
            else:
                for signum in RUN_SIGNALS:
                    signal.signal(signum, on_child_signal)
                child_rc = child.wait()
                rc = outcome["childRc"] = 128 - child_rc if child_rc < 0 else child_rc
    except Interrupted as exc:
        rc, kind = 128 + exc.signum, "interrupted"
        note(signal=exc.signum)
        log(f"error (exit {rc}, {kind}): interrupted by signal {exc.signum}")
    except Exception as exc:
        if acquired is None:
            raise
        log(f"error (exit {rc}, {kind}): {exc}")
    finally:
        if acquired is not None:
            try:
                child = None
                for signum in RUN_SIGNALS:
                    signal.signal(signum, on_signal)
                with phase("teardown"):
                    run_release(args.name, owner, acquired["udid"], acquired["leaseFd"], args.wait)
                outcome["teardown"] = "ok"
            except Interrupted as exc:
                outcome.update(stage="teardown", teardown="failed")
                rc, kind = 128 + exc.signum, "interrupted"
                note(signal=exc.signum)
                log(f"error (exit {rc}, {kind}): teardown interrupted by signal {exc.signum}")
            except Exception as exc:
                failure = exc if isinstance(exc, Fail) else Fail(str(exc), kind="teardown")
                outcome["teardown"] = ("held-by-other" if failure.kind == "held-by-other"
                                       else "failed")
                if rc:
                    log(f"warning: teardown failed (exit {failure.code}, {failure.kind}): {failure}")
                else:
                    outcome["stage"] = "teardown"
                    rc, kind = failure.code, failure.kind
                    log(f"error (exit {rc}, {kind}): {failure}")
                log(f"next: {failure.next_step}")
    return rc, kind if rc else None


def shutdown_command(name, wait=DEFAULT_WAIT, busy_locks=()):
    """Shut <name> down under the device lock, unless an owner still holds a lease (8).
    Never restores and never needs the vault."""
    with DeviceLock(name, wait):
        held, _ = reap_held_leases(lease_dir(name))
        if held:
            raise held_leased_fail(name, held)
        device = find_one(name)
        if device is None:
            log(f"no simulator named '{name}'; nothing to shut down")
            return {"action": "absent", "udid": None}
        udid = device["udid"]
        note(udid=udid)
        devices = all_devices()
        drop_stale_leases(name, {d["udid"] for d in devices})
        leases = read_leases(name, strict=True)
        if leases:
            raise Fail(f"'{name}' is leased by {describe_leases(leases)}; not shutting down",
                       EXIT_BUSY, "busy",
                       "it is still in use; release your own lease first and wait for the "
                       f"others; {stale_lease_hint(name)}")
        held = hold_busy_locks(busy_locks, udid)
        try:
            # Read the state again now that every lock is held: the device may have been
            # booted (or shut down) by a project run that held a --busy-lock meanwhile.
            state = device_state(udid)
            if state is None:
                log(f"'{name}' disappeared meanwhile; nothing to shut down")
                return {"action": "absent", "udid": None}
            if state == "Shutdown":
                action = "already-shutdown"
                log(f"{name} is already shut down")
            else:
                log(f"shutting down {name}")
                with phase("shutdown"):
                    shutdown_device(udid)
                action = "shutdown"
        finally:
            release_fds(held)
        remove_file(boot_record_path(name))
        return {"action": action, "udid": udid, "state": device_state(udid)}


# -- status / list / gc -----------------------------------------------------------------

def lease_age(lease):
    try:
        created = datetime.strptime(lease.get("createdAt"), "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (TypeError, ValueError):
        try:
            created = os.path.getmtime(lease.get("file") or lease["lockFile"])
        except OSError:
            return None
    return max(0, time.time() - created)


def status_lease(lease, present):
    kind = lease.get("kind")
    held = kind == "held"
    session = kind == "session"
    alive = lease["alive"] if held else True
    age = lease_age(lease)
    view = dict(lease_view(lease), kind=kind if held or session else "legacy", alive=alive,
                ageSeconds=round(age) if age is not None else None,
                overdue=not held and age is not None and age > 4 * 3600,
                stale=not alive if held else not session and lease.get("udid") not in present)
    if held:
        if view["owner"] is None:
            view["owner"] = held_label(lease)
        holder = verified_held_holder(lease)
        view["holderPids"] = [holder[0]] if holder else []
        if lease.get("unreadable"):
            view["unreadable"] = lease["unreadable"]
        elif alive and not holder:
            view["holderHint"] = f"{describe_held(lease, None)}. {held_candidate_hint(lease)}"
    if session:
        view.update(anchor=lease["anchor"], scope=lease["scope"],
                    anchorAlive=anchor_alive(lease["anchor"]), scopeExists=os.path.isdir(lease["scope"]))
    return view


def status(name):
    devices = all_devices()
    matches = [d for d in devices if d["name"] == name]
    available = [d for d in matches if d.get("isAvailable", True)]
    runtimes = list_runtimes()
    meta, info = archive_info(name, runtimes)
    present = {d["udid"] for d in devices}
    leases, held, _ = lease_snapshot(lease_dir(name))
    # Keep the existing file order, with lock-only leases appended.
    by_file = {l["file"]: l for l in held if "file" in l}
    leases = [by_file.get(l["file"], l) for l in leases] + [l for l in held if "file" not in l]
    one = available[0] if len(available) == 1 else None
    holder = lock_holder(name)
    restoring = [d["name"] for d in devices if d["name"].startswith(name + RESTORING)]
    return {
        "name": name,
        "udid": one["udid"] if one else None,
        "state": one["state"] if one else None,
        "count": len(available),
        "unavailableCount": len(matches) - len(available),
        "devices": [{"udid": d["udid"], "state": d["state"], "runtime": d["runtime"]}
                    for d in available],
        "archive": info,
        "matchesArchive": matches_archive(one["udid"], meta) if one else None,
        "leases": [status_lease(l, present) for l in leases],
        "bootedByLease": read_boot_record(name),
        "lock": {"held": holder is not None, "holder": holder},
        "posterMB": poster_kb(one["udid"]) // 1024 if one else None,
        "restoringLeftovers": restoring,
    }


def list_archives():
    vault = vault_dir()
    runtimes = list_runtimes()
    archives, errors, names = [], [], set()
    entries = sorted(os.listdir(vault))
    for entry in entries:
        # ._* are AppleDouble files the SMB client leaves next to real ones.
        if not entry.endswith(".json") or entry.startswith("._"):
            continue
        path = os.path.join(vault, entry)
        try:
            meta = read_meta(path)
            runtime = meta.get("runtime") or "?"
            tar = path[:-5] + ".tar"
            has_tar = os.path.exists(tar)
            archives.append(dict(meta, runtimeAvailable=runtime_available(runtime, runtimes),
                                 hasTar=has_tar,
                                 sizeMatches=has_tar and os.path.getsize(tar) == meta.get(
                                     "bytes")))
            names.add(entry[:-5])
        except Fail as exc:
            errors.append({"file": entry, "error": str(exc)})
            log(f"error: {exc}")
    orphans = [e for e in entries if e.endswith(".tar") and not e.startswith("._")
               and e[:-4] not in names]
    return {"archives": archives, "errors": errors, "orphanTars": orphans}


def print_list(result):
    for meta in result["archives"]:
        runtime = meta.get("runtime") or "?"
        status_text = "ok" if meta["runtimeAvailable"] else "runtime missing"
        if not meta["hasTar"]:
            status_text += ", tar missing"
        elif not meta["sizeMatches"]:
            status_text += ", tar/json size mismatch"
        print(f"{meta.get('name')}\t{(meta.get('bytes') or 0) / 1e9:.2f} GB\t"
              f"{meta.get('parkedAt')}\t{runtime.rsplit('.', 1)[-1]}\t{status_text}")


def parse_age(text):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"bad duration {text!r} (examples: 90m, 24h, 2d)")
    return float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[
        match.group(2)]


def gc(dry_run=False, leases_older_than=86400, prefix=None):
    deleted, attention = [], []
    wanted = (lambda n: n is not None and n.startswith(prefix)) if prefix else (lambda n: True)

    def act(kind, what, fn):
        deleted.append({"kind": kind, "what": what})
        log(f"{'would delete' if dry_run else 'deleting'} {kind}: {what}")
        if not dry_run:
            fn()

    def staging_sets():
        out = []
        if os.path.isdir(STAGING_ROOT):
            for entry in sorted(os.listdir(STAGING_ROOT)):
                path = os.path.join(STAGING_ROOT, entry)
                owner = read_holder(os.path.join(path, "owner.json")) or {}
                out.append((owner.get("name"), path))
        return out

    def reap_gc_leases(directory, snapshot=None):
        live, dead = reap_held_leases(directory, dry_run, snapshot=snapshot)
        if not dry_run:
            for lease in dead:
                deleted.append({"kind": "held-lease", "what": held_label(lease)})
        return live

    # Half-restored devices and staging sets: only under the name's lock, so a restore
    # still running (or a child of a killed one still holding the lock) is left alone.
    # The listing before the lock only picks the names; what to delete is listed again
    # once the lock is held, because a restore may have finished (renamed its temporary
    # device into the real one) in between.
    names = {d["name"].split(RESTORING)[0] for d in all_devices() if RESTORING in d["name"]}
    names |= {n for n, _path in staging_sets() if n}
    for name in sorted(n for n in names if wanted(n)):
        lock = try_lock(name)
        if lock is None:
            attention.append({"kind": "busy", "what": name, "note": "lock held; skipped"})
            continue
        # Holding the lock means no restore of this name is running (its children keep the
        # lock too), so what is left is a leftover whatever the recorded pid says.
        try:
            if reap_gc_leases(lease_dir(name)):
                continue
            for device in all_devices():
                if device["name"].startswith(name + RESTORING):
                    act("restoring-device", f"{device['name']} ({device['udid']})",
                        lambda u=device["udid"]: simctl("delete", u, keep_lock=True))
            for owner_name, path in staging_sets():
                if owner_name == name:
                    act("staging", path, lambda p=path: shutil.rmtree(p, ignore_errors=True))
        finally:
            lock.__exit__(None, None, None)
    devices = all_devices()
    for owner_name, path in staging_sets():
        if owner_name is None and not prefix:
            attention.append({"kind": "staging", "what": path, "note": "no owner.json"})
    # The old staging path has no pid or owner, and the old sim-vault may be restoring into
    # it right now: only sets untouched for an hour, and only when a name filter is not in
    # force (the device name inside is the only way to attribute them).
    if os.path.isdir(LEGACY_STAGING_ROOT) and not prefix:
        for entry in sorted(os.listdir(LEGACY_STAGING_ROOT)):
            path = os.path.join(LEGACY_STAGING_ROOT, entry)
            latest, _ = newest_mtime(path, time.time())
            age = time.time() - max(latest, os.lstat(path).st_mtime)
            if age > STALE_AFTER:
                act("legacy-staging", path, lambda p=path: shutil.rmtree(p, ignore_errors=True))
            else:
                attention.append({"kind": "legacy-staging", "what": path,
                                  "note": "touched within the hour; maybe an old restore"})
    # Vault leftovers.
    try:
        vault = vault_dir()
        entries = sorted(e for e in os.listdir(vault) if not e.startswith("._"))
    except Fail as exc:
        attention.append({"kind": "vault", "what": str(exc)})
        entries, vault = [], None
    for entry in entries:
        path = os.path.join(vault, entry)
        match = PARTIAL_RE.fullmatch(entry)
        if match:
            name = match["stem"]
            if not wanted(name):
                continue
            tar_path, meta_path = (os.path.join(vault, name + ".tar"),
                                   os.path.join(vault, name + ".json"))

            def keep_reason(st):
                if st is None:
                    return "gone"
                # A park killed between its renames: this metadata may be the only one that
                # matches the tar in place. park/restore of the name pair it after checking.
                pending = pending_meta_partials(tar_path, meta_path)
                if pending is None and match["ext"] == "json":
                    return ("cannot list the vault to tell whether it pairs with the tar in "
                            "place; not deleted")
                if pending and path in pending:
                    return ("may belong to the tar in place; the next park or restore of "
                            "this name pairs it; not deleted")
                if time.time() - st.st_mtime < STALE_AFTER:
                    return "younger than 1h"
                return None

            def stat(p):
                try:
                    return os.lstat(p)
                except FileNotFoundError:
                    return None

            first = stat(path)
            reason = keep_reason(first)
            if reason == "gone":
                continue
            if reason:
                attention.append({"kind": "partial", "what": path, "note": reason})
                continue
            lock = try_lock(name)
            if lock is None:
                attention.append({"kind": "partial", "what": path, "note": "lock held"})
                continue
            try:
                if reap_gc_leases(lease_dir(name)):
                    continue
                # Judge again under the lock: a park may have run between the first look
                # and the lock, replacing this file or making it the one that pairs.
                again = stat(path)
                reason = keep_reason(again)
                if reason is None and (again.st_ino, again.st_mtime) != (first.st_ino,
                                                                         first.st_mtime):
                    reason = "changed while gc waited for the lock"
                if reason == "gone":
                    continue
                if reason:
                    attention.append({"kind": "partial", "what": path, "note": reason})
                    continue
                act("partial", path, lambda p=path: remove_file(p))
            finally:
                lock.__exit__(None, None, None)
        elif entry.endswith((".tar", ".json")):
            stem, other = (entry[:-4], ".json") if entry.endswith(".tar") else (entry[:-5],
                                                                                ".tar")
            if wanted(stem) and stem + other not in entries:
                attention.append({"kind": "unpaired", "what": path,
                                  "note": f"no {stem}{other}"})
    # Legacy leases are only listed. Probe held leases before taking any lock, then
    # recheck under the DeviceLock before reaping a dead one.
    if os.path.isdir(LEASE_DIR):
        present = {d["udid"] for d in devices}
        names_by_key = {safe_key(d["name"]): d["name"] for d in devices}
        names_by_key.update((safe_key(name), name) for name in names)

        def real_name(key, leases):
            if key in names_by_key:
                return names_by_key[key]
            return next((l["name"] for l in leases if isinstance(l.get("name"), str)
                         and safe_key(l["name"]) == key), None)

        for key in sorted(os.listdir(LEASE_DIR)):
            directory = os.path.join(LEASE_DIR, key)
            # Finder's .DS_Store and the like; a directory removed meanwhile is skipped too.
            entries = lease_entries(directory)
            leases = read_lease_files(directory, entries, quiet=True)
            name = real_name(key, leases)
            key_matches = bool(prefix and key.startswith(safe_key(prefix)[:len(prefix)]))
            if wanted(name) or key_matches:
                held = scan_held_leases(directory, entries, leases)
                if not dry_run and wanted(name) and any(not l["alive"] for l in held):
                    lock = try_lock(name, key=key)
                    if lock is not None:
                        try:
                            snapshot = lease_snapshot(directory, quiet=True)
                            name = real_name(key, snapshot[0])
                            held = snapshot[1]
                            if wanted(name):
                                held = reap_gc_leases(directory, snapshot)
                        finally:
                            lock.__exit__(None, None, None)
                for lease in held:
                    holder = verified_held_holder(lease)
                    note_text = (describe_held(lease, holder)
                                 if lease["alive"] else f"{held_label(lease)} is dead")
                    item = {"kind": "held-lease", "what": note_text, "device": name or key}
                    if lease["alive"] and not (holder or lease.get("unreadable")):
                        item["note"] = held_candidate_hint(lease)
                    attention.append(item)
            for entry in entries:
                if entry.startswith("_") or not entry.endswith(".json"):
                    continue
                try:
                    # A release may delete the lease between the listing and here.
                    age = time.time() - os.lstat(os.path.join(directory, entry)).st_mtime
                except FileNotFoundError:
                    continue
                lease = read_holder(os.path.join(directory, entry))
                if lease is None and not os.path.exists(os.path.join(directory, entry)):
                    continue   # released after the lstat
                lease = lease or {}
                if lease.get("kind") == "held":
                    continue
                name = next((d["name"] for d in devices if d["udid"] == lease.get("udid")),
                            None)
                if prefix and not (wanted(name) or key_matches):
                    continue
                if lease.get("udid") not in present or age > leases_older_than:
                    attention.append({"kind": "lease", "what": lease.get("owner"),
                                      "device": name or key, "udid": lease.get("udid"),
                                      "createdAt": lease.get("createdAt"),
                                      "note": "device gone" if lease.get("udid") not in present
                                      else f"older than {leases_older_than / 3600:g}h"})
    for item in attention:
        log(f"attention: {item}")
    return {"dryRun": dry_run, "deleted": deleted, "attention": attention}


# -- CLI -------------------------------------------------------------------------------

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(f"{self.prog}: {message}", EXIT_USAGE, "usage",
                   f"see `{self.prog} --help`")


def build_parser():
    parser = Parser(prog="sim-vault", description=__doc__,
                    formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"sim-vault {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    def add(name, text):
        p = sub.add_parser(name, help=text, description=text)
        p.add_argument("--json", action="store_true",
                       help="print one JSON object on stdout, also on failure")
        return p

    def wait_flag(p):
        p.add_argument("--wait", type=float, default=DEFAULT_WAIT,
                       help="seconds to wait for the device lock (default 60), then exit 8")

    def scope_flag(p, *, required=False):
        p.add_argument("--lease-scope", metavar="DIR", required=required,
                       help="existing worktree directory for the session lease, using its canonical path")

    def acquisition_flags(p, *, held=False):
        p.add_argument("--create", metavar="TYPE",
                       help="on 3/4, create it with this device type (name or identifier) and the "
                            "newest installed runtime that supports it")
        p.add_argument("--boot", action="store_true",
                       help="boot it unless running, and wait until it has finished booting")
        group = p.add_mutually_exclusive_group() if held else p
        group.add_argument("--exclusive", action="store_true",
                           help="with --lease: refuse (8) if another owner holds a lease or the "
                                "device runs without one, and make later ensures by others refuse")
        group.add_argument("--deny-other-leases", action="store_true",
                           help="refuse (8) if another owner holds a lease; allowed owners "
                                "apply only at acquisition. Blocks later ensures by others"
                                if held else
                                "with --lease: refuse (8, naming them) while owners other than "
                                "OWNER and the --allow-lease ones hold a lease; else take a shared "
                                "lease as usual. A running device without leases passes; later "
                                "ensures by others are not refused (unlike --exclusive)")
        p.add_argument("--allow-lease", action="append", default=[], metavar="OWNER",
                       help="with --deny-other-leases: an owner whose lease does not count. "
                            "Repeatable")
        wait_flag(p)

    p = add("status", "what is local, archived and leased for <name>; never waits for the "
                      "device lock (probes it for an instant); 0 unless simctl fails")
    p.add_argument("name")
    p = add("ensure", "make <name> available: local device, else restore, else create "
                      "(with --create on 3/4); prints the UDID")
    p.add_argument("name")
    p.add_argument("--lease", metavar="OWNER",
                   help="register OWNER as using the device until `release`; park refuses "
                        "(8) while any lease exists. Use --lease auto --lease-scope DIR "
                        "to generate a session owner. Recorded before booting")
    scope_flag(p)
    p.add_argument("--lease-prefix", metavar="PREFIX", help="session token prefix (default auto)")
    acquisition_flags(p)
    p = sub.add_parser("run", help="run a command under a held simulator lease",
                       usage="%(prog)s <name> --lease PREFIX [options] -- <cmd...>",
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       epilog="""The command receives SIM_VAULT_NAME, SIM_VAULT_UDID, SIM_VAULT_LEASE
(the full owner) and SIM_VAULT_LEASE_FD. Its stdout goes directly to the caller.
The shell manages job control for run and the command as one job.
While waiting, run records SIGINT and SIGHUP sent to its PID alone.
Send SIGTERM to run's PID to forward it to the command.

Python launchers must pass the lease descriptor via pass_fds=(fd,), where
fd = int(os.environ["SIM_VAULT_LEASE_FD"]). Background helpers must close it:
  eval "helper ... ${SIM_VAULT_LEASE_FD}>&-"
Use numeric fd redirection on macOS /bin/bash 3.2. For a background command,
put the & inside eval so the background shell drops the descriptor:
  eval "helper ... ${SIM_VAULT_LEASE_FD}>&- &"

If a leftover process keeps the lease alive, run reports teardown=held-by-other
and leaves the process running. Use the reported lsof <lock path> command to find
candidates. Verify the process is yours before using kill <pid>.
The next command that takes the device lock removes the lease after the last
holder exits. Run leaves the device running during teardown.

The final stderr line reports stage, rc, childRc, and teardown.
Use --result-file to get the same fields as JSON.""")
    p.add_argument("name")
    p.add_argument("--lease", required=True, metavar="PREFIX",
                   help="unique held lease owner with this prefix")
    p.add_argument("--expect-udid", metavar="UDID",
                   help="exit 1 before leasing or booting if the resolved UDID differs")
    p.add_argument("--result-file", metavar="FILE",
                   help="write the run result as JSON")
    acquisition_flags(p, held=True)
    p = add("restore", "bring <name> back from the vault; never creates or boots and ignores "
                       "leases (for people and old scripts; lease users call ensure); "
                       "prints the UDID")
    p.add_argument("name")
    wait_flag(p)
    p = add("park", "archive <name>, verify it, then delete the device")
    p.add_argument("name")
    p.add_argument("--shutdown", action="store_true",
                   help="shut the device down first if it is booted (project policy)")
    p.add_argument("--replace", action="store_true",
                   help="replace an archive that came from a different device")
    p.add_argument("--busy-lock", action="append", default=[], metavar="FILE",
                   help="a project flock file meaning 'in use'; {udid} is substituted. Taken "
                        "without waiting (held: exit 8) and kept until park ends. Repeatable")
    p.add_argument("--missing-ok", action="store_true",
                   help="exit 0 when there is no such device")
    p.add_argument("--keep-posterboard", action="store_true",
                   help="by default, PosterBoard data over the 1 GiB limit is left out of the "
                        "archive (data/Library/Application Support/"
                        "{PRBPosterExtensionDataStore,com.apple.PosterKit}; the system "
                        "rebuilds them on the first boot after a restore, wallpaper and lock "
                        "screen may reset). With this flag park refuses with exit 9 instead, "
                        "touching nothing. Under the limit the archive is always complete")
    p.add_argument("--trim-posterboard", action="store_true",
                   help="no-op, kept for older callers: leaving bloated PosterBoard data out "
                        "is now the default")
    p.add_argument("--exclude-symbol-cache", action="store_true",
                   help="leave the system's data/Library/Caches/com.apple.coresymbolicationd "
                        "out of the archive (can be GBs; apps' own Caches are kept)")
    wait_flag(p)
    p = add("shutdown", "shut <name> down under the device lock unless someone holds a lease "
                        "(exit 8 naming the owners); 0 when already shut down or absent. "
                        "Never restores, works without the vault. For project `down` scripts: "
                        "release your own lease first")
    p.add_argument("name")
    p.add_argument("--busy-lock", action="append", default=[], metavar="FILE",
                   help="as for park: a project flock file meaning 'in use' ({udid} is "
                        "substituted); held: exit 8. Repeatable")
    wait_flag(p)
    p = add("release", "drop OWNER's lease on <name>; exit 0 when there is none")
    p.add_argument("name")
    p.add_argument("--lease", metavar="OWNER", required=True,
                   help="use --lease auto --lease-scope DIR to release this session's lease")
    scope_flag(p)
    p.add_argument("--shutdown", action="store_true",
                   help="if this is the last lease and `ensure --boot --lease` booted the "
                        "device, shut it down and wait; the lease is removed only after that")
    wait_flag(p)
    p = add("whoami", "show the current anchor on stderr and the session token on stdout")
    p.add_argument("name")
    scope_flag(p, required=True)
    add("list", "archives in the vault (bad or AppleDouble files are skipped and reported)")
    p = add("gc", "delete what interrupted runs left: half-restored devices, staging sets, "
                  "stale .partial files; list unpaired archives and old leases")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--leases-older-than", type=parse_age, default=86400, metavar="AGE",
                   help="list legacy leases older than AGE (default 24h)")
    p.add_argument("--name-prefix", metavar="TEXT", help="only names starting with TEXT")
    return parser


def emit(as_json, payload):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))


def write_run_result(path, outcome):
    fd, temporary = tempfile.mkstemp(prefix=".sim-vault-result-", suffix=".partial",
                                     dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(outcome, handle, indent=2)
        os.replace(temporary, path)
    finally:
        remove_file(temporary)


def write_history(record):
    """Append one JSON line to history.jsonl. O_APPEND plus flock keeps lines of parallel
    runs whole. A failure only warns: the exit code and stdout stay as they are."""
    try:
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode()
        os.makedirs(os.path.dirname(HISTORY_PATH) or ".", exist_ok=True)
        fd = os.open(HISTORY_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            # Bounded: a stopped process holding this lock must not keep the caller from
            # getting the exit code of work that is already done.
            deadline = time.monotonic() + HISTORY_LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise OSError(f"history lock busy for {HISTORY_LOCK_TIMEOUT}s; "
                                      "line not written")
                    time.sleep(0.05)
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
    except Exception as exc:
        log(f"warning: could not write {HISTORY_PATH}: {exc}")


def history_record(argv, args, rc, kind, run_outcome=None):
    """The history line of this call, or None for commands that are not recorded."""
    command = getattr(args, "command", None)
    name = getattr(args, "name", None)
    if command is None:
        # Usage error: argparse gave up; take the command and name from argv.
        words = [a for a in argv if not a.startswith("-")]
        command = words[0] if words else None
        name = words[1] if len(words) > 1 else None
    if command not in HISTORY_COMMANDS:
        return None
    fields = RUN["fields"]
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = None
    phases = {k: round(v, 3) for k, v in RUN["phases"].items()}
    record = {"ts": now_iso(), "cmd": command, "name": name, "udid": fields.get("udid"),
              "rc": rc, "kind": kind, "action": fields.get("action"),
              "bytes": fields.get("bytes"), "posterMB": fields.get("posterMB"), "cwd": cwd,
              "pid": os.getpid(), "seconds": round(time.monotonic() - RUN["started"], 3),
              "phases": phases}
    if "leaseKind" in fields:
        record["leaseKind"] = fields["leaseKind"]
    if run_outcome is not None:
        record.update(run_outcome, lockWait=phases.get("lockWait", 0), child=phases.get("child"))
        if "signal" in fields:
            record["signal"] = fields["signal"]
    return record


def main():
    RUN["started"] = time.monotonic()
    argv = sys.argv[1:]
    is_run = argv[:1] == ["run"]
    as_json = not is_run and "--json" in argv
    run_outcome = None
    command = []
    if is_run:
        note(leaseKind="held")
        run_outcome = {"stage": "acquire", "childRc": None, "teardown": "-"}
        if "--" in argv:
            boundary = argv.index("--")
            argv, command = argv[:boundary], argv[boundary + 1:]
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    args, rc, kind, payload, lines = None, 0, None, None, []
    try:
        args = build_parser().parse_args(argv)
        if is_run and not (args.lease and command and command[0]):
            raise Fail("run needs --lease PREFIX and -- <cmd...>", EXIT_USAGE, "usage",
                       "see `sim-vault run --help`")
        if getattr(args, "exclusive", False) and not args.lease:
            raise Fail("--exclusive needs --lease", EXIT_USAGE, "usage",
                       "pass --lease <owner> so the exclusivity outlives this command")
        if getattr(args, "deny_other_leases", False) and not args.lease:
            raise Fail("--deny-other-leases needs --lease", EXIT_USAGE, "usage",
                       "pass --lease <owner>: the check is for leases other than yours")
        if getattr(args, "allow_lease", None) and not args.deny_other_leases:
            raise Fail("--allow-lease needs --deny-other-leases", EXIT_USAGE, "usage",
                       "add --deny-other-leases, or drop --allow-lease")
        scope = getattr(args, "lease_scope", None)
        prefix = getattr(args, "lease_prefix", None)
        auto_lease = args.command in ("ensure", "release") and args.lease == "auto"
        if auto_lease and scope is None:
            raise Fail("--lease auto requires --lease-scope", EXIT_USAGE, "usage",
                       "use --lease auto --lease-scope DIR")
        if (scope is not None or prefix is not None) and not (auto_lease or args.command == "whoami"):
            raise Fail("--lease-scope and --lease-prefix are for auto leases", EXIT_USAGE,
                       "usage", "use --lease auto --lease-scope DIR")
        session = None
        inspection_error = None
        if scope is not None:
            normalized_scope = canonical_scope(scope)
            try:
                session = {"scope": normalized_scope, "anchor": session_anchor()}
            except ProcessInspectionFailed as exc:
                inspection_error = str(exc)
        if inspection_error:
            if auto_lease:
                note(leaseKind="session")
            if args.command == "ensure":
                raise Fail(inspection_error, kind="process-inspection-failed",
                           next_step="retry process inspection or use an explicit --lease OWNER")
            log(inspection_error)
            result = {"action": "no-lease", "lease": None, "message": inspection_error}
            if args.command == "whoami":
                result["anchor"] = None
            else:
                result.update(shutdown=False, remaining=[])
        elif args.command == "status":
            result = status(args.name)
            if not as_json:
                if result["udid"]:
                    print(result["udid"])
                for lease in result["leases"]:
                    details = ""
                    if lease["kind"] == "held":
                        details = f" holderPids={lease['holderPids']}"
                    elif lease["kind"] == "session":
                        details = f" anchorAlive={lease['anchorAlive']} scopeExists={lease['scopeExists']}"
                    log(f"lease {lease['owner']}: {lease['kind']} alive={lease['alive']} "
                        f"ageSeconds={lease['ageSeconds']} overdue={lease['overdue']} "
                        f"stale={lease['stale']}{details}")
                    if lease.get("unreadable"):
                        log(f"warning: unreadable held lease {lease['owner']}: {lease['unreadable']}")
                    elif lease.get("holderHint"):
                        log(lease["holderHint"])
        elif args.command == "ensure":
            result = ensure(args.name, args.create, args.boot, args.lease, args.exclusive,
                            args.wait, args.deny_other_leases, args.allow_lease,
                            session=session, lease_prefix=prefix, lease_scope=scope)
            if session is not None:
                write_stderr(f"lease: {result['lease']}")
            if not as_json:
                print(result["udid"])
        elif args.command == "run":
            rc, kind = run_command(args, command, run_outcome)
            result = None
        elif args.command == "restore":
            result = restore(args.name, args.wait)
            if not as_json:
                print(result["udid"])
        elif args.command == "park":
            result = park(args.name, args.shutdown, args.replace, args.wait, args.busy_lock,
                          args.missing_ok, args.keep_posterboard, args.exclude_symbol_cache)
        elif args.command == "shutdown":
            result = shutdown_command(args.name, args.wait, args.busy_lock)
        elif args.command == "release":
            result = release(args.name, args.lease, args.shutdown, args.wait, session=session)
        elif args.command == "whoami":
            result = whoami(args.name, **session)
            if not as_json:
                write_stderr(f"anchor: {json.dumps(result['anchor'])}")
                if result["lease"] is not None:
                    print(result["lease"])
        elif args.command == "list":
            result = list_archives()
            if not as_json:
                print_list(result)
        else:
            result = gc(args.dry_run, args.leases_older_than, args.name_prefix)
        if result is not None:
            note(action=result.get("action"), udid=result.get("udid") or RUN["fields"].get("udid"))
            payload = dict({"ok": True}, **result)
    except Fail as exc:
        rc, kind = exc.code, exc.kind
        lines = [f"error (exit {rc}, {kind}): {exc}", f"next: {exc.next_step}"]
        payload = {"ok": False, "code": rc, "kind": kind, "message": str(exc),
                   "next": exc.next_step}
    except (Interrupted, KeyboardInterrupt) as exc:
        signum = getattr(exc, "signum", signal.SIGINT)
        rc, kind = 128 + signum, "interrupted"
        if CLEANUP_FAILURES:
            done, next_step = "cleanup incomplete", "run `sim-vault gc --dry-run` to see what is left"
        else:
            done, next_step = "cleaned up", "rerun"
        lines = [f"error (exit {rc}, {kind}): interrupted by signal {signum}; {done}",
                 f"next: {next_step}"]
        payload = {"ok": False, "code": rc, "kind": kind, "message": f"signal {signum}",
                   "next": next_step}
    except SystemExit:
        raise   # --help
    except Exception as exc:  # no traceback: agents read it as a cue to guess
        rc, kind = EXIT_ERROR, "unexpected"
        lines = [f"error (exit {rc}, {kind}): unexpected {type(exc).__name__}: {exc}",
                 "next: stop and report this message"]
        payload = {"ok": False, "code": rc, "kind": kind,
                   "message": f"{type(exc).__name__}: {exc}",
                   "next": "stop and report this message"}
    # Finishing: a signal now would only cut the history line or the output short.
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, signal.SIG_IGN)
    for line in lines:
        log(line)
    if run_outcome is not None:
        run_outcome["rc"] = rc
        result_file = getattr(args, "result_file", None)
        if result_file is not None:
            try:
                write_run_result(result_file, run_outcome)
            except OSError as exc:
                log(f"warning: could not write {result_file}: {exc}")
    try:
        record = history_record(argv, args, rc, kind, run_outcome)
    except Exception as exc:
        record = None
        log(f"warning: could not build the history line: {exc}")
    if record is not None:
        write_history(record)
        if payload is not None:
            payload.update(seconds=record["seconds"], phases=record["phases"])
    if run_outcome is not None:
        child_rc = run_outcome["childRc"]
        write_stderr(f"sim-vault-run: stage={run_outcome['stage']} rc={rc} "
                     f"childRc={'-' if child_rc is None else child_rc} "
                     f"teardown={run_outcome['teardown']}")
    else:
        emit(as_json, payload)
    sys.exit(rc)


if __name__ == "__main__":
    main()
