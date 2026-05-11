"""
conftest.py - patches AV/filesystem watcher dependencies before subgen imports.

The mock setup must happen at collection time. subgen.py starts worker threads
at import time; they're daemon threads and harmless in these tests.
"""
import sys
import asyncio
from unittest.mock import MagicMock

import httpx
import fastapi.testclient

# ---------------------------------------------------------------------------
# Mock heavy dependencies that are not installed in CI
# ---------------------------------------------------------------------------
_MOCKED_MODULES = [
    "av",
    "ffmpeg",
    "watchdog",
    "watchdog.observers",
    "watchdog.observers.polling",
    "watchdog.events",
]

for _mod in _MOCKED_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Ensure watchdog attribute imports work
# e.g. `from watchdog.observers.polling import PollingObserver as Observer`
sys.modules["watchdog.observers.polling"].PollingObserver = MagicMock()
# e.g. `from watchdog.events import FileSystemEventHandler`
sys.modules["watchdog.events"].FileSystemEventHandler = object


class ASGITestClient:
    """Small sync wrapper around httpx.ASGITransport.

    The Python 3.14 test environment hangs in Starlette's thread-based
    TestClient. The app itself is ASGI-clean, so route tests use this direct
    transport instead.
    """

    __test__ = False

    def __init__(self, app):
        self.app = app

    def request(self, method, url, **kwargs):
        async def _request():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(_request())

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


fastapi.testclient.TestClient = ASGITestClient
