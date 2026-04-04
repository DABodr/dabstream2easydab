from __future__ import annotations

import errno
import ipaddress
import os
import select
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import BinaryIO, Callable, Optional

from .toolchain import Toolchain, ToolchainError


APP_NAME = "dabstream2easydab"
DEFAULT_RECONNECT_DELAY = 3.0
CHUNK_SIZE = 16 * 1024
ZMTP_GREETING_PREFIX = b"\xff\x00\x00\x00\x00\x00\x00\x00\x01\x7f"
DEFAULT_OUTPUT_PROFILE = "normal"
DEFAULT_STABILIZED_RATE_BPS = 288_000
MIN_STABILIZED_RATE_BPS = 192_000
MAX_STABILIZED_RATE_BPS = 384_000
STABILIZED_PREBUFFER_SECONDS = 3.0
STABILIZED_LOW_WATERMARK_SECONDS = 1.0
DEFAULT_SOURCE_IDLE_TIMEOUT = 8.0
DEFAULT_INITIAL_SOURCE_IDLE_TIMEOUT = 15.0
HTTP_IO_TIMEOUT_SECONDS = 5.0

LogCallback = Callable[[str], None]


class ConfigurationError(ValueError):
    """Raised when the source or listen configuration is invalid."""


@dataclass(frozen=True)
class SourceSpec:
    mode: str
    scheme: str
    host: Optional[str] = None
    port: Optional[int] = None
    url: Optional[str] = None


@dataclass(frozen=True)
class SessionConfig:
    source_mode: str
    output_mode: str
    source_uri: str
    listen_host: str
    listen_port: int
    output_profile: str = DEFAULT_OUTPUT_PROFILE


@dataclass(frozen=True)
class SessionStats:
    state: str = "Stopped"
    client_count: int = 0
    bytes_from_source: int = 0
    last_error: str = ""
    recognized_source_type: str = ""
    last_data_at: float = 0.0


def describe_source(mode: str, uri: str) -> str:
    spec = parse_source_uri(mode, uri)
    if spec.mode == "auto" and spec.scheme == "tcp":
        return "TCP stream (auto-detect)"
    if spec.mode == "eti" and spec.scheme == "zmq+tcp":
        return "ETI ZeroMQ"
    if spec.mode == "edi" and spec.scheme == "udp":
        return "EDI UDP"
    if spec.mode == "edi" and spec.scheme == "tcp":
        return "EDI TCP"
    if spec.mode == "eti" and spec.scheme == "http":
        return "ETI HTTP"
    if spec.mode == "eti" and spec.scheme == "https":
        return "ETI HTTPS"
    return "ETI"


def parse_source_uri(mode: str, uri: str) -> SourceSpec:
    mode = mode.strip().lower()
    cleaned_uri = uri.strip()
    if not cleaned_uri:
        raise ConfigurationError("The stream address is empty.")
    if mode not in {"auto", "eti", "edi"}:
        raise ConfigurationError(f"Unknown source mode: {mode}")

    if "://" not in cleaned_uri:
        default_scheme = _guess_default_scheme(mode, cleaned_uri)
        cleaned_uri = f"{default_scheme}://{cleaned_uri}"

    try:
        parsed = urllib.parse.urlparse(cleaned_uri)
    except ValueError as exc:
        raise ConfigurationError(f"Invalid address: {uri}") from exc
    scheme = parsed.scheme.lower()

    if mode == "auto":
        if scheme == "udp":
            host, port = _parse_host_port(parsed, cleaned_uri)
            return SourceSpec(mode="edi", scheme=scheme, host=host, port=port)
        if scheme in {"http", "https"}:
            return SourceSpec(mode="eti", scheme=scheme, url=cleaned_uri)
        if scheme in {"zmq", "zmq+tcp"}:
            host, port = _parse_host_port(parsed, cleaned_uri)
            return SourceSpec(mode="eti", scheme="zmq+tcp", host=host, port=port)
        if scheme == "tcp":
            host, port = _parse_host_port(parsed, cleaned_uri)
            return SourceSpec(mode="auto", scheme=scheme, host=host, port=port)
        raise ConfigurationError(
            "An auto source must use tcp://host:port, zmq+tcp://host:port, udp://host:port or http(s)://..."
        )

    if mode == "eti":
        if scheme in {"tcp", "zmq", "zmq+tcp"}:
            host, port = _parse_host_port(parsed, cleaned_uri)
            normalized_scheme = "zmq+tcp" if scheme in {"zmq", "zmq+tcp"} else scheme
            return SourceSpec(mode=mode, scheme=normalized_scheme, host=host, port=port)
        if scheme in {"http", "https"}:
            return SourceSpec(mode=mode, scheme=scheme, url=cleaned_uri)
        raise ConfigurationError(
            "An ETI source must use tcp://host:port, zmq+tcp://host:port or http(s)://..."
        )

    if scheme not in {"udp", "tcp"}:
        raise ConfigurationError("An EDI source must use udp://host:port or tcp://host:port")
    host, port = _parse_host_port(parsed, cleaned_uri)
    return SourceSpec(mode=mode, scheme=scheme, host=host, port=port)


def validate_listen_config(host: str, port: int) -> tuple[str, int]:
    cleaned_host = host.strip() or "0.0.0.0"
    if cleaned_host == "*":
        cleaned_host = "0.0.0.0"
    if port < 0 or port > 65535:
        raise ConfigurationError("The listen port must be between 0 and 65535.")
    return cleaned_host, port


def validate_output_mode(mode: str) -> str:
    cleaned_mode = mode.strip().lower()
    if cleaned_mode not in {"tcp", "zmq"}:
        raise ConfigurationError(f"Unknown output mode: {mode}")
    return cleaned_mode


def validate_output_profile(profile: str) -> str:
    cleaned_profile = profile.strip().lower()
    if cleaned_profile not in {"normal", "stabilized"}:
        raise ConfigurationError(f"Unknown output profile: {profile}")
    return cleaned_profile


def _parse_host_port(parsed: urllib.parse.ParseResult, original_uri: str) -> tuple[str, int]:
    if not parsed.hostname or parsed.port is None:
        raise ConfigurationError(f"Invalid address: {original_uri}")
    return parsed.hostname, parsed.port


def _guess_edi_scheme(uri: str) -> str:
    host_part = uri.rsplit(":", 1)[0].strip("[]")
    try:
        ip = ipaddress.ip_address(host_part)
    except ValueError:
        return "tcp"
    return "udp" if ip.is_multicast else "tcp"


def _guess_default_scheme(mode: str, uri: str) -> str:
    if mode == "eti":
        return "tcp"
    if mode == "edi":
        return _guess_edi_scheme(uri)
    return _guess_edi_scheme(uri)


def build_zmq_endpoint(host: str, port: int) -> str:
    bind_host = "*" if host in {"0.0.0.0", ""} else host
    return f"zmq+tcp://{bind_host}:{port}"


def allocate_local_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _edi_log_confirms_stream(message: str) -> bool:
    return any(
        pattern in message
        for pattern in (
            "Initialise next pseq",
            "EDI AF Packet initial sequence number",
            "AF Packet initial sequence number",
            "EDI-AF:",
        )
    )


def _looks_like_edi_af_packet(chunk: bytes) -> bool:
    return len(chunk) >= 2 and chunk[:2] == b"AF"


def _looks_like_zmtp_greeting(chunk: bytes) -> bool:
    return len(chunk) >= len(ZMTP_GREETING_PREFIX) and chunk.startswith(ZMTP_GREETING_PREFIX)


def _require_zmq_module():
    try:
        import zmq
    except ModuleNotFoundError as exc:
        raise ConfigurationError(
            "ETI ZeroMQ input support requires python3-zmq (pyzmq)."
        ) from exc
    return zmq


class RelayServer:
    def __init__(self, host: str, port: int, logger: LogCallback):
        self.host = host
        self.port = port
        self.logger = logger
        self._server_socket: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._running = threading.Event()

    def start(self) -> None:
        if self._running.is_set():
            return
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(4)
        self._server_socket.settimeout(1.0)
        self.port = self._server_socket.getsockname()[1]
        self._running.set()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="relay-accept",
            daemon=True,
        )
        self._accept_thread.start()
        self.logger(f"Local ETI server listening on {self.host}:{self.port}")

    def stop(self) -> None:
        self._running.clear()
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=1.5)
            self._accept_thread = None
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def broadcast(self, chunk: bytes) -> None:
        stale_clients: list[socket.socket] = []
        with self._lock:
            for client in self._clients:
                try:
                    client.sendall(chunk)
                except OSError:
                    stale_clients.append(client)
            for stale in stale_clients:
                try:
                    stale.close()
                except OSError:
                    pass
                if stale in self._clients:
                    self._clients.remove(stale)
        for _client in stale_clients:
            self.logger("EasyDAB client disconnected")

    def _accept_loop(self) -> None:
        while self._running.is_set():
            try:
                assert self._server_socket is not None
                client, address = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._clients.append(client)
            self.logger(f"EasyDAB client connected from {address[0]}:{address[1]}")


class ZmqEtiBridge:
    def __init__(self, host: str, port: int, logger: LogCallback, eti2zmq_path: str):
        self.host = host
        self.port = port
        self.logger = logger
        self.eti2zmq_path = eti2zmq_path
        self.endpoint = build_zmq_endpoint(host, port)
        self._tempdir: Optional[str] = None
        self._fifo_path: Optional[str] = None
        self._fifo_writer: Optional[BinaryIO] = None
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._stderr_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.is_running():
            return
        self._tempdir = tempfile.mkdtemp(prefix="dabstream2easydab-")
        self._fifo_path = os.path.join(self._tempdir, "eti.pipe")
        os.mkfifo(self._fifo_path)
        command = [self.eti2zmq_path, "-i", self._fifo_path, "-o", self.endpoint]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._process = process
        self._stderr_thread = threading.Thread(
            target=self._capture_logs,
            args=(process,),
            name="eti2zmq-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        self._fifo_writer = self._open_fifo_writer(process)
        self.logger(f"ETI -> ZeroMQ bridge active on {self.endpoint}")

    def stop(self) -> None:
        writer = self._fifo_writer
        self._fifo_writer = None
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1.0)
            self._stderr_thread = None
        fifo_path = self._fifo_path
        self._fifo_path = None
        if fifo_path is not None:
            try:
                os.unlink(fifo_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        tempdir = self._tempdir
        self._tempdir = None
        if tempdir is not None:
            try:
                os.rmdir(tempdir)
            except OSError:
                pass

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None and self._fifo_writer is not None

    def broadcast(self, chunk: bytes) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            raise RuntimeError("The eti2zmq bridge is no longer running.")
        writer = self._fifo_writer
        if writer is None:
            raise RuntimeError("The eti2zmq FIFO is not available.")
        try:
            writer.write(chunk)
            writer.flush()
        except BrokenPipeError as exc:
            raise RuntimeError("The eti2zmq bridge closed its FIFO.") from exc
        except OSError as exc:
            raise RuntimeError(f"Unable to write to eti2zmq: {exc}") from exc

    def _open_fifo_writer(self, process: subprocess.Popen[bytes]) -> BinaryIO:
        assert self._fifo_path is not None
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"eti2zmq stopped with exit code {process.returncode}")
            try:
                fd = os.open(self._fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    time.sleep(0.1)
                    continue
                raise RuntimeError(f"Unable to open the eti2zmq FIFO: {exc}") from exc
            return os.fdopen(fd, "wb", buffering=0)
        raise RuntimeError("eti2zmq did not open the FIFO in time.")

    def _capture_logs(self, process: subprocess.Popen[bytes]) -> None:
        if process.stderr is None:
            return
        for raw_line in iter(process.stderr.readline, b""):
            if not raw_line:
                break
            message = raw_line.decode("utf-8", errors="replace").strip()
            if message:
                self.logger(f"eti2zmq: {message}")


class StabilizedOutputBuffer:
    def __init__(
        self,
        target,
        logger: LogCallback,
        state_callback: Callable[[str], None],
        *,
        default_rate_bps: int = DEFAULT_STABILIZED_RATE_BPS,
        prebuffer_seconds: float = STABILIZED_PREBUFFER_SECONDS,
        low_watermark_seconds: float = STABILIZED_LOW_WATERMARK_SECONDS,
        minimum_prebuffer_bytes: int = 32 * 1024,
        minimum_low_watermark_bytes: int = 8 * 1024,
    ):
        self.target = target
        self.logger = logger
        self.state_callback = state_callback
        self.default_rate_bps = default_rate_bps
        self.prebuffer_seconds = prebuffer_seconds
        self.low_watermark_seconds = low_watermark_seconds
        self.minimum_prebuffer_bytes = minimum_prebuffer_bytes
        self.minimum_low_watermark_bytes = minimum_low_watermark_bytes
        self._condition = threading.Condition()
        self._buffer = bytearray()
        self._sender_thread: Optional[threading.Thread] = None
        self._running = False
        self._sending = False
        self._bytes_in = 0
        self._bytes_out = 0
        self._first_input_at: Optional[float] = None
        self._last_input_at: Optional[float] = None
        self._send_started_at: Optional[float] = None
        self._buffering_logged = False
        self._active_logged = False

    def start(self) -> None:
        with self._condition:
            if self._running:
                return
            self._running = True
            self._buffer.clear()
            self._sending = False
            self._bytes_in = 0
            self._bytes_out = 0
            self._first_input_at = None
            self._last_input_at = None
            self._send_started_at = None
            self._buffering_logged = False
            self._active_logged = False
            self._sender_thread = threading.Thread(
                target=self._sender_loop,
                name="stabilized-output",
                daemon=True,
            )
            self._sender_thread.start()
        self.logger(
            "Stabilized output enabled with a 3.0 s prebuffer and smoother ETI pacing."
        )

    def stop(self) -> None:
        sender_thread = None
        with self._condition:
            self._running = False
            self._buffer.clear()
            self._sending = False
            self._bytes_out = 0
            self._send_started_at = None
            self._condition.notify_all()
            sender_thread = self._sender_thread
            self._sender_thread = None
        if sender_thread is not None:
            sender_thread.join(timeout=1.5)

    def broadcast(self, chunk: bytes) -> None:
        now = time.monotonic()
        with self._condition:
            if not self._running:
                raise RuntimeError("The stabilized output buffer is not running.")
            self._buffer.extend(chunk)
            self._bytes_in += len(chunk)
            if self._first_input_at is None:
                self._first_input_at = now
            self._last_input_at = now
            self._condition.notify()

    def _sender_loop(self) -> None:
        while True:
            with self._condition:
                while self._running and not self._buffer:
                    self._condition.wait(timeout=0.1)
                if not self._running:
                    return

                rate_bps = self._estimated_rate_bps()
                prebuffer_bytes = max(
                    self.minimum_prebuffer_bytes,
                    int(rate_bps * self.prebuffer_seconds),
                )
                low_watermark_bytes = max(
                    self.minimum_low_watermark_bytes,
                    int(rate_bps * self.low_watermark_seconds),
                )

                if not self._sending:
                    if len(self._buffer) < prebuffer_bytes:
                        if not self._buffering_logged:
                            self._buffering_logged = True
                            self._active_logged = False
                            self.state_callback("Stabilized output prebuffering")
                            self.logger(
                                "Stabilized output is prebuffering before forwarding ETI."
                            )
                        self._condition.wait(timeout=0.05)
                        continue
                    self._sending = True
                    self._bytes_out = 0
                    self._send_started_at = time.monotonic()
                    if not self._active_logged:
                        self._active_logged = True
                        self._buffering_logged = False
                        self.state_callback("Stabilized output active")
                        self.logger("Stabilized output is now forwarding ETI.")

                if len(self._buffer) < low_watermark_bytes:
                    self._sending = False
                    self._bytes_out = 0
                    self._send_started_at = None
                    if not self._buffering_logged:
                        self._buffering_logged = True
                        self._active_logged = False
                        self.state_callback("Stabilized output rebuffering")
                        self.logger(
                            "Stabilized output buffer dropped too low, rebuilding reserve."
                        )
                    continue

                assert self._send_started_at is not None
                allowed_bytes = int(
                    (time.monotonic() - self._send_started_at) * rate_bps
                ) - self._bytes_out
                if allowed_bytes <= 0:
                    self._condition.wait(timeout=0.01)
                    continue

                send_size = min(len(self._buffer), allowed_bytes, CHUNK_SIZE)
                if send_size <= 0:
                    continue
                chunk = bytes(self._buffer[:send_size])
                del self._buffer[:send_size]
                self._bytes_out += send_size

            self.target.broadcast(chunk)

    def _estimated_rate_bps(self) -> int:
        if (
            self._first_input_at is None
            or self._last_input_at is None
            or self._last_input_at - self._first_input_at < 1.0
        ):
            return self.default_rate_bps
        observed_rate = int(
            self._bytes_in / max(self._last_input_at - self._first_input_at, 1.0)
        )
        return max(
            MIN_STABILIZED_RATE_BPS,
            min(MAX_STABILIZED_RATE_BPS, observed_rate),
        )


class StreamSession:
    def __init__(
        self,
        config: SessionConfig,
        logger: LogCallback,
        toolchain: Toolchain | None = None,
        reconnect_delay: float = DEFAULT_RECONNECT_DELAY,
        source_idle_timeout: float = DEFAULT_SOURCE_IDLE_TIMEOUT,
        initial_source_idle_timeout: float = DEFAULT_INITIAL_SOURCE_IDLE_TIMEOUT,
    ):
        self.config = config
        self.logger = logger
        self.reconnect_delay = reconnect_delay
        self.source_idle_timeout = source_idle_timeout
        self.initial_source_idle_timeout = initial_source_idle_timeout
        listen_host, listen_port = validate_listen_config(
            config.listen_host, config.listen_port
        )
        output_mode = validate_output_mode(config.output_mode)
        output_profile = validate_output_profile(config.output_profile)
        self.config = replace(
            config,
            output_mode=output_mode,
            listen_host=listen_host,
            listen_port=listen_port,
            output_profile=output_profile,
        )
        self.source_spec = parse_source_uri(config.source_mode, config.source_uri)
        self.toolchain = toolchain or Toolchain.discover()
        try:
            if self.source_spec.mode == "edi":
                self.toolchain.require("edi2eti")
                if self.source_spec.scheme == "tcp":
                    self.toolchain.require("odr-edi2edi")
            if self.source_spec.mode == "auto" and self.source_spec.scheme == "tcp":
                self.toolchain.require("odr-edi2edi")
                self.toolchain.require("edi2eti")
            if output_mode == "zmq":
                self.toolchain.require("eti2zmq")
        except ToolchainError as exc:
            raise ConfigurationError(str(exc)) from exc

        self.output_mode = output_mode
        self.output_profile = output_profile
        self.relay: Optional[RelayServer]
        self.zmq_bridge: Optional[ZmqEtiBridge]
        if output_mode == "tcp":
            self.relay = RelayServer(listen_host, listen_port, logger)
            self.zmq_bridge = None
        else:
            self.relay = None
            self.zmq_bridge = ZmqEtiBridge(
                listen_host,
                listen_port,
                logger,
                eti2zmq_path=self.toolchain.command("eti2zmq"),
            )
        self.output_buffer = (
            StabilizedOutputBuffer(
                self.relay if self.relay is not None else self.zmq_bridge,
                logger,
                self._set_state,
            )
            if output_profile == "stabilized"
            and (self.relay is not None or self.zmq_bridge is not None)
            else None
        )
        self._source_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._stats = SessionStats()
        self._stats_lock = threading.Lock()
        self._resource_lock = threading.Lock()
        self._active_socket: Optional[socket.socket] = None
        self._active_zmq_socket = None
        self._active_response = None
        self._active_process: Optional[subprocess.Popen[bytes]] = None
        self._active_aux_process: Optional[subprocess.Popen[bytes]] = None
        self._source_attempt_started_at = 0.0

    @property
    def listen_port(self) -> int:
        if self.relay is not None:
            return self.relay.port
        if self.zmq_bridge is not None:
            return self.zmq_bridge.port
        return self.config.listen_port

    @property
    def output_endpoint(self) -> str:
        if self.output_mode == "tcp":
            return f"tcp://{self.config.listen_host}:{self.listen_port}"
        return build_zmq_endpoint(self.config.listen_host, self.listen_port)

    def start(self) -> None:
        if self._source_thread and self._source_thread.is_alive():
            return
        self._stop_event.clear()
        self._set_recognized_type("")
        if self.relay is not None:
            self.relay.start()
            self._set_state(
                f"Waiting for EasyDAB clients on {self.config.listen_host}:{self.listen_port}"
            )
        elif self.zmq_bridge is not None:
            self.zmq_bridge.start()
            self._set_state(f"ZeroMQ output ready on {self.output_endpoint}")
        else:
            self._set_state(f"ZeroMQ output target {self.output_endpoint}")
        if self.output_buffer is not None:
            self.output_buffer.start()
        self._source_thread = threading.Thread(
            target=self._source_loop,
            name="source-loop",
            daemon=True,
        )
        self._source_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._close_active_resources()
        if self.output_buffer is not None:
            self.output_buffer.stop()
        if self.relay is not None:
            self.relay.stop()
        if self.zmq_bridge is not None:
            self.zmq_bridge.stop()
        if self._source_thread is not None:
            self._source_thread.join(timeout=2.0)
            self._source_thread = None
        self._set_recognized_type("")
        self._set_state("Stopped")

    def snapshot(self) -> SessionStats:
        with self._stats_lock:
            return replace(self._stats, client_count=self._client_count())

    def _source_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._begin_source_attempt()
                if self.zmq_bridge is not None and not self.zmq_bridge.is_running():
                    self.zmq_bridge.start()
                if self.source_spec.mode == "auto":
                    self._run_auto_tcp_source()
                elif self.source_spec.mode == "eti":
                    if self.source_spec.scheme == "tcp":
                        self._run_eti_tcp_source()
                    elif self.source_spec.scheme == "zmq+tcp":
                        self._run_eti_zmq_source()
                    else:
                        self._run_eti_http_source()
                else:
                    if self.source_spec.scheme == "tcp":
                        self._run_edi_tcp_bridge()
                    elif self.output_mode == "zmq":
                        self._run_edi_source_to_zmq()
                    else:
                        self._run_edi_source_to_tcp()
            except ConfigurationError as exc:
                self._set_error(str(exc))
                self._set_state("Configuration error")
                self.logger(str(exc))
                return
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                message = str(exc) or exc.__class__.__name__
                self._set_error(message)
                self._set_state(
                    f"Source error, retrying in {self.reconnect_delay:.0f}s"
                )
                self.logger(f"Source error: {message}")
                if self.zmq_bridge is not None:
                    self.zmq_bridge.stop()
                self._stop_event.wait(self.reconnect_delay)

    def _begin_source_attempt(self) -> None:
        self._source_attempt_started_at = time.monotonic()

    def _note_source_activity(self) -> None:
        with self._stats_lock:
            self._stats = replace(
                self._stats,
                last_data_at=time.monotonic(),
                last_error="",
                client_count=self._client_count(),
            )

    def _raise_if_source_stalled(self) -> None:
        now = time.monotonic()
        with self._stats_lock:
            last_data_at = self._stats.last_data_at
        if last_data_at >= self._source_attempt_started_at > 0:
            idle_for = now - last_data_at
            timeout = self.source_idle_timeout
        else:
            idle_for = now - self._source_attempt_started_at
            timeout = self.initial_source_idle_timeout
        if idle_for >= timeout:
            raise ConnectionError(
                f"No data received for {idle_for:.0f}s, forcing source reconnect."
            )

    def _run_eti_tcp_source(self) -> None:
        host = self.source_spec.host
        port = self.source_spec.port
        assert host is not None and port is not None
        self._set_state(f"Connecting ETI TCP to {host}:{port}")
        use_zmq = False
        with socket.create_connection((host, port), timeout=10.0) as sock:
            sock.settimeout(2.0)
            self._set_active_socket(sock)
            self.logger(f"ETI TCP source connected to {host}:{port}")
            try:
                first_chunk = self._recv_first_chunk(sock)
                if _looks_like_edi_af_packet(first_chunk):
                    raise ConnectionError(
                        "The TCP stream looks like EDI TCP. Use Auto or EDI mode."
                    )
                if _looks_like_zmtp_greeting(first_chunk):
                    use_zmq = True
                    self._set_recognized_type("ETI ZeroMQ")
                    self.logger(
                        f"TCP source recognized as an ETI ZeroMQ endpoint on {host}:{port}"
                    )
                    self._set_state(f"Switching to ETI ZeroMQ from {host}:{port}")
                    return
                self.logger(f"TCP source recognized as ETI on {host}:{port}")
                self._set_state(f"ETI stream active from {host}:{port}")
                self._set_recognized_type("ETI")
                self._broadcast(first_chunk)
                self._add_bytes(len(first_chunk))
                while not self._stop_event.is_set():
                    try:
                        chunk = sock.recv(CHUNK_SIZE)
                    except socket.timeout:
                        self._raise_if_source_stalled()
                        continue
                    if not chunk:
                        raise ConnectionError("The ETI source closed the connection.")
                    self._set_recognized_type("ETI")
                    self._broadcast(chunk)
                    self._add_bytes(len(chunk))
            finally:
                self._clear_active_socket(sock)
        if use_zmq:
            self._run_eti_zmq_source()

    def _run_auto_tcp_source(self) -> None:
        host = self.source_spec.host
        port = self.source_spec.port
        assert host is not None and port is not None
        self._set_state(f"Connecting TCP auto to {host}:{port}")
        recognized_as_edi = False
        recognized_as_zmq = False
        with socket.create_connection((host, port), timeout=10.0) as sock:
            sock.settimeout(2.0)
            self._set_active_socket(sock)
            self.logger(f"TCP source connected to {host}:{port} for detection")
            try:
                first_chunk = self._recv_first_chunk(sock)
                if _looks_like_edi_af_packet(first_chunk):
                    recognized_as_edi = True
                    self._set_recognized_type("EDI TCP")
                    self.logger(f"TCP source recognized as EDI TCP on {host}:{port}")
                elif _looks_like_zmtp_greeting(first_chunk):
                    recognized_as_zmq = True
                    self._set_recognized_type("ETI ZeroMQ")
                    self.logger(
                        f"TCP source recognized as an ETI ZeroMQ endpoint on {host}:{port}"
                    )
                else:
                    self._set_recognized_type("ETI")
                    self.logger(f"TCP source recognized as ETI on {host}:{port}")
                    self._set_state(f"ETI stream active from {host}:{port}")
                    self._broadcast(first_chunk)
                    self._add_bytes(len(first_chunk))
                    while not self._stop_event.is_set():
                        try:
                            chunk = sock.recv(CHUNK_SIZE)
                        except socket.timeout:
                            self._raise_if_source_stalled()
                            continue
                        if not chunk:
                            raise ConnectionError("The ETI source closed the connection.")
                        self._set_recognized_type("ETI")
                        self._broadcast(chunk)
                        self._add_bytes(len(chunk))
                    return
            finally:
                self._clear_active_socket(sock)
        if recognized_as_zmq:
            self._run_eti_zmq_source()
            return
        if recognized_as_edi:
            self._run_edi_tcp_bridge()
            return
        raise ConnectionError("The TCP stream type could not be determined.")

    def _run_eti_zmq_source(self) -> None:
        host = self.source_spec.host
        port = self.source_spec.port
        assert host is not None and port is not None
        zmq = _require_zmq_module()
        endpoint = f"tcp://{host}:{port}"
        self._set_state(f"Connecting ETI ZeroMQ to {host}:{port}")
        socket_in = zmq.Context.instance().socket(zmq.SUB)
        socket_in.setsockopt(zmq.SUBSCRIBE, b"")
        socket_in.setsockopt(zmq.LINGER, 0)
        socket_in.setsockopt(zmq.RCVTIMEO, 1000)
        self._set_active_zmq_socket(socket_in)
        warned_multipart = False
        try:
            socket_in.connect(endpoint)
            self.logger(f"Subscribed to ETI ZeroMQ source on zmq+tcp://{host}:{port}")
            self._set_state(f"ETI ZeroMQ stream active from {host}:{port}")
            while not self._stop_event.is_set():
                try:
                    frames = socket_in.recv_multipart()
                except zmq.Again:
                    self._raise_if_source_stalled()
                    continue
                except zmq.ZMQError as exc:
                    if self._stop_event.is_set():
                        break
                    raise ConnectionError(f"Unable to read from ZeroMQ: {exc}") from exc
                if not frames:
                    continue
                if len(frames) > 1 and not warned_multipart:
                    warned_multipart = True
                    self.logger(
                        f"ZeroMQ source delivers {len(frames)} frames per message; they will be concatenated."
                    )
                chunk = b"".join(frames)
                if not chunk:
                    continue
                self._set_recognized_type("ETI ZeroMQ")
                self._broadcast(chunk)
                self._add_bytes(len(chunk))
        finally:
            self._clear_active_zmq_socket(socket_in)
            try:
                socket_in.close()
            except Exception:
                pass

    def _run_eti_http_source(self) -> None:
        url = self.source_spec.url
        assert url is not None
        request = urllib.request.Request(
            url,
            headers={"User-Agent": APP_NAME},
        )
        self._set_state(f"Connecting ETI HTTP to {url}")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_IO_TIMEOUT_SECONDS) as response:
                self._set_active_response(response)
                status = getattr(response, "status", "200")
                self.logger(f"ETI HTTP source opened ({status})")
                self._set_state(f"ETI stream active from {url}")
                try:
                    while not self._stop_event.is_set():
                        try:
                            chunk = response.read(CHUNK_SIZE)
                        except TimeoutError:
                            self._raise_if_source_stalled()
                            continue
                        except socket.timeout:
                            self._raise_if_source_stalled()
                            continue
                        if not chunk:
                            raise ConnectionError("The ETI HTTP source ended.")
                        self._set_recognized_type("ETI")
                        self._broadcast(chunk)
                        self._add_bytes(len(chunk))
                finally:
                    self._clear_active_response(response)
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Unable to reach the HTTP source: {exc}") from exc

    def _run_edi_source_to_tcp(self) -> None:
        host = self.source_spec.host
        port = self.source_spec.port
        assert host is not None and port is not None
        self._run_edi_udp_converter_to_tcp(host, port, recognized_type="EDI UDP")

    def _run_edi_udp_converter_to_tcp(
        self,
        host: str,
        port: int,
        recognized_type: str,
    ) -> None:
        command = [self.toolchain.command("edi2eti"), f"{host}:{port}"]
        self._set_state(f"Starting edi2eti for {host}:{port}")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._set_active_process(process)
        stderr_thread = threading.Thread(
            target=self._capture_process_logs,
            args=(process, "edi2eti", "stderr", recognized_type),
            name="edi2eti-stderr",
            daemon=True,
        )
        stderr_thread.start()
        self.logger(f"Started edi2eti to receive EDI UDP from {host}:{port}")
        self._set_state(f"EDI UDP stream active via edi2eti from {host}:{port}")
        try:
            assert process.stdout is not None
            while not self._stop_event.is_set():
                chunk = self._read_process_stdout_chunk(process, timeout=1.0)
                if chunk:
                    self._set_recognized_type(recognized_type)
                    self._broadcast(chunk)
                    self._add_bytes(len(chunk))
                    continue
                if process.poll() is not None:
                    raise RuntimeError(f"edi2eti stopped with exit code {process.returncode}")
                self._raise_if_source_stalled()
        finally:
            self._clear_active_process(process)
            self._terminate_process(process)

    def _run_edi_source_to_zmq(self) -> None:
        host = self.source_spec.host
        port = self.source_spec.port
        assert host is not None and port is not None
        self._run_edi_udp_converter_to_tcp(host, port, recognized_type="EDI UDP")

    def _run_edi_udp_converter_to_zmq(
        self,
        host: str,
        port: int,
        recognized_type: str,
    ) -> None:
        endpoint = self.output_endpoint
        command = [self.toolchain.command("edi2eti"), "-L", "-o", endpoint, f"{host}:{port}"]
        self._set_state(f"Starting edi2eti to {endpoint}")
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._set_active_process(process)
        stderr_thread = threading.Thread(
            target=self._capture_process_logs,
            args=(process, "edi2eti", "stderr", recognized_type),
            name="edi2eti-stderr",
            daemon=True,
        )
        stderr_thread.start()
        self.logger(f"Started edi2eti to receive EDI UDP from {host}:{port}")
        self.logger(f"ZeroMQ output published on {endpoint}")
        self._set_state(f"EDI UDP stream active via edi2eti to {endpoint}")
        try:
            while not self._stop_event.is_set():
                if process.poll() is not None:
                    raise RuntimeError(f"edi2eti stopped with exit code {process.returncode}")
                time.sleep(0.2)
        finally:
            self._clear_active_process(process)
            self._terminate_process(process)

    def _run_edi_tcp_bridge(self) -> None:
        remote_host = self.source_spec.host
        remote_port = self.source_spec.port
        assert remote_host is not None and remote_port is not None
        local_udp_port = allocate_local_udp_port()
        command = [
            self.toolchain.command("odr-edi2edi"),
            "-P",
            "-c",
            f"{remote_host}:{remote_port}",
            "-d",
            "127.0.0.1",
            "-p",
            str(local_udp_port),
        ]
        self._set_state(
            f"Connecting EDI TCP to {remote_host}:{remote_port}, then relaying to local UDP {local_udp_port}"
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        self._set_active_aux_process(process)
        stdout_thread = threading.Thread(
            target=self._capture_process_logs,
            args=(process, "odr-edi2edi", "stdout", "EDI TCP"),
            name="odr-edi2edi-stdout",
            daemon=True,
        )
        stdout_thread.start()
        self.logger(
            f"Started odr-edi2edi to relay EDI TCP {remote_host}:{remote_port} to 127.0.0.1:{local_udp_port}"
        )
        try:
            time.sleep(0.5)
            if process.poll() is not None:
                raise RuntimeError(
                    f"odr-edi2edi stopped with exit code {process.returncode}"
                )
            if self.output_mode == "zmq":
                self._run_edi_udp_converter_to_tcp(
                    "127.0.0.1",
                    local_udp_port,
                    recognized_type="EDI TCP",
                )
            else:
                self._run_edi_udp_converter_to_tcp(
                    "127.0.0.1",
                    local_udp_port,
                    recognized_type="EDI TCP",
                )
        finally:
            self._clear_active_aux_process(process)
            self._terminate_process(process)

    def _capture_process_logs(
        self,
        process: subprocess.Popen[bytes],
        prefix: str = "edi2eti",
        stream_name: str = "stderr",
        recognized_type: str = "",
    ) -> None:
        stream = process.stderr if stream_name == "stderr" else process.stdout
        if stream is None:
            return
        for raw_line in iter(stream.readline, b""):
            if not raw_line:
                break
            message = raw_line.decode("utf-8", errors="replace").strip()
            if message:
                if recognized_type and _edi_log_confirms_stream(message):
                    self._set_recognized_type(recognized_type)
                    self._note_source_activity()
                self.logger(f"{prefix}: {message}")

    def _broadcast(self, chunk: bytes) -> None:
        if self.output_buffer is not None:
            self.output_buffer.broadcast(chunk)
            return
        if self.relay is not None:
            self.relay.broadcast(chunk)
            return
        if self.zmq_bridge is not None:
            self.zmq_bridge.broadcast(chunk)
            return
        raise RuntimeError("No ETI output is available.")

    def _client_count(self) -> int:
        if self.relay is None:
            return 0
        return self.relay.client_count()

    def _set_state(self, state: str) -> None:
        with self._stats_lock:
            self._stats = replace(
                self._stats,
                state=state,
                client_count=self._client_count(),
            )

    def _set_error(self, error: str) -> None:
        with self._stats_lock:
            self._stats = replace(
                self._stats,
                last_error=error,
                client_count=self._client_count(),
            )

    def _add_bytes(self, count: int) -> None:
        with self._stats_lock:
            self._stats = replace(
                self._stats,
                bytes_from_source=self._stats.bytes_from_source + count,
                last_data_at=time.monotonic(),
                last_error="",
                client_count=self._client_count(),
            )

    def _set_recognized_type(self, recognized_type: str) -> None:
        with self._stats_lock:
            self._stats = replace(
                self._stats,
                recognized_source_type=recognized_type,
                client_count=self._client_count(),
            )

    def _set_active_socket(self, sock: socket.socket) -> None:
        with self._resource_lock:
            self._active_socket = sock

    def _clear_active_socket(self, sock: socket.socket) -> None:
        with self._resource_lock:
            if self._active_socket is sock:
                self._active_socket = None

    def _set_active_response(self, response) -> None:
        with self._resource_lock:
            self._active_response = response

    def _set_active_zmq_socket(self, socket_in) -> None:
        with self._resource_lock:
            self._active_zmq_socket = socket_in

    def _clear_active_response(self, response) -> None:
        with self._resource_lock:
            if self._active_response is response:
                self._active_response = None

    def _clear_active_zmq_socket(self, socket_in) -> None:
        with self._resource_lock:
            if self._active_zmq_socket is socket_in:
                self._active_zmq_socket = None

    def _set_active_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._resource_lock:
            self._active_process = process

    def _clear_active_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._resource_lock:
            if self._active_process is process:
                self._active_process = None

    def _set_active_aux_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._resource_lock:
            self._active_aux_process = process

    def _clear_active_aux_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._resource_lock:
            if self._active_aux_process is process:
                self._active_aux_process = None

    def _close_active_resources(self) -> None:
        with self._resource_lock:
            sock = self._active_socket
            zmq_socket = self._active_zmq_socket
            response = self._active_response
            process = self._active_process
            aux_process = self._active_aux_process
            self._active_socket = None
            self._active_zmq_socket = None
            self._active_response = None
            self._active_process = None
            self._active_aux_process = None
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if zmq_socket is not None:
            # ZeroMQ sockets are closed from their own reader thread to avoid
            # libzmq assertions when another thread interrupts recv().
            pass
        if process is not None:
            self._terminate_process(process)
        if aux_process is not None:
            self._terminate_process(aux_process)

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()

    def _recv_first_chunk(self, sock: socket.socket) -> bytes:
        while not self._stop_event.is_set():
            try:
                chunk = sock.recv(CHUNK_SIZE)
            except socket.timeout:
                self._raise_if_source_stalled()
                continue
            if not chunk:
                raise ConnectionError("The TCP source closed the connection.")
            return chunk
        raise ConnectionError("Source read interrupted.")

    def _read_process_stdout_chunk(
        self,
        process: subprocess.Popen[bytes],
        *,
        timeout: float,
    ) -> bytes:
        assert process.stdout is not None
        ready, _writable, _errors = select.select([process.stdout], [], [], timeout)
        if not ready:
            return b""
        return process.stdout.read(CHUNK_SIZE)
