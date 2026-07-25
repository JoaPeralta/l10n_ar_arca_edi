# Copyright 2026 Leonobitech
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import logging

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

ARCA_ENVIRONMENTS = [
    ("testing", "Testing (Homologación)"),
    ("production", "Production"),
]

RSA_KEY_SIZE = 2048
CUIT_CHECK_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


def compute_cuit_check_digit(first_ten_digits):
    """Return the CUIT verification digit, or None when it cannot exist.

    Published by ARCA: weighted sum of the first ten digits modulo 11.
    """
    total = sum(int(d) * w for d, w in zip(first_ten_digits, CUIT_CHECK_WEIGHTS, strict=False))
    remainder = total % 11
    check = 11 - remainder
    if check == 11:
        return 0
    if check == 10:
        # ARCA does not assign this combination.
        return None
    return check


def is_valid_cuit(value):
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(digits) != 11:
        return False
    expected = compute_cuit_check_digit(digits[:10])
    return expected is not None and expected == int(digits[10])


class L10nArArcaCertificate(models.Model):
    _name = "l10n_ar.arca.certificate"
    _description = "ARCA Digital Certificate"
    _order = "id desc"

    name = fields.Char(
        required=True,
        help="Symbolic name for this certificate, used as CN in the CSR.",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company,
    )
    cuit = fields.Char(
        string="CUIT",
        required=True,
        help="Taxpayer number of the issuer, e.g. 20-29318820-4.",
    )
    environment = fields.Selection(
        ARCA_ENVIRONMENTS,
        required=True,
        default="testing",
        help="Production certificates issue legally binding invoices.",
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("csr_generated", "CSR Generated"),
            ("active", "Active"),
            ("expired", "Expired"),
            ("revoked", "Revoked"),
        ],
        default="draft",
        readonly=True,
        index=True,
    )

    # The private key is the one secret that must never leave the server. The
    # field group keeps it out of every ordinary read, export and RPC call; the
    # signing path reaches it deliberately through sudo.
    private_key = fields.Binary(
        attachment=True,
        groups="base.group_system",
        help="RSA private key in PEM format.",
    )
    private_key_filename = fields.Char(groups="base.group_system")
    private_key_stored = fields.Boolean(
        string="Private Key Stored",
        compute="_compute_private_key_stored",
        help="Whether a private key exists for this certificate.",
    )

    csr_pem = fields.Text(
        string="CSR (PEM)",
        readonly=True,
        help="Certificate signing request to upload to the ARCA portal.",
    )
    csr_filename = fields.Char(readonly=True)

    certificate = fields.Binary(
        attachment=True,
        help="Certificate issued by ARCA, in PEM format.",
    )
    certificate_filename = fields.Char()

    cert_subject = fields.Char(readonly=True)
    cert_issuer = fields.Char(readonly=True)
    cert_serial_number = fields.Char(readonly=True)
    cert_date_start = fields.Datetime(string="Valid From", readonly=True)
    cert_date_end = fields.Datetime(string="Valid Until", readonly=True)

    # Access tickets, keyed by ARCA service name. Credentials, so restricted.
    l10n_ar_arca_token_cache = fields.Json(
        string="WSAA Ticket Cache",
        groups="base.group_system",
        copy=False,
    )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @api.constrains("cuit")
    def _check_cuit(self):
        for record in self:
            if not is_valid_cuit(record.cuit):
                raise ValidationError(
                    _(
                        "'%s' is not a valid CUIT: it must have 11 digits and a "
                        "correct verification digit.",
                        record.cuit or "",
                    )
                )

    @api.constrains("company_id", "environment")
    def _check_production_company_vat(self):
        """A production certificate must belong to the company it invoices for."""
        for record in self.filtered(lambda r: r.environment == "production"):
            company_vat = record.company_id.partner_id.vat or ""
            company_digits = "".join(ch for ch in company_vat if ch.isdigit())
            if company_digits and company_digits != record._get_clean_cuit():
                raise ValidationError(
                    _(
                        "The certificate CUIT (%(cert)s) does not match the CUIT of "
                        "company '%(company)s' (%(company_cuit)s).",
                        cert=record._get_clean_cuit(),
                        company=record.company_id.display_name,
                        company_cuit=company_digits,
                    )
                )

    def _compute_private_key_stored(self):
        """Report that a key exists without ever handing out its contents."""
        for record in self:
            record.private_key_stored = bool(record.sudo().private_key)

    def _get_clean_cuit(self):
        self.ensure_one()
        return "".join(ch for ch in (self.cuit or "") if ch.isdigit())

    def _format_cuit_with_dashes(self):
        self.ensure_one()
        digits = self._get_clean_cuit()
        return f"{digits[:2]}-{digits[2:10]}-{digits[10]}"

    def _check_usable(self):
        """Refuse to use a certificate that cannot produce a valid signature."""
        self.ensure_one()
        if self.state == "revoked":
            raise UserError(_("Certificate '%s' was revoked.", self.name))
        if self.state == "expired":
            raise UserError(
                _(
                    "Certificate '%(name)s' expired on %(date)s. Generate a new CSR "
                    "and upload the certificate ARCA issues for it.",
                    name=self.name,
                    date=self.cert_date_end,
                )
            )
        if self.state != "active":
            raise UserError(
                _(
                    "Certificate '%s' is not active yet. Upload the certificate "
                    "issued by ARCA first.",
                    self.name,
                )
            )
        if self.cert_date_end and self.cert_date_end < fields.Datetime.now():
            self.sudo().state = "expired"
            raise UserError(
                _("Certificate '%s' has expired.", self.name)
            )
        if not self.sudo().private_key:
            raise UserError(
                _("Certificate '%s' has no private key stored.", self.name)
            )
        if not self.sudo().certificate:
            raise UserError(
                _("Certificate '%s' has no certificate file stored.", self.name)
            )
        return True

    # ------------------------------------------------------------------
    # Crypto material
    # ------------------------------------------------------------------

    def _load_private_key(self):
        """Load the private key for signing.

        Reached through sudo on purpose: an invoicing user must be able to sign
        a request without being able to read the key itself.
        """
        self.ensure_one()
        raw = self.sudo().private_key
        if not raw:
            raise UserError(_("Certificate '%s' has no private key.", self.name))
        try:
            return serialization.load_pem_private_key(base64.b64decode(raw), password=None)
        except Exception as exc:  # noqa: BLE001
            raise UserError(
                _("The private key of certificate '%s' could not be read.", self.name)
            ) from exc

    def _get_certificate_pem(self):
        self.ensure_one()
        raw = self.sudo().certificate
        if not raw:
            raise UserError(_("Certificate '%s' has no certificate file.", self.name))
        return base64.b64decode(raw)

    def _load_certificate(self):
        self.ensure_one()
        try:
            return x509.load_pem_x509_certificate(self._get_certificate_pem())
        except Exception as exc:  # noqa: BLE001
            raise UserError(
                _("The certificate file of '%s' could not be parsed.", self.name)
            ) from exc

    @api.model
    def _certificate_validity(self, cert):
        """Return (not_before, not_after) as naive UTC datetimes.

        cryptography deprecated the naive accessors in favour of the ``_utc``
        ones; both are supported so the module works across the versions Odoo
        pins on different Python releases.
        """
        try:
            not_before = cert.not_valid_before_utc.replace(tzinfo=None)
            not_after = cert.not_valid_after_utc.replace(tzinfo=None)
        except AttributeError:  # cryptography < 42
            not_before = cert.not_valid_before
            not_after = cert.not_valid_after
        return not_before, not_after

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_generate_key_and_csr(self):
        """Generate an RSA key pair and the CSR to submit to ARCA."""
        self.ensure_one()
        if self.state == "active":
            raise UserError(
                _(
                    "Certificate '%s' is active. Generating a new key would make "
                    "the certificate ARCA issued unusable. Create a new record "
                    "instead.",
                    self.name,
                )
            )
        if self.state not in ("draft", "csr_generated"):
            raise UserError(_("A CSR can only be generated on a draft certificate."))

        key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_SIZE)
        private_key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(
                x509.Name(
                    [
                        x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
                        x509.NameAttribute(
                            NameOID.ORGANIZATION_NAME, self.company_id.name or "Company"
                        ),
                        x509.NameAttribute(NameOID.COMMON_NAME, self.name),
                        x509.NameAttribute(
                            NameOID.SERIAL_NUMBER,
                            f"CUIT {self._format_cuit_with_dashes()}",
                        ),
                    ]
                )
            )
            .sign(key, hashes.SHA256())
        )
        csr_pem = csr.public_bytes(serialization.Encoding.PEM)

        self.sudo().write(
            {
                "private_key": base64.b64encode(private_key_pem),
                "private_key_filename": f"{self.name}.key",
                "csr_pem": csr_pem.decode("ascii"),
                "csr_filename": f"{self.name}.csr",
                "state": "csr_generated",
            }
        )
        _logger.info(
            "Generated an RSA-%s key and CSR for certificate %s (CUIT %s, env %s)",
            RSA_KEY_SIZE,
            self.id,
            self._format_cuit_with_dashes(),
            self.environment,
        )
        return self._notify(
            _("CSR generated"),
            _("Download the CSR and upload it to the ARCA portal."),
        )

    def action_upload_certificate(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Upload ARCA Certificate"),
            "res_model": "l10n_ar.arca.certificate.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_certificate_id": self.id},
        }

    def action_process_certificate(self, certificate_b64):
        """Validate and store the certificate ARCA issued.

        Everything checked here would otherwise surface much later as an opaque
        authentication failure: a certificate for the wrong key, for the wrong
        CUIT, or already expired.
        """
        self.ensure_one()
        try:
            cert = x509.load_pem_x509_certificate(base64.b64decode(certificate_b64))
        except Exception as exc:  # noqa: BLE001
            raise UserError(
                _("That file is not a valid PEM certificate.")
            ) from exc

        private_key = self._load_private_key()
        if cert.public_key().public_numbers() != private_key.public_key().public_numbers():
            raise UserError(
                _(
                    "This certificate does not match the private key stored on "
                    "'%s'. Upload the certificate ARCA issued for this record's CSR.",
                    self.name,
                )
            )

        self._check_certificate_cuit(cert)

        not_before, not_after = self._certificate_validity(cert)
        now = fields.Datetime.now()
        if not_after < now:
            raise UserError(
                _("That certificate expired on %s.", not_after)
            )
        if not_before > now:
            raise UserError(
                _("That certificate is not valid until %s.", not_before)
            )

        self.sudo().write(
            {
                "certificate": certificate_b64,
                "certificate_filename": f"{self.name}.crt",
                "cert_subject": cert.subject.rfc4514_string(),
                "cert_issuer": cert.issuer.rfc4514_string(),
                "cert_serial_number": str(cert.serial_number),
                "cert_date_start": not_before,
                "cert_date_end": not_after,
                "state": "active",
                # A new certificate invalidates every ticket issued for the old.
                "l10n_ar_arca_token_cache": False,
            }
        )
        _logger.info(
            "Certificate %s activated, valid until %s", self.id, not_after
        )
        return True

    def _check_certificate_cuit(self, cert):
        """The certificate must be issued for this record's CUIT."""
        self.ensure_one()
        serial_numbers = cert.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)
        if not serial_numbers:
            # Not every ARCA certificate carries it; do not block on absence.
            return True
        digits = "".join(ch for ch in str(serial_numbers[0].value) if ch.isdigit())
        if digits and digits != self._get_clean_cuit():
            raise UserError(
                _(
                    "That certificate was issued for CUIT %(cert_cuit)s but this "
                    "record is configured for %(record_cuit)s.",
                    cert_cuit=digits,
                    record_cuit=self._get_clean_cuit(),
                )
            )
        return True

    def action_test_connection(self):
        """Check that ARCA answers and that this certificate authenticates."""
        self.ensure_one()
        self._check_usable()
        status = self.env["l10n_ar.arca.wsfe"].check_connection(self)
        return self._notify(
            _("ARCA connection successful"),
            _(
                "Environment: %(env)s\nApplication server: %(app)s\n"
                "Database: %(db)s\nAuthentication: %(auth)s",
                env=dict(ARCA_ENVIRONMENTS)[self.environment],
                app=status.get("app_server"),
                db=status.get("db_server"),
                auth=status.get("auth_server"),
            ),
        )

    def action_revoke(self):
        """Stop using this certificate and drop its secrets."""
        self.ensure_one()
        self.sudo().write(
            {
                "state": "revoked",
                "l10n_ar_arca_token_cache": False,
                "private_key": False,
            }
        )
        _logger.warning("ARCA certificate %s was revoked; its private key was removed.", self.id)
        return True

    def _notify(self, title, message):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    @api.model
    def _cron_check_certificate_expiration(self, warn_days=30):
        """Mark expired certificates and warn before they expire."""
        now = fields.Datetime.now()
        expired = self.search(
            [("state", "=", "active"), ("cert_date_end", "<", now)]
        )
        if expired:
            expired.write({"state": "expired", "l10n_ar_arca_token_cache": False})
            _logger.warning(
                "Marked %s ARCA certificate(s) as expired: %s",
                len(expired),
                expired.ids,
            )

        soon = self.search(
            [
                ("state", "=", "active"),
                ("cert_date_end", "<", fields.Datetime.add(now, days=warn_days)),
            ]
        )
        for certificate in soon:
            _logger.warning(
                "ARCA certificate %s (%s) expires on %s; renew it before invoicing stops.",
                certificate.id,
                certificate.name,
                certificate.cert_date_end,
            )
        return True
