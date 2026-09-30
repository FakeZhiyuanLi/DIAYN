"""
A stand-in for `aiohttp`, for a box running the suite with bare `python3`.

`internship_poller` imports aiohttp at module scope, so without one the suite
cannot even import the module under test. The stub carries only the surface
the import touches; nothing in the suite makes a request. It lives in a file of
its own because the config tests also need it inside a child process, where
they run the scraper the way pm2 does.
"""

import sys
import types


def stub_aiohttp() -> bool:
    """Fakes `aiohttp`, unless the real one is installed."""
    try:
        import aiohttp  # noqa: F401
        return False
    except ModuleNotFoundError:
        pass
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = type("ClientError", (Exception,), {})
    aiohttp.ClientTimeout = lambda **kwargs: None
    aiohttp.ClientSession = object
    aiohttp.TCPConnector = lambda **kwargs: None
    sys.modules["aiohttp"] = aiohttp
    return True
