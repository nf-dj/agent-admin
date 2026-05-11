"""Harness abstraction — adapters for different agent runtimes.

v1 supports only `openclaw`. Future harnesses (Hermes, etc.) implement the
same interface and register themselves in `HARNESSES`.
"""
from .base import Harness, AgentSpec, AgentState
from .openclaw import OpenClawHarness

HARNESSES: dict[str, Harness] = {
    "openclaw": OpenClawHarness(),
}


def get_harness(name: str) -> Harness:
    if name not in HARNESSES:
        raise ValueError(f"unknown harness: {name}")
    return HARNESSES[name]


__all__ = ["Harness", "AgentSpec", "AgentState", "HARNESSES", "get_harness"]
