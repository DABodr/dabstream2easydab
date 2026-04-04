from __future__ import annotations

import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from queue import Queue

try:
    import zmq
except ModuleNotFoundError:  # pragma: no cover - optional dependency in tests
    zmq = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dabstream2easydab.session import (
    SessionConfig,
    StabilizedOutputBuffer,
    StreamSession,
    build_zmq_endpoint,
    describe_source,
    parse_source_uri,
)


class ParseSourceUriTests(unittest.TestCase):
    def test_parse_eti_without_scheme_defaults_to_tcp(self) -> None:
        spec = parse_source_uri("eti", "example.com:9000")
        self.assertEqual(spec.mode, "eti")
        self.assertEqual(spec.scheme, "tcp")
        self.assertEqual(spec.host, "example.com")
        self.assertEqual(spec.port, 9000)

    def test_parse_auto_hostname_without_scheme_defaults_to_auto_tcp(self) -> None:
        spec = parse_source_uri("auto", "edi-source.example.net:8101")
        self.assertEqual(spec.mode, "auto")
        self.assertEqual(spec.scheme, "tcp")
        self.assertEqual(spec.host, "edi-source.example.net")
        self.assertEqual(spec.port, 8101)

    def test_parse_edi_without_scheme_defaults_to_udp(self) -> None:
        spec = parse_source_uri("edi", "239.255.0.1:9000")
        self.assertEqual(spec.mode, "edi")
        self.assertEqual(spec.scheme, "udp")
        self.assertEqual(spec.host, "239.255.0.1")
        self.assertEqual(spec.port, 9000)

    def test_parse_edi_hostname_without_scheme_defaults_to_tcp(self) -> None:
        spec = parse_source_uri("edi", "edi-source.example.net:8101")
        self.assertEqual(spec.mode, "edi")
        self.assertEqual(spec.scheme, "tcp")
        self.assertEqual(spec.host, "edi-source.example.net")
        self.assertEqual(spec.port, 8101)

    def test_build_zmq_endpoint_uses_wildcard_for_any_address(self) -> None:
        endpoint = build_zmq_endpoint("0.0.0.0", 18081)
        self.assertEqual(endpoint, "zmq+tcp://*:18081")

    def test_parse_eti_zmq_source(self) -> None:
        spec = parse_source_uri("eti", "zmq+tcp://127.0.0.1:18081")
        self.assertEqual(spec.mode, "eti")
        self.assertEqual(spec.scheme, "zmq+tcp")
        self.assertEqual(spec.host, "127.0.0.1")
        self.assertEqual(spec.port, 18081)

    def test_describe_source_distinguishes_edi_and_eti(self) -> None:
        self.assertEqual(describe_source("edi", "239.255.0.1:9000"), "EDI UDP")
        self.assertEqual(describe_source("edi", "edi-source.example.net:8101"), "EDI TCP")
        self.assertEqual(describe_source("eti", "tcp://provider.example:18081"), "ETI")
        self.assertEqual(
            describe_source("eti", "zmq+tcp://provider.example:18081"),
            "ETI ZeroMQ",
        )
        self.assertEqual(
            describe_source("auto", "edi-source.example.net:8101"),
            "TCP stream (auto-detect)",
        )


class RelaySessionTests(unittest.TestCase):
    def test_tcp_source_is_forwarded_to_local_clients(self) -> None:
        payload = b"ETI_FRAME" * 256
        start_sending = threading.Event()
        upstream_port_queue: Queue[int] = Queue()

        def upstream_server() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", 0))
                server.listen(1)
                upstream_port_queue.put(server.getsockname()[1])
                conn, _addr = server.accept()
                with conn:
                    start_sending.wait(timeout=2.0)
                    conn.sendall(payload)
                    time.sleep(0.1)

        server_thread = threading.Thread(target=upstream_server, daemon=True)
        server_thread.start()

        upstream_port = upstream_port_queue.get(timeout=2.0)
        logs: list[str] = []
        session = StreamSession(
            SessionConfig(
                source_mode="eti",
                output_mode="tcp",
                source_uri=f"tcp://127.0.0.1:{upstream_port}",
                listen_host="127.0.0.1",
                listen_port=0,
            ),
            logger=logs.append,
            reconnect_delay=0.2,
        )
        session.start()

        deadline = time.time() + 2.0
        while session.listen_port == 0 and time.time() < deadline:
            time.sleep(0.01)
        self.assertNotEqual(session.listen_port, 0)

        deadline = time.time() + 2.0
        while not any("ETI TCP source connected" in line for line in logs) and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(any("ETI TCP source connected" in line for line in logs))

        received = bytearray()
        with socket.create_connection(("127.0.0.1", session.listen_port), timeout=2.0) as client:
            deadline = time.time() + 2.0
            while session.snapshot().client_count < 1 and time.time() < deadline:
                time.sleep(0.01)
            start_sending.set()
            client.settimeout(2.0)
            while len(received) < len(payload):
                chunk = client.recv(4096)
                if not chunk:
                    break
                received.extend(chunk)

        deadline = time.time() + 2.0
        stats = session.snapshot()
        while stats.bytes_from_source < len(payload) and time.time() < deadline:
            time.sleep(0.05)
            stats = session.snapshot()
        session.stop()
        self.assertEqual(bytes(received), payload)
        self.assertGreaterEqual(stats.bytes_from_source, len(payload))
        self.assertEqual(stats.recognized_source_type, "ETI")

    def test_tcp_source_reconnects_after_silent_stall(self) -> None:
        payload1 = b"ETI_FIRST" * 128
        payload2 = b"ETI_SECOND" * 128
        upstream_port_queue: Queue[int] = Queue()

        def upstream_server() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", 0))
                server.listen(2)
                upstream_port_queue.put(server.getsockname()[1])

                conn1, _addr1 = server.accept()
                with conn1:
                    conn1.settimeout(0.2)
                    conn1.sendall(payload1)
                    deadline = time.time() + 5.0
                    while time.time() < deadline:
                        try:
                            chunk = conn1.recv(1024)
                        except socket.timeout:
                            continue
                        except ConnectionResetError:
                            break
                        if not chunk:
                            break

                conn2, _addr2 = server.accept()
                with conn2:
                    conn2.sendall(payload2)
                    time.sleep(0.1)

        server_thread = threading.Thread(target=upstream_server, daemon=True)
        server_thread.start()

        upstream_port = upstream_port_queue.get(timeout=2.0)
        logs: list[str] = []
        session = StreamSession(
            SessionConfig(
                source_mode="eti",
                output_mode="tcp",
                source_uri=f"tcp://127.0.0.1:{upstream_port}",
                listen_host="127.0.0.1",
                listen_port=0,
            ),
            logger=logs.append,
            reconnect_delay=0.2,
            source_idle_timeout=0.6,
            initial_source_idle_timeout=1.5,
        )
        session.start()

        try:
            deadline = time.time() + 2.0
            while session.listen_port == 0 and time.time() < deadline:
                time.sleep(0.01)
            self.assertNotEqual(session.listen_port, 0)

            received = bytearray()
            with socket.create_connection(("127.0.0.1", session.listen_port), timeout=2.0) as client:
                deadline = time.time() + 4.0
                while session.snapshot().client_count < 1 and time.time() < deadline:
                    time.sleep(0.01)
                client.settimeout(0.5)
                target_size = len(payload1) + len(payload2)
                deadline = time.time() + 5.0
                while len(received) < target_size and time.time() < deadline:
                    try:
                        chunk = client.recv(4096)
                    except TimeoutError:
                        continue
                    if not chunk:
                        break
                    received.extend(chunk)

            deadline = time.time() + 3.0
            stats = session.snapshot()
            while stats.bytes_from_source < len(payload1) + len(payload2) and time.time() < deadline:
                time.sleep(0.05)
                stats = session.snapshot()

            self.assertGreaterEqual(stats.bytes_from_source, len(payload1) + len(payload2))
            self.assertIn(payload2, received)
            self.assertTrue(
                any("forcing source reconnect" in line.lower() for line in logs),
                msg=str(logs),
            )
        finally:
            session.stop()

    @unittest.skipUnless(zmq is not None, "pyzmq is required for ZeroMQ source tests")
    def test_auto_tcp_detects_zmq_source_and_relays_payload(self) -> None:
        payload = b"ETI_ZMQ_FRAME" * 128
        context = zmq.Context.instance()
        publisher = context.socket(zmq.PUB)
        publisher.setsockopt(zmq.LINGER, 0)
        publisher.bind("tcp://127.0.0.1:0")
        endpoint = publisher.getsockopt_string(zmq.LAST_ENDPOINT)
        port = int(endpoint.rsplit(":", 1)[1])

        logs: list[str] = []
        session = StreamSession(
            SessionConfig(
                source_mode="auto",
                output_mode="tcp",
                source_uri=f"127.0.0.1:{port}",
                listen_host="127.0.0.1",
                listen_port=0,
            ),
            logger=logs.append,
            reconnect_delay=0.2,
        )
        session.start()

        try:
            deadline = time.time() + 2.0
            while session.listen_port == 0 and time.time() < deadline:
                time.sleep(0.01)
            self.assertNotEqual(session.listen_port, 0)

            deadline = time.time() + 2.0
            while (
                "ETI ZeroMQ" not in session.snapshot().recognized_source_type
                and time.time() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(session.snapshot().recognized_source_type, "ETI ZeroMQ")

            with socket.create_connection(("127.0.0.1", session.listen_port), timeout=2.0) as client:
                client.settimeout(2.0)
                time.sleep(0.3)
                for _ in range(20):
                    publisher.send(payload)
                    time.sleep(0.05)
                received = client.recv(len(payload) * 2)

            deadline = time.time() + 2.0
            stats = session.snapshot()
            while stats.bytes_from_source < len(payload) and time.time() < deadline:
                time.sleep(0.05)
                stats = session.snapshot()

            self.assertGreaterEqual(len(received), len(payload))
            self.assertTrue(received.startswith(payload))
            self.assertGreaterEqual(stats.bytes_from_source, len(payload))
            self.assertEqual(stats.recognized_source_type, "ETI ZeroMQ")
            self.assertTrue(any("ZeroMQ" in line for line in logs))
        finally:
            session.stop()
            publisher.close()


class _MemoryOutputTarget:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self._lock = threading.Lock()

    def broadcast(self, chunk: bytes) -> None:
        with self._lock:
            self.chunks.append(chunk)

    def data(self) -> bytes:
        with self._lock:
            return b"".join(self.chunks)


class StabilizedOutputBufferTests(unittest.TestCase):
    def test_stabilized_output_waits_for_prebuffer_then_forwards(self) -> None:
        target = _MemoryOutputTarget()
        logs: list[str] = []
        states: list[str] = []
        buffer = StabilizedOutputBuffer(
            target,
            logger=logs.append,
            state_callback=states.append,
            default_rate_bps=640,
            prebuffer_seconds=0.1,
            low_watermark_seconds=0.01,
            minimum_prebuffer_bytes=64,
            minimum_low_watermark_bytes=1,
        )
        payload = (b"A" * 48) + (b"B" * 48)

        buffer.start()
        try:
            buffer.broadcast(payload[:48])
            time.sleep(0.05)
            self.assertEqual(target.data(), b"")

            buffer.broadcast(payload[48:])

            deadline = time.time() + 1.0
            while not target.data() and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(target.data())
            self.assertTrue(payload.startswith(target.data()))
            self.assertIn("Stabilized output prebuffering", states)
            self.assertIn("Stabilized output active", states)
        finally:
            buffer.stop()

    def test_stabilized_output_rebuffers_when_reserve_runs_low(self) -> None:
        target = _MemoryOutputTarget()
        logs: list[str] = []
        states: list[str] = []
        buffer = StabilizedOutputBuffer(
            target,
            logger=logs.append,
            state_callback=states.append,
            default_rate_bps=640,
            prebuffer_seconds=0.1,
            low_watermark_seconds=0.05,
            minimum_prebuffer_bytes=64,
            minimum_low_watermark_bytes=24,
        )

        buffer.start()
        try:
            buffer.broadcast(b"C" * 96)
            deadline = time.time() + 1.0
            while "Stabilized output active" not in states and time.time() < deadline:
                time.sleep(0.02)
            self.assertIn("Stabilized output active", states)

            deadline = time.time() + 1.0
            while "Stabilized output rebuffering" not in states and time.time() < deadline:
                time.sleep(0.02)
            self.assertIn("Stabilized output rebuffering", states)
        finally:
            buffer.stop()


if __name__ == "__main__":
    unittest.main()
