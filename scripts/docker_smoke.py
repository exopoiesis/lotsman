"""Smoke test against a running Lotsman container.

Run inside the container via `docker exec`, or from the host with port
forwarding. Exits 0 on success, 1 on any failure.

Usage:
    python scripts/docker_smoke.py [HOST] [PORT]

Default target: localhost:50051
"""

from __future__ import annotations

import sys
import time

import grpc

from lotsman.v1 import lotsman_pb2, lotsman_pb2_grpc


def main(argv: list[str]) -> int:
    host = argv[1] if len(argv) > 1 else "localhost"
    port = int(argv[2]) if len(argv) > 2 else 50051
    target = f"{host}:{port}"

    print(f"--> Connecting to {target}", flush=True)
    channel = grpc.insecure_channel(target)
    stub = lotsman_pb2_grpc.LotsmanServiceStub(channel)

    print("--> Whoami", flush=True)
    try:
        whoami = stub.Whoami(lotsman_pb2.WhoamiRequest(), timeout=5)
    except grpc.RpcError as e:
        print(f"FAIL: Whoami: {e}")
        return 1
    print(f"    lotsman_version={whoami.lotsman_version}, tool={whoami.tool!r}")

    print("--> Run echo", flush=True)
    run = stub.Run(lotsman_pb2.RunRequest(script="echo hello-from-container\n"))
    print(f"    jobId={run.job_id}, state={run.state}")

    print("--> Poll Status", flush=True)
    deadline = time.time() + 10
    final = None
    while time.time() < deadline:
        s = stub.Status(lotsman_pb2.StatusRequest(job_id=run.job_id))
        if s.state == lotsman_pb2.DONE:
            final = s
            break
        time.sleep(0.1)
    if final is None:
        print(f"FAIL: timeout waiting for DONE; last state={s.state}")
        return 1
    print(f"    final state DONE, exit_code={final.exit_code}")

    print("--> Logs", flush=True)
    logs = stub.Logs(lotsman_pb2.LogsRequest(job_id=run.job_id))
    stdout = logs.stdout.decode("utf-8")
    print(f"    stdout={stdout!r}")
    if stdout.strip() != "hello-from-container":
        print(f"FAIL: expected 'hello-from-container', got {stdout!r}")
        return 1

    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
