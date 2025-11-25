# Lightweight compatibility module that exposes the project's Declarative Base
# and ensures all SQLAlchemy model modules are imported so Alembic autogenerate
# can discover table metadata.
import os
import pkgutil
import importlib

from app.models.base import Base

# Import all modules under app.models.declare_models so mapped classes register
package_dir = os.path.join(os.path.dirname(__file__), "models", "declare_models")
if os.path.isdir(package_dir):
    for _, module_name, _ in pkgutil.iter_modules([package_dir]):
        importlib.import_module(f"app.models.declare_models.{module_name}")

__all__ = ["Base"]

