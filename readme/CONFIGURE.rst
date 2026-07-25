Two CUITs, and they are not the same thing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Before anything else, be clear about which number goes where. ARCA works with
two identities, and setups differ in whether they coincide:

.. code-block:: text

    Natural person / DN / certificate         <- CERTIFICATE HOLDER
      signs in to WSASS with their fiscal
      key and creates the certificate
              |
              |  "Crear autorizacion a servicio"
              |  authorizes that DN for the service `wsfe`
              |  representing another taxpayer
              v
    Company being invoiced for                <- REPRESENTED / ISSUER
      its CUIT is what appears on the
      invoice and in the QR code
              |
              v
            WSFEv1
      Auth.Token / Auth.Sign  -> prove who the HOLDER is
      Auth.Cuit               -> says which taxpayer is REPRESENTED

The WSFEv1 manual defines the field as "Cuit contribuyente (representado o
Emisora)". So:

**Certificate Holder CUIT**
    Whoever creates the certificate in the ARCA portal with their own fiscal
    key. In homologacion this is normally a natural person, because WSASS is
    reached with a personal fiscal key. Stored on the certificate record, put in
    the CSR subject, and checked against the certificate ARCA issues.

**Issuer CUIT**
    The taxpayer the invoices belong to. Taken from the Odoo company
    (*Settings > Companies > Tax ID*), never entered a second time. Reported to
    ARCA as ``Auth.Cuit`` and printed in the QR.

They are equal when a company uses its own certificate, and different when a
person invoices on behalf of a company. The module supports both; it just needs
to be told which is which.

Step 1: Check the company's tax number
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Go to **Settings > Users & Companies > Companies**, open the company, and make
sure its **Tax ID** is the CUIT that will issue the invoices. This is the
issuer, and everything fiscal follows it. A missing or malformed number is
refused with a message rather than silently substituted.

Step 2: Create the certificate record
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

#. Go to **Settings > Invoicing**, section **ARCA Electronic Invoicing**, and
   click **Create Certificate**.
#. Give it a name, for example ``MyCompany-Testing``.
#. **Certificate Holder CUIT**: the CUIT of whoever will sign in to WSASS. It
   defaults to the company's, which is right when the company uses its own
   certificate. If a person will create it, enter that person's CUIT.
#. **Invoices on behalf of** is shown read-only: it is the company's tax number
   and confirms which taxpayer will be reported to ARCA.
#. Choose **Testing (Homologación)**.
#. Click **Create Certificate**, then **Generate Key & CSR**.

The private key is generated on the server and stays there. The CSR subject
carries the holder's CUIT, because that is who ARCA issues the certificate to.

Step 3: Create the certificate in WSASS
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For **testing (homologacion)**:

#. Sign in at https://auth.afip.gob.ar with the **holder's** CUIT and fiscal
   key -- the same CUIT entered as Certificate Holder above.
#. Open **WSASS - Autogestion Certificados Homologacion**.
#. Click **Nuevo Certificado**.
#. Enter an alias for the DN.
#. Paste the CSR from Odoo into the PKCS#10 field.
#. Click **Crear DN y obtener certificado**.
#. Copy the resulting certificate, including the ``BEGIN`` and ``END`` lines,
   and save it as a ``.crt`` file.

For **production** the equivalent is **Administracion de Certificados
Digitales**, reached through **Administrador de Relaciones de Clave Fiscal**.
Production is out of scope for this guide.

Step 4: Authorize the DN to represent the issuer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This is the step that connects the two identities, and the one that is easy to
miss when they happen to be the same CUIT.

Still in WSASS:

#. Click **Crear autorizacion a servicio**.
#. **DN**: the one you just created -- the holder's.
#. **Servicio**: ``wsfe`` (Facturacion Electronica).
#. **CUIT representado**: the **issuer** CUIT, that is, the company's tax
   number from Step 1. When the holder and the company are the same taxpayer,
   this is that same number.
#. Click **Crear autorizacion de acceso**.

.. important::

   Without this authorization the certificate authenticates correctly and then
   every call fails with ARCA error **601, "CUIT representada no incluida en
   token"**. The module reports that error with a pointer back to this step.

Step 5: Upload the certificate in Odoo
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

#. Back in Odoo, click **Upload Certificate** and upload the ``.crt``.

It is accepted only if it matches the private key generated in Step 2, its
subject names the **holder** CUIT, and it is within its validity window. The
subject is not compared against the company's number: for a person invoicing on
behalf of a company those legitimately differ.

Step 6: Test the connection
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Click **Test Connection**. It goes as far as a real authenticated call on
behalf of the issuer, so a certificate that works but was never authorized for
the company fails here rather than on the first invoice.

Step 7: Configure the sales journal
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

#. Go to **Invoicing > Configuration > Journals** and open the sales journal.
#. Enable **Use Documents** and **Is ARCA POS?**.
#. Set **ARCA POS System** to **Electronic Invoice - Web Service**
   (``RAW_MAW``). This is the value that enables ARCA electronic invoicing on
   the journal. *Online Invoice* (``RLI_RLM``) means the invoices are typed
   into ARCA's own portal by hand, and does not enable it.
#. Set the **ARCA POS Number** matching the point of sale registered for the
   **issuer** CUIT.

Step 8: Align the numbering
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The number Odoo prints is the number sent to ARCA, so the two counters must
start in step. Before the first invoice, check what ARCA already has for that
point of sale and document type, and make sure Odoo's next number is exactly one
more.

If they differ, the first authorization is refused with a message naming both
numbers instead of issuing under a number the accounting does not show.

Issuing an invoice
~~~~~~~~~~~~~~~~~~

Posting an invoice does not contact ARCA. It produces a complete, committed
invoice whose ARCA status is **Pending**. Press **Request CAE** when you want it
authorized.

Requesting the CAE automatically on post is available under
**Request CAE automatically** and is off by default.

Running the homologacion tests
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The suite in ``tests/test_homologation.py`` talks to the real homologacion
environment. It comes in two halves, because they cost different things.

Both skip cleanly unless all five variables are set, and neither can touch
production: the certificate environment is pinned to ``testing`` and asserted
before every test.

.. code-block:: bash

   # Whoever created the certificate in WSASS
   export ARCA_HOMO_CERT_HOLDER_CUIT=20-12345678-9
   # The taxpayer the invoices belong to
   export ARCA_HOMO_REPRESENTED_CUIT=30-71234567-1
   export ARCA_HOMO_CERT="$(base64 -w0 homologacion.crt)"
   export ARCA_HOMO_PRIVATE_KEY="$(base64 -w0 homologacion.key)"
   export ARCA_HOMO_POS=1

**Read-only.** Consumes no voucher number, so run it as often as you like:

.. code-block:: bash

   odoo -d <db> -i l10n_ar_arca_edi --test-enable \
        --test-tags 'arca_homologation' --stop-after-init

It checks that ``FEDummy`` answers, that WSAA issues a ticket for the holder,
that the holder may act for the represented CUIT, that the point of sale is
enabled for it, that the receptor VAT condition table still matches what ARCA
reports, that the last authorized number is readable, and that querying a
voucher that does not exist returns nothing -- the behaviour reconciliation
depends on.

**Emission.** Issues a real voucher and consumes a number, so it needs an
explicit opt-in on top of the credentials:

.. code-block:: bash

   export ARCA_HOMO_ALLOW_EMISSION=1
   odoo -d <db> -i l10n_ar_arca_edi --test-enable \
        --test-tags 'arca_homologation_emission' --stop-after-init

It authorizes an invoice end to end and reads it back with ``FECompConsultar``,
checking that ARCA filed it under the represented CUIT.

.. warning::

   Use a point of sale reserved for testing. A consumed number cannot be
   returned.

In CI both live in a manually triggered job. The read-only half runs whenever
the secrets are configured; the emission half additionally requires the
``run_emission`` input to be turned on for that run.

The certificate and key are read from the environment and never written to the
repository. The ``secrets`` job fails the build if key material is ever
committed.
