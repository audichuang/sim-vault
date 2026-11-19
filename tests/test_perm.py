"""Vault directory and archive file permission tests."""
import json
import os
import stat
import time

from test_sim_vault import SimVaultCase


class VaultPermissionTests(SimVaultCase):
    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def current_umask(self):
        value = os.umask(0)
        os.umask(value)
        return value

    def test_park_creates_private_vault_dir_and_archive_files(self):
        name = "SVTEST perm"
        self.assertFalse(os.path.exists(self.vault))
        self.parked(name)
        self.assertEqual(self.mode(self.vault), 0o700)
        self.assertEqual(self.mode(os.path.join(self.vault, f"{name}.tar")), 0o600)
        self.assertEqual(self.mode(os.path.join(self.vault, f"{name}.json")), 0o600)

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

    def test_restore_works_on_0600_archive_and_keeps_0600_metadata(self):
        name = "SVTEST restore"
        old_udid = self.parked(name)
        tar_path = os.path.join(self.vault, f"{name}.tar")
        meta_path = os.path.join(self.vault, f"{name}.json")
        self.assertEqual(self.mode(self.vault), 0o700)
        self.assertEqual(self.mode(tar_path), 0o600)
        self.assertEqual(self.mode(meta_path), 0o600)

        r = self.sv("restore", name)
        self.assertEqual(r.returncode, 0, r.stderr)
        new_udid = r.stdout.strip()
        self.assertTrue(new_udid)
        self.assertNotEqual(new_udid, old_udid)
        self.assertEqual([d["UDID"] for d in self.devices(name)], [new_udid])
        self.assertEqual(self.meta(name)["restoredAs"], new_udid)
        self.assertEqual(self.mode(tar_path), 0o600)
        self.assertEqual(self.mode(meta_path), 0o600)
        self.assert_no_partials()

    def test_non_vault_state_files_keep_current_modes(self):
        umask = self.current_umask()
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
        self.assertEqual(self.mode(lease_subdirs[0]), 0o777 & ~umask)
        self.assertEqual(self.mode(lease_files[0]), 0o666 & ~umask)

        r = self.sv("release", name, "--lease", "owner-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.sv("park", name)
        self.assertEqual(r.returncode, 0, r.stderr)

        lock_files = [os.path.join(locks_dir, f) for f in os.listdir(locks_dir)]
        record_files = [os.path.join(records_dir, f) for f in os.listdir(records_dir)]
        self.assertEqual(len(lock_files), 1)
        self.assertEqual(len(record_files), 1)

        self.assertEqual(self.mode(locks_dir), 0o777 & ~umask)
        self.assertEqual(self.mode(records_dir), 0o777 & ~umask)
        self.assertEqual(self.mode(lock_files[0]), 0o644 & ~umask)
        self.assertEqual(self.mode(record_files[0]), 0o666 & ~umask)
        self.assertEqual(self.mode(history_path), 0o644 & ~umask)
