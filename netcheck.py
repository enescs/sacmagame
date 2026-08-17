"""Work out why two machines cannot see or reach each other.

Run it on the machine that cannot join, with the host's address:

    python netcheck.py 10.166.123.143

With no address it just listens for beacons and reports what it hears, which
is the quick way to tell whether discovery works at all.

Plain stdlib, no venv needed -- copy it to the other laptop and run it there.
"""

import json
import socket
import sys
import time

DISCOVERY_PORT = 50505
DISCOVERY_MAGIC = "sacma-lan-v1"
DEFAULT_PORT = 50500


def local_ipv4():
    """The address this machine would use to reach the rest of the network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets sent, just picks a route
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def subnet24(ip):
    return ip.rsplit(".", 1)[0] if ip else None


def check_tcp(host, port, timeout=4.0):
    """Report reachability. The failure mode matters more than the failure."""
    t0 = time.time()
    try:
        socket.create_connection((host, port), timeout).close()
        return "open", f"connected in {time.time() - t0:.2f}s"
    except socket.timeout:
        return "filtered", ("no reply at all -- a firewall or the network is "
                            "swallowing the packets")
    except ConnectionRefusedError:
        return "refused", ("reached the machine, but nothing is listening -- "
                           "the server is not running, or is on another port")
    except OSError as exc:
        return "error", str(exc)


def listen(seconds=6.0):
    """Collect discovery beacons, so we can say whether broadcast works."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as exc:
        print(f"  cannot listen on udp/{DISCOVERY_PORT}: {exc}")
        print("  (a game client is probably already open -- close it first)")
        return {}
    sock.settimeout(1.0)

    found = {}
    end = time.time() + seconds
    while time.time() < end:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            msg = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            continue
        if msg.get("magic") == DISCOVERY_MAGIC:
            found[addr[0]] = msg.get("name", "?")
    sock.close()
    return found


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else None
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT

    mine = local_ipv4()
    print(f"\nthis machine: {mine or 'no network address found'}")

    print(f"\nlistening for games for 6s ...")
    found = listen()
    if found:
        for ip, name in found.items():
            print(f"  heard '{name}' from {ip}")
    else:
        print("  heard nothing")

    if not host:
        print("\nrerun with the host's IP to test whether you can reach it:")
        print(f"  python {sys.argv[0]} 10.0.0.5\n")
        return

    print(f"\ntesting tcp {host}:{port} ...")
    state, detail = check_tcp(host, port)
    print(f"  {state}: {detail}")

    # The verdict is the combination: same subnet or not, heard or not,
    # reachable or not. Each pair points at a different fix.
    print("\nverdict:")
    same_subnet = mine and subnet24(mine) == subnet24(host)
    if same_subnet:
        print(f"  you and the host are both on {subnet24(mine)}.x -- same subnet")
    else:
        print(f"  you are on {subnet24(mine)}.x, the host is on "
              f"{subnet24(host)}.x -- DIFFERENT subnets")

    if state == "open" and host not in found:
        print("  the host is reachable, so you CAN play -- press M in the menu")
        print(f"  and type {host}:{port}. It just will not show in the list,")
        print("  because discovery broadcasts do not cross subnets. The host")
        print(f"  can fix the list with: --announce {subnet24(mine)}.0/24")
    elif state == "open":
        print("  reachable and visible -- networking is fine, joining should work")
    elif state == "refused":
        print("  the network path is FINE. The server just is not running on")
        print("  that machine, or it is using a different port.")
    elif state == "filtered" and same_subnet:
        print("  same subnet but no reply: either the host's firewall is")
        print("  blocking the port, or this network isolates clients from each")
        print("  other (common on corporate and guest wifi). Try a phone")
        print("  hotspot -- if it works there, it is the office network.")
    elif state == "filtered":
        print("  different subnets and no route between them. Nothing in the")
        print("  game can fix this. Get everyone onto the same wifi, or onto")
        print("  a phone hotspot.")
    print()


if __name__ == "__main__":
    main()
