"""DSH Mail Core: a stdio JSON service used by the ``dsh-mail-plugin``.

The wire contract lives in ``docs/IPC_PROTOCOL.md`` (protocol v1).  Run it as::

    <python> -m mail_core --data-dir <absolute path>
"""

from .protocol import PROTOCOL_VERSION, VERSION

__all__ = ["PROTOCOL_VERSION", "VERSION", "__version__"]

__version__ = VERSION
