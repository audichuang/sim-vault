"""Vault directory and archive file permission tests."""
import json
import os
import stat
import textwrap
import time

from test_sim_vault import SimVaultCase


PERM_LOADER_HOOKS = textwrap.dedent("""\
    if os.environ.get("SV_PERM_DENY_CHMOD"):
        def _deny_chmod(*a, **k):
            raise PermissionError(1, "Operation not permitted")
        os.chmod = _deny_chmod
        os.fchmod = _deny_chmod
    if os.environ.get("SV_PERM_OPEN_LOG"):
        import builtins as _builtins, json as _json, stat as _stat
        _log_path = os.environ["SV_PERM_OPEN_LOG"]
        _vault_real = os.path.realpath(os.environ["SIM_VAULT_DIR"])
        _real_os_open = os.open
        _real_builtin_open = _builtins.open
        _real_makedirs = os.makedirs
        _real_mkdir = os.mkdir
        def _in_vault(p):
            if not isinstance(p, (str, bytes, os.PathLike)):
                return False
            ap = os.path.abspath(os.fsdecode(p))
            return os.path.realpath(os.path.dirname(ap)) == _vault_real
        def _record(entry):
            with _real_builtin_open(_log_path, "a") as _h:
                _h.write(_json.dumps(entry) + "\\n")
        def _logged_os_open(path, flags, *args, **kwargs):
            fd = _real_os_open(path, flags, *args, **kwargs)
            if _in_vault(path) and (flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR)):
                st = os.fstat(fd)
                _record({"via": "os.open", "path": os.fsdecode(path), "flags": flags,
                         "mode": _stat.S_IMODE(st.st_mode), "size": st.st_size})
            return fd
        def _logged_builtin_open(file, mode="r", *args, **kwargs):
            handle = _real_builtin_open(file, mode, *args, **kwargs)
            if _in_vault(file) and any(c in mode for c in "wax+"):
                st = os.fstat(handle.fileno())
                _record({"via": "open", "path": os.fsdecode(file), "openMode": mode,
                         "mode": _stat.S_IMODE(st.st_mode), "size": st.st_size})
            return handle
        def _logged_makedirs(name, mode=0o777, exist_ok=False):
            existed = os.path.exists(name)
            res = _real_makedirs(name, mode=mode, exist_ok=exist_ok)
            if not existed and os.path.realpath(os.fsdecode(name)) == _vault_real:
                _record({"via": "makedirs", "path": os.fsdecode(name),
                         "mode": _stat.S_IMODE(os.stat(name).st_mode)})
            return res
        def _logged_mkdir(path, mode=0o777, *args, **kwargs):
            res = _real_mkdir(path, mode, *args, **kwargs)
            if os.path.realpath(os.fsdecode(path)) == _vault_real:
                _record({"via": "mkdir", "path": os.fsdecode(path),
                         "mode": _stat.S_IMODE(os.stat(path).st_mode)})
            return res
        os.open = _logged_os_open
        _builtins.open = _logged_builtin_open
        os.makedirs = _logged_makedirs
        os.mkdir = _logged_mkdir
    if os.environ.get("SV_PERM_PLANT_SYMLINK"):
        _link, _target = os.environ["SV_PERM_PLANT_SYMLINK"].split("::", 1)
        _real_write_json = mod.write_json
        def _plant_then_write(path, data, *args, **kwargs):
            if os.path.realpath(os.path.dirname(os.path.abspath(path))) == os.path.realpath(os.environ["SIM_VAULT_DIR"]):
                if not os.path.lexists(_link):
                    os.symlink(_target, _link)
            return _real_write_json(path, data, *args, **kwargs)
        mod.write_json = _plant_then_write
    if os.environ.get("SV_PERM_FIXED_TOKEN"):
        _tok = os.environ["SV_PERM_FIXED_TOKEN"]
        mod.partial_token = lambda: _tok
    if os.environ.get("SV_PERM_FAIL_META_REPLACE"):
        _real_replace = mod.os.replace
        def _fail_meta_replace(src, dst):
            if dst.endswith(".json") and os.path.realpath(os.path.dirname(os.path.abspath(dst))) == os.path.realpath(os.environ["SIM_VAULT_DIR"]):
                raise OSError(5, "simulated I/O error", dst)
            return _real_replace(src, dst)
        mod.os.replace = _fail_meta_replace
""")


class VaultPermissionTests(SimVaultCase):
    def setUp(self):
        super().setUp()
        with open(self.loader) as handle:
            code = handle.read()
        # Force child umask 022 so tests remain strict even if the runner's umask is 077.
        code = "import os\nos.umask(0o022)\n" + code
        code = code.replace("mod.main()\n", PERM_LOADER_HOOKS + "mod.main()\n")
        with open(self.loader, "w") as handle:
            handle.write(code)

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def read_open_log(self, log_path):
        if not os.path.exists(log_path):
            return []
        with open(log_path) as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_park_creates_private_vault_dir_and_archive_files_at_open_time(self):
        name = "SVTEST perm"
        self.assertFalse(os.path.exists(self.vault))
        self.create(name)
        log_path = os.path.join(self.base, "park-open.log")

        r = self.sv("park", name, SV_PERM_OPEN_LOG=log_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mode(self.vault), 0o700)
        self.assertEqual(self.mode(os.path.join(self.vault, f"{name}.tar")), 0o600)
        self.assertEqual(self.mode(os.path.join(self.vault, f"{name}.json")), 0o600)

        events = self.read_open_log(log_path)
        dir_events = [e for e in events if e["via"] in ("makedirs", "mkdir")]
        file_events = [e for e in events if e["via"] in ("os.open", "open")]
        self.assertTrue(dir_events, f"expected vault dir creation event, got {events}")
        for ev in dir_events:
            self.assertEqual(ev["mode"], 0o700, ev)
        self.assertEqual(len(file_events), 2, f"expected tar + json temp opens, got {file_events}")
        for ev in file_events:
            self.assertEqual(ev["via"], "os.open", ev)
            self.assertEqual(ev["mode"], 0o600, ev)
            self.assertEqual(ev["size"], 0, ev)
            self.assertTrue(ev["flags"] & os.O_CREAT, ev)
            self.assertTrue(ev["flags"] & os.O_EXCL, ev)
            if hasattr(os, "O_NOFOLLOW"):
                self.assertTrue(ev["flags"] & os.O_NOFOLLOW, ev)

    def test_partial_files_are_0600_while_park_is_in_progress(self):
        name = "SVTEST partial"
        self.create(name)
        proc = self.spawn("park", name, FAKE_TAR_SLEEP="1.5")
        try:
            deadline = time.monotonic() + 10
            tar_partials = []
            while time.monotonic() < deadline:
                tar_partials = self.partials("tar", name)
                if tar_partials or proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertEqual(len(tar_partials), 1, f"expected 1 tar partial, got {tar_partials}")
            self.assertIsNone(proc.poll(), "park finished before partial inspection")
            self.assertEqual(self.mode(tar_partials[0]), 0o600)
            self.assertEqual(self.mode(self.vault), 0o700)
            _, stderr = proc.communicate(timeout=30)
            self.assertNotIn("Traceback", stderr, stderr)
            self.assertEqual(proc.returncode, 0, stderr)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assert_no_partials()
        self.assertEqual(self.mode(os.path.join(self.vault, f"{name}.tar")), 0o600)
        self.assertEqual(self.mode(os.path.join(self.vault, f"{name}.json")), 0o600)

        # Also inspect the metadata .partial file before it is renamed into place.
        meta_name = "SVTEST partial-meta"
        self.create(meta_name)
        r = self.sv("park", meta_name, SV_TEST_EXIT_AFTER_TAR_REPLACE="1")
        self.assertNotEqual(r.returncode, 0)
        meta_partials = self.partials("json", meta_name)
        self.assertEqual(len(meta_partials), 1, f"expected 1 meta partial, got {meta_partials}")
        self.assertEqual(self.mode(meta_partials[0]), 0o600)
        self.assertEqual(self.mode(os.path.join(self.vault, f"{meta_name}.tar")), 0o600)

    def test_existing_vault_and_archive_keep_their_modes(self):
        os.mkdir(self.vault, 0o755)
        os.chmod(self.vault, 0o755)
        existing_tar = os.path.join(self.vault, "SVTEST existing.tar")
        existing_json = os.path.join(self.vault, "SVTEST existing.json")
        with open(existing_tar, "wb") as handle:
            handle.write(b"legacy-tar")
        with open(existing_json, "w") as handle:
            json.dump({"name": "SVTEST existing", "udid": "OLD-UDID"}, handle)
        os.chmod(existing_tar, 0o644)
        os.chmod(existing_json, 0o644)

        self.parked("SVTEST fresh")

        self.assertEqual(self.mode(self.vault), 0o755)
        self.assertEqual(self.mode(existing_tar), 0o644)
        self.assertEqual(self.mode(existing_json), 0o644)
        self.assertEqual(self.mode(os.path.join(self.vault, "SVTEST fresh.tar")), 0o600)
        self.assertEqual(self.mode(os.path.join(self.vault, "SVTEST fresh.json")), 0o600)

    def test_restore_works_on_0600_archive_and_opens_unique_0600_temp_file(self):
        name = "SVTEST restore"
        old_udid = self.parked(name)
        tar_path = os.path.join(self.vault, f"{name}.tar")
        meta_path = os.path.join(self.vault, f"{name}.json")
        self.assertEqual(self.mode(self.vault), 0o700)
        self.assertEqual(self.mode(tar_path), 0o600)
        self.assertEqual(self.mode(meta_path), 0o600)

        log_path = os.path.join(self.base, "restore-open.log")
        r = self.sv("restore", name, SV_PERM_OPEN_LOG=log_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        new_udid = r.stdout.strip()
        self.assertTrue(new_udid)
        self.assertNotEqual(new_udid, old_udid)
        self.assertEqual([d["UDID"] for d in self.devices(name)], [new_udid])
        self.assertEqual(self.meta(name)["restoredAs"], new_udid)
        self.assertEqual(self.mode(tar_path), 0o600)
        self.assertEqual(self.mode(meta_path), 0o600)
        self.assert_no_partials()

        file_events = [e for e in self.read_open_log(log_path) if e["via"] in ("os.open", "open")]
        self.assertEqual(len(file_events), 1, file_events)
        ev = file_events[0]
        self.assertEqual(ev["via"], "os.open", ev)
        self.assertNotEqual(ev["path"], meta_path + ".partial", ev)
        self.assertEqual(ev["mode"], 0o600, ev)
        self.assertEqual(ev["size"], 0, ev)
        self.assertTrue(ev["flags"] & os.O_CREAT, ev)
        self.assertTrue(ev["flags"] & os.O_EXCL, ev)
        if hasattr(os, "O_NOFOLLOW"):
            self.assertTrue(ev["flags"] & os.O_NOFOLLOW, ev)

    def test_park_and_restore_succeed_when_chmod_and_fchmod_raise_permission_error(self):
        name = "SVTEST smb-no-chmod"
        self.create(name)
        r = self.sv("park", name, SV_PERM_DENY_CHMOD="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mode(self.vault), 0o700)
        self.assertEqual(self.mode(os.path.join(self.vault, f"{name}.tar")), 0o600)
        self.assertEqual(self.mode(os.path.join(self.vault, f"{name}.json")), 0o600)

        r = self.sv("restore", name, SV_PERM_DENY_CHMOD="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mode(os.path.join(self.vault, f"{name}.json")), 0o600)
        self.assert_no_partials()

    def test_restore_and_park_do_not_follow_partial_symlink_or_delete_unowned_files(self):
        outside = os.path.join(self.base, "outside-secret.txt")
        with open(outside, "w") as handle:
            handle.write("do-not-touch")
        os.chmod(outside, 0o644)

        # 1. Pre-create <name>.json.partial as a symlink before restore.
        name = "SVTEST symlink-pre"
        self.parked(name)
        fixed_partial = os.path.join(self.vault, f"{name}.json.partial")
        os.symlink(outside, fixed_partial)
        r = self.sv("restore", name)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(outside) as handle:
            self.assertEqual(handle.read(), "do-not-touch")
        self.assertEqual(self.mode(outside), 0o644)
        self.assertFalse(os.path.islink(os.path.join(self.vault, f"{name}.json")))

        # 2. Symlink at <name>.json.partial present when restore writes restoredAs.
        name2 = "SVTEST symlink-mid"
        self.parked(name2)
        fixed_partial2 = os.path.join(self.vault, f"{name2}.json.partial")
        r = self.sv("restore", name2, SV_PERM_PLANT_SYMLINK=f"{fixed_partial2}::{outside}")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(outside) as handle:
            self.assertEqual(handle.read(), "do-not-touch")
        self.assertEqual(self.mode(outside), 0o644)
        self.assertTrue(os.path.islink(fixed_partial2))
        meta_path2 = os.path.join(self.vault, f"{name2}.json")
        self.assertFalse(os.path.islink(meta_path2))
        self.assertEqual(self.mode(meta_path2), 0o600)
        self.assertEqual(self.meta(name2)["restoredAs"], r.stdout.strip())
        os.unlink(fixed_partial2)

        # 3. Exact temp-name collision with a symlink: O_EXCL refuses it and leaves it alone.
        name3 = "SVTEST symlink-excl"
        self.create(name3)
        colliding = os.path.join(self.vault, f"{name3}.tar.999-aaaaaa.partial")
        os.symlink(outside, colliding)
        r = self.sv("park", name3, SV_PERM_FIXED_TOKEN=".999-aaaaaa")
        self.assertNotEqual(r.returncode, 0)
        with open(outside) as handle:
            self.assertEqual(handle.read(), "do-not-touch")
        self.assertEqual(self.mode(outside), 0o644)
        self.assertTrue(os.path.islink(colliding), "unowned colliding symlink must not be removed")
        os.unlink(colliding)

        # 4. Failure after creating restore's temp file removes only the temp file it created.
        name4 = "SVTEST fail-cleanup"
        self.parked(name4)
        unrelated = os.path.join(self.vault, "SVTEST other.json.1-abcdef.partial")
        with open(unrelated, "w") as handle:
            handle.write("keep")
        r = self.sv("restore", name4, SV_PERM_FAIL_META_REPLACE="1")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(self.partials("json", name4), [])
        self.assertTrue(os.path.exists(unrelated))

    def test_non_vault_state_files_keep_current_modes(self):
        name = "SVTEST state"
        self.create(name)
        r = self.sv("ensure", name, "--lease", "owner-a")
        self.assertEqual(r.returncode, 0, r.stderr)

        state_root = os.path.join(self.home, "Library/Developer/CoreSimulator/sim-vault")
        locks_dir = os.path.join(state_root, "locks")
        leases_root = os.path.join(state_root, "leases")
        records_dir = os.path.join(state_root, "archives")
        history_path = os.path.join(state_root, "history.jsonl")

        lease_subdirs = [os.path.join(leases_root, d) for d in os.listdir(leases_root)]
        self.assertEqual(len(lease_subdirs), 1)
        lease_files = [os.path.join(lease_subdirs[0], f) for f in os.listdir(lease_subdirs[0])]
        self.assertEqual(len(lease_files), 1)
        self.assertEqual(self.mode(lease_subdirs[0]), 0o755)
        self.assertEqual(self.mode(lease_files[0]), 0o644)

        r = self.sv("release", name, "--lease", "owner-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.sv("park", name)
        self.assertEqual(r.returncode, 0, r.stderr)

        lock_files = [os.path.join(locks_dir, f) for f in os.listdir(locks_dir)]
        record_files = [os.path.join(records_dir, f) for f in os.listdir(records_dir)]
        self.assertEqual(len(lock_files), 1)
        self.assertEqual(len(record_files), 1)

        self.assertEqual(self.mode(locks_dir), 0o755)
        self.assertEqual(self.mode(records_dir), 0o755)
        self.assertEqual(self.mode(lock_files[0]), 0o644)
        self.assertEqual(self.mode(record_files[0]), 0o644)
        self.assertEqual(self.mode(history_path), 0o644)
