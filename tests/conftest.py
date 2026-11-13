"""Test configuration (arbiter-owned).

Modules used to set env vars at import time, which leaks across modules
because pytest imports every module before running anything. These fixtures
make the environment explicit per test instead.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


def _apply_env(**kv: str) -> None:
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    from preflight.config import Config

    Config.reload()


@pytest.fixture()
def dev_env() -> Iterator[None]:
    """DEV_MODE on, free tier on, no attestation key."""
    _apply_env(
        DEV_MODE="1",
        FREE_TIER="1",
        PREFLIGHT_PAY_TO_ADDRESS="0x" + "22" * 20,
        PREFLIGHT_ATTEST_KEY=None,
    )
    yield
    _apply_env(DEV_MODE="1", FREE_TIER="1")


@pytest.fixture()
def attest_env() -> Iterator[None]:
    """DEV_MODE on with a deterministic throwaway attestation key."""
    _apply_env(
        DEV_MODE="1",
        FREE_TIER="1",
        PREFLIGHT_ATTEST_KEY="0x" + "11" * 32,
    )
    yield


@pytest.fixture()
def prod_env() -> Iterator[None]:
    """Production payment mode: no dev bypass, no free tier."""
    _apply_env(
        DEV_MODE="0",
        FREE_TIER="0",
        PREFLIGHT_PAY_TO_ADDRESS="0x" + "11" * 20,
        PREFLIGHT_ATTEST_KEY=None,
    )
    yield


# Core test fixtures for contract sources
@pytest.fixture
def clean_erc20() -> str:
    with open("tests/fixtures/clean_erc20.sol") as f:
        return f.read()


@pytest.fixture
def reentrancy_vulnerable() -> str:
    with open("tests/fixtures/reentrancy_vulnerable.sol") as f:
        return f.read()


@pytest.fixture
def unguarded_uups() -> str:
    with open("tests/fixtures/unguarded_uups.sol") as f:
        return f.read()


@pytest.fixture
def oracle_no_freshness() -> str:
    with open("tests/fixtures/oracle_no_freshness.sol") as f:
        return f.read()


@pytest.fixture
def honeypot_pausable() -> str:
    with open("tests/fixtures/honeypot_pausable.sol") as f:
        return f.read()


@pytest.fixture
def clean_reentrancyguard_vault() -> str:
    with open("tests/fixtures/clean_reentrancyguard_vault.sol") as f:
        return f.read()


@pytest.fixture
def clean_oracle_fresh() -> str:
    with open("tests/fixtures/clean_oracle_fresh.sol") as f:
        return f.read()


@pytest.fixture
def clean_pausable_oz() -> str:
    with open("tests/fixtures/clean_pausable_oz.sol") as f:
        return f.read()


@pytest.fixture
def honeypot_pausable_blacklist() -> str:
    with open("tests/fixtures/honeypot_pausable_blacklist.sol") as f:
        return f.read()