"""QOR — AI-Native Organization Runtime.

Layering is strict and enforced by tests (see tests/test_architecture.py):

    kernel   -> stdlib only
    domain   -> kernel + pydantic          (no I/O, no LLM, no workflow runtime)
    ports    -> domain
    services -> domain + ports
    adapters -> everything

Invariant I20: nothing under qor/domain may import langgraph or any LLM SDK.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
