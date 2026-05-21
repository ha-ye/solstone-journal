# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import asyncio
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

from solstone.convey.secure_listener.accept import SecureListener


def test_reuse_port_allows_coexisting_bind():
    executor = ThreadPoolExecutor(max_workers=1)
    listener = SecureListener(
        app=MagicMock(),
        tls_ctx=MagicMock(),
        authorized=set(),
        executor=executor,
        callosum_emit=lambda *a, **kw: None,
        host="127.0.0.1",
        port=0,
    )
    loop = asyncio.new_event_loop()
    s2 = None
    try:
        loop.run_until_complete(listener.start())
        assert listener.sockets
        port = listener.sockets[0].getsockname()[1]
        assert port != 0

        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s2.bind(("127.0.0.1", port))
    finally:
        if listener.sockets:
            loop.run_until_complete(listener.stop())
        if s2 is not None:
            s2.close()
        loop.close()
        executor.shutdown(wait=True, cancel_futures=True)


def test_stop_all_after_loop_closed_does_not_raise():
    from solstone.convey.secure_listener import runtime as rt

    previous_runtime = rt._runtime
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)

        loop = asyncio.new_event_loop()
        loop.close()
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        app = SimpleNamespace(secure_listener_started=True)
        listener = SimpleNamespace(sockets=(s,))
        state = rt.RuntimeState(
            loop=loop,
            thread=thread,
            apps=[app],
            executor=executor,
            listener=listener,
            sockets=(s,),
        )
        rt._runtime = state

        rt.stop_all_secure_listener()

        assert s.fileno() == -1
    finally:
        rt._runtime = previous_runtime
        s.close()
        executor.shutdown(wait=True, cancel_futures=True)
