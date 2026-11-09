"""Shared test configuration."""

# ADR 0018: record the requirement IDs of the tests that ran (--spec-report).
from scripts.spec_trace_plugin import pytest_addoption, pytest_configure

__all__ = ["pytest_addoption", "pytest_configure"]
