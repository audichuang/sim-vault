"""Exercise run with the fake xcrun harness, real children and real flock."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import subprocess
import textwrap
import threading
import time

from test_sim_vault import PY, SimVaultCase


NAME = "SVTEST run"
PREFIX = "test:run"
READY_TIMEOUT = 10


class RunTests(SimVaultCase):
    def setUp(self):
        super().setUp()
        key = "SVTEST_run-" + hashlib.sha1(NAME.encode()).hexdigest()[:8]
        self.root = Path(self.home) / "Library/Developer/CoreSimulator/sim-vault"
        self.directory = self.root / "leases" / key
        self.device_lock = self.root / "locks" / (key + ".lock")
        self.result = Path(self.base) / "run-result.json"

    def args(self, flags, command):
        return ["run", NAME, "--lease", PREFIX, *flags, "--", *command]

    def run_sv(self, code, *flags, **env):
        return self.sv(*self.args(flags, [PY, "-c", code]), **env)

    @contextmanager
    def running(self, command, *flags, pass_fds=()):
        with subprocess.Popen([PY, self.loader, *self.args(flags, command)], env=self.env(),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, pass_fds=pass_fds) as proc:
            try:
                yield proc
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=16)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()

    def ready(self, proc):
        self.assertTrue(select.select([proc.stdout], [], [], READY_TIMEOUT)[0],
                        "run did not start its child")
        line = proc.stdout.readline()
        if not line:
            self.fail("run did not start its child:\n" + proc.stderr.read())
        return line.rstrip("\n")

    def leases(self):
        proc = self.sv("status", NAME, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)["leases"]

    def assert_summary(self, stderr, stage, rc, child_rc, teardown):
        line = (f"sim-vault-run: stage={stage} rc={rc} "
                f"childRc={child_rc if child_rc is not None else '-'} teardown={teardown}")
        self.assertEqual(stderr.splitlines()[-1], line, stderr)

    def assert_no_shutdown(self):
        calls = [json.loads(line) for line in (Path(self.fake) / "log").read_text().splitlines()]
        self.assertNotIn(["simctl", "shutdown"], [call[:2] for call in calls])

    def lease_lock(self, owner):
        return self.directory / (hashlib.sha1(owner.encode()).hexdigest() + ".lock")

    def kill_pid(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def await_unlocked(self, path):
        deadline = time.monotonic() + READY_TIMEOUT
        with path.open("rb") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.fail(f"{path} stayed locked")
                    time.sleep(0.02)

    def wait_for(self, predicate, message):
        deadline = time.monotonic() + READY_TIMEOUT
        while not predicate():
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.02)

    def loader_hook(self, code):
        path = Path(self.loader)
        setup = path.read_text().removesuffix("mod.main()\n")
        path.write_text(setup + textwrap.dedent(code) + "\nmod.main()\n")

    def test_child_environment_live_lease_and_release_without_shutdown(self):
        udid = self.create(NAME)
        code = """
import json, os, sys
name, udid, owner, fd = (os.environ[f"SIM_VAULT_{key}"]
                         for key in ("NAME", "UDID", "LEASE", "LEASE_FD"))
assert os.get_inheritable(int(fd))
device_lock = sys.argv[1]
for entry in os.listdir("/dev/fd"):
    if entry.isdecimal():
        try:
            assert not os.path.samestat(os.fstat(int(entry)), os.stat(device_lock))
        except OSError:
            pass
print(json.dumps([name, udid, owner, os.getpid(), os.getpgrp()]), flush=True)
sys.stdin.read()
"""
        with self.running([PY, "-c", code, str(self.device_lock)], "--boot",
                          "--expect-udid", udid) as proc:
            name, resolved, owner, pid, pgid = json.loads(self.ready(proc))
            self.assertEqual((name, resolved), (NAME, udid))
            self.assertNotEqual(pid, proc.pid)
            self.assertEqual(pgid, os.getpgid(proc.pid))
            self.assertRegex(owner, rf"^{PREFIX}:{proc.pid}-[0-9a-f]{{6}}$")
            lease, = self.leases()
            self.assertEqual((lease["owner"], lease["kind"], lease["mode"], lease["alive"]),
                             (owner, "held", "shared", True))
            _, stderr = proc.communicate("", timeout=READY_TIMEOUT)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assert_summary(stderr, "child", 0, 0, "ok")
        self.assertEqual(self.leases(), [])
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.state(udid), "Booted")
        self.assert_no_shutdown()

    def test_child_exit_code_passthrough_and_result_file(self):
        self.create(NAME)
        for code in (8, 11, 65):
            with self.subTest(code=code):
                proc = self.run_sv(f"raise SystemExit({code})", "--result-file", str(self.result))
                self.assertEqual(proc.returncode, code, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assert_summary(proc.stderr, "child", code, code, "ok")
                self.assertEqual(json.loads(self.result.read_text()),
                                 {"stage": "child", "rc": code, "childRc": code, "teardown": "ok"})

    def test_forwards_signals_sent_only_to_run_and_reaps_child(self):
        self.create(NAME)
        code = "import os, time; print(os.getpgrp(), flush=True); time.sleep(60)"
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signum=signum):
                with self.running([PY, "-c", code]) as proc:
                    pgid = int(self.ready(proc))
                    proc.send_signal(signum)
                    _, stderr = proc.communicate(timeout=READY_TIMEOUT)
                    rc = 128 + signum
                    self.assertEqual(proc.returncode, rc, stderr)
                    self.assert_summary(stderr, "child", rc, rc, "ok")
                    self.assertEqual(pgid, os.getpgid(0), "child left run's process group")
                    entry = json.loads((self.root / "history.jsonl").read_text().splitlines()[-1])
                    self.assertEqual(entry["signal"], signum)
                self.assertEqual(self.leases(), [])

    def test_acquisition_failures_never_launch_child(self):
        os.mkdir(self.vault)
        marker = Path(self.base) / "started"
        child = f"open({str(marker)!r}, 'w').close()"
        proc = self.run_sv(child)
        self.check_acquisition_failure(proc, 3, "no-archive")
        self.assertFalse(marker.exists())
        self.assertEqual(self.leases(), [])
        self.create(NAME)
        self.assertEqual(self.sv("ensure", NAME, "--lease", "manual").returncode, 0)
        for rc, kind, flags in (
            (8, "busy", ("--deny-other-leases",)),
            (1, "udid-changed", ("--expect-udid", "wrong")),
        ):
            with self.subTest(kind=kind):
                proc = self.run_sv(child, "--result-file", str(self.result), *flags)
                self.check_acquisition_failure(proc, rc, kind)
                self.assertEqual(json.loads(self.result.read_text()),
                                 {"stage": "acquire", "rc": rc, "childRc": None, "teardown": "-"})
                self.assertFalse(marker.exists())
                self.assertEqual([lease["owner"] for lease in self.leases()], ["manual"])
                self.assertEqual(list(self.directory.glob("*.lock")), [])

    def check_acquisition_failure(self, proc, rc, kind):
        self.assertEqual(proc.returncode, rc, proc.stderr)
        self.assertIn(f"error (exit {rc}, {kind})", proc.stderr)
        self.assert_summary(proc.stderr, "acquire", rc, None, "-")

    def test_expected_udid_mismatch_with_boot_does_not_start_or_write_lease(self):
        udid = self.create(NAME)
        marker = Path(self.base) / "started"
        proc = self.run_sv(f"open({str(marker)!r}, 'w').close()", "--expect-udid", "wrong",
                           "--boot")
        self.check_acquisition_failure(proc, 1, "udid-changed")
        self.assertFalse(marker.exists())
        calls = [json.loads(line)[:2] for line in (Path(self.fake) / "log").read_text().splitlines()]
        self.assertNotIn(["simctl", "boot"], calls)
        self.assertNotIn(["simctl", "bootstatus"], calls)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.state(udid), "Shutdown")

    def test_boot_stub_keeps_lease_after_run_is_killed(self):
        self.assert_boot_step_keeps_lease("boot")

    def test_bootstatus_stub_keeps_lease_after_run_is_killed(self):
        self.assert_boot_step_keeps_lease("bootstatus")

    def assert_boot_step_keeps_lease(self, step):
        stub = Path(self.stub) / "xcrun"
        stub.write_text(stub.read_text().replace(
            '            time.sleep(hk["seconds"])',
            textwrap.indent(textwrap.dedent("""
                import signal
                with open(os.path.join(root, "boot-paused"), "w") as ready:
                    print(os.getpid(), file=ready)
                os.kill(os.getpid(), signal.SIGSTOP)
            """).strip(), " " * 12),
        ))
        udid = self.create(NAME)
        self.hooks([{"cmd": step, "n": 1, "action": "sleep", "seconds": 0}])
        paused = Path(self.fake) / "boot-paused"
        with self.running([PY, "-c", "raise SystemExit(65)"], "--boot") as proc:
            self.wait_for(lambda: paused.exists() and paused.stat().st_size,
                          f"stub never paused in {step}")
            stub_pid = int(paused.read_text())
            try:
                lease, = self.leases()
                lock = self.lease_lock(lease["owner"])
                proc.kill()
                self.assertEqual(proc.wait(timeout=READY_TIMEOUT), -signal.SIGKILL)
                shutdown = self.sv("shutdown", NAME)
                park = self.sv("park", NAME, "--shutdown")
                self.assertEqual((shutdown.returncode, park.returncode), (8, 8),
                                 shutdown.stderr + park.stderr)
                self.assert_no_shutdown()
            finally:
                os.kill(stub_pid, signal.SIGCONT)
        self.await_unlocked(lock)
        self.assertEqual(self.state(udid), "Booted")
        self.assertEqual(self.sv("ensure", NAME).returncode, 0)
        self.assertEqual(self.leases(), [])
        self.assert_no_shutdown()

    def test_failed_bootstatus_undoes_own_boot(self):
        udid = self.create(NAME)
        marker = Path(self.base) / "started"
        proc = self.run_sv(f"open({str(marker)!r}, 'w').close()", "--boot", FAKE_BOOTSTATUS_RC=1)
        self.check_acquisition_failure(proc, 1, "error")
        self.assertFalse(marker.exists())
        self.assertEqual(self.state(udid), "Shutdown")
        self.assertEqual(self.count("shutdown"), 1)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_exclusive_acquisition_and_allow_list(self):
        self.create(NAME, state="Booted")
        child = "raise SystemExit(65)"
        self.check_acquisition_failure(self.run_sv(child, "--exclusive"), 8, "busy")
        manual = self.sv("ensure", NAME, "--lease", "manual")
        self.assertEqual(manual.returncode, 0, manual.stderr)
        with self.running([PY, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                          "--deny-other-leases", "--allow-lease", "manual") as proc:
            self.assertEqual(self.ready(proc), "ready")
            held, = [lease for lease in self.leases() if lease["kind"] == "held"]
            self.assertEqual(held["mode"], "exclusive")
            blocked = self.sv("ensure", NAME, "--lease", "manual")
            self.assertEqual(blocked.returncode, 8, blocked.stderr)
            _, stderr = proc.communicate("", timeout=READY_TIMEOUT)
            self.assertEqual(proc.returncode, 0, stderr)

    def assert_blocked_while_running(self, owner):
        for args in (("park", NAME, "--shutdown"), ("shutdown", NAME),
                     ("release", NAME, "--lease", owner), ("ensure", NAME, "--lease", "other")):
            with self.subTest(args=args):
                blocked = self.sv(*args)
                self.assertEqual(blocked.returncode, 8, blocked.stderr)
                self.assertIn(owner, blocked.stderr)

    def test_live_bash_subtree_still_protects_device_after_run_is_killed(self):
        udid = self.create(NAME, state="Booted")
        script = '/bin/sleep 60 </dev/null & printf "%s %s\n" "$SIM_VAULT_LEASE" "$!"; wait'
        with self.running(["/bin/bash", "-c", script], "--deny-other-leases") as proc:
            owner, pid = self.ready(proc).split()
            try:
                self.assert_blocked_while_running(owner)
                proc.kill()
                proc.wait(timeout=READY_TIMEOUT)
                self.assert_blocked_while_running(owner)
            finally:
                self.kill_pid(int(pid))
            self.await_unlocked(self.lease_lock(owner))
            self.assertEqual(proc.communicate(timeout=READY_TIMEOUT)[0], "")
        reaped = self.sv("ensure", NAME)
        self.assertEqual(reaped.returncode, 0, reaped.stderr)
        self.assertEqual(self.leases(), [])
        self.assertFalse(self.lease_lock(owner).exists())
        self.assertEqual(self.state(udid), "Booted")
        self.assert_no_shutdown()

    def test_python_launcher_pass_fds_controls_lease_lifetime(self):
        self.create(NAME)
        code = """
import os, subprocess, sys
fd = int(os.environ["SIM_VAULT_LEASE_FD"])
sleep = subprocess.Popen(["/bin/sleep", "60"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         pass_fds=(fd,) if sys.argv[1] == "pass" else ())
print(os.environ["SIM_VAULT_LEASE"], sys.argv[2], os.getpid(), sleep.pid, flush=True)
sleep.wait()
"""
        for mode in ("drop", "pass"):
            with self.subTest(mode=mode):
                shell = '"$1" -c "$2" "$3" "$$" & wait'
                with self.running(["/bin/bash", "-c", shell, "bash", PY, code, mode],
                                  "--deny-other-leases") as proc:
                    owner, shell_pid, middle, sleeper = self.ready(proc).split()
                    try:
                        proc.kill()
                        proc.wait(timeout=READY_TIMEOUT)
                        self.kill_pid(int(middle))
                        # The script's shell has the fd until it notices Python exited.
                        # End the shell too, leaving just the grandchild sleep.
                        self.kill_pid(int(shell_pid))
                        if mode == "drop":
                            self.await_unlocked(self.lease_lock(owner))
                        blocked = self.sv("ensure", NAME, "--lease", "other")
                        self.assertEqual(blocked.returncode, 8 if mode == "pass" else 0,
                                         blocked.stderr)
                    finally:
                        self.kill_pid(int(sleeper))
                self.assertEqual(self.sv("release", NAME, "--lease", "other").returncode, 0)

    def test_bash_eval_closes_background_helpers_lease_fd(self):
        self.create(NAME)
        script = r'''
case $BASH_VERSION in 3.2.*) ;; *) exit 90;; esac
eval "/bin/sleep 60 </dev/null >/dev/null 2>&1 ${SIM_VAULT_LEASE_FD}>&- &"
printf '%s\n' "$!"
'''
        # Put the helper outside run's group so success depends on closing its fd.
        # The script itself still runs under macOS /bin/bash and uses numeric redirection.
        launcher = """
import os, subprocess, sys
subprocess.run(["/bin/bash", "-c", sys.argv[1]], start_new_session=True,
               pass_fds=(int(os.environ["SIM_VAULT_LEASE_FD"]),), check=True)
"""
        with self.running([PY, "-c", launcher, script]) as proc:
            helper = int(self.ready(proc))
            try:
                _, stderr = proc.communicate(timeout=READY_TIMEOUT)
                self.assertEqual(proc.returncode, 0, stderr)
                self.assert_summary(stderr, "child", 0, 0, "ok")
                os.kill(helper, 0)
                self.assertEqual(self.leases(), [])
            finally:
                self.kill_pid(helper)

    def test_detached_grandchild_keeps_lease_and_inherited_project_flock(self):
        self.create(NAME)
        project = Path(self.base) / "project.lock"
        code = """
import os, subprocess, sys
lease = int(os.environ["SIM_VAULT_LEASE_FD"])
detached = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, pass_fds=(lease, int(sys.argv[1])))
print(os.environ["SIM_VAULT_LEASE"], detached.pid, flush=True)
raise SystemExit(int(sys.argv[2]))
"""
        for child_rc in (0, 11):
            with self.subTest(child_rc=child_rc):
                with project.open("a") as caller_fd:
                    fd = caller_fd.fileno()
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    with self.running([PY, "-c", code, str(fd), str(child_rc)], "--result-file",
                                      str(self.result), pass_fds=(fd,)) as proc:
                        try:
                            owner, pid = self.ready(proc).split()
                        finally:
                            caller_fd.close()
                        try:
                            _, stderr = proc.communicate(timeout=READY_TIMEOUT)
                            rc = child_rc or 8
                            stage = "child" if child_rc else "teardown"
                            self.assertEqual(proc.returncode, rc, stderr)
                            self.assert_summary(stderr, stage, rc, child_rc, "held-by-other")
                            lock = self.lease_lock(owner)
                            self.assertIn(f"lsof {shlex.quote(str(lock))}", stderr)
                            self.assertNotIn(f"kill {pid}", stderr)
                            self.assertNotIn(f"kill {proc.pid}", stderr)
                            if child_rc:
                                self.assertIn("warning:", stderr)
                            self.assertEqual(json.loads(self.result.read_text()),
                                             {"stage": stage, "rc": rc, "childRc": child_rc,
                                              "teardown": "held-by-other"})
                            entry = json.loads((self.root / "history.jsonl").read_text().splitlines()[-1])
                            self.assertEqual((entry["rc"], entry["teardown"], entry["childRc"]),
                                             (rc, "held-by-other", child_rc))
                            with project.open("rb") as probe, self.assertRaises(BlockingIOError):
                                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            self.assertTrue(lock.is_file())
                        finally:
                            self.kill_pid(int(pid))
                        self.await_unlocked(lock)
                        proc = self.sv("ensure", NAME)
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertFalse(lock.exists())
        self.assert_no_shutdown()

    def test_same_group_background_process_is_left_alive_and_lease_is_kept(self):
        self.create(NAME)
        script = (
            '/bin/sleep 60 </dev/null >/dev/null 2>&1 & '
            'printf "%s %s\n" "$SIM_VAULT_LEASE" "$!"'
        )
        with self.running(["/bin/bash", "-c", script]) as proc:
            owner, sleeper = self.ready(proc).split()
            try:
                _, stderr = proc.communicate(timeout=READY_TIMEOUT)
                self.assertEqual(proc.returncode, 8, stderr)
                self.assert_summary(stderr, "teardown", 8, 0, "held-by-other")
                lease, = self.leases()
                self.assertEqual((lease["owner"], lease["alive"]), (owner, True))
            finally:
                self.kill_pid(int(sleeper))
        self.await_unlocked(self.lease_lock(owner))

    def test_teardown_retry_status_does_not_list_run_as_lease_holder(self):
        self.create(NAME)
        child = """
import os, subprocess
fd = int(os.environ["SIM_VAULT_LEASE_FD"])
leftover = subprocess.Popen(["/bin/sleep", "60"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, pass_fds=(fd,))
print(leftover.pid, flush=True)
"""
        with self.running([PY, "-c", child]) as proc:
            sleeper = int(self.ready(proc))
            try:
                # The child has started after acquisition released DeviceLock.
                # The next holder of this device lock is run's teardown retry.
                def releasing():
                    with self.device_lock.open("rb") as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            return True
                    return False

                self.wait_for(releasing, "run never entered teardown")
                lease, = self.leases()
                self.assertTrue(lease["alive"])
                self.assertNotIn(proc.pid, lease["holderPids"], lease)
                self.assertIsNone(lease["pid"], lease)
                self.assertIsNone(proc.poll(), "teardown had already finished")
            finally:
                self.kill_pid(sleeper)
            _, stderr = proc.communicate(timeout=READY_TIMEOUT)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assert_summary(stderr, "child", 0, 0, "ok")
        self.assertEqual(self.leases(), [])

    def test_interrupt_at_acquire_handoff_releases_lease_without_launching(self):
        self.create(NAME)
        self.loader_hook("""
            import signal
            original_ensure = mod.ensure
            def interrupt_after_ensure(*args, **kwargs):
                result = original_ensure(*args, **kwargs)
                os.kill(os.getpid(), signal.SIGTERM)
                return result
            mod.ensure = interrupt_after_ensure
        """)
        marker = Path(self.base) / "started"
        proc = self.run_sv(f"open({str(marker)!r}, 'w').close()")
        self.assertEqual(proc.returncode, 143, proc.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(list(self.directory.iterdir()), [], proc.stderr)
        self.assert_summary(proc.stderr, "acquire", 143, None, "ok")

    def test_signals_during_teardown_are_not_forwarded_to_reaped_child(self):
        self.create(NAME)
        forwarded = Path(self.base) / "forwarded"
        self.loader_hook(f"""
            import signal
            from pathlib import Path
            original_release = mod.run_release
            kill = os.kill
            killpg = os.killpg
            def watch(fn):
                def watched(pid, signum):
                    Path({str(forwarded)!r}).write_text(str(pid))
                    return fn(pid, signum)
                return watched
            def signal_during_release(*args, **kwargs):
                os.kill = watch(kill)
                os.killpg = watch(killpg)
                try:
                    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                        kill(os.getpid(), signum)
                    return original_release(*args, **kwargs)
                finally:
                    os.kill = kill
                    os.killpg = killpg
            mod.run_release = signal_during_release
        """)
        proc = self.run_sv("raise SystemExit(65)")
        self.assertEqual(proc.returncode, 65, proc.stderr)
        self.assert_summary(proc.stderr, "child", 65, 65, "ok")
        self.assertFalse(forwarded.exists(), "a signal was forwarded after reaping the child")
        self.assertEqual(self.leases(), [])

    def test_closed_stderr_pipe_preserves_child_rc(self):
        self.create(NAME)
        bogus_parent = Path(self.base) / "not-a-directory"
        bogus_parent.touch()
        code = "import sys; print('ready', flush=True); sys.stdin.read(); sys.exit(65)"
        for warning in (None, "result-file", "history"):
            with self.subTest(warning=warning):
                bad_path = str(bogus_parent / "out")
                flags = ["--result-file", bad_path] if warning == "result-file" else []
                env = self.env(**({"SIM_VAULT_HISTORY": bad_path} if warning == "history" else {}))
                args = self.args(flags, [PY, "-c", code])
                with subprocess.Popen([PY, self.loader, *args], env=env, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True) as proc:
                    try:
                        self.assertEqual(self.ready(proc), "ready")
                        proc.stderr.close()  # Close the only reader before teardown logs.
                    finally:
                        proc.stdin.close()
                    self.assertEqual(proc.wait(timeout=READY_TIMEOUT), 65)
                self.assertEqual(self.leases(), [])

    def test_concurrent_writers_use_distinct_temps_for_same_result_file(self):
        self.create(NAME)
        self.loader_hook(f"""
            from pathlib import Path
            import time
            target = Path({str(self.result)!r})
            replace = os.replace
            def replace_together(src, dst):
                if Path(dst) == target:
                    pid = os.getpid()
                    target.with_name(f"ready-{{pid}}").write_text(str(src))
                    deadline = time.monotonic() + 8
                    while len(list(target.parent.glob("ready-*"))) < 2:
                        assert time.monotonic() < deadline, "second run never reached rename"
                        time.sleep(0.02)
                return replace(src, dst)
            os.replace = replace_together
        """)
        def cmd(rc):
            return [PY, "-c", f"raise SystemExit({rc})"]

        flags = ("--result-file", str(self.result))
        with self.running(cmd(8), *flags) as first, self.running(cmd(65), *flags) as second:
            _, first_stderr = first.communicate(timeout=READY_TIMEOUT)
            _, second_stderr = second.communicate(timeout=READY_TIMEOUT)
            self.assertEqual((first.returncode, second.returncode), (8, 65),
                             first_stderr + second_stderr)
            temps = {path.read_text() for path in self.result.parent.glob("ready-*")}
            self.assertEqual(len(temps), 2, first_stderr + second_stderr)
            self.assertTrue(all(Path(src).parent == self.result.parent for src in temps))
            self.assertTrue(all(not Path(src).exists() for src in temps))
            self.assertNotIn("could not write", first_stderr + second_stderr)
        result = json.loads(self.result.read_text())
        self.assertIn(result["rc"], (8, 65))
        self.assertEqual(result["rc"], result["childRc"])

    def test_status_probes_during_teardown_and_boot_record_handoff(self):
        udid = self.create(NAME)
        code = """
import sys
print("ready", flush=True)
sys.stdin.read()
print("exiting", flush=True)
"""
        with self.running([PY, "-c", code], "--boot") as proc:
            self.assertEqual(self.ready(proc), "ready")
            lease, = self.leases()
            self.assertEqual(self.sv("ensure", NAME, "--lease", "remaining").returncode, 0)
            lock = self.lease_lock(lease["owner"])
            stopped, probed = threading.Event(), threading.Event()
            statuses = []

            def probe_status():
                while not stopped.is_set():
                    statuses.append(self.sv("status", NAME, "--json"))
                    probed.set()

            thread = threading.Thread(target=probe_status)
            thread.start()
            try:
                # Keep tear-down waiting for DeviceLock while status probes it.
                with self.device_lock.open("rb") as device_lock:
                    fcntl.flock(device_lock, fcntl.LOCK_EX)
                    proc.stdin.close()
                    proc.stdin = None
                    self.assertEqual(self.ready(proc), "exiting")
                    probed.clear()
                    self.assertTrue(probed.wait(READY_TIMEOUT))
                _, stderr = proc.communicate(timeout=READY_TIMEOUT)
                self.assertEqual(proc.returncode, 0, stderr)
                self.assert_summary(stderr, "child", 0, 0, "ok")
            finally:
                stopped.set()
                thread.join()
            self.assertTrue(all(status.returncode == 0 for status in statuses))
        self.assertFalse(lock.exists())
        status = json.loads(self.sv("status", NAME, "--json").stdout)
        self.assertEqual(status["bootedByLease"]["by"], ["remaining"])
        self.assertEqual(self.state(udid), "Booted")
        self.assert_no_shutdown()

    def test_history_records_acquisition_child_and_teardown(self):
        udid = self.create(NAME)
        history = Path(self.base) / "history.jsonl"
        proc = self.run_sv("import time; time.sleep(0.1)", "--boot", SIM_VAULT_HISTORY=history)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line, = history.read_text().splitlines()
        entry = json.loads(line)
        self.assertEqual((entry["cmd"], entry["name"], entry["udid"], entry["rc"],
                          entry["stage"], entry["childRc"], entry["teardown"]),
                         ("run", NAME, udid, 0, "child", 0, "ok"))
        self.assertGreaterEqual(entry["child"], 0.1)
        self.assertEqual(entry["phases"]["child"], entry["child"])
        self.assertEqual(entry["phases"]["lockWait"], entry["lockWait"])
        self.assertGreater(entry["phases"]["boot"], 0)

    def test_child_arguments_own_stdout_and_run_has_no_json_option(self):
        self.create(NAME)
        child = [PY, "-c", "import sys; print(sys.argv[1:])", "--json", "--"]
        proc = self.sv(*self.args((), child))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "['--json', '--']\n")
        invalid = self.sv(*self.args(("--json",), child))
        self.assertEqual(invalid.returncode, 2, invalid.stderr)
        self.assertEqual(invalid.stdout, "")
        self.assert_summary(invalid.stderr, "acquire", 2, None, "-")

    def test_help_preserves_wrapper_flags_and_documents_fd_contract(self):
        for args, text in ((("ensure", "--help"), "--deny-other-leases"),
                           (("park", "--help"), "--trim-posterboard"),
                           (("shutdown", "--help"), "--busy-lock"),
                           (("--help",), "run <name>")):
            proc = self.sv(*args)
            self.assertIn(text, proc.stdout)
        proc = self.sv("run", "--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for text in ("pass_fds", 'eval "helper ... ${SIM_VAULT_LEASE_FD}>&-"',
                     "lsof", "kill", "-- <cmd"):
            self.assertIn(text, proc.stdout)

    def test_background_job_does_not_change_terminal_foreground_group(self):
        self.create(NAME)
        code = """
import os
with open("/dev/tty") as tty:
    print("FOREGROUND=" + str(os.tcgetpgrp(tty.fileno())), flush=True)
print("CHILD_GROUP=" + str(os.getpgrp()), flush=True)
"""
        env = self.env()
        command = shlex.join([PY, self.loader, *self.args((), [PY, "-c", code])])
        script = (f'printf "SHELL_GROUP=%s\n" "$$"; {command} & '
                  'job=$!; printf "RUN_GROUP=%s\n" "$job"; wait "$job"; '
                  'rc=$?; printf "JOB_DONE=%s\n" "$rc"; exit "$rc"')
        argv = ["/bin/bash", "--noprofile", "--norc", "-ic", script]
        pid, tty_fd = pty.fork()
        if pid == 0:
            os.execve(argv[0], argv, env)
        terminal = bytearray()
        try:
            deadline = time.monotonic() + READY_TIMEOUT
            while b"JOB_DONE=" not in terminal or not terminal.endswith(b"\r\n"):
                left = deadline - time.monotonic()
                self.assertTrue(left > 0 and select.select([tty_fd], [], [], left)[0],
                                terminal.decode(errors="replace"))
                try:
                    data = os.read(tty_fd, 4096)
                except OSError:
                    data = b""
                self.assertTrue(data, terminal.decode(errors="replace"))
                terminal.extend(data)
            output = terminal.decode(errors="replace")
            groups = dict(line.split("=", 1) for line in output.splitlines()
                          if line.startswith(("FOREGROUND=", "SHELL_GROUP=", "RUN_GROUP=",
                                              "CHILD_GROUP=", "JOB_DONE=")))
            self.assertEqual(groups["FOREGROUND"], groups["SHELL_GROUP"], output)
            self.assertEqual(groups["CHILD_GROUP"], groups["RUN_GROUP"], output)
            self.assertEqual(groups["JOB_DONE"], "0", output)
            self.assertEqual(self.leases(), [])
        finally:
            os.close(tty_fd)
            self.wait_for(lambda: os.waitpid(pid, os.WNOHANG)[0],
                          "job-control shell did not exit after the pty was closed")
