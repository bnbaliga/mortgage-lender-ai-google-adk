import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from front_desk import tools
from front_desk.clinic import ClinicStore

# Monday 2026-10-05, 09:00 — every test runs on the same frozen morning.
NOW = datetime(2026, 10, 5, 9, 0)


@pytest.fixture
def store():
    s = ClinicStore.load(clock=lambda: NOW)
    tools.use_store(s)
    return s


@pytest.fixture
def ctx():
    return SimpleNamespace(session=SimpleNamespace(id="call-1"))


def run(coro):
    return asyncio.run(coro)


def tool(name):
    return SimpleNamespace(name=name)
