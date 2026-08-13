"""Concrete identity providers. See ``services/auth/__init__.py`` for the seam."""

from .base import AuthenticatedIdentity

__all__ = ["AuthenticatedIdentity"]
