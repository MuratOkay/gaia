"""Model package.

Importing the modules here ensures ``Base.metadata`` is complete before
Alembic autogenerate or ``create_all`` runs.
"""

from gaia.db.models.base import Base
from gaia.db.models import catalog  # noqa: F401

__all__ = ["Base", "catalog"]
