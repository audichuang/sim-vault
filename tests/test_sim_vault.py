"""sim-vault tests against a fake `xcrun simctl` and a vault under /tmp.

Run: python3 -m unittest discover -s tests (SV_TEST_PY picks the interpreter of the subprocesses).
Nothing here touches real simulators: every run gets its own HOME under /tmp, the stub
directory first on PATH, and a loader that refuses to start unless `xcrun` resolves to
the stub. The loader also stubs the same-volume check (both /tmp and CoreSimulator live
on the Data volume here); the vault-unavailable tests run without that patch.
"""
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = (os.environ.get("SV_TEST_SCRIPT_UNDER_TEST")
          or os.path.join(HERE, "..", "src", "sim_vault", "__init__.py"))
RT = "com.apple.CoreSimulator.SimRuntime.iOS-27-2"
RT_OLD = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
PY = os.environ.get("SV_TEST_PY") or (
    "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable)

FAKE_XCRUN = r'''#!/usr/bin/env python3
import fcntl, json, os, plistlib, shutil, sys, time, uuid
args = sys.argv[1:]
root = os.environ["FAKE_ROOT"]
with open(os.path.join(root, "log"), "a") as h:
    h.write(json.dumps(args) + "\n")
if args[:1] != ["simctl"]:
    sys.exit(0)
args = args[1:]
dset = os.path.join(os.environ["HOME"], "Library/Developer/CoreSimulator/Devices")
if args[:1] == ["--set"]:
    dset, args = args[1], args[2:]
sub, rest = args[0], args[1:]

def bump(key):
    path = os.path.join(root, "counts.json")
    with open(path + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        counts = json.load(open(path)) if os.path.exists(path) else {}
        counts[key] = counts.get(key, 0) + 1
        json.dump(counts, open(path, "w"))
        return counts[key]

def devices(s=None):
    s = s or dset
    out = []
    if not os.path.isdir(s):
        return out
    for u in sorted(os.listdir(s)):
        p = os.path.join(s, u, "device.plist")
        if os.path.exists(p):
            out.append(plistlib.load(open(p, "rb")))
    return out

def save(d, s=None):
    p = os.path.join(s or dset, d["UDID"], "device.plist")
    plistlib.dump(d, open(p, "wb"))

def find(u, s=None):
    for d in devices(s):
        if d["UDID"] == u or d["name"] == u:
            return d
    return None

def set_state(d, state):
    d["fakeState"] = state
    save(d)
    if state in ("Booted", "Booting"):
        marker = os.path.join(dset, d["UDID"], "data", "boot-marker")
        open(marker, "a").write("x")

n = bump(sub)
held = []
for fd in range(3, 256):
    try:
        path = fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0")[0].decode()   # F_GETPATH
    except OSError:
        continue
    if path.endswith(".lock"):
        held.append(path)
if held:
    with open(os.path.join(root, "inherited"), "a") as h:
        h.write(json.dumps([sub, held]) + "\n")
hooks = json.load(open(os.path.join(root, "hooks.json"))) if os.path.exists(
    os.path.join(root, "hooks.json")) else []
for hk in hooks:
    if hk["cmd"] == sub and hk["n"] == n:
        if hk["action"] == "sleep":
            time.sleep(hk["seconds"])
        elif hk["action"] == "state":
            d = find(hk["device"])
            if d:
                set_state(d, hk["state"])

runtimes = json.load(open(os.path.join(root, "runtimes.json")))
if sub == "list":
    what = [x for x in rest if x in ("devices", "runtimes", "devicetypes")][0]
    if what == "devices":
        out = {"devices": {}}
        for d in devices():
            if "available" in rest and not d.get("fakeAvailable", True):
                continue
            after = d.get("fakeAfter")
            if after:
                after["lists"] -= 1
                if after["lists"] <= 0:
                    d["fakeState"] = after["to"]
                    del d["fakeAfter"]
                save(d)
            out["devices"].setdefault(d["runtime"], []).append({
                "name": d["name"], "udid": d["UDID"], "state": d.get("fakeState", "Shutdown"),
                "isAvailable": d.get("fakeAvailable", True),
                "dataPath": os.path.join(dset, d["UDID"], "data")})
        print(json.dumps(out))
    elif what == "runtimes":
        print(json.dumps({"runtimes": runtimes}))
    else:
        names = ["iPhone 18 Pro Max", "iPhone 16 Plus"]
        print(json.dumps({"devicetypes": [{
            "name": nm, "identifier": "com.apple.CoreSimulator.SimDeviceType." + nm.replace(" ", "-"),
            "minRuntimeVersion": 0, "maxRuntimeVersion": 4294967295} for nm in names]}))
    sys.exit(0)

def fail(msg, rc=164):
    sys.stderr.write(msg + "\n")
    sys.exit(rc)

if sub == "create":
    if os.environ.get("FAKE_CREATE_RC"):
        fail("create failed", int(os.environ["FAKE_CREATE_RC"]))
    name, dtype = rest[0], rest[1]
    rt = rest[2] if len(rest) > 2 else runtimes[-1]["identifier"]
    if not dtype.startswith("com."):
        dtype = "com.apple.CoreSimulator.SimDeviceType." + dtype.replace(" ", "-")
    u = str(uuid.uuid4()).upper()
    os.makedirs(os.path.join(dset, u, "data", "Library"))
    open(os.path.join(dset, u, "data", "Library", "app-data"), "w").write("blank")
    save({"UDID": u, "name": name, "runtime": rt, "deviceType": dtype, "fakeState": "Shutdown"})
    print(u)
elif sub == "clone":
    src, newname = rest[0], rest[1]
    dest = rest[2] if len(rest) > 2 else dset
    d = find(src)
    if not d:
        fail("Invalid device: " + src)
    u = str(uuid.uuid4()).upper()
    shutil.copytree(os.path.join(dset, d["UDID"]), os.path.join(dest, u), symlinks=True)
    d = dict(d, UDID=u, name=newname, fakeState="Shutdown")
    save(d, dest)
    print(u)
elif sub in ("rename", "delete", "boot", "shutdown", "bootstatus", "spawn"):
    d = find(rest[0])
    if not d:
        fail("Invalid device: " + rest[0])
    st = d.get("fakeState", "Shutdown")
    if sub == "rename":
        d["name"] = rest[1]
        save(d)
    elif sub == "delete":
        shutil.rmtree(os.path.join(dset, d["UDID"]))
    elif sub == "boot":
        if st != "Shutdown":
            fail("Unable to boot device in current state: " + st, 149)
        set_state(d, "Booted")
    elif sub == "shutdown":
        if os.environ.get("FAKE_SHUTDOWN_RC"):
            fail("shutdown failed", int(os.environ["FAKE_SHUTDOWN_RC"]))
        if st == "Shutdown":
            fail("Unable to shutdown device in current state: Shutdown", 149)
        if os.environ.get("FAKE_SLOW_SHUTDOWN"):
            d["fakeState"] = "Shutting Down"
            d["fakeAfter"] = {"lists": 2, "to": "Shutdown"}
            save(d)
        else:
            set_state(d, "Shutdown")
    elif sub == "bootstatus":
        if os.environ.get("FAKE_BOOTSTATUS_RC"):
            fail("bootstatus failed", int(os.environ["FAKE_BOOTSTATUS_RC"]))
        if st in ("Shutdown", "Booting"):
            set_state(d, "Booted")
else:
    fail("unknown subcommand " + sub)
'''

# Wraps the real tar: optional delay, and "boot" a device in the middle of archiving.
FAKE_TAR = r'''#!/usr/bin/env python3
import os, plistlib, sys, time
args = sys.argv[1:]
if "-cf" in args:
    if os.environ.get("FAKE_TAR_SLEEP"):
        time.sleep(float(os.environ["FAKE_TAR_SLEEP"]))
    target = os.environ.get("FAKE_TAR_BOOT")
    once = os.environ.get("FAKE_TAR_ONCE")
    if target and once:
        if os.path.exists(once):
            target = None
        else:
            open(once, "w").close()
    mode = os.environ.get("FAKE_TAR_MODE", "both")   # both | state | touch
    if target:
        p = os.path.join(os.environ["HOME"], "Library/Developer/CoreSimulator/Devices", target)
        if mode in ("both", "state"):
            d = plistlib.load(open(os.path.join(p, "device.plist"), "rb"))
            d["fakeState"] = "Booted"
            plistlib.dump(d, open(os.path.join(p, "device.plist"), "wb"))
            # keep the plist's mtime old: only the state changes
            if mode == "state":
                os.utime(os.path.join(p, "device.plist"), (0, 0))
        if mode in ("both", "touch"):
            open(os.path.join(p, "data", "boot-marker"), "a").write("x")
        if os.environ.get("FAKE_TAR_RENAME"):
            d = plistlib.load(open(os.path.join(p, "device.plist"), "rb"))
            d["name"] = os.environ["FAKE_TAR_RENAME"]
            plistlib.dump(d, open(os.path.join(p, "device.plist"), "wb"))
os.execv("/usr/bin/tar", ["tar"] + args)
'''

LOADER = r'''import importlib.machinery, importlib.util, os, shutil, sys
stub = os.environ["SV_TEST_STUB_DIR"]
assert shutil.which("xcrun") == os.path.join(stub, "xcrun"), shutil.which("xcrun")
for key in ("HOME", "SIM_VAULT_DIR"):
    assert os.path.realpath(os.environ[key]).startswith("/private/tmp/"), key
loader = importlib.machinery.SourceFileLoader("sim_vault", os.environ["SV_TEST_SCRIPT"])
spec = importlib.util.spec_from_loader("sim_vault", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
if not os.environ.get("SV_TEST_REAL_VOLUME"):
    mod.same_volume = lambda a, b: False
if os.environ.get("SV_TEST_DEFAULT_VAULT"):
    mod.DEFAULT_VAULT = os.environ["SV_TEST_DEFAULT_VAULT"]
mod.QUIET_SECONDS = 0.3
mod.QUIET_TIMEOUT = 5
mod.POLL = 0.05
if (os.environ.get("SV_TEST_KILL_AFTER_TAR_REPLACE")
        or os.environ.get("SV_TEST_FAIL_TAR_REPLACE")):
    import signal
    real_replace = os.replace
    def replace(src, dst):
        if dst.endswith(".tar") and os.environ.get("SV_TEST_FAIL_TAR_REPLACE"):
            raise OSError(5, "simulated I/O error", dst)
        real_replace(src, dst)
        if dst.endswith(".tar"):
            if os.environ.get("SV_TEST_CORRUPT_TAR_AFTER_REPLACE"):
                with open(dst, "ab") as h:
                    h.write(b"\0" * 512)
            os.kill(os.getpid(), signal.SIGTERM)
    mod.os.replace = replace
if os.environ.get("SV_TEST_POSTER_LIMIT_KB"):
    mod.POSTER_LIMIT_KB = int(os.environ["SV_TEST_POSTER_LIMIT_KB"])
if os.environ.get("SV_TEST_EXIT_AFTER_TAR_REPLACE"):
    # SIGKILL stand-in: no handler, no finally, between the tar and the metadata rename.
    real_replace2 = os.replace
    def replace_then_die(src, dst):
        real_replace2(src, dst)
        if dst.endswith(".tar"):
            os._exit(137)
    mod.os.replace = replace_then_die
if os.environ.get("SV_TEST_NOCACHE_LOG") or os.environ.get("SV_TEST_NOCACHE_FAIL"):
    import fcntl as real_fcntl, json
    class FcntlProxy:
        def __getattr__(self, key):
            return getattr(real_fcntl, key)
        def fcntl(self, fd, cmd, arg=0):
            if cmd == mod.F_NOCACHE:
                if os.environ.get("SV_TEST_NOCACHE_FAIL"):
                    raise OSError(45, "Operation not supported")
                path = real_fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0")[0].decode()
                with open(os.environ["SV_TEST_NOCACHE_LOG"], "a") as h:
                    h.write(json.dumps({"arg": arg, "path": path,
                                        "offset": os.lseek(fd, 0, os.SEEK_CUR)}) + "\n")
            return real_fcntl.fcntl(fd, cmd, arg)
    mod.fcntl = FcntlProxy()
if os.environ.get("SV_TEST_LISTDIR_FAIL_FROM"):
    # EIO on the vault listing from the N-th listdir of the vault on.
    real_listdir = os.listdir
    listdir_calls = [0]
    def listdir(p="."):
        if os.path.realpath(str(p)) == os.path.realpath(os.environ["SIM_VAULT_DIR"]):
            listdir_calls[0] += 1
            if listdir_calls[0] >= int(os.environ["SV_TEST_LISTDIR_FAIL_FROM"]):
                raise OSError(5, "simulated EIO", p)
        return real_listdir(p)
    mod.os.listdir = listdir
if os.environ.get("SV_TEST_TAR_STAT_EIO"):
    # EIO from stat (and so os.path.exists) of the vault's <name>.tar.
    real_stat = os.stat
    def stat_eio(p, *a, **k):
        if str(p).endswith(".tar") and os.path.realpath(os.path.dirname(str(p))) == \
                os.path.realpath(os.environ["SIM_VAULT_DIR"]):
            raise OSError(5, "simulated EIO", p)
        return real_stat(p, *a, **k)
    mod.os.stat = stat_eio
if os.environ.get("SV_TEST_RAISE_IN"):
    def boom(*a, **k):
        raise RuntimeError("injected")
    setattr(mod, os.environ["SV_TEST_RAISE_IN"], boom)
if os.environ.get("SV_TEST_HISTORY_LOCK_TIMEOUT"):
    mod.HISTORY_LOCK_TIMEOUT = float(os.environ["SV_TEST_HISTORY_LOCK_TIMEOUT"])
if os.environ.get("SV_TEST_GC_HOOK"):
    # Run a command once, after gc's first look and before it takes the first lock.
    import json, subprocess
    real_try_lock = mod.try_lock
    hook_done = []
    def try_lock(name):
        if not hook_done:
            hook_done.append(1)
            env = dict(os.environ)
            env.pop("SV_TEST_GC_HOOK")
            subprocess.run(json.loads(os.environ["SV_TEST_GC_HOOK"]), env=env,
                           capture_output=True)
        return real_try_lock(name)
    mod.try_lock = try_lock
if os.environ.get("SV_TEST_LEASE_VANISHES"):
    # A release deleting a lease between gc's listing and its lstat.
    real_lstat = os.lstat
    def lstat(path, *a, **k):
        if "/leases/" in str(path) and str(path).endswith(".json") and os.path.exists(path):
            os.remove(path)
        return real_lstat(path, *a, **k)
    mod.os.lstat = lstat
if os.environ.get("SV_TEST_SLOW_REMOVE") or os.environ.get("SV_TEST_FAIL_REMOVE"):
    import time
    real_remove = os.remove
    def remove(path):
        if path.endswith(".partial"):
            if os.environ.get("SV_TEST_FAIL_REMOVE"):
                raise PermissionError(1, "denied", path)
            time.sleep(1.5)
        real_remove(path)
    mod.os.remove = remove
sys.argv = ["sim-vault"] + sys.argv[1:]
mod.main()
'''


def write(path, text):
    with open(path, "w") as h:
        h.write(text)


class SimVaultCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="svt-", dir="/tmp")
        self.home = os.path.join(self.base, "home")
        self.vault = os.path.join(self.base, "nas", "vault")
        self.fake = os.path.join(self.base, "fake")
        self.stub = os.path.join(self.base, "stub")
        for d in (self.home, os.path.dirname(self.vault), self.fake, self.stub):
            os.makedirs(d)
        self.devset = os.path.join(self.home, "Library/Developer/CoreSimulator/Devices")
        os.makedirs(self.devset)
        for name, body in (("xcrun", FAKE_XCRUN), ("tar", FAKE_TAR)):
            path = os.path.join(self.stub, name)
            with open(path, "w") as h:
                h.write(body.replace("#!/usr/bin/env python3", "#!" + PY, 1))
            os.chmod(path, 0o755)
        self.loader = os.path.join(self.base, "loader.py")
        with open(self.loader, "w") as h:
            h.write(LOADER)
        self.set_runtimes([RT])

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    # -- environment -----------------------------------------------------------------
    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "SIM_VAULT_DIR",
                                                          "SIM_VAULT_HISTORY")}
        env.update(HOME=self.home, PATH=f"{self.stub}:/usr/bin:/bin", FAKE_ROOT=self.fake,
                   SIM_VAULT_DIR=self.vault, SV_TEST_STUB_DIR=self.stub, SV_TEST_SCRIPT=SCRIPT,
                   TMPDIR=os.path.join(self.base, "tmp-default"))
        os.makedirs(env["TMPDIR"], exist_ok=True)
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def sv(self, *args, **extra):
        proc = subprocess.run([PY, self.loader, *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)
        self.assertNotIn("Traceback", proc.stderr, proc.stderr)
        return proc

    def spawn(self, *args, **extra):
        return subprocess.Popen([PY, self.loader, *args], env=self.env(**extra),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def xcrun(self, *args):
        out = subprocess.run([os.path.join(self.stub, "xcrun"), "simctl", *args],
                             env=self.env(), capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def set_runtimes(self, ids):
        with open(os.path.join(self.fake, "runtimes.json"), "w") as h:
            json.dump([{"identifier": i, "platform": "iOS", "isAvailable": True,
                        "version": i.rsplit("-", 2)[-2] + "." + i.rsplit("-", 1)[-1]}
                       for i in ids], h)

    def hooks(self, hooks):
        with open(os.path.join(self.fake, "hooks.json"), "w") as h:
            json.dump(hooks, h)
        counts = os.path.join(self.fake, "counts.json")
        if os.path.exists(counts):
            os.remove(counts)

    def count(self, sub):
        path = os.path.join(self.fake, "counts.json")
        if not os.path.exists(path):
            return 0
        with open(path) as h:
            return json.load(h).get(sub, 0)

    # -- devices ---------------------------------------------------------------------
    def create(self, name, runtime=RT, state="Shutdown", available=True):
        udid = self.xcrun("create", name, "iPhone 18 Pro Max", runtime)
        self.patch_device(udid, fakeState=state, fakeAvailable=available)
        return udid

    def plist_path(self, udid):
        return os.path.join(self.devset, udid, "device.plist")

    def patch_device(self, udid, **values):
        with open(self.plist_path(udid), "rb") as h:
            d = plistlib.load(h)
        d.update(values)
        with open(self.plist_path(udid), "wb") as h:
            plistlib.dump(d, h)

    def devices(self, name=None):
        out = []
        for u in sorted(os.listdir(self.devset)):
            p = self.plist_path(u)
            if os.path.exists(p):
                with open(p, "rb") as h:
                    d = plistlib.load(h)
                if name is None or d["name"] == name:
                    out.append(d)
        return out

    def state(self, udid):
        with open(self.plist_path(udid), "rb") as h:
            return plistlib.load(h).get("fakeState")

    def meta(self, name):
        with open(os.path.join(self.vault, name + ".json")) as h:
            return json.load(h)

    def tar_sha(self, name):
        with open(os.path.join(self.vault, name + ".tar"), "rb") as h:
            return hashlib.sha256(h.read()).hexdigest()

    def parked(self, name):
        """Create and park a device; return the archived UDID."""
        udid = self.create(name)
        r = self.sv("park", name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return udid

    def partials(self, ext, name="SVTEST a"):
        """This name's .partial files of one kind (park adds a per-run token)."""
        if not os.path.isdir(self.vault):
            return []
        pat = re.compile(re.escape(name) + r"\." + ext + r"(\.\d+-[0-9a-f]+)?\.partial")
        return sorted(os.path.join(self.vault, f) for f in os.listdir(self.vault)
                      if pat.fullmatch(f))

    def assert_no_partials(self):
        if os.path.isdir(self.vault):
            self.assertEqual([f for f in os.listdir(self.vault) if f.endswith(".partial")], [])


class T1Core(SimVaultCase):
    def test_roundtrip_and_metadata(self):
        udid = self.parked("SVTEST a")
        self.assertEqual(self.devices("SVTEST a"), [])
        meta = self.meta("SVTEST a")
        self.assertEqual(meta["udid"], udid)
        self.assertIn("host", meta)
        self.assertEqual(meta["toolVersion"], "3")
        r = self.sv("restore", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)
        new = r.stdout.strip()
        self.assertRegex(new, r"^[0-9A-F-]{36}$")   # stdout is only the UDID
        self.assertEqual([d["UDID"] for d in self.devices("SVTEST a")], [new])
        self.assertEqual(self.meta("SVTEST a")["restoredAs"], new)
        with open(os.path.join(self.devset, new, "data/Library/app-data")) as h:
            self.assertEqual(h.read(), "blank")
        r = self.sv("park", "SVTEST a")              # its own archive: no --replace needed
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(os.path.join(self.home,
                                                 "Library/Developer/CoreSimulator/sim-vault/staging")), [])

    def test_exit_2_usage(self):
        self.assertEqual(self.sv("park").returncode, 2)
        self.assertEqual(self.sv("nope").returncode, 2)

    def test_exit_3_no_archive(self):
        r = self.sv("restore", "SVTEST none")
        self.assertEqual(r.returncode, 3)
        self.assertIn("next:", r.stderr)

    def test_exit_4_runtime_gone(self):
        self.parked("SVTEST a")
        self.set_runtimes([RT_OLD])
        self.assertEqual(self.sv("restore", "SVTEST a").returncode, 4)

    def test_exit_5_vault_unavailable(self):
        self.create("SVTEST a")
        self.vault = os.path.join(self.base, "unmounted", "vault")
        self.assertEqual(self.sv("park", "SVTEST a").returncode, 5)
        self.assertEqual(self.sv("restore", "SVTEST b").returncode, 5)
        self.assertEqual(len(self.devices("SVTEST a")), 1)

    def test_exit_5_same_volume_unpatched(self):
        self.create("SVTEST a")
        r = self.sv("park", "SVTEST a", SV_TEST_REAL_VOLUME=1)
        self.assertEqual(r.returncode, 5)
        self.assertIn("same volume", r.stderr)

    def test_restore_existing_with_vault_unavailable_is_0(self):
        udid = self.create("SVTEST a")
        self.vault = os.path.join(self.base, "unmounted", "vault")
        r = self.sv("restore", "SVTEST a")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, udid))

    def test_exit_6_booted(self):
        udid = self.create("SVTEST a", state="Booted")
        self.assertEqual(self.sv("park", "SVTEST a").returncode, 6)
        self.assertEqual(self.state(udid), "Booted")

    def test_park_shutdown_flag(self):
        udid = self.create("SVTEST a", state="Booted")
        r = self.sv("park", "SVTEST a", "--shutdown")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.meta("SVTEST a")["udid"], udid)

    def test_park_shutting_down_waits(self):          # A-B9
        udid = self.create("SVTEST a")
        self.patch_device(udid, fakeState="Shutting Down", fakeAfter={"lists": 3, "to": "Shutdown"})
        r = self.sv("park", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_park_booting_with_shutdown(self):
        self.create("SVTEST a", state="Booting")
        r = self.sv("park", "SVTEST a", "--shutdown", FAKE_SLOW_SHUTDOWN=1)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_exit_7_other_device_and_replace(self):
        self.parked("SVTEST a")
        blank = self.create("SVTEST a")
        sha = self.tar_sha("SVTEST a")
        r = self.sv("park", "SVTEST a")
        self.assertEqual(r.returncode, 7)
        self.assertEqual(self.tar_sha("SVTEST a"), sha)
        self.assertEqual(self.sv("park", "SVTEST a", "--replace").returncode, 0)
        self.assertEqual(self.meta("SVTEST a")["udid"], blank)

    def test_exit_7_message_when_archive_runtime_gone(self):   # scenario (b)
        self.parked("SVTEST a")
        self.set_runtimes([RT_OLD])
        self.create("SVTEST a", runtime=RT_OLD)
        r = self.sv("park", "SVTEST a")
        self.assertEqual(r.returncode, 7)
        self.assertIn("--replace", r.stderr)
        self.assertIn("cannot be restored", r.stderr)

    def test_exit_10_blank_device_with_restorable_archive(self):   # A-B3, scenario (a)
        self.parked("SVTEST a")
        blank = self.create("SVTEST a")
        r = self.sv("restore", "SVTEST a")
        self.assertEqual(r.returncode, 10)
        self.assertEqual(r.stdout, "")
        self.assertIn("stop and report", r.stderr)
        self.assertIn(f"xcrun simctl rename {blank}", r.stderr)
        self.assertEqual([d["UDID"] for d in self.devices("SVTEST a")], [blank])

    def test_existing_passes_when_archive_runtime_gone(self):
        self.parked("SVTEST a")
        self.set_runtimes([RT_OLD])
        blank = self.create("SVTEST a", runtime=RT_OLD)
        r = self.sv("restore", "SVTEST a")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, blank))

    def test_duplicate_names_exit_1(self):
        self.create("SVTEST a")
        self.create("SVTEST a")
        r = self.sv("restore", "SVTEST a")
        self.assertEqual(r.returncode, 1)
        self.assertIn("2 simulators", r.stderr)
        self.assertEqual(self.sv("park", "SVTEST a").returncode, 1)
        self.assertEqual(len(self.devices("SVTEST a")), 2)

    def test_unavailable_same_name_is_ignored(self):           # scenario (g)
        udid = self.create("SVTEST a")
        self.create("SVTEST a", available=False)
        r = self.sv("park", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("unavailable", r.stderr)
        self.assertEqual(self.meta("SVTEST a")["udid"], udid)

    def test_booted_during_archive_keeps_old_archive(self):    # A-B4, scenario (d)
        self.parked("SVTEST a")
        r = self.sv("restore", "SVTEST a")
        new = r.stdout.strip()
        sha = self.tar_sha("SVTEST a")
        r = self.sv("park", "SVTEST a", FAKE_TAR_BOOT=new)
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertIn("old archive and the device were kept", r.stderr)
        self.assertEqual(self.tar_sha("SVTEST a"), sha)
        self.assertEqual([d["UDID"] for d in self.devices("SVTEST a")], [new])
        self.assert_no_partials()

    def test_booted_after_replace_keeps_device(self):          # 3.5 step 10
        udid = self.create("SVTEST a")
        self.hooks([])
        self.assertEqual(self.sv("park", "SVTEST a").returncode, 0)
        lists = self.count("list")
        r = self.sv("restore", "SVTEST a")
        new = r.stdout.strip()
        # The last `list` call of a park is the check right before delete.
        self.hooks([{"cmd": "list", "n": lists, "action": "state", "device": new,
                     "state": "Booted"}])
        r = self.sv("park", "SVTEST a")
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertIn("was archived but is now Booted", r.stderr)
        self.assertEqual(self.meta("SVTEST a")["udid"], new)
        self.assertEqual([d["UDID"] for d in self.devices("SVTEST a")], [new])
        self.assertNotEqual(udid, new)

    def test_future_mtime_does_not_block(self):                # A-B10
        udid = self.create("SVTEST a")
        future = os.path.join(self.devset, udid, "data", "future")
        write(future, "x")
        os.utime(future, (time.time() + 86400, time.time() + 86400))
        started = time.time()
        r = self.sv("park", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.time() - started, 5)

    def test_restore_interrupted_by_sigterm_then_restore(self):   # A-B2, scenario (c)
        self.parked("SVTEST a")
        self.hooks([{"cmd": "rename", "n": 1, "action": "sleep", "seconds": 30}])
        p = self.spawn("restore", "SVTEST a")
        self.wait_for(lambda: self.count("rename") >= 1)
        p.send_signal(signal.SIGTERM)
        _out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM, err)
        self.assertNotIn("Traceback", err)
        # The half-restored device was deleted in cleanup, staging removed.
        self.assertEqual([d["name"] for d in self.devices()], [])
        self.assertEqual(self.staging(), [])
        self.hooks([])
        r = self.sv("restore", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.devices("SVTEST a")), 1)

    def test_restore_killed_leaves_temp_device_cleaned_next_time(self):
        self.parked("SVTEST a")
        # Killed while the staging copy is being unregistered (after clone, before rename).
        self.hooks([{"cmd": "delete", "n": 1, "action": "sleep", "seconds": 2}])
        p = self.spawn("restore", "SVTEST a")
        self.wait_for(lambda: self.count("delete") >= 1)
        p.kill()
        p.communicate(timeout=30)
        time.sleep(2.5)   # the sleeping simctl child holds the lock until it exits
        names = [d["name"] for d in self.devices()]
        self.assertEqual(len(names), 1)
        self.assertIn("[sim-vault restoring", names[0])
        self.assertEqual(len(self.staging()), 1)
        self.hooks([])
        r = self.sv("restore", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([d["name"] for d in self.devices()], ["SVTEST a"])

    def test_park_interrupted_keeps_device_and_old_archive(self):   # A-B6
        self.parked("SVTEST a")
        new = self.sv("restore", "SVTEST a").stdout.strip()
        sha = self.tar_sha("SVTEST a")
        p = self.spawn("park", "SVTEST a", FAKE_TAR_SLEEP=30)
        self.wait_for(lambda: self.partials("tar"))
        p.send_signal(signal.SIGTERM)
        _out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM, err)
        self.assert_no_partials()
        self.assertEqual(self.tar_sha("SVTEST a"), sha)
        self.assertEqual([d["UDID"] for d in self.devices("SVTEST a")], [new])

    def test_lock_ignores_tmpdir(self):                         # A-B1
        self.create("SVTEST a")
        first = self.spawn("park", "SVTEST a", FAKE_TAR_SLEEP=4,
                           TMPDIR=self.mkdir("tmp-one"))
        self.wait_for(lambda: self.count("list") >= 1)
        time.sleep(0.5)
        r = self.sv("park", "SVTEST a", "--wait", "1", TMPDIR=self.mkdir("tmp-two"))
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertIn(f"pid {first.pid}", r.stderr)
        _out, err = first.communicate(timeout=60)
        self.assertEqual(first.returncode, 0, err)
        tar_t = subprocess.run(["/usr/bin/tar", "-tf", os.path.join(self.vault, "SVTEST a.tar")],
                               capture_output=True)
        self.assertEqual(tar_t.returncode, 0)

    def test_svtest_refused_on_real_vault(self):
        self.create("SVTEST a")
        r = self.sv("park", "SVTEST a", SV_TEST_DEFAULT_VAULT=self.vault)
        self.assertEqual(r.returncode, 1)
        self.assertIn("test device", r.stderr)
        self.assertEqual(len(self.devices("SVTEST a")), 1)

    def test_long_name_no_traceback(self):                      # A-B12
        r = self.sv("restore", "SVTEST " + "x" * 300)
        self.assertIn(r.returncode, (1, 3))

    def test_list_skips_appledouble_and_bad_json(self):        # A-B7
        self.parked("SVTEST a")
        write(os.path.join(self.vault, "._SVTEST a.json"), "\x00\x05\x16\x07")
        write(os.path.join(self.vault, "broken.json"), "{")
        r = self.sv("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertTrue(r.stdout.startswith("SVTEST a\t"))
        self.assertIn("broken.json", r.stderr)
        self.assertNotIn("._SVTEST", r.stderr)

    def test_legacy_json_is_read(self):
        udid = self.parked("SVTEST a")
        meta = self.meta("SVTEST a")
        legacy = {k: meta[k] for k in ("name", "udid", "runtime", "deviceType", "sha256",
                                       "bytes", "parkedAt")}
        with open(os.path.join(self.vault, "SVTEST a.json"), "w") as h:
            json.dump(legacy, h)
        r = self.sv("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.sv("restore", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(r.stdout.strip(), udid)

    def test_restore_aborts_when_metadata_write_fails(self):
        self.parked("SVTEST a")
        os.makedirs(os.path.join(self.vault, "SVTEST a.json.partial"))
        r = self.sv("restore", "SVTEST a")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("could not record the restored UDID", r.stderr)
        self.assertEqual(self.devices(), [])
        self.assertEqual(self.staging(), [])

    def test_tar_keeps_lock_after_parent_dies(self):
        self.create("SVTEST a")
        p = self.spawn("park", "SVTEST a", FAKE_TAR_SLEEP=4)
        self.wait_for(lambda: self.partials("tar"))
        p.kill()
        p.wait(timeout=30)   # not communicate(): tar holds the same stderr pipe
        p.stdout.close()
        p.stderr.close()
        r = self.sv("restore", "SVTEST a", "--wait", "1")
        self.assertEqual(r.returncode, 8, r.stderr)
        time.sleep(4)
        r = self.sv("restore", "SVTEST a", "--wait", "5")
        self.assertEqual(r.returncode, 0, r.stderr)   # the device was never deleted

    # -- helpers ---------------------------------------------------------------------
    def mkdir(self, name):
        path = os.path.join(self.base, name)
        os.makedirs(path, exist_ok=True)
        return path

    def staging(self):
        path = os.path.join(self.home, "Library/Developer/CoreSimulator/sim-vault/staging")
        return os.listdir(path) if os.path.isdir(path) else []

    def wait_for(self, predicate, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("condition not reached")


class T2SharedLayer(SimVaultCase):
    def js(self, *args, **extra):
        r = self.sv(*args, "--json", **extra)
        try:
            data = json.loads(r.stdout)        # exactly one object, also on failure
        except ValueError:
            self.fail(f"stdout is not one JSON object: {r.stdout!r} / {r.stderr}")
        if r.returncode:
            self.assertEqual((data["ok"], data["code"]), (False, r.returncode))
            self.assertTrue(data["kind"] and data["message"] and data["next"], data)
        else:
            self.assertTrue(data["ok"])
        return r.returncode, data

    def leases_dir(self):
        return os.path.join(self.home, "Library/Developer/CoreSimulator/sim-vault/leases")

    # -- status ----------------------------------------------------------------------
    def test_status(self):
        rc, d = self.js("status", "SVTEST a")
        self.assertEqual((rc, d["count"], d["udid"], d["archive"]["exists"]), (0, 0, None, False))
        udid = self.parked("SVTEST a")
        rc, d = self.js("status", "SVTEST a")
        self.assertEqual((d["archive"]["udid"], d["archive"]["runtimeAvailable"]), (udid, True))
        new = self.sv("restore", "SVTEST a").stdout.strip()
        self.create("SVTEST a", available=False)
        rc, d = self.js("status", "SVTEST a")
        self.assertEqual((d["udid"], d["state"], d["count"], d["unavailableCount"],
                          d["matchesArchive"], d["posterMB"]), (new, "Shutdown", 1, 1, True, 0))
        r = self.sv("status", "SVTEST a")
        self.assertEqual(r.stdout.strip(), new)

    def test_status_duplicates_and_vault_unavailable_is_0(self):
        self.create("SVTEST a")
        self.create("SVTEST a")
        self.vault = os.path.join(self.base, "unmounted", "vault")
        rc, d = self.js("status", "SVTEST a")
        self.assertEqual((rc, d["count"], d["udid"]), (0, 2, None))
        self.assertIn("vaultError", d["archive"])

    # -- ensure ----------------------------------------------------------------------
    def test_ensure_existing_restored_created(self):
        udid = self.create("SVTEST a")
        rc, d = self.js("ensure", "SVTEST a")
        self.assertEqual((rc, d["action"], d["udid"], d["matchesArchive"]), (0, "existing", udid, None))
        self.assertEqual(self.sv("park", "SVTEST a").returncode, 0)
        r = self.sv("ensure", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"^[0-9A-F-]{36}\n$")
        self.assertEqual(self.sv("park", "SVTEST a").returncode, 0)
        rc, d = self.js("ensure", "SVTEST a")
        self.assertEqual((rc, d["action"], d["matchesArchive"]), (0, "restored", True))
        rc, d = self.js("ensure", "SVTEST b")
        self.assertEqual((rc, d["kind"]), (3, "no-archive"))
        self.assertEqual(self.devices("SVTEST b"), [])
        rc, d = self.js("ensure", "SVTEST b", "--create", "iPhone 18 Pro Max")
        self.assertEqual((rc, d["action"], d["needsReplaceOnFirstPark"]), (0, "created", False))
        self.assertEqual(self.devices("SVTEST b")[0]["runtime"], RT)

    def test_ensure_create_after_4_then_existing_then_park_7(self):   # scenario (b)
        self.parked("SVTEST a")
        self.set_runtimes([RT_OLD])
        self.assertEqual(self.js("ensure", "SVTEST a")[0], 4)
        rc, d = self.js("ensure", "SVTEST a", "--create", "iPhone 18 Pro Max")
        self.assertEqual((rc, d["action"], d["needsReplaceOnFirstPark"]), (0, "created", True))
        rc, d = self.js("ensure", "SVTEST a")
        self.assertEqual((rc, d["action"], d["matchesArchive"]), (0, "existing", False))
        self.assertTrue(d["warnings"])
        r = self.sv("park", "SVTEST a")
        self.assertEqual(r.returncode, 7)
        self.assertIn("cannot be restored", r.stderr)

    def test_ensure_5_never_creates(self):
        self.vault = os.path.join(self.base, "unmounted", "vault")
        rc, d = self.js("ensure", "SVTEST a", "--create", "iPhone 18 Pro Max")
        self.assertEqual((rc, d["kind"]), (5, "vault-unavailable"))
        self.assertEqual(self.devices(), [])

    def test_ensure_10_blank_device(self):                       # scenario (a)
        self.parked("SVTEST a")
        self.create("SVTEST a")
        rc, d = self.js("ensure", "SVTEST a", "--create", "iPhone 18 Pro Max")
        self.assertEqual((rc, d["kind"]), (10, "not-archived-device"))

    def test_10_from_local_record_while_vault_unavailable(self):   # F2
        self.parked("SVTEST a")
        self.create("SVTEST a")
        self.vault = os.path.join(self.base, "unmounted", "vault")
        rc, d = self.js("restore", "SVTEST a")
        self.assertEqual(rc, 10)
        self.assertIn("record", d["message"])

    def test_ensure_restored_device_ok_while_vault_unavailable(self):
        self.parked("SVTEST a")
        new = self.sv("ensure", "SVTEST a").stdout.strip()
        self.vault = os.path.join(self.base, "unmounted", "vault")
        rc, d = self.js("ensure", "SVTEST a")
        self.assertEqual((rc, d["udid"], d["matchesArchive"]), (0, new, True))

    def test_ensure_boot_booting_device_waits(self):            # B-B7
        udid = self.create("SVTEST a", state="Booting")
        rc, d = self.js("ensure", "SVTEST a", "--boot")
        self.assertEqual((rc, d["state"], d["bootedByUs"]), (0, "Booted", False))
        self.assertEqual(self.state(udid), "Booted")

    # -- leases ----------------------------------------------------------------------
    def test_lease_cycle(self):
        udid = self.create("SVTEST a")
        rc, d = self.js("ensure", "SVTEST a", "--boot", "--lease", "t1")
        self.assertEqual((rc, d["bootedByUs"], d["lease"]), (0, True, "t1"))
        rc, d = self.js("park", "SVTEST a")
        self.assertEqual(rc, 8)
        self.assertIn("t1", d["message"])
        rc, d = self.js("release", "SVTEST a", "--lease", "t1", "--shutdown")
        self.assertEqual((rc, d["shutdown"]), (0, True))
        self.assertEqual(self.state(udid), "Shutdown")
        rc, d = self.js("release", "SVTEST a", "--lease", "t1", "--shutdown")   # idempotent
        self.assertEqual((rc, d["action"]), (0, "no-lease"))
        self.assertEqual(self.js("park", "SVTEST a")[0], 0)

    def test_exclusive_needs_lease(self):
        self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--exclusive")[0], 2)

    def test_exclusive_is_persistent(self):                     # Codex 2
        self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--exclusive", "--boot", "--lease", "A")[0], 0)
        self.assertEqual(self.js("ensure", "SVTEST a", "--boot", "--lease", "B")[0], 8)
        self.assertEqual(self.js("ensure", "SVTEST a")[0], 8)
        rc, d = self.js("ensure", "SVTEST a", "--exclusive", "--boot", "--lease", "A")   # re-entry
        self.assertEqual((rc, d["bootedByUs"]), (0, False))

    def test_exclusive_refuses_shared_holder_and_unleased_running(self):
        udid = self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "S")[0], 0)
        self.assertEqual(self.js("ensure", "SVTEST a", "--exclusive", "--lease", "X")[0], 8)
        self.assertEqual(self.js("release", "SVTEST a", "--lease", "S")[0], 0)
        self.patch_device(udid, fakeState="Booted")
        rc, d = self.js("ensure", "SVTEST a", "--exclusive", "--lease", "X")
        self.assertEqual(rc, 8)
        self.assertIn("no lease", d["message"])
        self.assertEqual(os.listdir(os.path.join(self.leases_dir(), os.listdir(self.leases_dir())[0])), [])

    def test_shared_leases_last_release_shuts_down(self):     # Codex 4
        udid = self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--boot", "--lease", "A")[0], 0)
        rc, d = self.js("ensure", "SVTEST a", "--boot", "--lease", "B")
        self.assertEqual((rc, d["bootedByUs"]), (0, False))
        rc, d = self.js("release", "SVTEST a", "--lease", "A", "--shutdown")
        self.assertEqual((rc, d["shutdown"]), (0, False))
        self.assertEqual(self.state(udid), "Booted")
        rc, d = self.js("release", "SVTEST a", "--lease", "B", "--shutdown")
        self.assertEqual((rc, d["shutdown"]), (0, True))
        self.assertEqual(self.state(udid), "Shutdown")

    def test_release_without_boot_by_lease_does_not_shut_down(self):
        udid = self.create("SVTEST a", state="Booted")
        self.assertEqual(self.js("ensure", "SVTEST a", "--boot", "--lease", "A")[0], 0)
        rc, d = self.js("release", "SVTEST a", "--lease", "A", "--shutdown")
        self.assertEqual((rc, d["shutdown"]), (0, False))
        self.assertEqual(self.state(udid), "Booted")

    def test_release_keeps_lease_when_shutdown_fails(self):     # Codex 3
        udid = self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--boot", "--lease", "A")[0], 0)
        rc, _ = self.js("release", "SVTEST a", "--lease", "A", "--shutdown", FAKE_SHUTDOWN_RC=1)
        self.assertEqual(rc, 1)
        self.assertEqual(self.js("park", "SVTEST a")[0], 8)      # lease still there
        rc, d = self.js("release", "SVTEST a", "--lease", "A", "--shutdown")
        self.assertEqual((rc, d["shutdown"]), (0, True))
        self.assertEqual(self.state(udid), "Shutdown")

    def test_ensure_failure_withdraws_new_lease(self):          # F5
        self.create("SVTEST a")
        rc, _ = self.js("ensure", "SVTEST a", "--boot", "--lease", "A", FAKE_BOOTSTATUS_RC=1)
        self.assertEqual(rc, 1)
        rc, d = self.js("status", "SVTEST a")
        self.assertEqual(d["leases"], [])

    def test_stale_lease_for_gone_device_is_dropped(self):      # F5, scenario (e)
        self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "A")[0], 0)
        old = self.devices("SVTEST a")[0]["UDID"]
        self.xcrun("delete", old)
        new = self.create("SVTEST a")
        rc, d = self.js("status", "SVTEST a")
        self.assertTrue(d["leases"][0]["stale"])
        rc, d = self.js("park", "SVTEST a")
        self.assertEqual(rc, 0, d)

    def test_live_lease_blocks_park_and_gc_lists_it(self):      # scenario (e)
        self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "A")[0], 0)
        rc, d = self.js("gc", "--leases-older-than", "0s", "--name-prefix", "SVTEST")
        self.assertEqual(rc, 0)
        self.assertEqual([a["what"] for a in d["attention"] if a["kind"] == "lease"], ["A"])
        self.assertEqual(self.js("park", "SVTEST a")[0], 8)

    def test_same_owner_new_udid_rewrites_lease(self):          # F6
        self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "A")[0], 0)
        self.assertEqual(self.js("release", "SVTEST a", "--lease", "A")[0], 0)
        self.assertEqual(self.js("park", "SVTEST a")[0], 0)
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "A")[0], 0)
        rc, d = self.js("status", "SVTEST a")
        self.assertEqual(d["leases"][0]["udid"], d["udid"])

    # -- park preflight ----------------------------------------------------------------
    def test_park_missing_ok(self):
        rc, d = self.js("park", "SVTEST none", "--missing-ok")
        self.assertEqual((rc, d["action"]), (0, "absent"))
        self.assertEqual(self.js("park", "SVTEST none")[0], 1)

    def test_posterboard_limit(self):
        udid = self.create("SVTEST a", state="Booted")
        store = os.path.join(self.devset, udid,
                             "data/Library/Application Support/PRBPosterExtensionDataStore")
        os.makedirs(store)
        with open(os.path.join(store, "big"), "wb") as h:
            h.write(os.urandom(1024 * 1024) * 1025)          # real bytes, not sparse
        rc, d = self.js("park", "SVTEST a", "--shutdown", "--keep-posterboard")
        self.assertEqual((rc, d["kind"]), (9, "posterboard-bloated"))
        self.assertEqual(self.state(udid), "Booted")         # not shut down or deleted
        self.assertIn("--keep-posterboard", d["next"])
        os.truncate(os.path.join(store, "big"), 900 * 1024 * 1024)
        rc, d = self.js("park", "SVTEST a", "--shutdown", "--keep-posterboard")
        self.assertEqual((rc, d["posterMB"]), (0, 900))

    def test_busy_lock_probe_and_hold(self):                    # Codex 1
        import fcntl
        udid = self.create("SVTEST a")
        tmpl = os.path.join(self.base, "hl-{udid}.lock")
        path = tmpl.replace("{udid}", udid)
        write(path, "")
        with open(path) as h:
            fcntl.flock(h, fcntl.LOCK_EX)
            rc, d = self.js("park", "SVTEST a", "--busy-lock", tmpl)
            self.assertEqual(rc, 8)
        p = self.spawn("park", "SVTEST a", "--busy-lock", tmpl, FAKE_TAR_SLEEP=3)
        self.wait_for(lambda: self.partials("tar"))
        with open(path) as h:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _o, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(os.path.getsize(path), 0)            # not truncated or written
        missing = os.path.join(self.base, "nothing-{udid}.lock")
        self.create("SVTEST b")
        self.assertEqual(self.js("park", "SVTEST b", "--busy-lock", missing)[0], 0)
        self.assertFalse(os.path.exists(missing.replace("{udid}", "x")))

    # -- json / usage / list ------------------------------------------------------------
    def test_json_errors_are_one_object(self):
        rc, d = self.js("restore", "SVTEST none")
        self.assertEqual(d["code"], 3)
        rc, d = self.js("park")
        self.assertEqual((rc, d["kind"]), (2, "usage"))
        rc, d = self.js("release", "SVTEST a")
        self.assertEqual(rc, 2)

    def test_list_json(self):
        self.parked("SVTEST a")
        write(os.path.join(self.vault, "broken.json"), "{")
        write(os.path.join(self.vault, "orphan.tar"), "")
        rc, d = self.js("list")
        self.assertEqual(rc, 0)
        self.assertEqual([a["name"] for a in d["archives"]], ["SVTEST a"])
        self.assertEqual([e["file"] for e in d["errors"]], ["broken.json"])
        self.assertEqual(d["orphanTars"], ["orphan.tar"])

    def test_help_lists_commands_and_codes(self):
        r = self.sv("--help")
        self.assertEqual(r.returncode, 0)
        for word in ("ensure", "release", "gc", "10 restore/ensure", "9  park"):
            self.assertIn(word, r.stdout)

    # -- gc ----------------------------------------------------------------------------
    def test_gc(self):
        self.parked("SVTEST a")
        # Killed while the staging copy is being unregistered (after clone, before rename).
        self.hooks([{"cmd": "delete", "n": 1, "action": "sleep", "seconds": 2}])
        p = self.spawn("restore", "SVTEST a")
        self.wait_for(lambda: self.count("delete") >= 1)
        p.kill()
        p.communicate(timeout=30)
        time.sleep(2.5)   # the sleeping simctl child holds the lock until it exits
        self.hooks([])
        old_partial = os.path.join(self.vault, "SVTEST a.tar.partial")
        write(old_partial, "x")
        os.utime(old_partial, (time.time() - 7200, time.time() - 7200))
        write(os.path.join(self.vault, "SVTEST lone.json"), "{}")
        write(os.path.join(self.vault, "OTHER x.tar.partial"), "x")
        os.utime(os.path.join(self.vault, "OTHER x.tar.partial"), (0, 0))
        rc, d = self.js("gc", "--dry-run", "--name-prefix", "SVTEST")
        self.assertEqual(rc, 0, d)
        kinds = sorted(x["kind"] for x in d["deleted"])
        self.assertEqual(kinds, ["partial", "restoring-device", "staging"])
        self.assertEqual(len(self.devices()), 1)               # dry run touched nothing
        self.assertIn("unpaired", [a["kind"] for a in d["attention"]])
        rc, d = self.js("gc", "--name-prefix", "SVTEST")
        self.assertEqual(self.devices(), [])
        self.assertEqual(self.staging(), [])
        self.assertFalse(os.path.exists(old_partial))
        self.assertTrue(os.path.exists(os.path.join(self.vault, "OTHER x.tar.partial")))

    def test_gc_and_ensure_ignore_live_pid_when_lock_is_free(self):
        self.parked("SVTEST a")
        temp = self.xcrun("create", f"SVTEST a [sim-vault restoring {os.getpid()}]",
                          "iPhone 18 Pro Max", RT)
        rc, d = self.js("gc", "--dry-run", "--name-prefix", "SVTEST")
        self.assertEqual([x["kind"] for x in d["deleted"]], ["restoring-device"])
        rc, d = self.js("ensure", "SVTEST a")
        self.assertEqual((rc, d["action"]), (0, "restored"))
        self.assertNotIn(temp, [x["UDID"] for x in self.devices()])

    def test_gc_skips_when_lock_held(self):                    # Codex 5
        self.parked("SVTEST a")
        self.hooks([{"cmd": "rename", "n": 1, "action": "sleep", "seconds": 8}])
        p = self.spawn("restore", "SVTEST a")
        self.wait_for(lambda: self.count("rename") >= 1)
        rc, d = self.js("gc", "--name-prefix", "SVTEST")
        self.assertEqual(d["deleted"], [])
        self.assertIn("busy", [a["kind"] for a in d["attention"]])
        _o, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual([x["name"] for x in self.devices()], ["SVTEST a"])

    def shim(self):
        path = os.path.join(self.stub, "sim-vault")
        write(path, f'#!/bin/bash\nexec {PY} {self.loader} "$@"\n')
        os.chmod(path, 0o755)

    def test_bash32_caller_example(self):
        self.shim()
        script = textwrap.dedent("""\
            set -euo pipefail
            N="SVTEST a"
            rc=0; udid="$(sim-vault ensure "$N" --create "iPhone 18 Pro Max" --boot)" || rc=$?
            if [ "$rc" -ne 0 ]; then echo "ensure failed: ${rc}" >&2; exit "$rc"; fi
            echo "got ${udid}"
            rc=0; sim-vault park "$N" || rc=$?
            echo "park rc=${rc}"
            rc=0; sim-vault park "$N" --shutdown --missing-ok || rc=$?
            echo "park2 rc=${rc}"
            rc=0; sim-vault ensure "$N" >/dev/null || rc=$?
            echo "ensure2 rc=${rc}"
            """)
        r = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", script], env=self.env(),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("park rc=6", r.stdout)
        self.assertIn("park2 rc=0", r.stdout)
        self.assertIn("ensure2 rc=0", r.stdout)

    def test_python_caller_example(self):
        self.shim()
        code = textwrap.dedent("""\
            import json, subprocess, sys
            def sv(*a):
                p = subprocess.run(["sim-vault", *a, "--json"], capture_output=True, text=True)
                return p.returncode, json.loads(p.stdout)
            rc, d = sv("ensure", "SVTEST a", "--create", "iPhone 18 Pro Max",
                       "--exclusive", "--boot", "--lease", "/tmp/evidence")
            assert rc == 0 and d["bootedByUs"], d
            rc, d = sv("release", "SVTEST a", "--lease", "/tmp/evidence", "--shutdown")
            assert rc == 0 and d["shutdown"], d
            rc, d = sv("park", "SVTEST a", "--missing-ok")
            assert rc == 0 and d["action"] == "parked", d
            print("ok")
            """)
        r = subprocess.run([PY, "-c", code], env=self.env(), capture_output=True, text=True,
                           timeout=120)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"), r.stderr)

    def mkdir(self, name):
        return T1Core.mkdir(self, name)

    def staging(self):
        return T1Core.staging(self)

    def wait_for(self, predicate, timeout=20):
        return T1Core.wait_for(self, predicate, timeout)


class T3Review(SimVaultCase):
    """Round-1 review findings (Fable, Codex), one test or more per finding."""
    js = T2SharedLayer.js
    leases_dir = T2SharedLayer.leases_dir
    mkdir = T1Core.mkdir
    staging = T1Core.staging
    wait_for = T1Core.wait_for

    def inherited(self):
        path = os.path.join(self.fake, "inherited")
        if not os.path.exists(path):
            return []
        with open(path) as h:
            return [json.loads(line)[0] for line in h]

    def boot_record(self, name="SVTEST a"):
        import glob
        found = glob.glob(os.path.join(self.leases_dir(), "*", "_boot.json"))
        return json.load(open(found[0])) if found else None

    # 1 gc must not delete a device that a restore finished renaming meanwhile
    def test_r1_gc_relists_under_lock(self):
        self.parked("SVTEST a")
        self.parked("SVTEST b")
        self.xcrun("create", "SVTEST a [sim-vault restoring 1]", "iPhone 18 Pro Max", RT)
        self.hooks([{"cmd": "rename", "n": 1, "action": "sleep", "seconds": 3},
                    {"cmd": "delete", "n": 2, "action": "sleep", "seconds": 6}])
        p = self.spawn("restore", "SVTEST b")
        self.wait_for(lambda: self.count("rename") >= 1)
        g = self.spawn("gc", "--name-prefix", "SVTEST", "--json")
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)
        g.communicate(timeout=60)
        self.assertEqual([d["name"] for d in self.devices()], ["SVTEST b"])

    # 2 --busy-lock file that does not exist yet is created and held
    def test_r2_busy_lock_created_and_held(self):
        import fcntl
        udid = self.create("SVTEST a")
        tmpl = os.path.join(self.base, "hl-{udid}.lock")
        path = tmpl.replace("{udid}", udid)
        p = self.spawn("park", "SVTEST a", "--busy-lock", tmpl, FAKE_TAR_SLEEP=3)
        self.wait_for(lambda: self.partials("tar"))
        self.assertTrue(os.path.exists(path))
        with open(path) as h:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _o, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(os.path.getsize(path), 0)

    def test_r2_busy_lock_uncreatable_refuses(self):
        self.create("SVTEST a")
        rc, d = self.js("park", "SVTEST a", "--busy-lock",
                        os.path.join(self.base, "no-such-dir", "{udid}.lock"))
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(self.devices("SVTEST a")), 1)

    # 3 restore honours another owner's exclusive lease
    def test_r3_restore_ignores_leases(self):
        # Round 2: restore stays the policy-free command of ecc1e91 (lease users call ensure).
        udid = self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--exclusive", "--lease", "A")[0], 0)
        r = self.sv("restore", "SVTEST a")
        self.assertEqual((r.returncode, r.stdout), (0, udid + "\n"))

    # 4a a boot interrupted before its record was written still gets shut down later
    def test_r4a_boot_intent_written_before_boot(self):
        udid = self.create("SVTEST a")
        self.hooks([{"cmd": "boot", "n": 1, "action": "sleep", "seconds": 30}])
        p = self.spawn("ensure", "SVTEST a", "--boot", "--lease", "A")
        self.wait_for(lambda: self.count("boot") >= 1)
        time.sleep(0.3)
        p.kill()
        p.wait(timeout=30)
        p.stdout.close()
        p.stderr.close()
        self.assertIsNotNone(self.boot_record())
        self.hooks([])
        self.patch_device(udid, fakeState="Booted")      # the orphaned simctl boot went through
        rc, d = self.js("release", "SVTEST a", "--lease", "A", "--shutdown", "--wait", "40")
        self.assertEqual((rc, d["shutdown"]), (0, True))
        self.assertEqual(self.state(udid), "Shutdown")

    # 4b bootstatus failure: what we booted is shut down again, same owner can retry
    def test_r4b_bootstatus_failure_cleans_up(self):
        udid = self.create("SVTEST a")
        rc, _ = self.js("ensure", "SVTEST a", "--exclusive", "--boot", "--lease", "A",
                        FAKE_BOOTSTATUS_RC=1)
        self.assertEqual(rc, 1)
        self.assertEqual(self.state(udid), "Shutdown")
        self.assertIsNone(self.boot_record())
        rc, d = self.js("ensure", "SVTEST a", "--exclusive", "--boot", "--lease", "A")
        self.assertEqual((rc, d["bootedByUs"]), (0, True))

    # 4c last lease released without --shutdown drops the record; a Shutdown device too
    def test_r4c_record_cleared(self):
        udid = self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--boot", "--lease", "A")[0], 0)
        self.assertEqual(self.js("release", "SVTEST a", "--lease", "A")[0], 0)
        self.assertIsNone(self.boot_record())
        self.patch_device(udid, fakeState="Shutdown")
        self.assertEqual(self.js("ensure", "SVTEST a", "--boot", "--lease", "B")[0], 0)
        self.assertIsNotNone(self.boot_record())
        self.patch_device(udid, fakeState="Shutdown")   # shut down by hand
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "B")[0], 0)
        self.assertIsNone(self.boot_record())

    # 4d release --shutdown without a lease of one's own does not shut down
    def test_r4d_release_needs_own_lease(self):
        udid = self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--boot", "--lease", "A")[0], 0)
        for f in os.listdir(os.path.join(self.leases_dir(), os.listdir(self.leases_dir())[0])):
            if not f.startswith("_"):
                os.remove(os.path.join(self.leases_dir(), os.listdir(self.leases_dir())[0], f))
        rc, d = self.js("release", "SVTEST a", "--lease", "C", "--shutdown")
        self.assertEqual((rc, d["shutdown"]), (0, False))
        self.assertEqual(self.state(udid), "Booted")

    # 5 each half of the re-check is needed on its own
    def check_torn(self, mode):
        self.parked("SVTEST a")
        new = self.sv("restore", "SVTEST a").stdout.strip()
        sha = self.tar_sha("SVTEST a")
        r = self.sv("park", "SVTEST a", FAKE_TAR_BOOT=new, FAKE_TAR_MODE=mode)
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertEqual(self.tar_sha("SVTEST a"), sha)
        self.assertEqual([d["UDID"] for d in self.devices("SVTEST a")], [new])

    def test_r5_written_while_archiving_state_unchanged(self):
        self.check_torn("touch")

    def test_r5_booted_while_archiving_no_write(self):
        self.check_torn("state")

    # 6 interrupted between the two replaces: json follows the new tar
    def test_r6_tar_and_json_stay_paired(self):
        self.parked("SVTEST a")
        new = self.sv("restore", "SVTEST a").stdout.strip()
        r = self.sv("park", "SVTEST a", SV_TEST_KILL_AFTER_TAR_REPLACE=1)
        self.assertEqual(r.returncode, 143, r.stderr)
        self.assertEqual(self.meta("SVTEST a")["udid"], new)
        self.assertEqual(self.meta("SVTEST a")["sha256"], self.tar_sha("SVTEST a"))

    def test_r6_list_flags_size_mismatch(self):
        self.parked("SVTEST a")
        with open(os.path.join(self.vault, "SVTEST a.tar"), "ab") as h:
            h.write(b"\0" * 512)
        rc, d = self.js("list")
        self.assertFalse(d["archives"][0]["sizeMatches"])
        self.assertIn("size mismatch", self.sv("list").stdout)

    # 7 a second signal does not cut the cleanup short; failed cleanup is not "cleaned up"
    def test_r7_second_signal_ignored_during_cleanup(self):
        self.create("SVTEST a")
        p = self.spawn("park", "SVTEST a", FAKE_TAR_SLEEP=30, SV_TEST_SLOW_REMOVE=1)
        self.wait_for(lambda: self.partials("tar"))
        p.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        p.send_signal(signal.SIGHUP)
        _o, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM, err)
        self.assert_no_partials()

    def test_r7_failed_cleanup_is_reported(self):
        self.create("SVTEST a")
        p = self.spawn("park", "SVTEST a", FAKE_TAR_SLEEP=30, SV_TEST_FAIL_REMOVE=1)
        self.wait_for(lambda: self.partials("tar"))
        p.send_signal(signal.SIGTERM)
        _o, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM, err)
        self.assertNotIn("cleaned up", err)
        self.assertIn("gc", err)

    # 8 only tar and clone inherit the lock; a dead holder is named as such
    def test_r8_only_protected_children_inherit_lock(self):
        self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--boot", "--lease", "A")[0], 0)
        self.assertEqual(self.js("release", "SVTEST a", "--lease", "A", "--shutdown")[0], 0)
        self.assertEqual(self.js("park", "SVTEST a")[0], 0)
        self.assertEqual(self.js("restore", "SVTEST a")[0], 0)
        # shutdown too: a shutdown outliving a killed sim-vault must keep others out.
        self.assertEqual(sorted(set(self.inherited())), ["clone", "delete", "rename",
                                                         "shutdown"])

    def test_r8_dead_holder_message(self):
        self.create("SVTEST a")
        p = self.spawn("park", "SVTEST a", FAKE_TAR_SLEEP=4)
        self.wait_for(lambda: self.partials("tar"))
        p.kill()
        p.wait(timeout=30)
        p.stdout.close()
        p.stderr.close()
        r = self.sv("restore", "SVTEST a", "--wait", "1")
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertIn("has exited", r.stderr)
        time.sleep(4)

    # 9 metadata without a tar
    def test_r9_json_without_tar(self):
        self.parked("SVTEST a")
        os.remove(os.path.join(self.vault, "SVTEST a.tar"))
        rc, d = self.js("ensure", "SVTEST a", "--create", "iPhone 18 Pro Max")
        self.assertEqual((rc, d["action"], d["needsReplaceOnFirstPark"]), (0, "created", True))
        r = self.sv("park", "SVTEST a")
        self.assertEqual(r.returncode, 7)
        self.assertIn("cannot be restored", r.stderr)

    # -- round 2 -------------------------------------------------------------------------
    def test_q1_boot_lost_to_someone_else_is_not_undone(self):
        udid = self.create("SVTEST a")
        self.hooks([{"cmd": "boot", "n": 1, "action": "state", "device": udid, "state": "Booted"}])
        rc, _ = self.js("ensure", "SVTEST a", "--boot", "--lease", "A")
        self.assertEqual(rc, 1)
        self.assertEqual(self.state(udid), "Booted")

    def test_q3_existing_with_json_only_needs_replace(self):
        self.parked("SVTEST a")
        os.remove(os.path.join(self.vault, "SVTEST a.tar"))
        self.create("SVTEST a")
        rc, d = self.js("ensure", "SVTEST a")
        self.assertEqual((rc, d["action"], d["needsReplaceOnFirstPark"]), (0, "existing", True))

    def test_q4_sigint_then_sigterm_does_not_cut_cleanup(self):
        self.create("SVTEST a")
        p = self.spawn("park", "SVTEST a", FAKE_TAR_SLEEP=30, SV_TEST_SLOW_REMOVE=1)
        self.wait_for(lambda: self.partials("tar"))
        p.send_signal(signal.SIGINT)
        time.sleep(0.5)
        p.send_signal(signal.SIGTERM)
        _o, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGINT, err)
        self.assert_no_partials()

    def test_q6_mismatched_json_partial_is_kept(self):
        self.parked("SVTEST a")
        self.sv("restore", "SVTEST a")
        old = self.meta("SVTEST a")
        r = self.sv("park", "SVTEST a", SV_TEST_KILL_AFTER_TAR_REPLACE=1,
                    SV_TEST_CORRUPT_TAR_AFTER_REPLACE=1)
        self.assertEqual(r.returncode, 143, r.stderr)
        self.assertEqual(self.meta("SVTEST a"), old)
        self.assertEqual(len(self.partials("json")), 1)

    def test_q7_one_retry_when_only_written_during_archive(self):
        self.parked("SVTEST a")
        new = self.sv("restore", "SVTEST a").stdout.strip()
        r = self.sv("park", "SVTEST a", FAKE_TAR_BOOT=new, FAKE_TAR_MODE="touch",
                    FAKE_TAR_ONCE=os.path.join(self.base, "tar-once"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("once more", r.stderr)
        self.assertEqual(self.meta("SVTEST a")["udid"], new)

    def test_z1_retry_refuses_when_renamed_meanwhile(self):
        self.parked("SVTEST a")
        new = self.sv("restore", "SVTEST a").stdout.strip()
        sha = self.tar_sha("SVTEST a")
        r = self.sv("park", "SVTEST a", FAKE_TAR_BOOT=new, FAKE_TAR_MODE="touch",
                    FAKE_TAR_ONCE=os.path.join(self.base, "tar-once"),
                    FAKE_TAR_RENAME="SVTEST renamed")
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertEqual([d["UDID"] for d in self.devices("SVTEST renamed")], [new])
        self.assertEqual(self.tar_sha("SVTEST a"), sha)
        self.assert_no_partials()

    def test_z2_failed_boot_withdraws_boot_record(self):
        udid = self.create("SVTEST a")
        self.hooks([{"cmd": "boot", "n": 1, "action": "state", "device": udid, "state": "Booted"}])
        rc, _ = self.js("ensure", "SVTEST a", "--boot", "--lease", "A")
        self.assertEqual(rc, 1)
        self.assertIsNone(self.boot_record())

    def test_q8_failed_tar_replace_keeps_old_pair(self):
        self.parked("SVTEST a")
        self.sv("restore", "SVTEST a")
        sha = self.tar_sha("SVTEST a")
        r = self.sv("park", "SVTEST a", SV_TEST_FAIL_TAR_REPLACE=1)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual((self.tar_sha("SVTEST a"), self.meta("SVTEST a")["sha256"]), (sha, sha))
        self.assert_no_partials()

    def test_q8_big_resource_fork_with_copyfile_disabled(self):
        import ctypes
        udid = self.create("SVTEST a")
        f = os.path.join(self.devset, udid, "data", "fork")
        write(f, "")
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        val = bytes(range(256)) * 4096
        self.assertEqual(libc.setxattr(f.encode(), b"com.apple.ResourceFork", val, len(val), 0, 0), 0)
        r = self.sv("park", "SVTEST a", COPYFILE_DISABLE=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.mkdir("extract")
        subprocess.run(["/usr/bin/tar", "-xf", os.path.join(self.vault, "SVTEST a.tar"), "-C",
                        out], check=True)
        g = os.path.join(out, udid, "data", "fork").encode()
        self.assertEqual(libc.getxattr(g, b"com.apple.ResourceFork", None, 0, 0, 0), len(val))

    # 10 help text
    def test_r10_help_wording(self):
        out = self.sv("--help").stdout
        self.assertIn("120", out)
        self.assertNotIn("always 0", out)
        self.assertNotIn("takes no lock", self.sv("status", "--help").stdout)

    # 11 a big resource-fork xattr no longer breaks the archive; old archives restore
    def test_r11_big_resource_fork(self):
        import ctypes
        udid = self.create("SVTEST a")
        f = os.path.join(self.devset, udid, "data", "fork")
        write(f, "")
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        val = bytes(range(256)) * 4096          # 1 MiB; 900 KB already broke bsdtar
        self.assertEqual(libc.setxattr(f.encode(), b"com.apple.ResourceFork", val, len(val), 0, 0), 0)
        r = self.sv("park", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.sv("restore", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)
        # The fake clone (copytree) drops xattrs; check the archive itself keeps it.
        out = self.mkdir("extract")
        subprocess.run(["/usr/bin/tar", "-xf", os.path.join(self.vault, "SVTEST a.tar"), "-C",
                        out], check=True)
        g = os.path.join(out, udid, "data", "fork").encode()
        self.assertEqual(libc.getxattr(g, b"com.apple.ResourceFork", None, 0, 0, 0), len(val))

    def test_r11_plain_tar_archive_still_restores(self):
        udid = self.create("SVTEST a")
        tar = os.path.join(self.vault, "SVTEST a.tar")
        os.makedirs(self.vault, exist_ok=True)
        subprocess.run(["/usr/bin/tar", "-C", self.devset, "-cf", tar, udid], check=True)
        with open(tar, "rb") as h:
            sha = hashlib.sha256(h.read()).hexdigest()
        with open(os.path.join(self.vault, "SVTEST a.json"), "w") as h:
            json.dump({"name": "SVTEST a", "udid": udid, "runtime": RT, "deviceType": "x",
                       "sha256": sha, "bytes": os.path.getsize(tar), "parkedAt": "old"}, h)
        self.xcrun("delete", udid)
        r = self.sv("restore", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)


class T4V3(SimVaultCase):
    """v3: read-back without the page cache, history.jsonl, error line, PosterBoard trim,
    symbol-cache exclusion, gc on odd lease entries, `shutdown`, a park killed between its
    two renames."""
    js = T2SharedLayer.js
    leases_dir = T2SharedLayer.leases_dir
    mkdir = T1Core.mkdir
    wait_for = T1Core.wait_for
    inherited = T3Review.inherited

    def history(self):
        path = os.path.join(self.home, "Library/Developer/CoreSimulator/sim-vault/history.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as h:
            return [json.loads(line) for line in h]

    def members(self, name):
        out = subprocess.run(["/usr/bin/tar", "-tf", os.path.join(self.vault, name + ".tar")],
                             capture_output=True, text=True, check=True).stdout
        return out.splitlines()

    def put(self, udid, rel, size=10):
        path = os.path.join(self.devset, udid, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as h:
            h.write(os.urandom(size))
        return path

    def nocache_log(self):
        return os.path.join(self.base, "nocache.jsonl")

    def nocache_calls(self):
        if not os.path.exists(self.nocache_log()):
            return []
        with open(self.nocache_log()) as h:
            return [json.loads(line) for line in h]

    # -- 1 F_NOCACHE -------------------------------------------------------------------
    def test_nocache_set_before_first_read(self):
        self.create("SVTEST a")
        rc, _ = self.js("park", "SVTEST a", SV_TEST_NOCACHE_LOG=self.nocache_log())
        self.assertEqual(rc, 0)
        calls = self.nocache_calls()
        self.assertEqual([(c["arg"], c["offset"]) for c in calls], [(1, 0)])
        self.assertRegex(calls[0]["path"], r"/SVTEST a\.tar\.\d+-[0-9a-f]+\.partial$")
        os.remove(self.nocache_log())
        rc, _ = self.js("restore", "SVTEST a", SV_TEST_NOCACHE_LOG=self.nocache_log())
        self.assertEqual(rc, 0)
        calls = self.nocache_calls()
        self.assertEqual([(c["arg"], c["offset"]) for c in calls], [(1, 0)])
        self.assertTrue(calls[0]["path"].endswith("/SVTEST a.tar"), calls)

    def test_nocache_failure_keeps_device_on_park_warns_on_restore(self):
        self.parked("SVTEST a")
        new = self.sv("restore", "SVTEST a").stdout.strip()
        sha = self.tar_sha("SVTEST a")
        rc, d = self.js("park", "SVTEST a", SV_TEST_NOCACHE_FAIL=1)
        self.assertEqual(rc, 1)
        self.assertIn("read caching", d["message"])
        self.assertEqual([x["UDID"] for x in self.devices("SVTEST a")], [new])
        self.assertEqual(self.tar_sha("SVTEST a"), sha)
        self.assert_no_partials()
        self.xcrun("delete", new)
        r = self.sv("restore", "SVTEST a", SV_TEST_NOCACHE_FAIL=1)
        self.assertEqual(r.returncode, 0, r.stderr)        # the checksum still checks the bytes
        self.assertIn("warning: cannot turn off read caching", r.stderr)

    # -- 2 history.jsonl ---------------------------------------------------------------
    def test_history_one_line_per_recorded_call(self):
        udid = self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a")[0], 0)                   # existing
        self.assertEqual(self.js("park", "SVTEST none", "--missing-ok")[0], 0)  # absent
        self.assertEqual(self.js("release", "SVTEST a", "--lease", "X")[0], 0)  # no-lease
        self.assertEqual(self.js("status", "SVTEST a")[0], 0)
        self.assertEqual(self.js("list")[0], 0)
        self.assertEqual(self.js("gc", "--dry-run")[0], 0)
        rc, parked = self.js("park", "SVTEST a")
        self.assertEqual(rc, 0)
        rc, restored = self.js("restore", "SVTEST a")
        self.assertEqual(rc, 0)
        self.assertEqual(self.sv("park").returncode, 2)                          # usage
        self.assertEqual(self.sv("restore", "SVTEST none").returncode, 3)
        rc, shut = self.js("shutdown", "SVTEST a")
        self.assertEqual(rc, 0)
        h = self.history()
        self.assertEqual([(x["cmd"], x["rc"], x["action"]) for x in h], [
            ("ensure", 0, "existing"), ("park", 0, "absent"), ("release", 0, "no-lease"),
            ("park", 0, "parked"), ("restore", 0, "restored"), ("park", 2, None),
            ("restore", 3, None), ("shutdown", 0, "already-shutdown")])
        for x in h:
            self.assertEqual(set(x), {"ts", "cmd", "name", "udid", "rc", "kind", "action",
                                      "bytes", "posterMB", "cwd", "pid", "seconds", "phases"})
        self.assertEqual((h[0]["udid"], h[0]["name"]), (udid, "SVTEST a"))
        self.assertEqual((h[5]["kind"], h[6]["kind"]), ("usage", "no-archive"))
        park = h[3]
        self.assertEqual((park["udid"], park["bytes"], park["posterMB"]),
                         (udid, parked["bytes"], 0))
        self.assertEqual(set(park["phases"]), {"lockWait", "shutdownQuiesce", "write",
                                               "readBack", "delete"})
        self.assertGreaterEqual(park["seconds"], sum(park["phases"].values()) - 0.01)
        self.assertEqual(set(h[4]["phases"]), {"lockWait", "extract", "clone"})
        self.assertEqual(h[4]["udid"], restored["udid"])
        # --json carries the same timing.
        self.assertEqual(set(parked["phases"]), set(park["phases"]))
        self.assertIn("seconds", parked)
        self.assertIn("seconds", shut)

    def test_history_rc_5_8_9(self):
        self.create("SVTEST lease")
        self.assertEqual(self.js("ensure", "SVTEST lease", "--lease", "A")[0], 0)
        self.assertEqual(self.js("park", "SVTEST lease")[0], 8)
        udid = self.create("SVTEST poster")
        self.put(udid, "data/Library/Application Support/PRBPosterExtensionDataStore/x",
                 200 * 1024)
        self.assertEqual(self.js("park", "SVTEST poster", "--keep-posterboard",
                                 SV_TEST_POSTER_LIMIT_KB=64)[0], 9)
        good_vault = self.vault
        self.vault = os.path.join(self.base, "unmounted", "vault")
        self.assertEqual(self.js("park", "SVTEST lease")[0], 5)
        self.vault = good_vault
        h = self.history()[1:]
        self.assertEqual([(x["cmd"], x["rc"], x["kind"]) for x in h], [
            ("park", 8, "busy"), ("park", 9, "posterboard-bloated"),
            ("park", 5, "vault-unavailable")])
        self.assertEqual(h[1]["posterMB"], 0)      # 200 KB rounds down; still recorded
        self.assertEqual(h[1]["udid"], udid)

    def test_history_on_signal(self):
        self.create("SVTEST a")
        sigs = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
        for sig in sigs:
            p = self.spawn("park", "SVTEST a", FAKE_TAR_SLEEP=30)
            self.wait_for(lambda: self.partials("tar"))
            p.send_signal(sig)
            _o, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 128 + sig, err)
            self.assertIn(f"error (exit {128 + sig}, interrupted): interrupted by "
                          f"signal {sig}; cleaned up", err)
            self.assertIn("next: rerun", err)
            self.wait_for(lambda: not self.partials("tar"))
        h = self.history()
        self.assertEqual([(x["cmd"], x["rc"], x["kind"]) for x in h],
                         [("park", 128 + sig, "interrupted") for sig in sigs])
        for x in h:
            self.assertIn("write", x["phases"])

    def test_history_parallel_writers(self):
        procs = [self.spawn("release", f"SVTEST p{i}", "--lease", "A") for i in range(16)]
        for p in procs:
            _o, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
        path = os.path.join(self.home, "Library/Developer/CoreSimulator/sim-vault/history.jsonl")
        with open(path) as h:
            lines = h.read().split("\n")
        self.assertEqual(lines[-1], "")
        parsed = [json.loads(line) for line in lines[:-1]]
        self.assertEqual(sorted(x["name"] for x in parsed), sorted(f"SVTEST p{i}"
                                                                   for i in range(16)))
        self.assertEqual(len({x["pid"] for x in parsed}), 16)

    def test_history_unwritable_keeps_rc_and_stdout(self):
        udid = self.create("SVTEST a")
        os.makedirs(os.path.join(self.home,
                                 "Library/Developer/CoreSimulator/sim-vault/history.jsonl"))
        r = self.sv("ensure", "SVTEST a")
        self.assertEqual((r.returncode, r.stdout), (0, udid + "\n"))
        self.assertIn("warning: could not write", r.stderr)
        rc, d = self.js("restore", "SVTEST none")
        self.assertEqual((rc, d["kind"]), (3, "no-archive"))

    def test_history_counts_both_archive_attempts(self):
        self.parked("SVTEST a")
        new = self.sv("restore", "SVTEST a").stdout.strip()
        rc, d = self.js("park", "SVTEST a", FAKE_TAR_BOOT=new, FAKE_TAR_MODE="touch",
                        FAKE_TAR_ONCE=os.path.join(self.base, "tar-once"), FAKE_TAR_SLEEP=1)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(d["phases"]["write"], 2.0)
        self.assertGreaterEqual(self.history()[-1]["phases"]["write"], 2.0)

    def test_parked_message_has_total_and_phases(self):
        self.create("SVTEST a")
        r = self.sv("park", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stderr, r"parked SVTEST a: [\d.]+ GB in \d+s \(lockWait [\d.]+s, "
                                   r"shutdownQuiesce [\d.]+s, write [\d.]+s, readBack [\d.]+s, "
                                   r"delete [\d.]+s\); device deleted")

    # -- 3 error line ------------------------------------------------------------------
    def test_error_line_names_exit_and_kind(self):
        r = self.sv("restore", "SVTEST none")
        self.assertIn("sim-vault: error (exit 3, no-archive): no archive for 'SVTEST none'",
                      r.stderr)
        self.assertIn("sim-vault: next: ", r.stderr)
        r = self.sv("park")
        self.assertIn("error (exit 2, usage): ", r.stderr)
        # An unexpected exception (injected).
        name = "SVTEST a"
        self.create(name)
        r = self.sv("ensure", name, SV_TEST_RAISE_IN="restore_locked")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("error (exit 1, unexpected): unexpected ", r.stderr)
        self.assertIn("next: stop and report this message", r.stderr)
        self.assertEqual(self.history()[-1]["kind"], "unexpected")

    def test_trim_records_only_existing_dirs(self):
        udid = self.create("SVTEST a")
        self.put(udid, "data/Library/Application Support/PRBPosterExtensionDataStore/big",
                 200 * 1024)
        rc, d = self.js("park", "SVTEST a", "--exclude-symbol-cache",
                        SV_TEST_POSTER_LIMIT_KB=64)
        self.assertEqual(rc, 0, d)
        self.assertEqual(self.meta("SVTEST a")["excluded"],
                         ["data/Library/Application Support/PRBPosterExtensionDataStore"])

    # -- 4 --trim-posterboard ----------------------------------------------------------
    def poster_device(self, name="SVTEST a", state="Booted"):
        udid = self.create(name, state=state)
        support = "data/Library/Application Support/"
        self.put(udid, support + "PRBPosterExtensionDataStore/big", 200 * 1024)
        self.put(udid, support + "com.apple.PosterKit/k")
        self.put(udid, support + "PRBPosterExtensionDataStore-bak/b")     # same prefix
        app = "data/Containers/Data/Application/APP1/Library/Application Support/"
        self.put(udid, app + "PRBPosterExtensionDataStore/app")
        self.put(udid, app + "com.apple.PosterKit/app")
        return udid

    def test_trim_posterboard_over_limit_by_default(self):
        udid = self.poster_device()
        rc, d = self.js("park", "SVTEST a", "--shutdown", "--keep-posterboard",
                        SV_TEST_POSTER_LIMIT_KB=64)
        self.assertEqual((rc, d["kind"]), (9, "posterboard-bloated"))
        self.assertEqual(self.state(udid), "Booted")
        r = self.sv("park", "SVTEST a", "--shutdown", "--json", SV_TEST_POSTER_LIMIT_KB=64)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertIn("leaving it out of the archive", r.stderr)
        meta = self.meta("SVTEST a")
        excluded = ["data/Library/Application Support/PRBPosterExtensionDataStore",
                    "data/Library/Application Support/com.apple.PosterKit"]
        self.assertEqual((meta["posterboardTrimmed"], meta["excluded"]), (True, excluded))
        self.assertEqual((d["posterboardTrimmed"], d["excluded"]), (True, excluded))
        members = self.members("SVTEST a")
        sys_support = f"{udid}/data/Library/Application Support/"
        self.assertFalse([m for m in members
                          if m.startswith(sys_support + "PRBPosterExtensionDataStore/")
                          or m.startswith(sys_support + "com.apple.PosterKit")], members)
        self.assertIn(sys_support + "PRBPosterExtensionDataStore-bak/b", members)
        app = f"{udid}/data/Containers/Data/Application/APP1/Library/Application Support/"
        self.assertIn(app + "PRBPosterExtensionDataStore/app", members)
        self.assertIn(app + "com.apple.PosterKit/app", members)
        r = self.sv("restore", "SVTEST a")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_trim_posterboard_alias_still_accepted(self):
        udid = self.poster_device(state="Shutdown")
        rc, d = self.js("park", "SVTEST a", "--trim-posterboard", SV_TEST_POSTER_LIMIT_KB=64)
        self.assertEqual((rc, d["posterboardTrimmed"]), (0, True))

    def test_trim_posterboard_under_limit_archives_everything(self):
        udid = self.poster_device(state="Shutdown")
        rc, d = self.js("park", "SVTEST a")
        self.assertEqual(rc, 0, d)
        self.assertNotIn("posterboardTrimmed", self.meta("SVTEST a"))
        self.assertNotIn("excluded", self.meta("SVTEST a"))
        self.assertIn(f"{udid}/data/Library/Application Support/PRBPosterExtensionDataStore/big",
                      self.members("SVTEST a"))

    # -- 5 --exclude-symbol-cache ------------------------------------------------------
    def test_exclude_symbol_cache(self):
        import ctypes
        udid = self.create("SVTEST a")
        cache = "Library/Caches/com.apple.coresymbolicationd/"
        self.put(udid, "data/" + cache + "sym", 4096)
        self.put(udid, "data/Containers/Data/Application/APP1/" + cache + "keep")
        self.put(udid, "data/Containers/Shared/AppGroup/G1/" + cache + "group")
        self.put(udid, "data/Containers/Shared/AppGroup/G1/Library/data.sqlite")
        fork = self.put(udid, "data/Containers/Data/Application/APP1/Documents/fork")
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        val = bytes(range(256)) * 4096
        self.assertEqual(libc.setxattr(fork.encode(), b"com.apple.ResourceFork", val, len(val),
                                       0, 0), 0)
        rc, d = self.js("park", "SVTEST a", "--exclude-symbol-cache")
        self.assertEqual(rc, 0, d)
        meta = self.meta("SVTEST a")
        self.assertEqual(meta["excluded"], ["data/Library/Caches/com.apple.coresymbolicationd"])
        self.assertNotIn("posterboardTrimmed", meta)
        members = self.members("SVTEST a")
        self.assertFalse([m for m in members if m.startswith(f"{udid}/data/{cache}")], members)
        for rel in ("data/Containers/Data/Application/APP1/" + cache + "keep",
                    "data/Containers/Shared/AppGroup/G1/" + cache + "group",
                    "data/Containers/Shared/AppGroup/G1/Library/data.sqlite"):
            self.assertIn(f"{udid}/{rel}", members)
        out = self.mkdir("extract")
        subprocess.run(["/usr/bin/tar", "-xf", os.path.join(self.vault, "SVTEST a.tar"), "-C",
                        out], check=True)
        g = os.path.join(out, udid, "data/Containers/Data/Application/APP1/Documents/fork")
        self.assertEqual(libc.getxattr(g.encode(), b"com.apple.ResourceFork", None, 0, 0, 0),
                         len(val))

    # -- 6 gc and odd lease entries ----------------------------------------------------
    def test_gc_skips_non_directories_in_leases(self):
        self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "A")[0], 0)
        write(os.path.join(self.leases_dir(), ".DS_Store"), "x")
        rc, d = self.js("gc", "--dry-run", "--leases-older-than", "0s")
        self.assertEqual(rc, 0, d)
        self.assertEqual([a["what"] for a in d["attention"] if a["kind"] == "lease"], ["A"])
        self.assertTrue(os.path.exists(os.path.join(self.leases_dir(), ".DS_Store")))

    def test_gc_skips_lease_released_meanwhile(self):
        self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "A")[0], 0)
        rc, d = self.js("gc", "--dry-run", "--leases-older-than", "0s",
                        SV_TEST_LEASE_VANISHES=1)
        self.assertEqual(rc, 0, d)
        self.assertEqual([a for a in d["attention"] if a["kind"] == "lease"], [])

    # -- 7 shutdown --------------------------------------------------------------------
    def test_shutdown_command(self):
        udid = self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--boot", "--lease", "A")[0], 0)
        rc, d = self.js("shutdown", "SVTEST a")
        self.assertEqual((rc, d["kind"]), (8, "busy"))
        self.assertIn("leased by A", d["message"])
        self.assertEqual(self.state(udid), "Booted")
        self.assertEqual(self.count("shutdown"), 0)
        self.assertEqual(self.js("release", "SVTEST a", "--lease", "A")[0], 0)
        # Works without the vault: never restores, never reads the archive.
        self.vault = os.path.join(self.base, "unmounted", "vault")
        rc, d = self.js("shutdown", "SVTEST a")
        self.assertEqual((rc, d["action"], d["state"]), (0, "shutdown", "Shutdown"))
        self.assertEqual(self.state(udid), "Shutdown")
        self.assertIn("shutdown", self.inherited())     # the simctl child holds the lock
        rc, d = self.js("shutdown", "SVTEST a")
        self.assertEqual((rc, d["action"]), (0, "already-shutdown"))
        rc, d = self.js("shutdown", "SVTEST none")
        self.assertEqual((rc, d["action"]), (0, "absent"))

    def test_shutdown_respects_busy_lock_and_device_lock(self):
        import fcntl
        udid = self.create("SVTEST a", state="Booted")
        tmpl = os.path.join(self.base, "hl-{udid}.lock")
        path = tmpl.replace("{udid}", udid)
        write(path, "")
        with open(path) as h:
            fcntl.flock(h, fcntl.LOCK_EX)
            rc, d = self.js("shutdown", "SVTEST a", "--busy-lock", tmpl)
            self.assertEqual(rc, 8)
        self.assertEqual(self.state(udid), "Booted")
        p = self.spawn("park", "SVTEST a", "--shutdown", FAKE_TAR_SLEEP=3)
        self.wait_for(lambda: self.partials("tar"))
        rc, d = self.js("shutdown", "SVTEST a", "--wait", "0.5")
        self.assertEqual(rc, 8)
        _o, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        rc, d = self.js("shutdown", "SVTEST a", "--busy-lock", tmpl)
        self.assertEqual((rc, d["action"]), (0, "absent"))

    # -- 8 killed between the tar and the metadata rename ------------------------------
    def killed_between_renames(self):
        self.parked("SVTEST a")
        new = self.sv("restore", "SVTEST a").stdout.strip()
        old = self.meta("SVTEST a")
        r = self.sv("park", "SVTEST a", SV_TEST_EXIT_AFTER_TAR_REPLACE=1)
        self.assertEqual(r.returncode, 137, r.stderr)
        self.assertEqual([d["UDID"] for d in self.devices("SVTEST a")], [new])
        self.assertEqual(self.meta("SVTEST a"), old)                  # old metadata ...
        self.assertNotEqual(old["sha256"], self.tar_sha("SVTEST a"))  # ... new tar
        found = self.partials("json")
        self.assertEqual(len(found), 1)
        return new, found[0]

    def test_killed_park_partial_survives_gc_and_restore_pairs_it(self):
        new, partial = self.killed_between_renames()
        os.utime(partial, (time.time() - 7200, time.time() - 7200))
        rc, d = self.js("gc", "--name-prefix", "SVTEST")
        self.assertEqual((rc, d["deleted"]), (0, []))
        self.assertIn(partial, [a["what"] for a in d["attention"]])
        self.assertTrue(os.path.exists(partial))
        rc, d = self.js("restore", "SVTEST a", SV_TEST_NOCACHE_LOG=self.nocache_log())
        self.assertEqual((rc, d["action"], d["udid"]), (0, "existing", new))
        self.assertTrue(self.nocache_calls()[0]["path"].endswith("/SVTEST a.tar"))
        self.assertFalse(os.path.exists(partial))
        self.assertEqual(self.meta("SVTEST a")["sha256"], self.tar_sha("SVTEST a"))
        self.assertEqual(self.meta("SVTEST a")["udid"], new)
        # The paired archive restores on its own.
        self.xcrun("delete", new)
        self.assertEqual(self.js("restore", "SVTEST a")[1]["action"], "restored")

    def test_killed_park_next_park_recollects(self):
        new, partial = self.killed_between_renames()
        rc, d = self.js("park", "SVTEST a")
        self.assertEqual((rc, d["action"]), (0, "parked"))
        self.assertEqual(self.meta("SVTEST a")["sha256"], self.tar_sha("SVTEST a"))
        self.assert_no_partials()
        self.assertEqual(self.devices("SVTEST a"), [])
        self.assertEqual(self.js("restore", "SVTEST a")[1]["action"], "restored")

    def test_mismatched_partial_is_left_alone(self):
        new, partial = self.killed_between_renames()
        with open(partial) as h:
            data = json.load(h)
        data["sha256"] = "0" * 64
        with open(partial, "w") as h:
            json.dump(data, h)
        old = self.meta("SVTEST a")
        rc, d = self.js("restore", "SVTEST a")
        # Unsettled pair: refused (round-2 F5), everything left as it is.
        self.assertEqual((rc, d["kind"]), (1, "pending-pair"))
        self.assertTrue(os.path.exists(partial))
        self.assertEqual(self.meta("SVTEST a"), old)
        self.assertEqual([x["UDID"] for x in self.devices("SVTEST a")], [new])

    # -- review round (Codex) ------------------------------------------------------------
    def test_retry_with_unreadable_tar_keeps_pending_metadata(self):
        new, partial = self.killed_between_renames()
        old = self.meta("SVTEST a")
        tar_sha = self.tar_sha("SVTEST a")
        rc, d = self.js("park", "SVTEST a", SV_TEST_NOCACHE_FAIL=1)
        self.assertEqual((rc, d["kind"]), (1, "pending-pair"))
        self.assertEqual(self.partials("json"), [partial])        # not cleaned away
        self.assertEqual((self.meta("SVTEST a"), self.tar_sha("SVTEST a")), (old, tar_sha))
        self.assertEqual([x["UDID"] for x in self.devices("SVTEST a")], [new])
        self.assertEqual(self.partials("tar"), [])
        rc, d = self.js("park", "SVTEST a")                       # vault reads fine again
        self.assertEqual((rc, d["action"]), (0, "parked"))
        self.assertEqual(self.meta("SVTEST a")["sha256"], self.tar_sha("SVTEST a"))
        self.assert_no_partials()

    def test_mismatched_pending_blocks_park(self):
        new, partial = self.killed_between_renames()
        with open(partial) as h:
            data = json.load(h)
        data["sha256"] = "0" * 64
        with open(partial, "w") as h:
            json.dump(data, h)
        tar_sha = self.tar_sha("SVTEST a")
        rc, d = self.js("park", "SVTEST a")
        self.assertEqual((rc, d["kind"]), (1, "pending-pair"))
        self.assertEqual((self.partials("json"), self.tar_sha("SVTEST a")), ([partial], tar_sha))
        self.assertEqual(len(self.devices("SVTEST a")), 1)

    def test_stale_metadata_partial_removed_once_paired(self):
        self.parked("SVTEST a")
        stale = os.path.join(self.vault, "SVTEST a.json.1-abcdef.partial")
        write(stale, json.dumps({"udid": "x", "sha256": "0" * 64}))
        self.sv("restore", "SVTEST a")
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(self.meta("SVTEST a")["sha256"], self.tar_sha("SVTEST a"))

    def test_each_park_run_cleans_only_its_own_partials(self):
        self.create("SVTEST a")
        other = os.path.join(self.vault, "SVTEST a.tar.1-abcdef.partial")
        os.makedirs(self.vault, exist_ok=True)
        write(other, "someone else's")
        p = self.spawn("park", "SVTEST a", FAKE_TAR_SLEEP=30)
        self.wait_for(lambda: len(self.partials("tar")) == 2)
        p.send_signal(signal.SIGTERM)
        _o, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM, err)
        self.assertEqual(self.partials("tar"), [other])

    def test_gc_rechecks_under_lock_after_interrupted_park(self):
        self.parked("SVTEST a")
        new = self.sv("restore", "SVTEST a").stdout.strip()
        old_partial = os.path.join(self.vault, "SVTEST a.tar.partial")      # v2 leftover
        write(old_partial, "x")
        os.utime(old_partial, (time.time() - 7200, time.time() - 7200))
        hook = json.dumps([PY, self.loader, "park", "SVTEST a"])
        rc, d = self.js("gc", "--name-prefix", "SVTEST", SV_TEST_GC_HOOK=hook,
                        SV_TEST_EXIT_AFTER_TAR_REPLACE=1)
        self.assertEqual(rc, 0, d)
        pending = self.partials("json")
        self.assertEqual(len(pending), 1)                    # the interrupted park's metadata
        self.assertNotIn(pending[0], [x["what"] for x in d["deleted"]])
        rc, d = self.js("restore", "SVTEST a")
        self.assertEqual((rc, d["action"], d["udid"]), (0, "existing", new))
        self.assertEqual(self.meta("SVTEST a")["sha256"], self.tar_sha("SVTEST a"))

    def test_gc_keeps_partial_replaced_while_waiting_for_lock(self):
        self.parked("SVTEST a")
        old_partial = os.path.join(self.vault, "SVTEST a.tar.partial")
        write(old_partial, "x")
        os.utime(old_partial, (time.time() - 7200, time.time() - 7200))
        code = (f"import os; p={old_partial!r}; os.remove(p); open(p, 'w').write('new'); "
                "os.utime(p, (0, 0))")              # replaced, and still looks old
        rc, d = self.js("gc", "--name-prefix", "SVTEST",
                        SV_TEST_GC_HOOK=json.dumps([PY, "-c", code]))
        self.assertEqual((rc, d["deleted"]), (0, []))
        self.assertIn("changed while gc waited", str(d["attention"]))
        with open(old_partial) as h:
            self.assertEqual(h.read(), "new")

    def lease_files(self):
        out = []
        for root, _dirs, files in os.walk(self.leases_dir()):
            out += [os.path.join(root, f) for f in files if not f.startswith("_")]
        return out

    def test_unreadable_lease_stops_shutdown_and_park(self):
        udid = self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--boot", "--lease", "A")[0], 0)
        lease = self.lease_files()[0]
        os.chmod(lease, 0)
        try:
            rc, d = self.js("shutdown", "SVTEST a")
            self.assertEqual((rc, d["kind"]), (1, "lease-unreadable"))
            self.assertEqual(self.state(udid), "Booted")
            rc, d = self.js("park", "SVTEST a", "--shutdown")
            self.assertEqual((rc, d["kind"]), (1, "lease-unreadable"))
            self.assertEqual(self.state(udid), "Booted")
        finally:
            os.chmod(lease, 0o644)
        write(lease, "{not json")
        self.assertEqual(self.js("shutdown", "SVTEST a")[1]["kind"], "lease-unreadable")
        write(lease, "[1]")
        self.assertEqual(self.js("park", "SVTEST a", "--shutdown")[1]["kind"],
                         "lease-unreadable")
        self.assertEqual(self.state(udid), "Booted")
        self.assertEqual(self.count("shutdown"), 0)

    def test_shutdown_rereads_state_after_locks(self):
        udid = self.create("SVTEST a")
        # Booted by someone else between shutdown's first look and taking the locks.
        self.hooks([{"cmd": "list", "n": 2, "action": "state", "device": udid,
                     "state": "Booted"}])
        rc, d = self.js("shutdown", "SVTEST a")
        self.assertEqual((rc, d["action"], d["state"]), (0, "shutdown", "Shutdown"))
        self.assertEqual(self.state(udid), "Shutdown")

    def test_history_lock_held_does_not_block_exit(self):
        import fcntl
        udid = self.create("SVTEST a")
        path = os.path.join(self.home, "Library/Developer/CoreSimulator/sim-vault/history.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as h:
            fcntl.flock(h, fcntl.LOCK_EX)
            started = time.time()
            r = self.sv("ensure", "SVTEST a", SV_TEST_HISTORY_LOCK_TIMEOUT=0.5)
            self.assertLess(time.time() - started, 5)
        self.assertEqual((r.returncode, r.stdout), (0, udid + "\n"))
        self.assertIn("history lock busy", r.stderr)
        self.assertEqual(self.history(), [])

    def orphan_tar(self):
        self.parked("SVTEST a")
        os.remove(os.path.join(self.vault, "SVTEST a.json"))
        return self.tar_sha("SVTEST a")

    def test_orphan_tar_is_not_no_archive(self):
        sha = self.orphan_tar()
        rc, d = self.js("restore", "SVTEST a")
        self.assertEqual((rc, d["kind"]), (1, "archive-incomplete"))
        rc, d = self.js("ensure", "SVTEST a", "--create", "iPhone 18 Pro Max")
        self.assertEqual((rc, d["kind"]), (1, "archive-incomplete"))
        self.assertEqual(self.devices(), [])                 # --create did not create
        self.assertEqual(self.tar_sha("SVTEST a"), sha)
        self.assertIn("archive-incomplete", str(self.js("status", "SVTEST a")[1]["archive"]))

    def test_park_refuses_to_overwrite_orphan_tar(self):
        sha = self.orphan_tar()
        blank = self.create("SVTEST a")
        rc, d = self.js("park", "SVTEST a")
        self.assertEqual((rc, d["kind"]), (7, "archive-incomplete"))
        self.assertEqual(self.tar_sha("SVTEST a"), sha)
        self.assertEqual([x["UDID"] for x in self.devices("SVTEST a")], [blank])
        self.assertEqual(self.js("park", "SVTEST a", "--replace")[0], 0)
        self.assertEqual(self.meta("SVTEST a")["udid"], blank)

    def test_deny_other_leases(self):
        udid = self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "manual")[0], 0)
        rc, d = self.js("ensure", "SVTEST a", "--lease", "test", "--deny-other-leases")
        self.assertEqual((rc, d["kind"]), (8, "busy"))
        self.assertIn("manual", d["message"])
        self.assertEqual(len(self.lease_files()), 1)         # no lease taken
        rc, d = self.js("ensure", "SVTEST a", "--lease", "test", "--deny-other-leases",
                        "--allow-lease", "other", "--allow-lease", "manual")
        self.assertEqual(rc, 0, d)
        self.assertEqual(len(self.lease_files()), 2)
        rc, d = self.js("status", "SVTEST a")
        self.assertEqual({l["mode"] for l in d["leases"]}, {"shared"})
        # Own lease again: passes.
        rc, d = self.js("ensure", "SVTEST a", "--lease", "test", "--deny-other-leases",
                        "--allow-lease", "manual")
        self.assertEqual(rc, 0, d)
        self.assertEqual(self.js("release", "SVTEST a", "--lease", "manual")[0], 0)
        self.assertEqual(self.js("release", "SVTEST a", "--lease", "test")[0], 0)
        # Running without any lease: passes (not --exclusive).
        self.patch_device(udid, fakeState="Booted")
        rc, d = self.js("ensure", "SVTEST a", "--lease", "test", "--deny-other-leases")
        self.assertEqual((rc, d["state"]), (0, "Booted"))
        self.assertEqual(self.js("ensure", "SVTEST a", "--deny-other-leases")[0], 2)
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "x", "--allow-lease",
                                 "y")[0], 2)

    def test_release_shutdown_stops_on_unreadable_lease(self):
        udid = self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--boot", "--lease", "A")[0], 0)
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "B")[0], 0)
        b = [f for f in self.lease_files()
             if json.load(open(f)).get("owner") == "B"][0]
        write(b, "{broken")
        rc, d = self.js("release", "SVTEST a", "--lease", "A", "--shutdown")
        self.assertEqual((rc, d["kind"]), (1, "lease-unreadable"))
        self.assertEqual(self.state(udid), "Booted")
        self.assertEqual(len(self.lease_files()), 2)          # A's lease kept too
        rc, d = self.js("release", "SVTEST a", "--lease", "A")  # plain release still works
        self.assertEqual((rc, d["action"]), (0, "released"))

    def test_gc_rechecks_pairing_under_lock(self):
        self.parked("SVTEST a")
        tar_partial = os.path.join(self.vault, "SVTEST a.tar.partial")
        json_partial = os.path.join(self.vault, "SVTEST a.json.partial")
        for p in (tar_partial, json_partial):
            write(p, "{}")
            os.utime(p, (time.time() - 7200, time.time() - 7200))
        # Between gc's first look and its lock, the tar .partial is renamed into place:
        # the JSON .partial is now the metadata that may pair with it.
        code = (f"import os; os.replace({tar_partial!r}, "
                f"{os.path.join(self.vault, 'SVTEST a.tar')!r})")
        rc, d = self.js("gc", "--name-prefix", "SVTEST",
                        SV_TEST_GC_HOOK=json.dumps([PY, "-c", code]))
        self.assertEqual(rc, 0, d)
        self.assertTrue(os.path.exists(json_partial))
        self.assertNotIn(json_partial, [x["what"] for x in d["deleted"]])

    def test_existing_with_orphan_tar_needs_replace(self):
        self.orphan_tar()
        self.create("SVTEST a")
        rc, d = self.js("ensure", "SVTEST a")
        self.assertEqual((rc, d["action"], d["needsReplaceOnFirstPark"]), (0, "existing", True))

    # -- review round 2 (refuter F5, F6) -------------------------------------------------
    def foreign_interrupted(self, runtime_a):
        """Archive of device A; then a different device B parked with --replace, killed
        between the renames: B's tar is in place, A's metadata still names A."""
        a = self.create("SVTEST a", runtime=runtime_a)
        self.assertEqual(self.sv("park", "SVTEST a").returncode, 0)
        self.xcrun("delete", self.sv("restore", "SVTEST a").stdout.strip())
        b = self.create("SVTEST a")
        r = self.sv("park", "SVTEST a", "--replace", SV_TEST_EXIT_AFTER_TAR_REPLACE=1)
        self.assertEqual(r.returncode, 137, r.stderr)
        self.assertEqual(self.meta("SVTEST a")["udid"], a)
        return b

    def test_f5_unsettled_pair_stops_ensure(self):
        b = self.foreign_interrupted(RT)
        rc, d = self.js("ensure", "SVTEST a", SV_TEST_NOCACHE_FAIL=1)
        self.assertEqual((rc, d["kind"]), (1, "pending-pair"))       # not 10 from old metadata
        rc, d = self.js("ensure", "SVTEST a")
        self.assertEqual((rc, d["action"], d["udid"]), (0, "existing", b))

    def test_f5_unsettled_pair_does_not_create(self):
        self.set_runtimes([RT_OLD, RT])
        b = self.foreign_interrupted(RT_OLD)
        self.xcrun("delete", b)
        self.set_runtimes([RT])                  # old metadata's runtime gone: would be 4
        rc, d = self.js("ensure", "SVTEST a", "--create", "iPhone 18 Pro Max",
                        SV_TEST_NOCACHE_FAIL=1)
        self.assertEqual((rc, d["kind"]), (1, "pending-pair"))
        self.assertEqual(self.devices(), [])

    def test_f6_gc_keeps_partial_when_vault_listing_fails(self):
        new, partial = self.killed_between_renames()
        os.utime(partial, (time.time() - 7200, time.time() - 7200))
        rc, d = self.js("gc", "--name-prefix", "SVTEST", SV_TEST_LISTDIR_FAIL_FROM=2)
        self.assertEqual((rc, d["deleted"]), (0, []))
        self.assertTrue(os.path.exists(partial))
        self.assertIn(partial, [a["what"] for a in d["attention"]])

    def test_f6_park_stops_when_vault_listing_fails(self):
        new, partial = self.killed_between_renames()
        old, sha = self.meta("SVTEST a"), self.tar_sha("SVTEST a")
        rc, d = self.js("park", "SVTEST a", SV_TEST_LISTDIR_FAIL_FROM=1)
        self.assertEqual((rc, d["kind"]), (1, "pending-pair"))
        self.assertEqual((self.meta("SVTEST a"), self.tar_sha("SVTEST a")), (old, sha))
        self.assertEqual(self.partials("json"), [partial])
        self.assertEqual([x["UDID"] for x in self.devices("SVTEST a")], [new])

    # -- final round ----------------------------------------------------------------------
    def test_tar_stat_eio_is_unknown_not_absent(self):
        new, partial = self.killed_between_renames()
        os.utime(partial, (time.time() - 7200, time.time() - 7200))
        rc, d = self.js("gc", "--name-prefix", "SVTEST", SV_TEST_TAR_STAT_EIO=1)
        self.assertEqual((rc, d["deleted"]), (0, []))
        self.assertTrue(os.path.exists(partial))
        self.assertIn(partial, [a["what"] for a in d["attention"]])
        old, sha = self.meta("SVTEST a"), self.tar_sha("SVTEST a")
        rc, d = self.js("park", "SVTEST a", SV_TEST_TAR_STAT_EIO=1)
        self.assertEqual((rc, d["kind"]), (1, "pending-pair"))
        self.assertEqual((self.meta("SVTEST a"), self.tar_sha("SVTEST a")), (old, sha))
        self.assertEqual(self.partials("json"), [partial])
        self.assertEqual([x["UDID"] for x in self.devices("SVTEST a")], [new])

    def test_busy_by_lease_next_says_when_release_is_ok(self):
        self.create("SVTEST a")
        self.assertEqual(self.js("ensure", "SVTEST a", "--lease", "A")[0], 0)
        for args in (("park", "SVTEST a"), ("shutdown", "SVTEST a"),
                     ("ensure", "SVTEST a", "--lease", "B", "--deny-other-leases")):
            rc, d = self.js(*args)
            self.assertEqual(rc, 8, args)
            self.assertIn("worktree no longer exists or that session has ended", d["next"])


if __name__ == "__main__":
    unittest.main()
