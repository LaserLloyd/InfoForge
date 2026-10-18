"""The conftest guard must really stop network and external-program access."""

from __future__ import annotations

import asyncio
import socket
import subprocess

import pytest
import requests
from conftest import RealNetworkAccess


def test_connecting_a_real_socket_fails_the_test():
    sock = socket.socket()
    with pytest.raises(RealNetworkAccess):
        sock.connect(("127.0.0.1", 9))
    sock.close()


def test_connect_ex_is_guarded_too():
    sock = socket.socket()
    with pytest.raises(RealNetworkAccess):
        sock.connect_ex(("203.0.113.1", 80))
    sock.close()


def test_requests_cannot_reach_the_network():
    with pytest.raises(RealNetworkAccess):
        requests.get("http://127.0.0.1:18080/", timeout=1)


def test_external_programs_are_blocked():
    with pytest.raises(RealNetworkAccess):
        subprocess.run(["podman", "ps"], capture_output=True)


def test_asyncio_still_works_with_the_guard():
    async def go():
        return await asyncio.sleep(0, result="ok")

    assert asyncio.run(go()) == "ok"
