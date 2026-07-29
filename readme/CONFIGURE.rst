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

La sesion de homologacion
~~~~~~~~~~~~~~~~~~~~~~~~~

Homologacion corre contra una base PostgreSQL **persistente y dedicada**, nunca
contra una base que se destruya al terminar.

La razon es el ticket de acceso. WSAA emite uno por certificado y servicio, lo
mantiene vivo unas doce horas y rechaza emitir un segundo mientras el primero
siga vigente::

   El CEE ya posee un TA valido para el acceso al WSN solicitado

Un ticket emitido no se puede des-emitir. El modulo ya lo persiste
correctamente -- lo confirma en una transaccion propia antes de devolver el
control -- pero eso no sirve de nada si la base desaparece con la corrida. Por
eso la base sobrevive, y por eso ya no existe una sesion ejecutada como test de
Odoo: bajo ``current_test`` la topologia transaccional degrada y no demuestra
durabilidad.

Preparar la base (una sola vez)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

La provision de PostgreSQL vive en el repositorio de deployment. Una vez que la
base existe y el modulo esta instalado, el bootstrap del addon carga el material
fiscal:

.. code-block:: bash

   ARCA_HOMO_ALLOW_BOOTSTRAP=1        odoo shell -d <base> ... < tools/arca_homologation_bootstrap.py

Es idempotente y manual. Nunca sobrescribe el certificado, la clave privada, el
cache del ticket ni los intentos de autorizacion: solo completa lo que falta.
Fija ``ir_attachment.location = db`` antes de cargar nada, porque
``certificate`` y ``private_key`` son ``Binary(attachment=True)`` y de otro modo
irian a un filestore que el runner no tiene. Al terminar verifica en SQL que el
material quedo en ``ir_attachment.db_datas``.

Las credenciales se pasan **solo en la siembra inicial**, con el PEM codificado
en base64. Despues viven en la base y las variables deben eliminarse de donde se
hayan cargado.

Inspeccionar la base
^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

   ARCA_HOMO_MODE=preflight        odoo shell -d <base> ... < tools/arca_homologation_runner.py

Dos modos, ambos de solo lectura y ninguno llega a ARCA:

* ``preflight`` -- verifica TLS contra ``pg_stat_ssl``, que el modulo este
  instalado, que ``installed_sha`` coincida con el codigo en ejecucion, que el
  almacenamiento de adjuntos sea la base, que ``auto_request_cae`` este apagado,
  que haya exactamente un certificado en ``testing`` y que su material este
  presente.
* ``ticket-status`` -- lo anterior, mas el ticket cacheado informado **solo por
  vencimiento**.

Todo chequeo aborta en lugar de corregir. Un modo desconocido tambien aborta: no
hay valor por defecto.

En CI
~~~~~

``.github/workflows/arca-homologation.yml`` es el unico punto operacional, y es
manual (``workflow_dispatch``). Corre en un GitHub Environment protegido, exige
TLS, toma un grupo de concurrency global con ``cancel-in-progress: false``, tiene
timeout de veinte minutos y **no instala ni actualiza el modulo**.

Un paso posterior cuenta las lineas ``WSAA: requesting a ticket for service`` del
log y falla si no son cero: ningun modo de este workflow puede autenticar.

El CI ordinario ya no puede alcanzar ARCA. El certificado y la clave nunca se
escriben en el repositorio, y el job ``secrets`` falla el build si aparece
material de clave commiteado.

.. note::

   Los modos que si llegan a ARCA -- una llamada dummy, la prueba de reutilizacion
   del ticket y la emision de un comprobante -- todavia no existen. No estan
   deshabilitados: estan ausentes, y llegan con el cambio que este autorizado a
   crearlos.
