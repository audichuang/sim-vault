"""Held lease tests through the CLI, using fake xcrun and real process locks."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import sys
import textwrap
import time

from test_sim_vault import PY, SimVaultCase


NAME = "SVTEST held"
OWNER = "runner:held"
STAMP = "2026-01-01T00:00:00+00:00"
KEY = "SVTEST_held-" + hashlib.sha1(NAME.encode()).hexdigest()[:8]
OWNER_KEY = hashlib.sha1(OWNER.encode()).hexdigest()


class HeldLeaseTests(SimVaultCase):
    def setUp(self):
        super().setUp()
        self.directory = (Path(self.home) / "Library/Developer/CoreSimulator/sim-vault"
                          / "leases" / KEY)
        self.lease_file = self.directory / (OWNER_KEY + ".json")
        self.lock_file = self.lease_file.with_suffix(".lock")
        self.boot_record = self.directory / "_boot.json"

    def js(self, *args):
        proc = self.sv(*args, "--json")
        return proc.returncode, json.loads(proc.stdout)

    def helper_loader(self, snippet):
        path = Path(self.base) / "held-helper.py"
        setup = Path(self.loader).read_text().removesuffix("mod.main()\n")
        path.write_text(setup + textwrap.dedent(snippet))
        return path

    @contextmanager
    def holder(self, udid, mode="exclusive", *, metadata=True):
        self.directory.mkdir(parents=True, exist_ok=True)
        script = """
import fcntl, sys
with open(sys.argv[1], "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    print("ready", flush=True)
    sys.stdin.read()
"""
        with subprocess.Popen([sys.executable, "-c", script, str(self.lock_file)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
            try:
                self.assertTrue(select.select([child.stdout], [], [], 10)[0],
                                "holder did not signal readiness")
                self.assertEqual(child.stdout.readline().strip(), "ready")
                if metadata:
                    self.lease_file.write_text(json.dumps({
                        "owner": OWNER, "udid": udid, "mode": mode, "pid": child.pid,
                        "createdAt": STAMP, "updatedAt": STAMP, "kind": "held",
                    }))
                yield child
            finally:
                child.stdin.close()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()

    def assert_busy(self, args, child, *, orphan=False):
        rc, result = self.js(*args)
        self.assertEqual((rc, result.get("kind")), (8, "busy"), result)
        self.assertIn("held lease", result["message"])
        self.assertIn(OWNER_KEY[:8] if orphan else OWNER, result["message"])
        self.assertIn("inherited", result["message"])
        self.assertIn(f"lsof {shlex.quote(str(self.lock_file))}", result["next"])
        self.assertNotIn(f"kill {child.pid}", result["next"])
        self.assertIn("once", result["next"])
        self.assertIn("exits", result["next"])

    def assert_no_shutdown(self):
        calls = [json.loads(line) for line in (Path(self.fake) / "log").read_text().splitlines()]
        self.assertFalse([call for call in calls if call[:2] == ["simctl", "shutdown"]])

    def test_live_held_lease_blocks_other_users(self):
        udid = self.create(NAME)
        self.assertEqual(self.js("ensure", NAME, "--boot", "--lease", "manual")[0], 0)
        with self.holder(udid) as child:
            for args in (("park", NAME, "--shutdown"), ("shutdown", NAME),
                         ("ensure", NAME, "--lease", "other")):
                with self.subTest(args=args):
                    self.assert_busy(args, child)
            self.assert_no_shutdown()
        # Deny-other-leases also blocks a shared held lease.
        with self.holder(udid, mode="shared") as child:
            self.assert_busy(("ensure", NAME, "--lease", "other", "--deny-other-leases"), child)

    def test_same_owner_cannot_release_or_overwrite_live_held_lease(self):
        udid = self.create(NAME)
        with self.holder(udid, mode="shared") as child:
            original = self.lease_file.read_bytes()
            for args in (("release", NAME, "--lease", OWNER), ("ensure", NAME, "--lease", OWNER)):
                with self.subTest(args=args):
                    self.assert_busy(args, child)
                    self.assertEqual(self.lease_file.read_bytes(), original)
                    self.assertTrue(self.lock_file.is_file())

    def test_lock_without_json_still_blocks(self):
        udid = self.create(NAME, state="Booted")
        with self.holder(udid) as child:
            self.lease_file.unlink()  # Simulate a v3 release.
            for args in (("shutdown", NAME), ("park", NAME, "--shutdown"),
                         ("ensure", NAME, "--lease", OWNER),
                         ("release", NAME, "--lease", OWNER)):
                with self.subTest(args=args):
                    self.assert_busy(args, child, orphan=True)
            self.assert_no_shutdown()
            self.assertTrue(self.lock_file.is_file())

    def test_live_held_lease_survives_device_disappearance(self):
        udid = self.create(NAME)
        with self.holder(udid) as child:
            self.xcrun("delete", udid)
            lease, = self.js("status", NAME)[1]["leases"]
            self.assertEqual((lease.get("alive"), lease["stale"]), (True, False))
            self.assert_busy(("ensure", NAME, "--create", "iPhone 18 Pro Max"), child)
            self.assertTrue(self.lease_file.exists())
            self.assertEqual(self.devices(NAME), [])

    def test_busy_release_keeps_other_lease_files(self):
        udid = self.create(NAME)
        other = self.directory / (hashlib.sha1(b"dead").hexdigest() + ".json")
        with self.holder(udid):
            pass
        other.write_text(json.dumps(dict(json.loads(self.lease_file.read_text()), owner="dead")))
        other_lock = other.with_suffix(".lock")
        self.lock_file.rename(other_lock)
        with self.holder(udid) as child:
            before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
            self.assert_busy(("release", NAME, "--lease", OWNER), child)
            self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_ensure_reaps_dead_held_lease_and_clears_last_boot_record(self):
        udid = self.create(NAME)
        self.assertEqual(self.js("ensure", NAME, "--boot", "--lease", OWNER)[0], 0)
        with self.holder(udid):
            pass
        rc, result = self.js("ensure", NAME)
        self.assertEqual((rc, result.get("state")), (0, "Booted"), result)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assert_no_shutdown()

    def test_park_reaps_dead_held_lease_without_shutting_down(self):
        udid = self.create(NAME, state="Booted")
        with self.holder(udid):
            pass
        rc, result = self.js("park", NAME)
        self.assertEqual(rc, 6, result)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assert_no_shutdown()

    def test_restore_reaps_dead_held_lease_and_transfers_boot_record(self):
        udid = self.create(NAME)
        self.assertEqual(self.js("ensure", NAME, "--boot", "--lease", OWNER)[0], 0)
        with self.holder(udid, mode="shared"):
            self.assertEqual(self.js("ensure", NAME, "--lease", "remaining")[0], 0)
        self.assertEqual(self.js("restore", NAME)[0], 0)
        self.assertFalse(self.lease_file.exists())
        self.assertFalse(self.lock_file.exists())
        self.assertEqual(json.loads(self.boot_record.read_text())["by"], ["remaining"])
        self.assert_no_shutdown()

    def test_status_preserves_old_fields_and_reports_liveness(self):
        udid = self.create(NAME)
        baseline = self.js("status", NAME)[1]
        with self.holder(udid) as child:
            rc, live = self.js("status", NAME)
            self.assertEqual(rc, 0, live)
            lease, = live.pop("leases")
            self.assertEqual(live, {k: v for k, v in baseline.items() if k != "leases"})
            expected = {"owner": OWNER, "udid": udid, "mode": "exclusive", "pid": child.pid,
                        "createdAt": STAMP, "updatedAt": STAMP, "stale": False}
            self.assertEqual({k: lease[k] for k in expected}, expected)
            self.assertEqual((lease.get("kind"), lease.get("alive"), lease.get("overdue")),
                             ("held", True, False))
            self.assertGreaterEqual(lease["ageSeconds"], 0)
            self.assertEqual(lease["holderPids"], [])
        dead, = self.js("status", NAME)[1]["leases"]
        self.assertEqual((dead.get("alive"), dead.get("stale"), dead.get("holderPids")),
                         (False, True, []))
        self.assertTrue(self.lease_file.is_file())
        self.assertTrue(self.lock_file.is_file())
        plain = self.sv("status", NAME)
        self.assertEqual(plain.stdout.strip(), udid)
        self.assertIn("held", plain.stderr)
        self.assertIn("stale", plain.stderr)

    def test_status_probe_leaves_live_holder_untouched(self):
        udid = self.create(NAME)
        with self.holder(udid) as child:
            with self.lock_file.open("rb") as probe:
                for _ in range(2):
                    rc, result = self.js("status", NAME)
                    self.assertEqual(rc, 0, result)
                    self.assertIsNone(child.poll())
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    self.assertIs(result["leases"][0].get("alive"), True)

    def test_gc_reports_lock_without_json_and_reaps_only_under_device_lock(self):
        udid = self.create(NAME)
        args = ("gc", "--name-prefix", NAME)
        with self.holder(udid, metadata=False):
            rc, result = self.js(*args)
            self.assertEqual(rc, 0, result)
            self.assertIn(f"held lease {OWNER_KEY[:8]} held by", json.dumps(result["attention"]))
            self.assertTrue(self.lock_file.exists())
        self.assertEqual(self.js(*args, "--dry-run")[0], 0)
        self.assertTrue(self.lock_file.exists())
        lock_path = self.directory.parent.parent / "locks" / (KEY + ".lock")
        lock_path.parent.mkdir(exist_ok=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.js(*args)[0], 0)
            self.assertTrue(self.lock_file.exists())
        self.assertEqual(self.js(*args)[0], 0)
        self.assertFalse(self.lock_file.exists())
        self.assert_no_shutdown()

    def test_legacy_lease_stays_live_when_overdue(self):
        udid = self.create(NAME)
        self.assertEqual(self.js("ensure", NAME, "--boot", "--lease", OWNER)[0], 0)
        legacy = json.loads(self.lease_file.read_text())
        self.assertNotIn("kind", legacy)
        legacy.update(createdAt=STAMP, updatedAt=STAMP)
        self.lease_file.write_text(json.dumps(legacy))
        old = time.time() - 5 * 3600
        os.utime(self.lease_file, (old, old))
        self.assertEqual(self.js("gc", "--leases-older-than", "4h")[0], 0)
        self.assertEqual(self.js("park", NAME)[0], 8)
        status, = self.js("status", NAME)[1]["leases"]
        self.assertEqual({k: status[k] for k in legacy}, legacy)
        self.assertEqual((status.get("kind"), status.get("alive"), status.get("overdue"),
                          status["stale"]), ("legacy", True, True, False))
        self.assertGreater(status["ageSeconds"], 4 * 3600)
        rc, result = self.js("release", NAME, "--lease", OWNER, "--shutdown")
        self.assertEqual((rc, result["shutdown"]), (0, True))
        self.assertEqual(self.state(udid), "Shutdown")

    def test_internal_writer_locks_before_publication_and_keeps_legacy(self):
        udid = self.create(NAME)
        self.assertEqual(self.js("ensure", NAME, "--lease", "legacy")[0], 0)
        loader = Path(self.loader)
        setup = loader.read_text().removesuffix("mod.main()\n")
        verify = textwrap.dedent("""
                import fcntl
                name, owner, udid = sys.argv[1:]
                legacy = mod.lease_path(name, "legacy")
                with open(legacy, "rb") as source:
                    previous = source.read()
                original_write = mod.write_json
                def check_locked(path, data):
                    if path == mod.lease_path(name, owner):
                        with open(path.removesuffix(".json") + ".lock") as lock:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                            except BlockingIOError:
                                pass
                            else:
                                raise AssertionError("JSON published before locking")
                    original_write(path, data)
                mod.write_json = check_locked
                with mod.DeviceLock(name):
                    try:
                        mod.write_held_lease(name, "legacy", udid, "shared")
                    except mod.Fail as exc:
                        assert (exc.code, exc.kind) == (8, "busy")
                    else:
                        raise AssertionError("legacy lease was overwritten")
                    with open(legacy, "rb") as source:
                        assert source.read() == previous
                    fd = mod.write_held_lease(name, owner, udid, "exclusive")
                    assert fd not in mod.HELD_FDS
                print("ready", flush=True)
                sys.stdin.read()
                os.close(fd)
            """)
        loader.write_text(setup + verify)
        with subprocess.Popen([PY, self.loader, NAME, OWNER, udid], env=self.env(),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as child:
            try:
                self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                ready = child.stdout.readline().strip()
                if ready != "ready":
                    self.fail(child.stderr.read())
                data = json.loads(self.lease_file.read_text())
                self.assertEqual((data["owner"], data["udid"], data["kind"], data["mode"]),
                                 (OWNER, udid, "held", "exclusive"))
                with self.lock_file.open("rb") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            finally:
                child.stdin.close()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()

    def test_help_describes_held_leases_and_keeps_wrapper_flags(self):
        self.assertIn("--deny-other-leases", self.sv("ensure", "--help").stdout)
        self.assertIn("--trim-posterboard", self.sv("park", "--help").stdout)
        self.assertIn("--busy-lock", self.sv("shutdown", "--help").stdout)
        self.assertIn("Held leases", self.sv("--help").stdout)

    def test_reap_never_transfers_boot_duty_across_rename_and_reuse(self):
        original = self.create(NAME)
        self.assertEqual(self.js("ensure", NAME, "--boot", "--lease", OWNER)[0], 0)
        with self.holder(original, mode="shared"):
            self.assertEqual(self.js("ensure", NAME, "--lease", "original-app")[0], 0)
            self.xcrun("rename", original, NAME + " renamed")
            replacement = self.create(NAME)
            self.assertEqual(self.js("ensure", NAME, "--lease", "replacement-app")[0], 0)
        self.assertEqual(self.js("restore", NAME)[0], 0)
        boot = json.loads(self.boot_record.read_text())
        self.assertEqual((boot["udid"], boot["by"]), (original, ["original-app"]))
        for owner in ("original-app", "replacement-app"):
            rc, result = self.js("release", NAME, "--lease", owner, "--shutdown")
            self.assertEqual((rc, result.get("shutdown")), (0, False), result)
        self.assertEqual((self.state(original), self.state(replacement)), ("Booted", "Shutdown"))
        self.assert_no_shutdown()

    def test_writer_can_publish_while_passive_probe_holds_shared_lock(self):
        udid = self.create(NAME)
        loader = self.helper_loader("""
            import fcntl
            name, owner, udid, preexisting = sys.argv[1:]
            path = mod.held_lock_path(mod.lease_path(name, owner))
            original_flock = fcntl.flock
            probe = None
            def concurrent_probe(fd, operation):
                global probe
                if operation & fcntl.LOCK_EX and probe is None and os.path.exists(path):
                    probe = open(path, "rb")
                    original_flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return original_flock(fd, operation)
            with mod.DeviceLock(name):
                if preexisting == "yes":
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    open(path, "a").close()
                    probe = open(path, "rb")
                    original_flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock = concurrent_probe
                fd = mod.write_held_lease(name, owner, udid, "exclusive")
                try:
                    assert os.fstat(fd).st_ino == os.stat(path).st_ino
                    if probe is not None:
                        assert os.fstat(probe.fileno()).st_ino != os.stat(path).st_ino
                finally:
                    os.close(fd)
                    if probe is not None:
                        probe.close()
            """)
        for preexisting in ("no", "yes"):
            with self.subTest(preexisting=preexisting):
                result = subprocess.run([PY, loader, NAME, OWNER, udid, preexisting],
                                        env=self.env(), capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(set(self.directory.iterdir()), {self.lease_file, self.lock_file})
                self.lease_file.unlink()
                self.lock_file.unlink()

    def test_diagnostics_verify_recorded_process_and_ignore_open_file_observers(self):
        udid = self.create(NAME)
        # Model lsof returning the process that opened the file without flock. The test
        # process keeps that fd open while the writer child holds the actual lock.
        lsof = Path(self.stub) / "lsof"
        invoked = Path(self.fake) / "lsof-invoked"
        lsof.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(invoked))}\n"
                        f"printf '%s\\n' p{os.getpid()} cobserver "
                        f"{shlex.quote('n' + str(self.lock_file.resolve()))}\n")
        lsof.chmod(0o755)
        loader = self.helper_loader("""
            name, owner, udid = sys.argv[1:]
            with mod.DeviceLock(name):
                fd = mod.write_held_lease(name, owner, udid, "exclusive")
            print("ready", flush=True)
            sys.stdin.read()
            os.close(fd)
            """)
        with subprocess.Popen([PY, loader, NAME, OWNER, udid], env=self.env(),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
            try:
                self.assertTrue(select.select([child.stdout], [], [], 20)[0])
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.lock_file.open("rb"):
                    lease, = self.js("status", NAME)[1]["leases"]
                    self.assertEqual(lease["holderPids"], [child.pid])
                    rc, result = self.js("shutdown", NAME)
                    self.assertEqual((rc, result["kind"]), (8, "busy"))
                    self.assertRegex(result["message"], rf"pid {child.pid} \([^)]+\)")
                    self.assertIn(f"kill {child.pid}", result["next"])
                    for start_time in (1, None):
                        with self.subTest(pidStartedAt=start_time):
                            record = json.loads(self.lease_file.read_text())
                            record["pidStartedAt"] = start_time
                            self.lease_file.write_text(json.dumps(record))
                            self.assert_busy(("shutdown", NAME), child)
                            lease, = self.js("status", NAME)[1]["leases"]
                            self.assertEqual(lease["holderPids"], [])
                    self.assertFalse(invoked.exists())
            finally:
                child.stdin.close()
                child.wait(timeout=20)

    def test_gc_prefix_uses_real_name_for_deletion(self):
        udid = self.create(NAME)
        with self.holder(udid):
            pass
        # Both prefixes sanitize to the same key prefix, but only one matches NAME.
        rc, result = self.js("gc", "--name-prefix", "SVTEST_held")
        self.assertEqual(rc, 0, result)
        self.assertEqual(result["deleted"], [])
        self.assertTrue(self.lease_file.exists())
        self.assertTrue(self.lock_file.exists())
        self.xcrun("delete", udid)
        rc, result = self.js("gc", "--name-prefix", NAME)
        self.assertEqual(rc, 0, result)
        self.assertTrue(self.lease_file.exists(), "key-only match is for listing")
        data = json.loads(self.lease_file.read_text())
        self.lease_file.write_text(json.dumps(dict(data, name=NAME)))
        self.assertEqual(self.js("gc", "--name-prefix", NAME)[0], 0)
        self.assertFalse(self.lease_file.exists(), "JSON supplies the real name")
        self.assertFalse(self.lock_file.exists())

    def test_release_shutdown_releases_own_lease_with_other_held_lease(self):
        udid = self.create(NAME)
        self.assertEqual(self.js("ensure", NAME, "--boot", "--lease", "manual")[0], 0)
        path = self.directory / (hashlib.sha1(b"manual").hexdigest() + ".json")
        with self.holder(udid):
            rc, result = self.js("release", NAME, "--lease", "manual", "--shutdown")
            self.assertEqual((rc, result.get("action"), result.get("shutdown")),
                             (0, "released", False), result)
            self.assertFalse(path.exists())
            self.assertTrue(self.lease_file.exists())
            self.assertEqual(json.loads(self.boot_record.read_text())["by"], [OWNER])
            self.assertEqual(self.state(udid), "Booted")
        self.assert_no_shutdown()

    def test_gc_reaps_absent_devices_by_key_under_device_lock(self):
        udid = self.create(NAME)
        with self.holder(udid):
            pass
        self.xcrun("delete", udid)
        lock_path = self.directory.parent.parent / "locks" / (KEY + ".lock")
        lock_path.parent.mkdir(exist_ok=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.js("gc")[0], 0)
            self.assertTrue(self.lease_file.exists())
            self.assertTrue(self.lock_file.exists())
        rc, result = self.js("gc")
        self.assertEqual(rc, 0, result)
        self.assertFalse(self.lease_file.exists())
        self.assertFalse(self.lock_file.exists())

    def test_unreadable_held_lock_fails_decisions_and_survives_passive_scans(self):
        udid = self.create(NAME)
        self.assertEqual(self.js("ensure", NAME, "--boot", "--lease", "manual")[0], 0)
        with self.holder(udid, mode="shared"):
            pass
        self.lock_file.chmod(0)
        for args in (("park", NAME), ("shutdown", NAME),
                     ("release", NAME, "--lease", "manual", "--shutdown"),
                     ("ensure", NAME, "--lease", "other", "--exclusive"),
                     ("ensure", NAME, "--lease", "other", "--deny-other-leases")):
            with self.subTest(args=args):
                rc, result = self.js(*args)
                self.assertEqual((rc, result.get("kind")), (1, "lease-unreadable"), result)
        rc, result = self.js("status", NAME)
        self.assertEqual(rc, 0, result)
        lease, = (lease for lease in result["leases"] if lease["owner"] == OWNER)
        self.assertEqual((lease["alive"], lease["stale"], lease["holderPids"]), (True, False, []))
        self.assertIn("Permission denied", lease["unreadable"])
        rc, result = self.js("gc")
        self.assertEqual(rc, 0, result)
        self.assertIn("unreadable", json.dumps(result["attention"]))
        self.assertTrue(self.lease_file.exists())
        self.assertTrue(self.lock_file.exists())
        self.assert_no_shutdown()

    def test_status_shared_probe_ignores_other_shared_probe(self):
        udid = self.create(NAME)
        with self.holder(udid):
            pass
        with self.lock_file.open("rb") as probe:
            fcntl.flock(probe, fcntl.LOCK_SH)
            rc, result = self.js("status", NAME)
            self.assertEqual(rc, 0, result)
            lease, = result["leases"]
            self.assertEqual((lease["alive"], lease["stale"]), (False, True))
        self.assertTrue(self.lease_file.exists())

    def test_shared_held_lease_survives_gone_udid_in_ensure_and_release(self):
        udid = self.create(NAME)
        with self.holder(udid, mode="shared"):
            original = self.lease_file.read_bytes()
            self.xcrun("delete", udid)
            rc, result = self.js("ensure", NAME, "--create", "iPhone 18 Pro Max", "--lease", "other")
            self.assertEqual((rc, result.get("action")), (0, "created"), result)
            self.assertTrue(self.lease_file.exists())
            rc, result = self.js("release", NAME, "--lease", "other")
            self.assertEqual((rc, result.get("action")), (0, "released"), result)
            self.assertEqual(self.lease_file.read_bytes(), original)
            self.assertTrue(self.lock_file.exists())

    def test_reap_preserves_boot_record_if_legacy_lease_is_unreadable(self):
        udid = self.create(NAME)
        self.assertEqual(self.js("ensure", NAME, "--boot", "--lease", OWNER)[0], 0)
        with self.holder(udid):
            pass
        bad = self.directory / (hashlib.sha1(b"legacy").hexdigest() + ".json")
        bad.write_text("{broken")
        original = self.boot_record.read_bytes()
        self.assertEqual(self.js("restore", NAME)[0], 0)
        self.assertTrue(self.boot_record.is_file(), "unreadable owner may still need its record")
        self.assertEqual(self.boot_record.read_bytes(), original)
        self.assertFalse(self.lease_file.exists())
        self.assertFalse(self.lock_file.exists())

    def test_gc_reports_distinct_lock_paths_that_share_a_symlink_target(self):
        udid = self.create(NAME)
        self.directory.mkdir(parents=True)
        target = Path(self.base) / "external-lock"
        self.lock_file.symlink_to(target)
        alias = self.directory / "alias.lock"
        alias.symlink_to(target)
        with self.holder(udid):
            rc, result = self.js("gc")
            self.assertEqual(rc, 0, result)
            held = [entry["what"] for entry in result["attention"] if entry["kind"] == "held-lease"]
            self.assertEqual(len(held), 2, held)
            self.assertIn("held lease alias.lo", "\n".join(held))

    def test_status_reports_age_as_integer_seconds(self):
        udid = self.create(NAME)
        with self.holder(udid):
            rc, result = self.js("status", NAME)
            self.assertEqual(rc, 0, result)
            lease, = result["leases"]
            self.assertIsInstance(lease["ageSeconds"], int)
            self.assertRegex(self.sv("status", NAME).stderr, r"ageSeconds=\d+ overdue=")
