# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import os

from odoo import _
from odoo.exceptions import UserError

ARCA_ENVIRONMENTS = [
    ("testing", "Testing (Homologación)"),
    ("production", "Production"),
]
_ENVIRONMENT_CODES = frozenset(code for code, _label in ARCA_ENVIRONMENTS)


def runtime_environment():
    """Read the process authority, never a database, context, or Railway name."""
    value = os.environ.get("ARCA_RUNTIME_ENV")
    return value if value in _ENVIRONMENT_CODES else None


def require_runtime_environment(*record_environments):
    """Check service configuration and any existing certificate or attempt labels."""
    environment = runtime_environment()
    if environment is None:
        raise UserError(_("Set ARCA_RUNTIME_ENV to testing or production before using ARCA."))
    if any(value != environment for value in record_environments):
        raise UserError(_("The specified ARCA environment does not match this service."))
    return environment
