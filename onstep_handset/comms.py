"""TCP client for OnStep's LX200 command channel.

OnStep accepts raw ASCII commands over TCP, each framed ``:CC...#``. Replies are
terminated by ``#`` (motion commands send no reply). This client keeps one
persistent socket open and transparently reconnects with exponential backoff +
jitter when the link drops -- important for a field device on flaky 2.4 GHz WiFi.

All socket I/O for the application is funnelled through a single instance living
on the comms thread, so the class itself does not need to be thread-safe.
"""

from __future__ import annotations

import logging
import socket
import time

log = logging.getLogger(__name__)

# Deterministic jitter sequence (Math.random is unavailable / undesirable here);
# small fractional offsets to avoid synchronised reconnect storms.
_JITTER = (0.13, 0.41, 0.07, 0.29, 0.37, 0.19, 0.03, 0.23)


class OnStepClient:
    """Persistent, auto-reconnecting LX200-over-TCP client."""

    def __init__(
        self,
        host: str | None,
        port: int = 9999,
        timeout: float = 4.0,
        backoff_min: float = 0.5,
        backoff_max: float = 8.0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max

        self._sock: socket.socket | None = None
        self._backoff = backoff_min
        self._attempt = 0

    # --- connection management ------------------------------------------

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> bool:
        """(Re)establish the socket. Returns True on success, False otherwise.

        On failure the caller should back off (see :meth:`sleep_backoff`).
        """
        self.close()
        if not self.host:
            return False
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.settimeout(self.timeout)
            _enable_keepalive(sock)
            self._sock = sock
            self._backoff = self.backoff_min
            self._attempt = 0
            log.info("connected to OnStep at %s:%s", self.host, self.port)
            return True
        except OSError as exc:
            log.warning("connect to %s:%s failed: %s", self.host, self.port, exc)
            self._sock = None
            return False

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def next_backoff(self) -> float:
        """Return the next backoff delay (seconds) and grow it (capped).

        Does NOT sleep -- the caller waits, so the wait can stay interruptible
        (e.g. a button press should open the menu even mid-backoff)."""
        jitter = _JITTER[self._attempt % len(_JITTER)] * self._backoff
        delay = min(self._backoff + jitter, self.backoff_max)
        self._attempt += 1
        self._backoff = min(self._backoff * 2, self.backoff_max)
        return delay

    def sleep_backoff(self) -> None:
        """Blocking backoff sleep (kept for callers that don't need interruption)."""
        time.sleep(self.next_backoff())

    # --- command I/O ----------------------------------------------------

    def send(self, command: str) -> None:
        """Send a no-reply command (e.g. a move/stop). Raises on disconnect."""
        self._write(command)

    def query(self, command: str) -> str:
        """Send a command and read the ``#``-terminated reply. Raises on disconnect.

        Discards any stale buffered bytes first -- some 'fire and forget' commands
        (e.g. ``:Td#`` -> ``1#``) do return a reply that ``send()`` doesn't read,
        and we must not let that leftover corrupt this query's response.
        """
        self._drain_input()
        self._write(command)
        return self._read_until_hash()

    # --- internals ------------------------------------------------------

    def _drain_input(self) -> None:
        """Non-blocking discard of any bytes already waiting on the socket."""
        if self._sock is None:
            return
        try:
            self._sock.setblocking(False)
            while True:
                if self._sock.recv(256) == b"":
                    break  # peer closed; let the next real read surface it
        except (BlockingIOError, InterruptedError):
            pass  # nothing (more) buffered -- normal
        except OSError:
            pass
        finally:
            if self._sock is not None:
                self._sock.settimeout(self.timeout)

    def _write(self, command: str) -> None:
        if self._sock is None:
            raise ConnectionError("not connected")
        try:
            self._sock.sendall(command.encode("ascii"))
        except OSError as exc:
            self.close()
            raise ConnectionError(f"write failed: {exc}") from exc

    def _read_until_hash(self, max_bytes: int = 256) -> str:
        if self._sock is None:
            raise ConnectionError("not connected")
        chunks: list[bytes] = []
        total = 0
        try:
            while total < max_bytes:
                b = self._sock.recv(1)
                if b == b"":
                    # Peer closed the connection cleanly.
                    self.close()
                    raise ConnectionError("peer closed connection")
                chunks.append(b)
                total += 1
                if b == b"#":
                    break
        except socket.timeout as exc:
            # A timeout mid-read means the link is unhealthy; force reconnect.
            self.close()
            raise ConnectionError(f"read timeout: {exc}") from exc
        except OSError as exc:
            self.close()
            raise ConnectionError(f"read failed: {exc}") from exc
        return b"".join(chunks).decode("ascii", errors="replace")


def _enable_keepalive(sock: socket.socket) -> None:
    """Enable TCP keepalive so half-open connections are detected reasonably fast.

    The per-idle/interval/count options are Linux-only; guarded so this also runs
    on a macOS dev box for testing against a mock server.
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt_name, value in (
        ("TCP_KEEPIDLE", 30),
        ("TCP_KEEPINTVL", 10),
        ("TCP_KEEPCNT", 3),
    ):
        opt = getattr(socket, opt_name, None)
        if opt is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError:
                pass
