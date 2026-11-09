"""Version-dependent code, and nowhere else.

ADR 0012: neolith supports Django 5.2 LTS on Python 3.12 up to the newest
Django on the newest Python it supports. When the preferred API exists only
above that floor, the version check and both code paths live here, each with a
comment naming the Python or Django release that lets the shim go. No other
module compares versions.

This module is empty until the first such case.
"""
