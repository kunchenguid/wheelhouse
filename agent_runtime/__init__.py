"""Wheelhouse provider-agnostic agent runtime.

Trusted Wheelhouse code owns this package. Model harnesses execute only inside a
sandboxed adapter worker and never receive GitHub acting credentials.
"""

API_VERSION = "wheelhouse.agent-runtime/v1alpha1"
RUNTIME_VERSION = "1.0.0"
