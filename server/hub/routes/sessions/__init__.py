"""/sessions HTTP route package (split from the 2499-line routes/sessions.py)
into lifecycle / actions / inspect / llm / ops, sharing sessions/_base.py.
Importing registers every route on the shared router; re-exports router +
the helpers/handlers external modules import."""

from server.hub.routes.sessions._base import *  # noqa: F401,F403
from server.hub.routes.sessions._base import router  # noqa: F401
from server.hub.routes.sessions import (  # noqa: F401  (import = register routes)
    lifecycle, actions, inspect, llm, ops,
)
from server.hub.routes.sessions.lifecycle import close_session  # noqa: F401
from server.hub.routes.sessions.lifecycle import create_session  # noqa: F401
from server.hub.routes.sessions.lifecycle import list_sessions  # noqa: F401
from server.hub.routes.sessions.llm import session_agent  # noqa: F401
from server.hub.routes.sessions.ops import session_save_cookies_to_host  # noqa: F401
