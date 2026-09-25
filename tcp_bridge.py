#!/usr/bin/env python3
"""
tcp_bridge.py - the mirror-image of relay.py, for a satellite whose flowgraph
runs its own TCP_SERVER instead of connecting out as a TCP_CLIENT.

relay.py can only ever listen on both sides - every satellite that uses
it has a flowgraph that connects OUT to it as a client. Some flowgraphs
do the opposite: they run their own TCP_SERVER and wait for a specific
downstream app (an SSDV image viewer, say) to connect IN. That's fine
while the flowgraph is running, but the flowgraph is short-lived - it
only exists during a pass - so without something in between, the
downstream app has no stable endpoint to connect to between passes, and
has to detect the drop and reconnect at the start of every single one.

tcp_bridge.py solves that the same way relay.py solves it for the opposite
direction: it connects OUT to the flowgraph's TCP_SERVER as a client
(retrying patiently whenever the flowgraph isn't running - i.e. between
passes), while LISTENING persistently for the real downstream consumer.
The consumer gets one stable address to point at, for the life of a
session, exactly like a SatsDecoder tab does with relay.py.

One-directional only (flowgraph -> downstream consumer) - this is for a
data stream like SSDV frames, not a two-way control channel. Doesn't
parse or care what's inside the bytes, same as relay.py.

Configured per satellite via an extra_outputs entry with its own
dedicated protocol, tcp_bridge:

    extra_outputs:
      - name: ssdv_viewer
        protocol: tcp_bridge
        block: network_socket_pdu_0
        port: 9985          # the flowgraph's own TCP_SERVER - tcp_bridge.py
                             # connects out to this as a client
        bridge_port: 19985   # what the real downstream consumer (the
                             # SSDV Viewer app) should actually connect to

Usage:
    python3 tcp_bridge.py [--verbose]
"""

import argparse
import asyncio
import sys
import yaml

CONFIG_PATH = "satellites.yaml"
RETRY_DELAY_S = 2


class Bridge:
    def __init__(self, name, upstream_host, upstream_port, verbose=False):
        self.name = name
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.verbose = verbose
        self.downstream_writers = []

    def log(self, msg):
        print(f"[{self.name}] {msg}", flush=True)

    def vlog(self, msg):
        if self.verbose:
            self.log(msg)

    async def handle_downstream(self, reader, writer):
        peer = writer.get_extra_info("peername")
        self.log(f"downstream consumer connected: {peer}")
        self.downstream_writers.append(writer)
        try:
            while not reader.at_eof():
                await reader.read(65536)  # consumer never sends us anything meaningful
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.log(f"downstream consumer disconnected: {peer}")
            if writer in self.downstream_writers:
                self.downstream_writers.remove(writer)
            writer.close()

    async def pump_upstream(self):
        """Connects out to the flowgraph's TCP_SERVER, retrying patiently
        whenever it's not there (i.e. between passes), and forwards
        whatever arrives to every currently-connected downstream writer."""
        while True:
            try:
                self.vlog(f"connecting upstream to {self.upstream_host}:{self.upstream_port} ...")
                reader, writer = await asyncio.open_connection(
                    self.upstream_host, self.upstream_port)
                self.log(f"connected upstream to {self.upstream_host}:{self.upstream_port}")
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    dead = []
                    for w in self.downstream_writers:
                        try:
                            w.write(data)
                            await w.drain()
                        except (ConnectionResetError, BrokenPipeError):
                            dead.append(w)
                    for w in dead:
                        self.downstream_writers.remove(w)
                self.log("upstream connection closed (flowgraph likely exited at LOS) - "
                         "will keep retrying")
            except (ConnectionRefusedError, OSError) as e:
                self.vlog(f"upstream not available yet ({e}) - retrying in {RETRY_DELAY_S}s")
            await asyncio.sleep(RETRY_DELAY_S)


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    tasks = []
    any_bridged = False
    for sat in cfg.get("satellites", []):
        for extra in sat.get("extra_outputs", []):
            if extra.get("protocol") != "tcp_bridge":
                continue
            any_bridged = True
            bridge = Bridge(f"{sat['name']}:{extra['name']}",
                             "127.0.0.1", extra["port"], verbose=args.verbose)
            server = await asyncio.start_server(
                bridge.handle_downstream, "127.0.0.1", extra["bridge_port"])
            print(f"[{bridge.name}] listening on 127.0.0.1:{extra['bridge_port']} "
                  f"for downstream consumers, bridging to the flowgraph's own "
                  f"127.0.0.1:{extra['port']}", flush=True)
            tasks.append(asyncio.create_task(bridge.pump_upstream()))
            tasks.append(asyncio.create_task(server.serve_forever()))

    if not any_bridged:
        print("No satellites configured with a tcp_bridge extra_output - "
              "nothing to do.", flush=True)
        return

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
