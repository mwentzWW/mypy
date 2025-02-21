import sys
import time
from multiprocessing import Process, Queue
from unittest import TestCase, main

import pytest

from mypy.ipc import IPCClient, IPCServer

CONNECTION_NAME = "dmypy-test-ipc"


def server(msg: str, q: "Queue[str]") -> None:
    server = IPCServer(CONNECTION_NAME)
    q.put(server.connection_name)
    data = b""
    while not data:
        with server:
            server.write(msg.encode())
            data = server.read()
    server.cleanup()


class IPCTests(TestCase):
    def test_transaction_large(self) -> None:
        queue: Queue[str] = Queue()
        msg = "t" * 200000  # longer than the max read size of 100_000
        p = Process(target=server, args=(msg, queue), daemon=True)
        p.start()
        connection_name = queue.get()
        with IPCClient(connection_name, timeout=1) as client:
            assert client.read() == msg.encode()
            client.write(b"test")
        queue.close()
        queue.join_thread()
        p.join()

    def test_connect_twice(self) -> None:
        queue: Queue[str] = Queue()
        msg = "this is a test message"
        p = Process(target=server, args=(msg, queue), daemon=True)
        p.start()
        connection_name = queue.get()
        with IPCClient(connection_name, timeout=1) as client:
            assert client.read() == msg.encode()
            client.write(b"")  # don't let the server hang up yet, we want to connect again.

        with IPCClient(connection_name, timeout=1) as client:
            assert client.read() == msg.encode()
            client.write(b"test")
        queue.close()
        queue.join_thread()
        p.join()
        assert p.exitcode == 0

    # Run test_connect_twice a lot, in the hopes of finding issues.
    # This is really slow, so it is skipped, but can be enabled if
    # needed to debug IPC issues.
    @pytest.mark.skip
    def test_connect_alot(self) -> None:
        t0 = time.time()
        for i in range(1000):
            try:
                print(i, "start")
                self.test_connect_twice()
            finally:
                t1 = time.time()
                print(i, t1 - t0)
                sys.stdout.flush()
                t0 = t1


if __name__ == "__main__":
    main()
