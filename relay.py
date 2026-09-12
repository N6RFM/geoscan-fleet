#!/usr/bin/env python3
"""
Persistent KISS relay.

Runs forever (independent of any satellite pass). For each satellite it
opens two listening TCP ports:

  - producer_port: the currently-running per-satellite .grc connects INTO
    this as a TCP client (blocks_socket_pdu, TCP_CLIENT) and pushes KISS
    frame bytes.
  - consumer_port: your downstream decoder/dashboard connects INTO this
    (same port it already expects) and receives whatever the relay
    forwards. This socket never closes just because a pass ends - only
    the producer side comes and goes.

Run this once, alongside run_passes.py, before any pass starts.
"""

import asyncio
import yaml

CONFIG_PATH = "satellites.yaml"


class SatRelay:
    def __init__(self, name):
        self.name = name
        self.consumers = set()  # set of asyncio.StreamWriter

    async def handle_producer(self, reader, writer):
        peer = writer.get_extra_info("peername")
        print(f"[{self.name}] producer connected: {peer}")
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                dead = set()
                for w in self.consumers:
                    try:
                        w.write(data)
                        await w.drain()
                    except (ConnectionResetError, BrokenPipeError):
                        dead.add(w)
                self.consumers -= dead
        finally:
            print(f"[{self.name}] producer disconnected: {peer}")
            writer.close()

    async def handle_consumer(self, reader, writer):
        peer = writer.get_extra_info("peername")
        print(f"[{self.name}] consumer connected: {peer}")
        self.consumers.add(writer)
        try:
            while not reader.at_eof():
                await reader.read(4096)  # discard anything the consumer sends
        finally:
            print(f"[{self.name}] consumer disconnected: {peer}")
            self.consumers.discard(writer)
            writer.close()


async def main():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    servers = []
    for sat_cfg in cfg["satellites"]:
        relay = SatRelay(sat_cfg["name"])
        prod_srv = await asyncio.start_server(
            relay.handle_producer, "127.0.0.1", sat_cfg["producer_port"])
        cons_srv = await asyncio.start_server(
            relay.handle_consumer, "127.0.0.1", sat_cfg["consumer_port"])
        servers += [prod_srv, cons_srv]
        print(f"[{sat_cfg['name']}] producer :{sat_cfg['producer_port']}  "
              f"consumer :{sat_cfg['consumer_port']}")

    async with servers[0]:
        await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
