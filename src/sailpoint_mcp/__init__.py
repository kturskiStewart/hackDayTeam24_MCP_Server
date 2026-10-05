"""MCP server for SailPoint Identity Security Cloud."""

import ssl

__version__ = "0.1.0"


def _trust_system_certificates() -> None:
    """Make TLS work behind Netskope (TLS-inspecting proxy) on Python 3.13.

    Python's bundled CA list doesn't include the Netskope root, and 3.13 enables
    VERIFY_X509_STRICT, which rejects that root (no key usage extension). Use the
    OS trust store and drop the strict flag; chain and hostname checks stay on.
    """
    try:
        import truststore
    except ImportError:
        return

    truststore.inject_into_ssl()
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    original_init = ssl.SSLContext.__init__

    def relaxed_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.verify_flags &= ~strict

    ssl.SSLContext.__init__ = relaxed_init


_trust_system_certificates()
