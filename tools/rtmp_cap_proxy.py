"""Disposable localhost TCP relay with an optional OBS-to-receiver byte cap."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path


class Relay:
    def __init__(self, target_host: str, target_port: int, bytes_per_second: int, stats_path: Path):
        self.target_host = target_host
        self.target_port = target_port
        self.bytes_per_second = bytes_per_second
        self.stats_path = stats_path
        self.bytes_forwarded = 0
        self.connections = 0
        self.started = time.monotonic()

    def write_stats(self) -> None:
        self.stats_path.write_text(json.dumps({
            "bytes_forwarded": self.bytes_forwarded,
            "connections": self.connections,
            "bytes_per_second": self.bytes_per_second,
            "elapsed_seconds": time.monotonic() - self.started,
        }, indent=2), encoding="utf-8")

    async def copy(self, source: asyncio.StreamReader, target: asyncio.StreamWriter, throttled: bool) -> None:
        started = time.monotonic()
        sent = 0
        while data := await source.read(64 * 1024):
            target.write(data)
            await target.drain()
            if throttled and self.bytes_per_second:
                sent += len(data)
                expected = sent / self.bytes_per_second
                delay = expected - (time.monotonic() - started)
                if delay > 0:
                    await asyncio.sleep(delay)
                self.bytes_forwarded += len(data)
                self.write_stats()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.write_stats()
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(self.target_host, self.target_port)
            await asyncio.gather(
                self.copy(reader, upstream_writer, True),
                self.copy(upstream_reader, writer, False),
            )
        finally:
            writer.close()
            await writer.wait_closed()
            if "upstream_writer" in locals():
                upstream_writer.close()
                await upstream_writer.wait_closed()


async def main(args: argparse.Namespace) -> None:
    relay = Relay(args.target_host, args.target_port, args.bytes_per_second, Path(args.stats))
    server = await asyncio.start_server(relay.handle, args.listen_host, args.listen_port)
    relay.write_stats()
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=19350)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=19351)
    parser.add_argument("--bytes-per-second", type=int, required=True)
    parser.add_argument("--stats", required=True)
    asyncio.run(main(parser.parse_args()))
