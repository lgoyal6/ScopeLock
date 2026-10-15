"""Default tests are offline, including SDK construction during collection.

Real Guava roleplays require explicit --live-guava plus a real credential. This
switch does not provide spending approval. The SDK's deprecation check is bypassed
only in tests; actual SDK command generation and local policy paths still execute.
"""
import os
import socket
from unittest.mock import patch

_OFFLINE_PATCHES = []


def pytest_addoption(parser):
    parser.addoption('--live-guava', action='store_true', default=False,
                     help='Enable authorized live provider tests; may use paid credits')


def _deny_network(*args, **kwargs):
    raise RuntimeError('Default tests are offline; --live-guava requires separate approval')


def pytest_configure(config):
    if config.getoption('--live-guava'):
        return
    os.environ['GUAVA_API_KEY'] = 'gva-test-placeholder-not-a-real-key'
    os.environ['GUAVA_DISABLE_TELEMETRY'] = 'true'
    # The real SDK otherwise checks its deprecation endpoint on first construction.
    # This metadata check is unrelated to the command/policy behavior under test.
    for item in [patch('guava.client.first_client.claim', return_value=False),
                 patch.object(socket.socket, 'connect', _deny_network),
                 patch.object(socket.socket, 'connect_ex', _deny_network),
                 patch.object(socket, 'create_connection', _deny_network)]:
        item.start()
        _OFFLINE_PATCHES.append(item)


def pytest_unconfigure(config):
    while _OFFLINE_PATCHES:
        _OFFLINE_PATCHES.pop().stop()
