"""/jobs HTTP route package.

Split from the former 4029-line routes/jobs.py into concern modules
(artifacts / assets / uploads / events / lifecycle) sharing jobs/_base.py.
Importing this package registers every route on the shared ``router``.
Re-exports ``router`` + the helpers/handlers external modules import."""

from server.hub.routes.jobs._base import *  # noqa: F401,F403
from server.hub.routes.jobs._base import router  # noqa: F401
from server.hub.routes.jobs import (  # noqa: F401  (import = register routes)
    artifacts, assets, uploads, events, lifecycle,
)
from server.hub.routes.jobs.lifecycle import create_job  # noqa: F401
