"""SPONG plugin loader with user override support.

Plugins are normally loaded from the bundled package
``spong.plugins.<category>.<name>``. To allow users to customize a plugin
without their changes being overwritten on a ``.deb`` upgrade, an override
directory is checked first:

    /usr/local/spong/etc/plugins/<category>/<name>.py

If that file exists, it is loaded in place of the bundled module and
registered as ``spong.plugins.<category>.<name>`` in ``sys.modules``, so
relative imports within the override (``from . import _camara``) resolve to
sibling modules in the bundled package. The override file is never touched
by the package manager, surviving upgrades.

Categories: ``network`` and ``client``.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

from . import BASE_DIR

log = logging.getLogger(__name__)

OVERRIDE_ROOT = Path(BASE_DIR) / "etc" / "plugins"


def override_path(category: str, name: str) -> Path:
    return OVERRIDE_ROOT / category / f"{name}.py"


def load_plugin(category: str, name: str):
    """Return the module for plugin ``name`` in ``category``.

    Override at ``etc/plugins/<category>/<name>.py`` wins; falls back to the
    bundled ``spong.plugins.<category>.<name>``. Raises ``ImportError`` if
    neither is available. If an override exists but fails to load, logs the
    error and falls back to the bundled version.
    """
    bundled = f"spong.plugins.{category}.{name}"
    path = override_path(category, name)
    if path.is_file():
        try:
            importlib.import_module(f"spong.plugins.{category}")
            spec = importlib.util.spec_from_file_location(bundled, str(path))
            if spec is None or spec.loader is None:
                raise ImportError(f"could not build spec for {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[bundled] = module
            spec.loader.exec_module(module)
            log.info("Loaded override plugin %s from %s", bundled, path)
            return module
        except Exception as e:
            log.error(
                "Override plugin %s failed to load (%s); using bundled version",
                path, e,
            )
            sys.modules.pop(bundled, None)
    return importlib.import_module(bundled)
