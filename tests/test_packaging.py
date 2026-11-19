"""--version and SIM_VAULT_HISTORY, on top of the fake-xcrun harness in test_sim_vault."""
import json
import os
import re
import unittest

from test_sim_vault import SimVaultCase


class TestVersion(SimVaultCase):
    def test_version_flag(self):
        proc = self.sv("--version")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "sim-vault 3.1.0")

    def test_version_flag_ignores_missing_subcommand(self):
        proc = self.sv("--version", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(re.fullmatch(r"sim-vault \d+\.\d+\.\d+\n", proc.stdout), proc.stdout)


class TestHistoryOverride(SimVaultCase):
    def default_history(self):
        return os.path.join(self.home, "Library/Developer/CoreSimulator/sim-vault/history.jsonl")

    def test_override_path_receives_the_line(self):
        target = os.path.join(self.base, "elsewhere", "deep", "h.jsonl")
        proc = self.sv("shutdown", "SVTEST none", "--json", SIM_VAULT_HISTORY=target)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(target) as h:
            lines = [json.loads(line) for line in h]
        self.assertEqual([(r["cmd"], r["name"], r["rc"]) for r in lines],
                         [("shutdown", "SVTEST none", 0)])
        self.assertFalse(os.path.exists(self.default_history()))

    def test_bare_filename_writes_to_cwd(self):
        proc = self.sv("shutdown", "SVTEST none", "--json", SIM_VAULT_HISTORY="h.jsonl")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("warning", proc.stderr)
        # the subprocess inherits this process's cwd
        path = os.path.join(os.getcwd(), "h.jsonl")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with open(path) as h:
            self.assertEqual([json.loads(line)["cmd"] for line in h], ["shutdown"])

    def test_outer_variable_does_not_leak_into_tests(self):
        self.assertNotIn("SIM_VAULT_HISTORY", self.env())

    def test_unset_keeps_default_path(self):
        proc = self.sv("shutdown", "SVTEST none", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(self.default_history()))

    def test_help_mentions_variable(self):
        proc = self.sv("--help")
        self.assertIn("SIM_VAULT_HISTORY", proc.stdout)


if __name__ == "__main__":
    unittest.main()
