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
SCRIPT = os.path.join(HERE, "sim-vault")
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
        if st == "Shutdown":
            fail("Unable to shutdown device in current state: Shutdown", 149)
        if os.environ.get("FAKE_SLOW_SHUTDOWN"):
            d["fakeState"] = "Shutting Down"
            d["fakeAfter"] = {"lists": 2, "to": "Shutdown"}
            save(d)
        else:
            set_state(d, "Shutdown")
    elif sub == "bootstatus":
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
    if target:
        p = os.path.join(os.environ["HOME"], "Library/Developer/CoreSimulator/Devices", target)
        d = plistlib.load(open(os.path.join(p, "device.plist"), "rb"))
        d["fakeState"] = "Booted"
        plistlib.dump(d, open(os.path.join(p, "device.plist"), "wb"))
        open(os.path.join(p, "data", "boot-marker"), "a").write("x")
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
        self.hooks([{"cmd": "rename", "n": 1, "action": "sleep", "seconds": 30}])
        p = self.spawn("restore", "SVTEST a")
        self.wait_for(lambda: self.count("rename") >= 1)
        p.kill()
        p.communicate(timeout=30)
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


if __name__ == "__main__":
    unittest.main()
