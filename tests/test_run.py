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
            self.assertEqual((name, resolved, pgid), (NAME, udid, pid))
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

    def test_forwards_signals_to_child_group(self):
        self.create(NAME)
        code = "import time; print('ready', flush=True); time.sleep(60)"
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signum=signum):
                with self.running([PY, "-c", code]) as proc:
                    self.assertEqual(self.ready(proc), "ready")
                    proc.send_signal(signum)
                    _, stderr = proc.communicate(timeout=READY_TIMEOUT)
                    rc = 128 + signum
                    self.assertEqual(proc.returncode, rc, stderr)
                    self.assert_summary(stderr, "child", rc, rc, "ok")
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
print(os.environ["SIM_VAULT_LEASE"], os.getpgrp(), os.getpid(), sleep.pid, flush=True)
sleep.wait()
"""
        for mode in ("drop", "pass"):
            with self.subTest(mode=mode):
                shell = '"$1" -c "$2" "$3" & wait'
                with self.running(["/bin/bash", "-c", shell, "bash", PY, code, mode],
                                  "--deny-other-leases") as proc:
                    owner, pgid, middle, sleeper = self.ready(proc).split()
                    try:
                        proc.kill()
                        proc.wait(timeout=READY_TIMEOUT)
                        self.kill_pid(int(middle))
                        # The script's shell has the fd until it notices Python exited.
                        # End the shell too, leaving just the grandchild sleep.
                        self.kill_pid(int(pgid))
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

    def test_ctrl_z_returns_tty_then_sigcont_resumes_foreground_child(self):
        self.create(NAME)
        code = """
import os
assert os.tcgetpgrp(0) == os.getpgrp()
print("TTY_READY", flush=True)
os.read(0, 128)
assert os.tcgetpgrp(0) == os.getpgrp()
print("TTY_RESUMED", flush=True)
"""
        env = self.env()
        argv = [PY, self.loader, *self.args((), [PY, "-c", code])]
        pid, tty_fd = pty.fork()
        if pid == 0:
            os.execve(PY, argv, env)
        terminal = bytearray()

        def read_until(marker, timeout=READY_TIMEOUT):
            deadline = time.monotonic() + timeout
            while marker not in terminal:
                left = deadline - time.monotonic()
                self.assertTrue(left > 0 and select.select([tty_fd], [], [], left)[0],
                                terminal.decode(errors="replace"))
                try:
                    data = os.read(tty_fd, 4096)
                except OSError:
                    data = b""
                self.assertTrue(data, terminal.decode(errors="replace"))
                terminal.extend(data)

        waited = False
        try:
            read_until(b"TTY_READY\r\n")
            os.write(tty_fd, b"\x1a")
            deadline = time.monotonic() + READY_TIMEOUT
            while True:
                stopped, status = os.waitpid(pid, os.WUNTRACED | os.WNOHANG)
                if stopped:
                    waited = os.WIFEXITED(status) or os.WIFSIGNALED(status)
                    self.assertTrue(os.WIFSTOPPED(status), terminal)
                    break
                self.assertLess(time.monotonic(), deadline, "run did not stop on Ctrl-Z")
                time.sleep(0.02)
            os.kill(pid, signal.SIGCONT)
            os.write(tty_fd, b"resume\n")
            read_until(b"teardown=ok\r\n")
            _, status = os.waitpid(pid, 0)
            waited = True
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, terminal)
            self.assertIn(b"TTY_RESUMED\r\n", terminal)
            self.assertEqual(self.leases(), [])
        finally:
            if not waited:
                self.kill_pid(pid)
                os.waitpid(pid, 0)
            os.close(tty_fd)
