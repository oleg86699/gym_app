"""XML-RPC клиент для WordPress.

См. .client.XmlRpcPoster — единая точка входа для воркера.
"""

from infrastructure.wp_client.client import (  # noqa: F401
    DEFINITIVE_CRED_INVALID_KINDS,
    ErrorKind,
    PostOutcome,
    ValidateOutcome,
    XmlRpcPoster,
)
