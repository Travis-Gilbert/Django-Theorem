"""Dependency-derived propositions with recoverable evidence and MiniCheck scores."""
from .decompose import Claim, Decomposer, Decomposition
from .resolve_span import resolve_span

__all__ = ['Claim', 'Decomposer', 'Decomposition', 'resolve_span']
