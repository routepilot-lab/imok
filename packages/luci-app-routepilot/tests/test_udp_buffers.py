from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import udp_buffers


class BufferTests(unittest.TestCase):
    def exercise(self, peer, buffers):
        libc = MagicMock()
        libc.syscall.side_effect = [10, 11]
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.getpeername.return_value = (peer, 443)
        sock.getsockopt.side_effect = [socket.SOCK_DGRAM] + buffers
        with (
            patch.object(udp_buffers.ctypes, "CDLL", return_value=libc),
            patch.object(udp_buffers.Path, "iterdir", return_value=[Path("/proc/42/fd/7")]),
            patch.object(udp_buffers.os, "readlink", return_value="socket:[123]"),
            patch.object(udp_buffers.socket, "socket", return_value=sock),
            patch.object(udp_buffers.os, "close") as close,
        ):
            result = udp_buffers.tune(42)
            close.assert_called_once_with(10)
        return sock, result

    def test_only_cloudflare_udp_socket_grows_without_sysctl_changes(self):
        sock, result = self.exercise("162.159.197.2", [212992, 212992, 4194304, 4194304])
        self.assertEqual(result[0]["after"], [4194304, 4194304])
        self.assertEqual(sock.setsockopt.call_count, 2)
        sock.setsockopt.assert_any_call(socket.SOL_SOCKET, 32, 2097152)

    def test_foreign_socket_is_not_changed(self):
        sock, result = self.exercise("223.5.5.5", [])
        sock.setsockopt.assert_not_called()
        self.assertEqual(result, [])

    def test_already_large_buffers_are_not_reduced_or_reset(self):
        sock, result = self.exercise("162.159.197.2", [8388608, 4194304])
        sock.setsockopt.assert_not_called()
        self.assertEqual(result, [])

    def test_missing_kernel_support_is_nonfatal(self):
        libc = MagicMock()
        libc.syscall.return_value = -1
        with patch.object(udp_buffers.ctypes, "CDLL", return_value=libc):
            self.assertEqual(udp_buffers.tune(42), [])


if __name__ == "__main__":
    unittest.main()
