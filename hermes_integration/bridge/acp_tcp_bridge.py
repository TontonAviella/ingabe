"""TCP-to-stdio bridge for hermes-acp.

`hermes-acp` speaks the Agent Client Protocol over stdin/stdout (its
docstring at acp_adapter/entry.py says: "configures logging to write to
stderr (so stdout is reserved for ACP JSON-RPC transport)").

Mundi-app and the hermes-gateway run in separate containers, so stdio-
pipe access is friction. This bridge exposes hermes-acp over a docker-
internal TCP port: each inbound TCP connection forks a fresh hermes-acp
subprocess and wires stdin/stdout to the socket. ACP messages flow
bidirectionally; stderr is dropped to /dev/null (gateway has its own
log captures).

WHY pure asyncio (not socat)
─────────────────────────────
socat would do the same job in a one-liner. Choosing Python here so:
  - no new apt package in the image (socat isn't installed)
  - inherits the same hermes-acp environment + venv as the gateway
  - error paths logged with our own structure
  - clean cancellation when the gateway shuts down

WHY one subprocess per connection
─────────────────────────────────
`hermes-acp` is single-session per process — it speaks one ACP
conversation at a time. Spawning per connection isolates failures and
gives each mundi-app chat turn a clean state machine. Cost: ~300ms
subprocess spawn per turn, which is dwarfed by LLM latency (10-60s).

SECURITY
────────
- Listens on 0.0.0.0:9999 but the docker compose service has NO `ports:`
  block — the port is only reachable from inside the `mundiai_default`
  docker network. UFW on the host blocks external 9999.
- No auth between mundi-app and bridge — both run in the same trust
  domain (this docker host). If we ever expose the bridge externally,
  add mTLS or shared-secret framing in the JSON-RPC envelope.

INVOCATION
──────────
  python -m hermes_integration.bridge.acp_tcp_bridge
  # or with custom port:
  ACP_BRIDGE_PORT=9999 python -m hermes_integration.bridge.acp_tcp_bridge
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any, Optional

logger = logging.getLogger("acp_tcp_bridge")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
logger.addHandler(handler)

ACP_BIN = os.environ.get("ACP_BIN", "/app/.venv/bin/hermes-acp")
PORT = int(os.environ.get("ACP_BRIDGE_PORT", "9999"))
HOST = os.environ.get("ACP_BRIDGE_HOST", "0.0.0.0")


async def _pump(
    src: asyncio.StreamReader,
    dst: asyncio.StreamWriter,
    direction: str,
    conn_id: int,
) -> None:
    """Copy bytes from src to dst until EOF. Logs + closes dst on exit."""
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionResetError, BrokenPipeError) as e:
        logger.info("conn=%d %s: peer closed (%s)", conn_id, direction, e.__class__.__name__)
    except Exception:
        logger.exception("conn=%d %s: unexpected pump failure", conn_id, direction)
    finally:
        try:
            dst.close()
        except Exception:
            pass


_next_conn_id = 0


STDERR_LOG_DIR = os.environ.get("ACP_BRIDGE_STDERR_DIR", "/tmp/hermes-acp-stderr")


async def _handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Per-connection handler: spawn hermes-acp, pump bytes both ways."""
    global _next_conn_id
    _next_conn_id += 1
    conn_id = _next_conn_id
    peer = writer.get_extra_info("peername")
    logger.info("conn=%d open from %s — spawning %s", conn_id, peer, ACP_BIN)

    # Capture hermes-acp's stderr to a per-connection log file. The old
    # `stderr=DEVNULL` made every Hermes-side failure invisible — we
    # caught the empty-prompt bug (2026-05-15) only by spawning hermes-acp
    # standalone outside the bridge. Per-connection files keep races
    # between concurrent connections from clobbering each other.
    try:
        os.makedirs(STDERR_LOG_DIR, exist_ok=True)
        stderr_path = os.path.join(STDERR_LOG_DIR, f"conn-{conn_id}.log")
        stderr_fp: Any = open(stderr_path, "ab", buffering=0)
    except OSError:
        # If we can't open the log file (perms, full disk), fall back to
        # DEVNULL rather than crashing the bridge. Connections still work.
        logger.exception("conn=%d failed to open stderr log; using DEVNULL", conn_id)
        stderr_fp = asyncio.subprocess.DEVNULL
        stderr_path = "<devnull>"

    try:
        proc = await asyncio.create_subprocess_exec(
            ACP_BIN,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_fp,
        )
    except FileNotFoundError:
        logger.error("conn=%d hermes-acp binary not found at %s", conn_id, ACP_BIN)
        writer.close()
        if hasattr(stderr_fp, "close"):
            stderr_fp.close()
        return

    logger.info("conn=%d stderr → %s", conn_id, stderr_path)

    if proc.stdin is None or proc.stdout is None:
        logger.error("conn=%d subprocess pipes are None — bailing", conn_id)
        proc.kill()
        writer.close()
        return

    # Bidirectional pump:
    #   socket reader  → subprocess stdin   (client → agent)
    #   subprocess stdout → socket writer   (agent → client)
    to_agent = asyncio.create_task(
        _pump(reader, proc.stdin, "client→agent", conn_id)
    )
    from_agent = asyncio.create_task(
        _pump(proc.stdout, writer, "agent→client", conn_id)
    )

    # Wait for either direction to close. When client disconnects, kill
    # the subprocess. When subprocess exits, close the socket.
    done, pending = await asyncio.wait(
        [to_agent, from_agent], return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()

    if proc.returncode is None:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("conn=%d subprocess didn't terminate cleanly; killing", conn_id)
            proc.kill()
            await proc.wait()

    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

    # Close the stderr log file so fd doesn't leak between connections.
    if hasattr(stderr_fp, "close"):
        try:
            stderr_fp.close()
        except Exception:
            pass

    logger.info("conn=%d closed (rc=%s)", conn_id, proc.returncode)


async def main() -> None:
    server = await asyncio.start_server(_handle_client, HOST, PORT)
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets)
    logger.info("ACP TCP bridge listening on %s (bin=%s)", sockets, ACP_BIN)

    # Graceful shutdown on SIGTERM (compose `down` sends SIGTERM)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig_name), stop.set)
        except NotImplementedError:
            pass  # Windows fallback, not relevant in our container

    async with server:
        serve_task = asyncio.create_task(server.serve_forever())
        await stop.wait()
        logger.info("shutdown signal received; closing server")
        server.close()
        await server.wait_closed()
        serve_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
