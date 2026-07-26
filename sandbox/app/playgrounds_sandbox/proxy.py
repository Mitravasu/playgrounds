"""A narrow CONNECT proxy for manually requested public HTTPS pages."""

import asyncio
import ipaddress
import socket
from contextlib import suppress

MAX_REQUEST_BYTES = 16 * 1024


def _public_address(value: str) -> bool:
    """Return whether an address is globally routable public IP space."""

    address = ipaddress.ip_address(value)
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


async def resolve_public_host(hostname: str) -> list[str]:
    """Resolve and reject any hostname with a non-public answer."""

    normalized = hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise ValueError("literal IP destinations are not allowed")
    records = await asyncio.get_running_loop().getaddrinfo(normalized, 443, type=socket.SOCK_STREAM)
    addresses = sorted(
        {str(record[4][0]) for record in records},
        key=lambda address: (ipaddress.ip_address(address).version, address),
    )
    if not addresses or not all(_public_address(address) for address in addresses):
        raise ValueError("destination resolved to a non-public address")
    return addresses


async def _copy(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, direction: str
) -> None:
    """Relay one tunnel direction while recording bounded byte-flow diagnostics."""

    total_bytes = 0
    first_chunk = True
    try:
        while chunk := await reader.read(64 * 1024):
            total_bytes += len(chunk)
            if first_chunk:
                print(
                    f"proxy relay first-bytes direction={direction} bytes={len(chunk)}",
                    flush=True,
                )
                first_chunk = False
            writer.write(chunk)
            await writer.drain()
    finally:
        print(f"proxy relay closed direction={direction} bytes={total_bytes}", flush=True)
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def _reply(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()


def _connect_target(request: bytes) -> str:
    """Parse one exact HTTPS CONNECT request and return its approved host."""

    lines = request.decode("iso-8859-1").split("\r\n")
    method, target, version = lines[0].split(" ")
    if method != "CONNECT" or version != "HTTP/1.1" or target.count(":") != 1:
        raise ValueError("only HTTPS CONNECT requests are supported")
    hostname, port = target.rsplit(":", 1)
    if not hostname or port != "443":
        raise ValueError("only port 443 is allowed")
    return hostname


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Validate a tunnel destination, connect by validated IP, then relay it."""

    try:
        request = await reader.readuntil(b"\r\n\r\n")
        if len(request) > MAX_REQUEST_BYTES:
            raise ValueError("proxy request is too large")
        hostname = _connect_target(request)
        print(f"proxy CONNECT received host={hostname}", flush=True)
        addresses = await resolve_public_host(hostname)
        print(f"proxy DNS validated host={hostname} addresses={addresses}", flush=True)
        upstream_reader: asyncio.StreamReader | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        for address in addresses:
            try:
                print(f"proxy connecting host={hostname} address={address}:443", flush=True)
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(address, 443), timeout=3
                )
                print(f"proxy upstream connected host={hostname} address={address}:443", flush=True)
                break
            except (OSError, TimeoutError) as error:
                print(
                    f"proxy upstream failed host={hostname} address={address}:443 error={error}",
                    flush=True,
                )
                continue
        if upstream_reader is None or upstream_writer is None:
            raise OSError("unable to connect to a validated destination")
        await _reply(writer, "200 Connection Established")
        print(f"proxy CONNECT established host={hostname}", flush=True)
        await asyncio.gather(
            _copy(reader, upstream_writer, direction="browser-to-site"),
            _copy(upstream_reader, writer, direction="site-to-browser"),
            return_exceptions=True,
        )
        print(f"proxy tunnel closed host={hostname}", flush=True)
    except (ValueError, asyncio.IncompleteReadError, OSError, UnicodeDecodeError) as error:
        print(f"denying proxy request: {error}", flush=True)
        with suppress(Exception):
            await _reply(writer, "403 Forbidden")
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def main() -> None:
    """Serve the fixed proxy only on its analyzer-facing internal network."""

    server = await asyncio.start_server(handle_client, "0.0.0.0", 8080)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
