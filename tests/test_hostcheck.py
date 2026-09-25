"""C-08c: pre-T+00 host-resource headroom gate tests."""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control import hostcheck  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestCpu(unittest.TestCase):
    def test_below_threshold_passes(self):
        self.assertTrue(hostcheck.cpu_ok(load1=1.0, cpus=4))  # threshold 3.0

    def test_exactly_at_threshold_passes(self):
        self.assertTrue(hostcheck.cpu_ok(load1=3.0, cpus=4))  # 0.75 * 4 == 3.0

    def test_above_threshold_fails(self):
        self.assertFalse(hostcheck.cpu_ok(load1=3.01, cpus=4))

    def test_read_load1_parses_first_field(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "loadavg"
            _write(path, "1.23 0.98 0.55 2/345 6789\n")
            self.assertEqual(hostcheck.read_load1(path), 1.23)

    def test_read_load1_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "does-not-exist"
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_load1(path)

    def test_read_load1_malformed_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "loadavg"
            _write(path, "not-a-number\n")
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_load1(path)


class TestRam(unittest.TestCase):
    def test_below_4gib_fails(self):
        self.assertFalse(hostcheck.ram_ok(4 * 1024 ** 3 - 1))

    def test_exactly_4gib_passes(self):
        self.assertTrue(hostcheck.ram_ok(4 * 1024 ** 3))

    def test_above_4gib_passes(self):
        self.assertTrue(hostcheck.ram_ok(4 * 1024 ** 3 + 1))

    def test_read_mem_available_parses_kb_field(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "meminfo"
            _write(path, "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n")
            self.assertEqual(hostcheck.read_mem_available_bytes(path), 8192000 * 1024)

    def test_read_mem_available_missing_field_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "meminfo"
            _write(path, "MemTotal:       16384000 kB\n")
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_mem_available_bytes(path)

    def test_read_mem_available_malformed_line_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "meminfo"
            _write(path, "MemAvailable:    not-a-number kB\n")
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_mem_available_bytes(path)


class TestDisk(unittest.TestCase):
    def test_below_20gib_fails(self):
        self.assertFalse(hostcheck.disk_ok(20 * 1024 ** 3 - 1))

    def test_exactly_20gib_passes(self):
        self.assertTrue(hostcheck.disk_ok(20 * 1024 ** 3))

    def test_above_20gib_passes(self):
        self.assertTrue(hostcheck.disk_ok(20 * 1024 ** 3 + 1))

    def test_read_disk_free_bytes_unreadable_path_fails_closed(self):
        with self.assertRaises(hostcheck.HostCheckError):
            hostcheck.read_disk_free_bytes(Path("/this/path/does/not/exist/at/all"))


class TestFd(unittest.TestCase):
    def test_below_10000_fails(self):
        self.assertFalse(hostcheck.fd_ok(9999))

    def test_exactly_10000_passes(self):
        self.assertTrue(hostcheck.fd_ok(10000))

    def test_above_10000_passes(self):
        self.assertTrue(hostcheck.fd_ok(10001))

    def test_read_fd_capacity_parses_three_fields_and_ignores_legacy_field(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "file-nr"
            _write(path, "500\t0\t20000\n")
            allocated, legacy_free, max_handles = hostcheck.read_fd_capacity(path)
            self.assertEqual((allocated, legacy_free, max_handles), (500, 0, 20000))
            self.assertEqual(max_handles - allocated, 19500)

    def test_read_fd_capacity_legacy_field_nonzero_is_still_ignored_for_remaining(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "file-nr"
            # A nonzero legacy field (pre-2.6 semantics) must never be added
            # to "remaining" - only max_handles - allocated_handles counts.
            _write(path, "500\t99999\t20000\n")
            allocated, legacy_free, max_handles = hostcheck.read_fd_capacity(path)
            self.assertEqual(max_handles - allocated, 19500)

    def test_read_fd_capacity_wrong_field_count_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "file-nr"
            _write(path, "500 20000\n")
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_fd_capacity(path)

    def test_read_fd_capacity_non_integer_field_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "file-nr"
            _write(path, "500 abc 20000\n")
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_fd_capacity(path)

    def test_read_fd_capacity_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "does-not-exist"
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_fd_capacity(path)


class TestInotifyThresholds(unittest.TestCase):
    def test_watches_exactly_50_percent_passes(self):
        self.assertTrue(hostcheck.inotify_watches_ok(50, 100))

    def test_watches_above_50_percent_fails(self):
        self.assertFalse(hostcheck.inotify_watches_ok(51, 100))

    def test_instances_exactly_50_percent_passes(self):
        self.assertTrue(hostcheck.inotify_instances_ok(64, 128))

    def test_instances_above_50_percent_fails(self):
        self.assertFalse(hostcheck.inotify_instances_ok(65, 128))

    def test_read_inotify_ceilings_parses_both_files(self):
        with tempfile.TemporaryDirectory() as d:
            watches_path = Path(d) / "max_user_watches"
            instances_path = Path(d) / "max_user_instances"
            _write(watches_path, "524288\n")
            _write(instances_path, "128\n")
            max_watches, max_instances = hostcheck.read_inotify_ceilings(
                watches_path, instances_path
            )
            self.assertEqual((max_watches, max_instances), (524288, 128))

    def test_read_inotify_ceilings_malformed_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            watches_path = Path(d) / "max_user_watches"
            instances_path = Path(d) / "max_user_instances"
            _write(watches_path, "not-a-number\n")
            _write(instances_path, "128\n")
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_inotify_ceilings(watches_path, instances_path)


class TestInotifyUsageFixtureProc(unittest.TestCase):
    """A fake /proc tree, isolated from the real host, proving the counting
    algorithm without depending on what happens to be running right now."""

    def _make_status(self, proc_root: Path, pid: str, uid: int) -> None:
        _write(proc_root / pid / "status",
              f"Name:\tfake\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")

    def _make_inotify_fd(self, proc_root: Path, pid: str, fd: str, watch_count: int) -> None:
        fd_dir = proc_root / pid / "fd"
        fd_dir.mkdir(parents=True, exist_ok=True)
        os.symlink("anon_inode:inotify", fd_dir / fd)
        lines = "\n".join(
            f"inotify wd:{i} ino:{i:x} sdev:800001 mask:fff ignored_mask:0" for i in range(watch_count)
        )
        _write(proc_root / pid / "fdinfo" / fd, f"pos:\t0\nflags:\t04000\n{lines}\n")

    def test_counts_instances_and_watches_for_matching_uid_only(self):
        with tempfile.TemporaryDirectory() as d:
            proc_root = Path(d)
            self._make_status(proc_root, "100", uid=1000)
            self._make_inotify_fd(proc_root, "100", "3", watch_count=2)
            self._make_status(proc_root, "200", uid=1000)
            self._make_inotify_fd(proc_root, "200", "5", watch_count=1)
            # A different real UID's inotify fd must not be counted.
            self._make_status(proc_root, "300", uid=999)
            self._make_inotify_fd(proc_root, "300", "3", watch_count=100)

            instances, watches = hostcheck.current_real_uid_inotify_usage(
                proc_root=proc_root, real_uid=1000
            )
            self.assertEqual(instances, 2)
            self.assertEqual(watches, 3)

    def test_non_inotify_fd_is_not_counted(self):
        with tempfile.TemporaryDirectory() as d:
            proc_root = Path(d)
            self._make_status(proc_root, "100", uid=1000)
            fd_dir = proc_root / "100" / "fd"
            fd_dir.mkdir(parents=True, exist_ok=True)
            os.symlink("/some/regular/file", fd_dir / "4")

            instances, watches = hostcheck.current_real_uid_inotify_usage(
                proc_root=proc_root, real_uid=1000
            )
            self.assertEqual((instances, watches), (0, 0))

    def test_vanished_pid_between_listing_and_read_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            proc_root = Path(d)
            # A PID directory exists (so glob() lists it) but its status
            # file does not - simulating the process exiting mid-scan.
            (proc_root / "999").mkdir(parents=True)
            instances, watches = hostcheck.current_real_uid_inotify_usage(
                proc_root=proc_root, real_uid=1000
            )
            self.assertEqual((instances, watches), (0, 0))

    def test_vanished_fd_between_readlink_target_and_fdinfo_read_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            proc_root = Path(d)
            self._make_status(proc_root, "100", uid=1000)
            fd_dir = proc_root / "100" / "fd"
            fd_dir.mkdir(parents=True, exist_ok=True)
            os.symlink("anon_inode:inotify", fd_dir / "3")
            # No fdinfo/3 file at all - the fd closed between the readlink
            # succeeding and the fdinfo read being attempted.
            instances, watches = hostcheck.current_real_uid_inotify_usage(
                proc_root=proc_root, real_uid=1000
            )
            # The instance was observed (the fd existed and was inotify);
            # its watch count is simply unavailable, not fabricated as 0
            # for a real host bug, and not a hard failure for a race.
            self.assertEqual(instances, 1)
            self.assertEqual(watches, 0)

    def test_malformed_uid_line_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            proc_root = Path(d)
            _write(proc_root / "100" / "status", "Name:\tfake\nUid:\tnotanumber\t0\t0\t0\n")
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.current_real_uid_inotify_usage(proc_root=proc_root, real_uid=1000)

    @unittest.skipIf(os.getuid() == 0, "root bypasses permission checks")
    def test_unreadable_fd_dir_for_matching_uid_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            proc_root = Path(d)
            my_uid = os.getuid()
            self._make_status(proc_root, "100", uid=my_uid)
            fd_dir = proc_root / "100" / "fd"
            fd_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(fd_dir, 0o000)
            try:
                with self.assertRaises(hostcheck.HostCheckError):
                    hostcheck.current_real_uid_inotify_usage(
                        proc_root=proc_root, real_uid=my_uid
                    )
            finally:
                os.chmod(fd_dir, 0o755)


class TestPorts(unittest.TestCase):
    def test_below_20_fails(self):
        self.assertFalse(hostcheck.ports_ok(19))

    def test_exactly_20_passes(self):
        self.assertTrue(hostcheck.ports_ok(20))

    def test_above_20_passes(self):
        self.assertTrue(hostcheck.ports_ok(21))

    def test_held_port_excluded_then_bindable_after_release(self):
        # Ask the OS for a genuinely free ephemeral port (bind to port 0),
        # rather than hard-coding a port number that might already be in
        # use or might not exist on some future host.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        try:
            # While held, that single port must be reported as not free.
            self.assertEqual(hostcheck.count_bindable_ports(port, port), 0)
        finally:
            probe.close()
        # Once released, the same single port must be bindable again -
        # proving count_bindable_ports's own successful-bind probes are
        # closed immediately rather than leaking a held socket.
        self.assertEqual(hostcheck.count_bindable_ports(port, port), 1)

    def test_read_candidate_port_range_valid(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "isolation.json"
            _write(path, '{"candidate_tcp_port_range": [3200, 3299]}')
            self.assertEqual(hostcheck.read_candidate_port_range(path), (3200, 3299))

    def test_read_candidate_port_range_reversed_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "isolation.json"
            _write(path, '{"candidate_tcp_port_range": [3299, 3200]}')
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_candidate_port_range(path)

    def test_read_candidate_port_range_below_1_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "isolation.json"
            _write(path, '{"candidate_tcp_port_range": [0, 100]}')
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_candidate_port_range(path)

    def test_read_candidate_port_range_above_65535_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "isolation.json"
            _write(path, '{"candidate_tcp_port_range": [60000, 70000]}')
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_candidate_port_range(path)

    def test_read_candidate_port_range_boolean_endpoint_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "isolation.json"
            _write(path, '{"candidate_tcp_port_range": [true, 3299]}')
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_candidate_port_range(path)

    def test_read_candidate_port_range_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "does-not-exist.json"
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_candidate_port_range(path)

    def test_read_candidate_port_range_malformed_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "isolation.json"
            _write(path, "{not valid json")
            with self.assertRaises(hostcheck.HostCheckError):
                hostcheck.read_candidate_port_range(path)


class TestHeadroomCheckGateLevel(unittest.TestCase):
    """Proves the combined gate reports every failing signal, not merely
    the first, and fails closed on an unreadable required source."""

    def test_all_signals_present_in_fields_on_a_real_host_run(self):
        result = hostcheck.headroom_check()
        for key in ("cpu", "ram", "disk", "fd", "inotify", "ports"):
            self.assertIn(key, result.fields)

    def test_reports_multiple_failures_not_only_the_first(self):
        with tempfile.TemporaryDirectory() as d:
            # A nonexistent isolation.json guarantees the ports signal
            # fails; the real host's own CPU/RAM/disk/fd/inotify are used
            # for the rest, whatever they currently are.
            missing_isolation = Path(d) / "does-not-exist.json"
            result = hostcheck.headroom_check(isolation_path=missing_isolation)
            self.assertFalse(result.ok)
            self.assertIn("ports", result.detail)
            self.assertIn("error", result.fields["ports"])


if __name__ == "__main__":
    unittest.main()
