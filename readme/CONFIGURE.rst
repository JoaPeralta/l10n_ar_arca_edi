All configuration is done from **Settings > Invoicing > ARCA Electronic
Invoicing**.

Step 1: Create Certificate in Odoo
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

#. Go to **Settings > Invoicing**.
#. In the **ARCA Electronic Invoicing** section, click **Create Certificate**.
#. Fill in the name (e.g., ``MyCompany-Testing``), select the company, and
   choose the environment (testing or production).
#. Click **Create Certificate**.
#. Click **Generate Key & CSR** to generate the private key and CSR.

Step 2: Register Certificate in ARCA Portal
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For **testing (homologacion)**:

#. Login at https://auth.afip.gob.ar with your CUIT and clave fiscal.
#. Search for **WSASS - Autogestion Certificados Homologacion**.
#. This takes you to the WSASS portal.

For **production**:

#. Login at https://auth.afip.gob.ar with your CUIT and clave fiscal.
#. Search for **Administracion de Certificados Digitales**.

Once inside the WSASS portal:

#. Click **Nuevo Certificado** in the left sidebar.
#. Enter a symbolic name for the DN.
#. Copy the CSR content from Odoo and paste it in the PKCS#10 field.
#. Click **Crear DN y obtener certificado**.
#. Copy the resulting certificate (including the ``BEGIN`` and ``END`` lines)
   and save it as a ``.crt`` file.

Step 3: Authorize wsfe Service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Still in the WSASS portal:

#. Click **Crear autorizacion a servicio** in the left sidebar.
#. Select the DN you created, enter your CUIT as the represented entity, and
   select **wsfe - Facturacion Electronica** as the service.
#. Click **Crear autorizacion de acceso**.

.. important::

   Without this step, the connection test will fail with
   "Computador no autorizado a acceder al servicio".

Step 4: Upload Certificate in Odoo
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

#. Back in Odoo Settings, click **Upload Certificate**.
#. Upload the ``.crt`` file saved from ARCA.
#. Click **Upload**.

Step 5: Test Connection
~~~~~~~~~~~~~~~~~~~~~~~

#. Click **Test Connection**.
#. A success message confirms the WSAA authentication and shows the token
   expiration time (typically 12 hours).

Step 6: Configure Sales Journal
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

#. Go to **Invoicing > Configuration > Journals**.
#. Open your sales journal.
#. Enable **Use Documents** and **Is ARCA POS?**.
#. Set **ARCA POS System** to **Electronic Invoice - Web Service**
   (``RAW_MAW``). This is the value that enables ARCA electronic invoicing on
   the journal. *Online Invoice* (``RLI_RLM``) means the invoices are typed
   into ARCA's own portal by hand, and does not enable it.
#. Set the **ARCA POS Number** matching your point of sale registered in ARCA.

Step 7: Align the numbering
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The number Odoo prints is the number sent to ARCA, so the two counters must
start in step. Before the first invoice, check what ARCA already has for that
point of sale and document type, and make sure Odoo's next number is exactly one
more.

If they differ, the first authorization is refused with a message naming both
numbers rather than issuing under a number the accounting does not show.

Running the homologacion tests
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The suite in ``tests/test_homologation.py`` talks to the real homologacion
environment. It skips cleanly unless all four variables are set, and it never
touches production: the certificate environment is pinned to ``testing`` and
asserted before every test.

.. code-block:: bash

   export ARCA_HOMO_CUIT=20-12345678-9
   export ARCA_HOMO_CERT="$(base64 -w0 homologacion.crt)"
   export ARCA_HOMO_PRIVATE_KEY="$(base64 -w0 homologacion.key)"
   export ARCA_HOMO_POS=1

   odoo -d <db> -i l10n_ar_arca_edi --test-enable         --test-tags 'arca_homologation' --stop-after-init

What it checks:

* ``FEDummy`` answers and the three ARCA subsystems report OK
* WSAA issues an access ticket for this certificate
* the configured point of sale is enabled for this CUIT
* the receptor VAT condition table embedded in the module still matches what
  ARCA reports
* an invoice is authorized end to end, and reading it back with
  ``FECompConsultar`` returns the same CAE
* querying a voucher that does not exist returns nothing, which is the
  behaviour reconciliation depends on

The certificate and key are read from the environment and never written to the
repository. The ``secrets`` job in CI fails the build if key material is ever
committed.

.. warning::

   These tests issue real vouchers in homologacion, consuming numbers at that
   point of sale. Use a point of sale reserved for testing.
