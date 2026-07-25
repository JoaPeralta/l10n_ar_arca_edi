# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Outcome taxonomy for ARCA calls.

These three classes exist because "the call failed" is not a single fact. The
distinction below is the difference between a harmless retry and a duplicated
fiscal document, so it is modelled explicitly instead of being inferred from
exception text at the call site.
"""


class ArcaError(Exception):
    """Base class for every ARCA outcome that is not a success."""

    def __init__(self, message, code=None, observations=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.observations = observations or []

    def __str__(self):
        return self.message


class ArcaAborted(ArcaError):
    """The request provably never reached ARCA.

    No fiscal document can exist, the number is still free, and retrying is
    safe. Raised for connection-stage failures and for anything that fails
    before the SOAP envelope is transmitted.
    """


class ArcaBusinessError(ArcaError):
    """ARCA answered and refused.

    Deterministic: the same request will be refused again. No fiscal document
    was created, so the number remains available once the cause is fixed.
    """


class ArcaUncertain(ArcaError):
    """The request was transmitted and the answer was lost.

    A fiscal document may or may not exist at ARCA. Resending the same request
    can create a second one, and skipping it can leave a legally issued voucher
    unrecorded. The only correct resolution is FECompConsultar.
    """
