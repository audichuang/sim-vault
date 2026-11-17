"""sim-vault tests against a fake `xcrun simctl` and a vault under /tmp.

Run: python3 -m unittest modern-ios-guidance/scripts/test_sim_vault.py (or pytest).
Nothing here touches real simulators: every run gets its own HOME under /tmp, the stub
directory first on PATH, and a loader that refuses to start unless `xcrun` resolves to
the stub. The loader also stubs the same-volume check (both /tmp and CoreSimulator live
on the Data volume here); the vault-unavailable tests run without that patch.
"""
import hashlib
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("SV_TEST_SCRIPT_UNDER_TEST") or os.path.join(HERE, "sim-vault")
RT = "com.apple.CoreSimulator.SimRuntime.iOS-27-2"
RT_OLD = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable

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
        env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "SIM_VAULT_DIR")}
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
        self.assertEqual(meta["toolVersion"], "2")
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
        self.wait_for(lambda: os.path.exists(os.path.join(self.vault, "SVTEST a.tar.partial")))
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
        self.wait_for(lambda: os.path.exists(os.path.join(self.vault, "SVTEST a.tar.partial")))
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
        rc, d = self.js("park", "SVTEST a", "--shutdown")
        self.assertEqual((rc, d["kind"]), (9, "posterboard-bloated"))
        self.assertEqual(self.state(udid), "Booted")         # not even shut down
        os.truncate(os.path.join(store, "big"), 900 * 1024 * 1024)
        rc, d = self.js("park", "SVTEST a", "--shutdown")
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
        self.wait_for(lambda: os.path.exists(os.path.join(self.vault, "SVTEST a.tar.partial")))
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
        self.wait_for(lambda: os.path.exists(os.path.join(self.vault, "SVTEST a.tar.partial")))
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
        self.wait_for(lambda: os.path.exists(os.path.join(self.vault, "SVTEST a.tar.partial")))
        p.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        p.send_signal(signal.SIGHUP)
        _o, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM, err)
        self.assert_no_partials()

    def test_r7_failed_cleanup_is_reported(self):
        self.create("SVTEST a")
        p = self.spawn("park", "SVTEST a", FAKE_TAR_SLEEP=30, SV_TEST_FAIL_REMOVE=1)
        self.wait_for(lambda: os.path.exists(os.path.join(self.vault, "SVTEST a.tar.partial")))
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
        self.assertEqual(sorted(set(self.inherited())), ["clone", "delete", "rename"])

    def test_r8_dead_holder_message(self):
        self.create("SVTEST a")
        p = self.spawn("park", "SVTEST a", FAKE_TAR_SLEEP=4)
        self.wait_for(lambda: os.path.exists(os.path.join(self.vault, "SVTEST a.tar.partial")))
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
        self.wait_for(lambda: os.path.exists(os.path.join(self.vault, "SVTEST a.tar.partial")))
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
        self.assertTrue(os.path.exists(os.path.join(self.vault, "SVTEST a.json.partial")))

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


if __name__ == "__main__":
    unittest.main()
