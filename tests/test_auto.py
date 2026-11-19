"""Manual session leases through the fake-xcrun CLI, with real anchor processes."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import subprocess
import textwrap

from test_sim_vault import HERE, PY, SimVaultCase


NAME = "SVTEST manual"
REPO = Path(HERE).parent


class SessionLeaseCase(SimVaultCase):
    def setUp(self):
        super().setUp()
        self.scope = Path(self.base) / "worktree"
        self.scope.mkdir()
        self.state_root = Path(self.home) / "Library/Developer/CoreSimulator/sim-vault"

    def set_anchor(self):
        child = subprocess.Popen(["/bin/sleep", "60"])
        self.addCleanup(self.stop_child, child)
        return child

    @staticmethod
    def stop_child(child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)

    def session(self, command, anchor, *args, scope=None, **env):
        return self.sv(command, NAME, "--lease", "auto", "--lease-scope",
                       str(self.scope if scope is None else scope), *args,
                       SIM_VAULT_ANCHOR_PID=anchor.pid, **env)

    def result(self, proc, rc=0):
        self.assertEqual(proc.returncode, rc, proc.stderr)
        return json.loads(proc.stdout)

    def acquire(self, anchor, *args, scope=None):
        proc = self.session("ensure", anchor, "--json", *args, scope=scope)
        result = self.result(proc)
        self.assertIn(f"lease: {result['lease']}", proc.stderr.splitlines())
        return result

    def whoami(self, anchor, scope=None):
        return self.result(self.sv("whoami", NAME, "--lease-scope",
                                   str(self.scope if scope is None else scope), "--json",
                                   SIM_VAULT_ANCHOR_PID=anchor.pid))

    def lease_file(self, token):
        filename = hashlib.sha1(token.encode()).hexdigest() + ".json"
        path, = self.state_root.glob(f"leases/*/{filename}")
        return path

    def helper_cli(self, snippet, *args, **env):
        """Use the guarded harness loader with an injected system observation."""
        loader = Path(self.base) / "auto-loader.py"
        setup, entry = Path(self.loader).read_text().rsplit("mod.main()\n", 1)
        loader.write_text(setup + textwrap.dedent(snippet) + "\nmod.main()\n" + entry)
        return subprocess.run([PY, str(loader), *args], env=self.env(**env),
                              capture_output=True, text=True, timeout=10)


class SessionLeaseTests(SessionLeaseCase):
    def test_ensure_keeps_stdout_for_the_udid_and_reports_session_token(self):
        udid = self.create(NAME)
        anchor = self.set_anchor()
        proc = self.session("ensure", anchor, "--lease-prefix", "project:up")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, udid + "\n")
        token_line, = [line for line in proc.stderr.splitlines() if line.startswith("lease: ")]
        token = token_line.removeprefix("lease: ")
        self.assertRegex(token, r"^project:up:[0-9a-f]{8}$")
        record = json.loads(self.lease_file(token).read_text())
        self.assertEqual((record["kind"], record["scope"]), ("session", str(self.scope.resolve())))
        self.assertEqual(record["anchor"]["pid"], anchor.pid)
        self.assertEqual(record["anchor"]["comm"], "sleep")
        self.assertEqual(record["anchor"]["foundBy"], "SIM_VAULT_ANCHOR_PID")
        self.assertEqual(record["anchor"]["startSource"], "proc_pidinfo")
        self.assertGreater(record["anchor"]["startedAt"], 0)

    def test_same_scope_blocks_both_same_and_resumed_anchor(self):
        self.create(NAME)
        first, second = self.set_anchor(), self.set_anchor()
        token = self.acquire(first)["lease"]
        self.assertRegex(token, r"^auto:[0-9a-f]{8}$")
        path = self.lease_file(token)
        original = path.read_bytes()
        for anchor in (first, second):
            with self.subTest(anchor=anchor.pid):
                busy = self.result(self.session("ensure", anchor, "--json"), 8)
                self.assertEqual(busy["kind"], "busy")
                self.assertIn(token, busy["message"])
                self.assertRegex(busy["message"], r"age \d+s")
                self.assertIn("anchor alive", busy["message"])
                self.assertIn("scope exists", busy["message"])
                self.assertEqual("before a resume" in busy["message"], anchor is second)
                self.assertEqual(shlex.split(busy["next"]),
                                 ["sim-vault", "release", NAME, "--lease", token])
                self.assertEqual(path.read_bytes(), original)
        explicit = self.result(self.sv("ensure", NAME, "--lease", token, "--json"), 8)
        self.assertEqual(explicit["kind"], "busy")
        self.assertEqual(path.read_bytes(), original)
        self.assertIsNone(self.whoami(second)["lease"])
        missing = self.result(self.session("release", second, "--shutdown", "--json"))
        self.assertEqual(missing["action"], "no-lease")
        self.assertTrue(path.exists())

    def test_distinct_scopes_and_last_release_shutdown(self):
        udid = self.create(NAME)
        anchor = self.set_anchor()
        first = self.acquire(anchor, "--boot")["lease"]
        second_scope = self.scope.with_name("other")
        second_scope.mkdir()
        self.assertIsNone(self.whoami(anchor, second_scope)["lease"])
        second = self.acquire(anchor, scope=second_scope)["lease"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.whoami(anchor)["lease"], first)
        self.assertEqual(self.whoami(anchor, second_scope)["lease"], second)
        for scope, token, shutdown in ((self.scope, first, False), (second_scope, second, True)):
            result = self.result(self.session("release", anchor, "--shutdown", "--json", scope=scope))
            self.assertEqual((result["action"], result["lease"], result["shutdown"]),
                             ("released", token, shutdown))
        self.assertEqual(self.state(udid), "Shutdown")
        self.assertEqual(self.result(self.session("release", anchor, "--json"))["action"], "no-lease")

    def test_symlink_resolves_to_same_scope(self):
        self.create(NAME)
        anchor = self.set_anchor()
        alias = self.scope.with_name("alias")
        alias.symlink_to(self.scope, target_is_directory=True)
        token = self.acquire(anchor, scope=alias)["lease"]
        self.assertEqual(self.whoami(anchor)["lease"], token)
        self.result(self.session("ensure", anchor, "--json"), 8)
        released = self.result(self.session("release", anchor, "--json"))
        self.assertEqual(released["lease"], token)

    def test_null_anchor_uses_scope_only_for_whoami_and_release(self):
        self.create(NAME)
        token = self.acquire(self.set_anchor())["lease"]
        no_ancestors = """
        import subprocess
        real_run = subprocess.run
        def run(argv, **opts):
            if argv[0] == "/bin/ps" and "-axww" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="")
            return real_run(argv, **opts)
        subprocess.run = run
        """
        scope = ("--lease-scope", str(self.scope))
        env = {"SIM_VAULT_ANCHOR_PID": "", "CLAUDE_PID": ""}
        found = self.result(self.helper_cli(no_ancestors, "whoami", NAME, *scope, "--json", **env))
        self.assertEqual((found["anchor"], found["lease"]), (None, token))
        released = self.result(self.helper_cli(no_ancestors, "release", NAME, "--lease", "auto",
                                               *scope, "--json", **env))
        self.assertEqual((released["action"], released["lease"]), ("released", token))
        acquired = self.result(self.helper_cli(no_ancestors, "ensure", NAME, "--lease", "auto",
                                               *scope, "--json", **env))
        record = json.loads(self.lease_file(acquired["lease"]).read_text())
        self.assertIsNone(record["anchor"])

    def test_claude_override_must_be_an_ancestor(self):
        outsider = self.set_anchor()
        args = ("whoami", NAME, "--lease-scope", str(self.scope), "--json")
        proc = self.sv(*args, CLAUDE_PID=outsider.pid, SIM_VAULT_ANCHOR_PID="")
        found = self.result(proc)["anchor"]
        if found:
            self.assertNotEqual(found["pid"], outsider.pid)
        # Run the same loader from a known parent identified through CLAUDE_PID.
        launcher = textwrap.dedent("""
            import os, subprocess, sys
            os.environ["CLAUDE_PID"] = str(os.getpid())
            print(os.getpid(), flush=True)
            sys.exit(subprocess.call([sys.executable, *sys.argv[1:]]))
        """)
        proc = subprocess.run([PY, "-c", launcher, self.loader, *args],
                              env=self.env(SIM_VAULT_ANCHOR_PID=""),
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pid, output = proc.stdout.splitlines()
        found = json.loads(output)["anchor"]
        self.assertEqual((found["pid"], found["foundBy"]), (int(pid), "CLAUDE_PID"))

    def test_ancestor_allowlist_uses_nearest_basename(self):
        first, second = self.set_anchor(), self.set_anchor()
        observation = f"""
        import subprocess
        real_run = subprocess.run
        def run(argv, **opts):
            if argv[0] == "/bin/ps" and "-axww" in argv:
                rows = f"{{os.getpid()}} {first.pid} python\\n"
                rows += "{first.pid} {second.pid} /bin/-tmux\\n{second.pid} 1 /bin/codex\\n"
                return subprocess.CompletedProcess(argv, 0, stdout=rows)
            return real_run(argv, **opts)
        subprocess.run = run
        """
        found = self.result(self.helper_cli(observation, "whoami", NAME, "--lease-scope",
                                            str(self.scope), "--json", CLAUDE_PID="",
                                            SIM_VAULT_ANCHOR_PID=""))["anchor"]
        self.assertEqual((found["pid"], found["comm"], found["foundBy"]),
                         (first.pid, "tmux", "ancestor"))

    def test_ps_start_time_is_not_compared_against_kernel_time(self):
        self.create(NAME)
        anchor = self.set_anchor()
        unavailable = "mod.process_identity = lambda pid: None"
        args = (NAME, "--lease", "auto", "--lease-scope", str(self.scope), "--json")
        env = {"SIM_VAULT_ANCHOR_PID": anchor.pid}
        token = self.result(self.helper_cli(unavailable, "ensure", *args, **env))["lease"]
        record = json.loads(self.lease_file(token).read_text())
        self.assertEqual(record["anchor"]["startSource"], "ps")
        self.assertIsNotNone(record["anchor"]["startedAt"])
        self.assertIsNone(self.whoami(anchor)["lease"])
        row, = self.result(self.sv("status", NAME, "--json"))["leases"]
        self.assertTrue(row["anchorAlive"])
        released = self.result(self.helper_cli(unavailable, "release", *args, **env))
        self.assertEqual(released["lease"], token)

    def test_whoami_does_not_write_or_touch_the_simulator(self):
        anchor = self.set_anchor()
        self.assertFalse(self.state_root.exists())
        who = self.whoami(anchor)
        self.assertIsNone(who["lease"])
        self.assertEqual(who["anchor"]["pid"], anchor.pid)
        self.assertFalse(self.state_root.exists())
        self.assertFalse((Path(self.fake) / "log").exists())
        proc = self.sv("whoami", NAME, "--lease-scope", str(self.scope),
                       SIM_VAULT_ANCHOR_PID=anchor.pid)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn(str(anchor.pid), proc.stderr)

    def test_session_stays_live_in_status_and_on_disk_after_anchor_and_device_exit(self):
        udid = self.create(NAME)
        anchor = self.set_anchor()
        token = self.acquire(anchor)["lease"]
        path = self.lease_file(token)
        fresh, = self.result(self.sv("status", NAME, "--json"))["leases"]
        self.assertEqual((fresh["anchorAlive"], fresh["scopeExists"], fresh["overdue"]),
                         (True, True, False))
        self.stop_child(anchor)
        self.scope.rmdir()
        self.xcrun("delete", udid)
        record = json.loads(path.read_text())
        record["createdAt"] = "2026-01-01T00:00:00+00:00"
        path.write_text(json.dumps(record))
        aged, = self.result(self.sv("status", NAME, "--json"))["leases"]
        self.assertEqual((aged["kind"], aged["alive"], aged["stale"], aged["overdue"],
                          aged["anchorAlive"], aged["scopeExists"]),
                         ("session", True, False, True, False, False))
        self.assertGreater(aged["ageSeconds"], 4 * 3600)
        human = self.sv("status", NAME)
        self.assertIn("anchorAlive=False scopeExists=False", human.stderr)
        busy = self.result(self.session("ensure", self.set_anchor(), "--json"), 8)
        self.assertIn("anchor exited", busy["message"])
        self.assertIn("scope missing", busy["message"])
        self.result(self.sv("gc", "--json"))
        self.assertTrue(path.exists())
        self.create(NAME)
        self.result(self.sv("shutdown", NAME, "--json"), 8)
        self.assertTrue(path.exists())
        self.assertEqual(self.result(self.sv("release", NAME, "--lease", token, "--json"))["action"],
                         "released")

    def test_old_owner_files_still_guard_their_scopes(self):
        anchor = self.set_anchor()
        directory = self.state_root / "owners"
        directory.mkdir(parents=True)
        scope_hash = hashlib.sha1(os.fsencode(self.scope.resolve())).hexdigest()
        for name, marker, owner in (
            ("DD iPhone 18 Pro", f"daydo-{scope_hash}-iphone", "dd:up:old"),
            ("DD iPad Pro 11", f"daydo-{scope_hash}-ipad", "dd:up:old"),
            ("DSA iPhone 18 Pro Max", f"dsa-{scope_hash}-DSA-iPhone-18-Pro-Max", "dsa:up:old"),
        ):
            with self.subTest(name=name):
                self.create(name)
                self.result(self.sv("ensure", name, "--lease", owner, "--json"))
                (directory / marker).write_text(owner + "\n")
                args = ("ensure", name, "--lease", "auto", "--lease-scope", str(self.scope), "--json")
                env = {"SIM_VAULT_ANCHOR_PID": anchor.pid}
                busy = self.result(self.sv(*args, **env), 8)
                self.assertIn(owner, busy["message"])
                self.result(self.sv("release", name, "--lease", owner, "--json"))
                self.result(self.sv(*args, **env))

    def test_history_records_session_held_and_legacy_kinds(self):
        self.create(NAME)
        anchor = self.set_anchor()
        self.acquire(anchor)
        self.result(self.session("ensure", anchor, "--json"), 8)
        self.result(self.session("release", anchor, "--json"))
        self.result(self.sv("ensure", NAME, "--lease", "auto", "--json"))
        self.result(self.sv("release", NAME, "--lease", "auto", "--json"))
        self.result(self.sv("release", NAME, "--lease", "missing", "--json"))
        run = self.sv("run", NAME, "--lease", "test", "--", "/usr/bin/true")
        self.assertEqual(run.returncode, 0, run.stderr)
        records = [json.loads(line) for line in (self.state_root / "history.jsonl").read_text().splitlines()]
        self.assertEqual([(r["cmd"], r["rc"], r.get("leaseKind")) for r in records],
                         [("ensure", 0, "session"), ("ensure", 8, "session"),
                          ("release", 0, "session"), ("ensure", 0, "legacy"),
                          ("release", 0, "legacy"), ("release", 0, None), ("run", 0, "held")])
        self.assertNotIn("leaseKind", records[-2])

    def test_help_and_release_notes_cover_manual_sessions(self):
        version = self.sv("--version")
        self.assertEqual((version.returncode, version.stdout), (0, "sim-vault 4.0.0\n"))
        for args, labels in (
            ((), ("whoami", "--lease auto", "--lease-scope", "--lease-prefix")),
            (("ensure",), ("--lease-scope", "--lease-prefix")),
            (("release",), ("--lease-scope",)),
            (("whoami",), ("--lease-scope",)),
        ):
            proc = self.sv(*args, "--help")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for label in labels:
                self.assertIn(label, proc.stdout)
        readme = (REPO / "README.md").read_text()
        self.assertIn("Manual sessions", readme)
        self.assertIn("non-interactive SSH", readme)
        notes = (REPO / "CHANGELOG.md").read_text().split("## 3.1.0")[0]
        for item in ("## 4.0.0", "held lease", "run", "--lease auto", "whoami",
                     "0700", "0600", "https://github.com/audichuang/sim-vault/issues/1"):
            self.assertIn(item, notes)


class MixedVersionTests(SessionLeaseCase):
    def setUp(self):
        super().setUp()
        self.old_script = Path(self.base) / "sim-vault-v3.py"
        with self.old_script.open("wb") as handle:
            subprocess.run(["git", "-C", str(REPO), "show", "v3.1.0:src/sim_vault/__init__.py"],
                           stdout=handle, check=True)

    def v3(self, *args):
        return self.sv(*args, "--json", SV_TEST_SCRIPT=self.old_script)

    @contextmanager
    def run_holder(self):
        child = subprocess.Popen(
            [PY, self.loader, "run", NAME, "--lease", "mixed", "--", PY, "-c",
             "import os, sys; print(os.environ['SIM_VAULT_LEASE'], flush=True); sys.stdin.read()"],
            env=self.env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertTrue(select.select([child.stdout], [], [], 10)[0], "run child did not start")
            owner = child.stdout.readline().strip()
            self.assertRegex(owner, r"^mixed:\d+-[0-9a-f]+$")
            yield owner
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.stop_child(child)
            stderr = child.stderr.read()
            child.stderr.close()
            child.stdout.close()
            self.assertEqual(child.returncode, 0, stderr)

    def test_v3_reads_new_sessions_and_held_leases_and_v4_honors_orphan_lock(self):
        self.create(NAME)
        session = self.acquire(self.set_anchor())["lease"]
        blocked_commands = (("park", NAME, "--shutdown"), ("shutdown", NAME),
                            ("ensure", NAME, "--lease", "old", "--deny-other-leases"))
        with self.run_holder() as held:
            status = self.result(self.v3("status", NAME))
            self.assertEqual({row["owner"]: row["stale"] for row in status["leases"]},
                             {held: False, session: False})
            for args in blocked_commands:
                busy = self.result(self.v3(*args), 8)
                self.assertIn(held, busy["message"])
                self.assertIn(session, busy["message"])
            held_file = self.lease_file(held)
            released = self.result(self.v3("release", NAME, "--lease", held))
            self.assertEqual(released["action"], "released")
            self.assertFalse(held_file.exists())
            self.assertTrue(held_file.with_suffix(".lock").is_file())
            busy = self.result(self.sv("park", NAME, "--shutdown", "--json"), 8)
            self.assertIn("held lease", busy["message"])
            self.assertIn(held_file.stem[:8], busy["message"])
        for args in blocked_commands:
            self.assertIn(session, self.result(self.v3(*args), 8)["message"])

    def test_v4_honors_v3_legacy_lease_policy_and_shutdown(self):
        udid = self.create(NAME)
        self.result(self.v3("ensure", NAME, "--boot", "--lease", "old"))
        row, = self.result(self.sv("status", NAME, "--json"))["leases"]
        self.assertEqual((row["owner"], row["kind"], row["alive"], row["stale"]),
                         ("old", "legacy", True, False))
        for args in (("park", NAME, "--shutdown"), ("shutdown", NAME),
                     ("ensure", NAME, "--lease", "other", "--deny-other-leases")):
            self.assertEqual(self.result(self.sv(*args, "--json"), 8)["kind"], "busy")
        self.result(self.sv("ensure", NAME, "--lease", "old", "--json"))
        released = self.result(self.sv("release", NAME, "--lease", "old", "--shutdown", "--json"))
        self.assertEqual((released["action"], released["shutdown"]), ("released", True))
        self.assertEqual(self.state(udid), "Shutdown")
