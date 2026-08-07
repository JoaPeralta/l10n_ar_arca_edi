<!--
Copyright 2026 Leonobitech
License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
-->

# Read-only audit — 2026-07-29

Verbatim record of the external read-only audit of this module. The body below
is reproduced unchanged, in the language it was written in (Spanish); only this
header was added.

## Provenance

| Field | Value |
| --- | --- |
| Issued | 2026-07-29 (UTC) |
| Scope | Read-only. No file, branch, commit, PR, workflow run, database or ARCA call was produced by the audit. |
| Repository audited | `JoaPeralta/l10n_ar_arca_edi` |
| Branch audited | `feat/production-hardening` |
| SHA audited | `9c41e7db919577629e745d8ff431e76b47cb53f2` |
| Secondary: `JoaPeralta/meli_oerp` | `19.0` @ `236dc4eb3e6af18b14f955f7680328c04e8d3f49` (integration boundary only) |
| Secondary: `JoaPeralta/odoo-viarengo` | `feat/arca-edi-integration` @ `3cb1f71115ea3223cc88b44007e897f7a51efb33` |

## Re-verification on import

The three SHAs above were re-read from their remotes with `git ls-remote` before
this file was committed. All three still match the audited values, so no commit
has landed since the audit and every finding below is stated against the tree
that is still the branch head.

## Sanitisation

The report was swept for private key blocks, certificate blocks, URLs carrying
credentials, `token`/`sign` assignments, bare CUITs, GitHub tokens, long base64
blobs and cloud access keys before it was committed. Every class matched zero
times, so the text is stored as it was received. It carries SHAs, file paths,
line numbers and ARCA error strings, none of which are secret.

## How to read it

The findings are numbered `H-01`…`H-07` (HIGH) and `M-01`…`M-12` (MEDIUM), and
section K carries the author's own PR plan. That plan is advisory: the order
actually being executed is the one in
[`docs/runbooks/arca-homologation.md`](../runbooks/arca-homologation.md), which
puts access-ticket durability ahead of everything else. Where a finding is
already fixed in the tree, the fix is recorded in the runbook rather than by
editing the report — this file is evidence, not a worklist.

---

# Auditoría técnica READ-ONLY — `l10n_ar_arca_edi` (ARCA/WSFEv1 sobre Odoo 19 Community)

Fecha de emisión: **2026-07-29 (UTC)**. Auditoría exclusivamente de lectura.

---

## A. Estado de acceso

| Ítem | Resultado |
| --- | --- |
| **MCP de GitHub invocable** | **NO.** No hay servidor MCP de GitHub en la sesión. `gh` CLI tampoco está instalado (`command not found` en Bash y en PowerShell). |
| **Acceso remoto alternativo** | **SÍ**, vía `git` autenticado por HTTPS: `git ls-remote` resolvió los tres repos y `git clone --depth 50 --single-branch` recuperó los dos que no estaban localmente. Todas las operaciones son de lectura. |
| **MCP oficial de Mercado Libre invocable** | **SÍ.** `search_documentation` y `get_documentation_page` respondieron (es_ar / MLA). Solo se leyó documentación; no se llamó a ninguna API operativa. |
| **Odoo 19 oficial** | Consultado por HTTP público (`raw.githubusercontent.com/odoo/odoo/19.0`). |

### Repositorios y SHA auditados (verificados contra el remoto, no asumidos)

| Repositorio | Rama | SHA completo | Último commit (autor local) |
| --- | --- | --- | --- |
| `JoaPeralta/l10n_ar_arca_edi` | `feat/production-hardening` | `9c41e7db919577629e745d8ff431e76b47cb53f2` | 2026-07-28T14:16:54-03:00 — `docs(audit): record persistent WSAA lease debt` |
| `JoaPeralta/meli_oerp` | `19.0` | `236dc4eb3e6af18b14f955f7680328c04e8d3f49` | 2026-07-28T23:44:00-03:00 — `PR C — TD10: authorize meli_login and meli_logout at the RPC boundary (#62)` |
| `JoaPeralta/odoo-viarengo` | `feat/arca-edi-integration` | `3cb1f71115ea3223cc88b44007e897f7a51efb33` | 2026-07-25T18:34:56-03:00 — `build: pin ARCA EDI to the holder/represented split` |

`git ls-remote` confirmó que `9c41e7d…` es el HEAD remoto **vigente** de la rama auditada (no un SHA caché obsoleto).

**Advertencia de fidelidad, resuelta:** el working tree local estaba posicionado en `main` (`bb1eb0e1…`), que difiere de la rama auditada en **29 commits / 62 archivos / +9.898 −2.684 líneas**. Toda la auditoría se hizo sobre un árbol extraído con `git archive 9c41e7d`, verificado por SHA-256 contra `git show` (la única diferencia era CRLF; contenido idéntico). **Nada se leyó de `main`.**

### Confirmación de no-escritura

No se modificó ningún archivo de ningún repositorio; no se crearon ramas, commits ni PRs; no se ejecutaron GitHub Actions; no se tocó Railway, variables, módulos ni bases de datos; no se llamó a ARCA ni se solicitó ningún CAE; no se llamó a ninguna API operativa de Mercado Libre. No se ejecutó ningún test.

**Única escritura realizada, y es fuera de los repositorios:** se materializaron copias de solo lectura en el directorio temporal de sesión (`…/scratchpad/audit-src/`) — el árbol del SHA auditado y clones `--depth 50` de los dos repos secundarios, que no estaban disponibles localmente. Ningún repositorio del usuario fue alterado.

### Metodología

13 auditorías dimensionales en paralelo sobre el árbol del SHA, más **45 agentes refutadores independientes** que revisaron cada hallazgo CRITICAL/HIGH releyendo el código con mandato de refutar. Resultado: **153 hallazgos brutos → 12 refutados y degradados, 33 supervivientes**. Los 3 CRITICAL propuestos **no sobrevivieron** la refutación (degradados a HIGH/MEDIUM). Los hallazgos aquí incluidos fueron además verificados personalmente sobre el código.

---

## B. Resumen ejecutivo

### Fortalezas (reales y poco habituales)

1. **La operación remota está correctamente separada de la transacción de negocio.** `_post()` no llama a ARCA: solo marca `pending` y, opcionalmente, agenda vía `cr.postcommit`. Una falla de ARCA no puede deshacer una factura contabilizada.
2. **Evidencia durable antes del acto irreversible.** El orden en `_l10n_ar_arca_authorize` es: relecturar estado bajo lock → `FECompUltimoAutorizado` → crear `attempt` y `COMMIT` → recién ahí `FECAESolicitar`. Es el orden correcto y es lo que hace recuperable una respuesta perdida.
3. **El estado "no sé" existe y es terminal-hasta-reconciliar.** `ArcaUncertain` → `l10n_ar_arca_state = 'uncertain'`, y `_l10n_ar_arca_check_ready` **rechaza** reintentar desde ahí. Un timeout de lectura se clasifica como incierto, no como rechazo (`_classify_transport_exception`).
4. **Lock de numeración correcto a nivel PostgreSQL.** `pg_try_advisory_xact_lock` sobre una conexión dedicada, con clave derivada del **CUIT emisor** (no de la compañía Odoo), porque ARCA numera por CUIT. Los tests fuerzan la ruta de producción y verifican liberación tras `SQL error`.
5. **Backstop en base de datos.** Índice único parcial sobre `(company_id, issuer_cuit, pos_number, document_type_code, document_number) WHERE state='authorized'`.
6. **Secretos correctamente restringidos.** `private_key`, `private_key_filename` y `l10n_ar_arca_token_cache` llevan `groups="base.group_system"`; el firmado accede por `sudo()` deliberado. Hay tests que verifican que token/sign/clave no llegan al log ni al payload persistido.
7. **Aislamiento multiempresa por `ir.rule`** en certificados e intentos, con tests que lo comprueban con usuarios reales.
8. **Los importes se delegan a `l10n_ar`** (`_l10n_ar_get_amounts`, `_get_vat`) en vez de reimplementar la clasificación fiscal. La invariante `ImpTotal = ImpNeto+ImpIVA+ImpTrib+ImpOpEx+ImpTotConc` se verifica localmente con la tolerancia real de la validación 10048 (`abs>0,01 **Y** rel>0,0001` para rechazar — que es el complemento correcto de "rel ≤ 0,01 % **o** abs ≤ 0,01").
9. **Alcance declarado honestamente.** Factura E (WSFEX), FCE MiPyME y CAEA se **rechazan con mensaje explicativo**, no se envían para que ARCA los rechace.

### Riesgos fiscales (los que importan)

10. **`ImpTrib` se envía sin el array `Tributos`.** Toda factura con percepción IIBB o impuesto interno declara "otros tributos" sin detallarlos. En Argentina esto no es un caso de borde: es el caso común de un Responsable Inscripto.
11. **La representación impresa está rota para toda factura autorizada** (`len()` sobre un `fields.Date`). El CAE se obtiene bien y no se puede entregar el comprobante — que es el objeto del módulo.
12. **El botón "Reconciliar" evade las tres protecciones** que el cron sí tiene (lock de secuencia, ventana de obsolescencia, transacción fiscal), y puede devolver una factura `processing` a `pending`, liberando el número.
13. **La reconciliación adopta cualquier comprobante** que ARCA reporte en esa coordenada sin verificar `Resultado`, ni que haya CAE, ni que `ImpTotal`/`DocNro` correspondan a esta factura.

### Riesgos transaccionales

14. **La topología de doble cursor dedicado nunca se ejercita con datos ORM.** `fiscal_transaction` toma la rama `dedicated=False` bajo `current_test`, así que `checkpoint()` es un `flush()` en todos los tests ORM. La durabilidad real está probada solo por tests de primitiva sin ORM.

### Riesgos de seguridad

15. **Escalada de privilegios por `sudo()` sin control de acceso.** `action_revoke`, `action_process_certificate` y `action_generate_key_and_csr` son métodos públicos que hacen `self.sudo().write(...)` sin verificar permisos: un usuario de facturación con ACL de **solo lectura** puede destruir la clave privada por RPC. No hay un solo `check_access` en el módulo.

### Bloqueadores de homologación

16. No se puede validar el comprobante impreso (hallazgo H-01) ni probar facturas con percepciones (H-04). **Nunca se emitió un CAE**, ni siquiera en homologación: el propio `AUDIT.md` declara que solo se confirmaron WSAA + `FEDummy`.

### Bloqueadores de producción

17. Todo lo anterior, más: ausencia total de fail-closed ante base restaurada/neutralizada (H-06) y ausencia de registro del ambiente sobre el `account.move` (H-07) — una factura de homologación es indistinguible de una real en el asiento, el reporte y el QR.

---

## C. Arquitectura detectada (flujo real, reconstruido del código)

```text
sale.order (u origen manual)
  └─ account.move  [account.move]
       ├─ journal_id  → account_journal.py:16  l10n_ar_arca_edi_enabled
       │                (compute store=True: l10n_ar_is_pos AND l10n_ar_afip_pos_system=='RAW_MAW')
       ├─ punto de venta → journal.l10n_ar_afip_pos_number
       ├─ tipo comprobante → l10n_latam_document_type_id   ◄── RESUELTO POR l10n_ar, no reimplementado
       ├─ posición fiscal / condición receptor → partner.l10n_ar_afip_responsibility_type_id
       └─ impuestos → account.tax / tax groups de l10n_ar

account.move.action_post()
  └─ AccountMove._post()                      account_move.py:108-139
       ├─ super()._post()                                    [transacción de posting]
       ├─ l10n_ar_arca_state = 'pending'                     [único write fiscal aquí]
       └─ si company.l10n_ar_arca_auto_request_cae:
            cr.postcommit.add(_run)           account_move.py:141-160   ◄── FUERA de la transacción

  ⚠ NO existe account.edi.document / account.edi.format. Ver §E "Odoo EDI".

_l10n_ar_arca_request_cae()                   account_move.py:200-224
  ├─ _l10n_ar_arca_check_ready()   [cursor del llamador, solo lectura]
  ├─ lock_key = blake2b(issuer_cuit : pos : doc_type) & 2^63-1
  └─ with fiscal_transaction(env, lock_key):  fiscal_transaction.py:77-113
        conexión #1 → pg_try_advisory_xact_lock(key)      (xact-scoped, se libera sola)
        conexión #2 → work_env, SET LOCAL lock_timeout='10s'

  _l10n_ar_arca_authorize(fiscal)             account_move.py:265-398
    1. _l10n_ar_arca_check_ready()  ← RE-CHEQUEO bajo lock, snapshot propio
    2. certificate._check_usable() ; issuer_cuit = company._l10n_ar_arca_issuer_cuit()
    3. coherencia nº factura ↔ punto de venta del diario        [289-297]
    4. WSFE.fe_comp_ultimo_autorizado(...) → _l10n_ar_arca_check_sequence  [300-303]
    5. _l10n_ar_arca_prepare_request(...)                        [305-307]
    6. attempt.create(state='sent') ; move.state='processing'
       fiscal.checkpoint()  ═══ COMMIT ═══                       [309-329]
    7. WSFE.fe_cae_solicitar(...)   ◄── ACTO IRREVERSIBLE        [332]
         ├─ ArcaUncertain     → attempt 'uncertain' + move 'uncertain' + COMMIT + UserError
         ├─ ArcaAborted       → attempt 'aborted'   + move 'rejected'  + COMMIT + UserError
         ├─ ArcaBusinessError → attempt 'rejected'  + move 'rejected'  + COMMIT + UserError
         └─ OK → attempt 'authorized' + move 'authorized' + CAE + venc. + COMMIT

Reconciliación
  ├─ cron  _cron_reconcile_open_attempts   attempt.py:293-321  (c/15 min, ACTIVO por defecto)
  │     └─ _reconcile_isolated()  → fiscal_transaction + lock + ventana 5 min  ✔ protegido
  └─ botón action_l10n_ar_arca_reconcile   account_move.py:437-445
        └─ attempt._reconcile()  ✗ SIN lock, SIN ventana, SIN transacción fiscal  → H-03

Representación
  └─ reports/report_invoice.xml  custom_footer  → QR (RG 4892/2020) + CAE + vencimiento
        ✗ TypeError en toda factura autorizada → H-01
```

---

## D. Máquina de estados

`l10n_ar_arca_state` en `account.move` (`account_move.py:33-49`), `state` en `l10n_ar.arca.attempt` (`attempt.py:87-99`).

| Estado | Entrada | Salida | Persistencia | Operación remota | Riesgo |
| --- | --- | --- | --- | --- | --- |
| `not_required` | default del campo | → `pending` en `_post` si es EDI | commit del posting | ninguna | Si el diario se habilita después, la factura ya posteada queda `not_required` para siempre (MEDIUM, STATE-09) |
| `pending` | `_post` (:130); reconciliación negativa (:470) | → `processing` | commit del posting / commit fiscal | ninguna | Fallas de pre-vuelo (certificado, secuencia, importes) **no persisten nada**: la factura queda `pending` sin rastro (MEDIUM, STATE-07) |
| `processing` | `_l10n_ar_arca_authorize` :325, **antes** del envío | → `authorized` / `rejected` / `uncertain`; o → `pending` por reconciliación | **COMMIT explícito** (:329) antes de la llamada | `FECAESolicitar` en vuelo | ✔ `check_ready` bloquea reintento. ✗ `button_draft` **no** lo bloquea → **H-02** |
| `authorized` | :377-386 tras respuesta `A` | terminal (`button_draft` lo rechaza) | COMMIT (:387) | — | ✗ Puede llegar sin CAE si `Resultado='P'` (MEDIUM). ✗ La reconciliación puede estamparlo sin validar el comprobante (MEDIUM) |
| `rejected` | :358-364 (`ArcaAborted` **y** `ArcaBusinessError`) | → `processing` (reintento permitido) | COMMIT (:365) | ninguna creada | Mezcla "no salió" con "ARCA rechazó". Refutado como LOW: ambos son seguros de reintentar; el costo es diagnóstico, no fiscal |
| `uncertain` | :333-341 (`ArcaUncertain`) | solo por reconciliación | COMMIT (:341) | request transmitido, respuesta perdida | ✔ Correcto y bloqueante. ✗ Arrastra `error_code` viejo (LOW) |
| — sin `cancelled` — | — | — | — | — | No hay estado de anulación; ARCA WSFEv1 tampoco anula. Se resuelve con nota de crédito |

**Transiciones inválidas alcanzables (confirmadas):**
- `processing → draft` vía `button_draft` (**H-02**).
- `processing → pending` vía botón Reconciliar sin lock ni ventana (**H-03**), liberando un número que puede estar autorizado en ARCA.
- `authorized → pending` posible por la misma vía si el `_reconcile` negativo corre sobre un intento cuyo move ya fue autorizado por otro camino (MEDIUM, STATE-03: `_l10n_ar_arca_apply_reconciliation` no limpia el CAE al degradar).

---

## E. Matriz de cobertura

| Área | Estado | Código | Tests | Riesgo |
| --- | --- | --- | --- | --- |
| Odoo EDI (`account_edi`) | **No usado, deliberado** | `__manifest__.py` depende solo de `l10n_ar` | — | INFO. `account.edi.format`/`account.edi.document` **sí existen** en Odoo 19 (verificado en `odoo/19.0/addons/account_edi/models/`). Divergencia consciente, documentada en `AUDIT.md` M8 |
| Posting | Correcto | `account_move.py:108-139` | `test_authorization.py` | ✔ ARCA fuera de la transacción de posting |
| Selección de diario | Correcto | `account_journal.py:31-70` | `test_numbering.py:83+` | ✔ `@api.constrains` exige diario de venta AR + punto de venta |
| Punto de venta | Parcial | `account_move.py:289-297` | `test_numbering.py` | MEDIUM: no se valida contra `FEParamGetPtosVenta`; el flag `blocked` se parsea y se descarta |
| Tipo de comprobante | Correcto | `account_move.py:531-577` | `test_payload.py` | ✔ Delegado a `l10n_ar`; la regla ingenua "CUIT⇒A" **no** está reimplementada |
| Receptor | Parcial | `account_move.py:626-717` | `test_payload.py` | MEDIUM: cliente identificado puede caer silenciosamente a DocTipo 99 en clase B/C |
| Impuestos | Parcial | `account_move.py:765-803` | `test_payload.py` (30 tests) | **HIGH — H-04**: `ImpTrib` sin `Tributos` |
| Redondeo | Correcto | `float_round(..., 2)` + tolerancia 10048 | `test_payload.py:310` | ✔ Tolerancia correcta respecto de ARCA |
| Moneda | Correcto | `account_move.py:597-624` | `test_payload.py` | ✔ Invierte `invoice_currency_rate` explícitamente. MEDIUM: asume ARS si moneda = moneda compañía |
| CAE | Correcto | `account_move.py:368-387` | `test_authorization.py` | MEDIUM: `Resultado='P'` aceptado sin CAE |
| Vencimiento CAE | Correcto (dato) / **roto (impresión)** | `_parse_arca_date` | ninguno | **HIGH — H-01** |
| Idempotencia | Fuerte | advisory lock + índice único + `check_ready` | `test_concurrency.py` (15) | MEDIUM: el índice omite `environment` y se scopea por `company_id` mientras el lock se scopea por CUIT |
| Concurrencia | Fuerte | `fiscal_transaction.py` | `test_concurrency.py` con PG real | ✔ Sólido a nivel primitiva. HIGH-de-cobertura: no end-to-end por el módulo |
| Timeout | Correcto | `_classify_transport_exception` | `test_transport.py` (17) | ✔ `ReadTimeout`⇒incierto, `ConnectTimeout`⇒abortado |
| Reconciliación | **Débil** | `attempt.py:209-291` | `test_authorization.py:377+` | **HIGH — H-03** + MEDIUM (no valida el comprobante) |
| Notas de crédito | Parcial | `account_move.py:719-763` | `test_payload.py` | MEDIUM: no verifica que el original esté autorizado ni que el receptor coincida |
| Homologación | Aislada por certificado | `certificate.environment` | `test_homologation.py` (1, `-standard`) | ✔ Endpoints separados. **HIGH — H-07**: el ambiente no queda en el move |
| Producción | **Sin fail-closed** | — | ninguno | **HIGH — H-06** |
| Certificados | Bien protegidos, mal autorizados | `certificate.py` | `test_certificate.py` (23), `test_security.py` (9) | ✔ `groups=base.group_system`. **HIGH — H-05**: `sudo()` sin `check_access` |
| Multiempresa | Correcto | `arca_security.xml` | `test_security.py` | ✔ `ir.rule` global sobre certificados e intentos |
| Mercado Libre | **Sin acoplamiento** | — | — | ✔ **Cero** referencias a MeLi en el addon ARCA (grep exhaustivo) |

---

## F. Hallazgos detallados

> Solo se listan los que **sobrevivieron** la refutación independiente y que además verifiqué personalmente. Hallazgos duplicados entre dimensiones fueron consolidados.

---

```text
ID:            H-01
Severidad:     HIGH
Título:        La representación impresa falla en TODA factura autorizada
Estado:        BUG CONFIRMADO
Repositorio:   JoaPeralta/l10n_ar_arca_edi
SHA:           9c41e7db919577629e745d8ff431e76b47cb53f2
Archivo:       reports/report_invoice.xml
Líneas:        158-161
Clase:         template custom_footer (hereda l10n_ar.custom_header / report_invoice)
Método:        bloque t-set / t-if
```
**Evidencia** (literal en el árbol auditado):
```xml
<t t-set="cae_due" t-value="o.l10n_ar_arca_cae_due_date"/>
<span t-if="cae_due and len(cae_due) == 8"
      t-out="'%s/%s/%s' % (cae_due[6:8], cae_due[4:6], cae_due[0:4])"/>
```
Y en `models/account_move.py:57`: `l10n_ar_arca_cae_due_date = fields.Date(...)`.

**Comportamiento actual:** `o.l10n_ar_arca_cae_due_date` devuelve un `datetime.date`. `len(datetime.date)` lanza `TypeError`. El `and` no cortocircuita porque la fecha es *truthy*. Todo el bloque está dentro de `t-if="o.l10n_ar_arca_cae"` (línea 143), es decir **se alcanza exactamente cuando la factura está autorizada**.

**Origen:** es una regresión del propio fix `L2` documentado en `AUDIT.md` ("`l10n_ar_arca_cae_due_date` era `Char` con `YYYYMMDD`. Ahora es `Date`") — se cambió el campo y no la plantilla que dependía de la forma `Char`.

**Impacto fiscal:** la RG 4892/2020 exige CAE, vencimiento y QR en el comprobante impreso. El CAE se obtiene y persiste bien, pero **el comprobante no se puede emitir al cliente**.

**Escenario reproducible:** postear una factura AR en diario ARCA → autorizar → Imprimir. QWeb evalúa `len(datetime.date(2026,12,31))` → `TypeError` → `QWebException`, sin PDF.

**Test existente:** ninguno. `grep -rn "_render_qweb|report_action|render" tests/*.py` → 0 coincidencias en 12 archivos y 195 métodos.
**Test faltante:** autorizar y llamar `ir.actions.report._render_qweb_html` sobre el reporte de factura AR, aseverando que renderiza y que aparece el vencimiento formateado.
**Solución conceptual:** eliminar la rama de *string slicing* y formatear el objeto `Date` con las utilidades de Odoo (`t-field`, `format_date`).
**Dependencias:** ninguna.

---

```text
ID:            H-02
Severidad:     HIGH
Título:        button_draft no bloquea 'processing': una factura con CAE posiblemente emitido puede volver a borrador y editarse
Estado:        BUG CONFIRMADO
Archivo:       models/account_move.py
Líneas:        1035-1055
Clase:         AccountMove
Método:        button_draft
```
**Evidencia:** el método filtra `authorized` (1037) y `uncertain` (1046) y llama `super().button_draft()` (1055). `processing` nunca se evalúa — pese a que `_l10n_ar_arca_check_ready` (248-255) lo trata como **igual de peligroso** que `uncertain` ("A request for this invoice is already in flight").

**Comportamiento actual:** tras un crash posterior al `checkpoint` (estado `processing` commiteado, `attempt` en `sent` — exactamente el fixture de `test_authorization.py:385-404`), `button_draft()` tiene éxito. La factura se edita y se re-postea. El `attempt` abierto sigue nombrando el punto de venta, tipo y número **originales**; cuando el cron reconcilia y ARCA sí tiene el comprobante, `_l10n_ar_arca_apply_reconciliation` (447-466) estampa ese CAE sobre el move ya editado.

**Impacto fiscal:** divergencia entre el registro contable y el fiscal sobre un documento que no se puede des-emitir. El QR se construye desde el move **actual** (`_l10n_ar_arca_qr_payload`, 955-985), así que la página de verificación de ARCA contradice el papel.

**Test existente:** `test_authorization.py:260-273` cubre `button_draft` solo para `authorized` y `uncertain`. `test_a_processing_invoice_refuses_a_second_request` cubre re-autorización, no reset.
**Test faltante:** `test_a_processing_invoice_cannot_be_reset_to_draft` sobre el fixture `_crashed_invoice()`.
**Solución conceptual:** incluir `processing` en el conjunto de rechazo. Mejor: derivar la guarda de la existencia de intentos abiertos (`_find_open_for_move`) en vez del estado del move, para que ambos no puedan divergir.
**Dependencias:** ninguna. Conviene junto con la verificación de coordenadas en `_l10n_ar_arca_apply_reconciliation`.

---

```text
ID:            H-03
Severidad:     HIGH
Título:        El botón "Reconciliar" evade el lock de secuencia, la ventana de obsolescencia y la transacción fiscal
Estado:        BUG CONFIRMADO
Archivo:       models/account_move.py
Líneas:        437-445  (UI: views/account_move_views.xml:21-25 · contraparte cron: models/l10n_ar_arca_attempt.py:263-291)
Clase:         AccountMove
Método:        action_l10n_ar_arca_reconcile
```
**Evidencia:** la ruta manual es literalmente:
```python
attempts = self.env["l10n_ar.arca.attempt"]._find_open_for_move(self)
for attempt in attempts:
    attempt._reconcile()
```
La ruta del cron, para la operación idéntica, está envuelta (`attempt.py:276-279`) en `fiscal_transaction(...)` + `checkpoint()`, y además rechaza todo `attempt` en `sent` más joven que `STALE_ATTEMPT_MINUTES` (306-318). `_find_open_for_move` (134-139) **no tiene filtro de edad** y devuelve intentos en `sent`.

**Comportamiento actual:** reproduce exactamente la condición de carrera que el propio `AUDIT.md` R4 dice haber corregido — pero solo la corrigió en el cron.

**Impacto fiscal:** con una factura en `processing` y un request realmente en vuelo, `FECompConsultar` responde "no existe" porque ARCA aún no lo registró; `_reconcile` marca el intento `aborted` y `_l10n_ar_arca_apply_reconciliation(authorized=False)` (467-482) escribe `l10n_ar_arca_state='pending'` **sin ninguna guarda sobre el estado actual**. El número queda liberado para un segundo `FECAESolicitar` mientras ARCA registra el primero.

**Escenario reproducible:** factura en `processing`, contador presiona "Reconciliar con ARCA" a los 30 s. ARCA aún no registró → intento `aborted`, move `pending`. ARCA registra el comprobante. La factura queda `pending` con un comprobante autorizado en ARCA y un intento `aborted` que el cron nunca volverá a mirar (su dominio es `state in ('sent','uncertain')`).

**Test existente:** `test_authorization.py:448-457` (`test_the_reconciler_leaves_a_request_that_may_still_be_running`) asevera exactamente esta propiedad — **para el cron**.
**Test faltante:** el espejo para el botón: intento `sent` fresco + `action_l10n_ar_arca_reconcile` no debe consultar a ARCA.
**Solución conceptual:** enrutar el botón por `_reconcile_isolated`, heredando lock, transacción dedicada y ventana; exponer `ArcaSequenceBusy` como el mismo `UserError` "probá en un momento" que ya usa la ruta de autorización.
**Dependencias:** ninguna.

---

```text
ID:            H-04
Severidad:     HIGH
Título:        Se envía ImpTrib sin el array Tributos
Estado:        FUNCIONALIDAD FALTANTE (no declarada como fuera de alcance)
Archivo:       models/account_move.py
Líneas:        786, 795 (_l10n_ar_arca_amounts) · 891 (_l10n_ar_arca_prepare_request)
Clase:         AccountMove
Método:        _l10n_ar_arca_amounts / _l10n_ar_arca_prepare_request
```
**Evidencia:** línea 795 asigna `"ImpTrib": amounts["not_vat_taxes_amount"]`; el `detail` construido en 877-895 contiene `ImpTrib` y **ninguna** clave `Tributos`. `grep -rn "Tributos|Tributo"` sobre todo el árbol auditado no devuelve código que construya una entrada `Tributo`. `readme/ROADMAP.rst` lista como limitaciones conocidas: documentos de exportación, FCE, CAEA, lotes, tabla RG 5616 y padrón — **no lista Tributos**.

**Comportamiento actual:** `test_payload.py:191-221` construye una percepción IIBB y asevera `detail["ImpTrib"] > 0`. Es decir, el módulo **efectivamente envía** `ImpTrib` distinto de cero sin detalle.

**Impacto fiscal:** el manual del desarrollador define `ImpTrib` como la sumatoria de los `Importe` del array `Tributos`. Si ARCA aplica la correspondencia, **toda factura con percepción o impuesto interno es rechazada en `FECAESolicitar`** — es decir, un Responsable Inscripto sujeto a regímenes de percepción de IIBB no puede facturar. Si ARCA la acepta, el comprobante queda autorizado declarando un monto de "otros tributos" sin identificar el régimen.

**Escenario reproducible:** factura clase A, una línea de 100,00 al 21 % más percepción IIBB 3 %. Se envía `ImpNeto 100,0 · ImpIVA 21,0 · ImpTrib 3,0 · ImpTotal 124,0` y ningún grupo `Tributos`.

**Test existente:** `test_payload.py:172-189` y `191-221` aseveran solo el escalar.
**Test faltante:** aseverar que una percepción produce `detail["Tributos"]["Tributo"]` con `Id` tomado del código de tributo AFIP del grupo de impuesto, `Desc`, `BaseImp`, `Alic`, `Importe`, y que `sum(Importe) == ImpTrib`.
**Solución conceptual:** construir el grupo `Tributos` desde los mismos `base_lines`, con una aserción local de que la suma coincide con `ImpTrib` antes de enviar. Alternativa mínima coherente con la filosofía del módulo: **rechazar explícitamente** las facturas con `ImpTrib != 0` y declararlo en el ROADMAP.
**Dependencias:** requiere el mapeo de código de tributo de `l10n_ar` (no verificable sin Odoo instalado — ver §N).

---

```text
ID:            H-05
Severidad:     HIGH  (propuesto CRITICAL, degradado por el refutador: destruye, no filtra)
Título:        Escalada de privilegios: sudo() sin control de acceso permite a un usuario de solo lectura destruir la clave privada
Estado:        BUG CONFIRMADO
Archivo:       models/l10n_ar_arca_certificate.py
Líneas:        473-484 (action_revoke) · 370-425 (action_process_certificate) · 294-357 (action_generate_key_and_csr)
               ACL: security/ir.model.access.csv:2
Clase:         L10nArArcaCertificate
Método:        action_revoke (y las otras dos acciones públicas)
```
**Evidencia:**
```python
def action_revoke(self):
    self.ensure_one()
    self.sudo().write({"state": "revoked", "l10n_ar_arca_token_cache": False, "private_key": False})
```
Método público ⇒ expuesto por `call_kw`. Sin guarda de estado y sin control de acceso: `grep -rn "check_access" models/ wizards/` → **cero coincidencias en todo el módulo**. `ensure_one()` y la lectura implícita de `self.id` requieren solo `perm_read`, que la ACL concede al grupo de facturación:
`access_l10n_ar_arca_certificate_invoice,…,account.group_account_invoice,1,0,0,0`.
El `write` posterior corre como superusuario, evadiendo el `perm_write=0` que debía impedirlo.

**Impacto:** pérdida total e irreversible de la capacidad de emitir. La clave no se puede regenerar en el lugar (`action_generate_key_and_csr` rechaza sobre un certificado activo, 296-305); recuperarse exige nuevo CSR, login con clave fiscal en el portal ARCA, emisión de nuevo certificado y re-autorización en WSASS. Toda factura esperando CAE se detiene mientras dure.

**Test existente:** `test_certificate.py:248-251` asevera `AccessError` solo sobre `.write({"name": ...})`.
**Test faltante:** para cada acción pública del modelo, aseverar que un usuario `account.group_account_invoice` recibe `AccessError` y que la clave sigue presente.
**Solución conceptual:** toda acción pública que termine en `self.sudo().write(...)` debe primero aseverar los derechos reales del llamador (`self.check_access('write')` en la API de Odoo 18/19) antes de escalar. Conceptualmente, el `sudo()` debe acotarse al campo protegido, no envolver la decisión de autorización.
**Dependencias:** ninguna.

---

```text
ID:            H-06
Severidad:     HIGH
Título:        Sin fail-closed ante base restaurada o neutralizada: una copia habla con ARCA real
Estado:        FUNCIONALIDAD FALTANTE
Archivo:       __manifest__.py:55-66 (lista data) — ausencia a nivel módulo
Clase:         —
Método:        —
```
**Evidencia:** búsqueda sobre los 55 archivos del módulo de `neutrali|config_parameter|tools.config|_neutralize|test_mode|dbfilter|db_name|list_db` → **0 coincidencias**. No existe `data/neutralize.sql`. El mecanismo de neutralización de Odoo ejecuta el `neutralize.sql` de cada módulo instalado y fija `ir.config_parameter database.is_neutralized`; este módulo no aporta el archivo ni lee ese parámetro.

**Impacto fiscal:** una copia de una base productiva es fiscalmente indistinguible de producción. Puede solicitar CAE reales bajo el CUIT real (irreversible: el número se consume en ARCA) y, como mínimo, consume el ticket WSAA de la compañía. En hosts tipo odoo.sh, restaurar a staging es una operación rutinaria y desatendida.

**Atenuante honesto:** la neutralización nativa de Odoo **sí** desactiva los `ir.cron`, lo que cubre el camino del cron en una restauración *correctamente neutralizada*. No cubre: restauraciones sin neutralizar (`pg_dump`/`pg_restore` a mano), ni el camino `postcommit` de `auto_request_cae`, que dispara con solo postear una factura.

**Test existente:** ninguno (`grep -i "neutral" tests/` → 0).
**Test faltante:** con `database.is_neutralized` fijado, `_l10n_ar_arca_request_cae` debe fallar **antes** de cualquier llamada de transporte y `service.requests` debe quedar vacío.
**Solución conceptual:** dos capas complementarias. (a) Enviar `data/neutralize.sql` que limpie el cableado fiscal (revocar certificados / forzar `environment='testing'`, anular `private_key` y el cache de ticket, desvincular `res_company.l10n_ar_arca_certificate_id`, apagar `auto_request_cae`). (b) Una guarda única en `_l10n_ar_arca_check_ready` que lea `database.is_neutralized` y rechace.
**Dependencias:** ninguna.

---

```text
ID:            H-07
Severidad:     HIGH
Título:        El ambiente ARCA no se registra en el account.move: una factura de homologación es indistinguible de una real
Estado:        FUNCIONALIDAD FALTANTE
Archivo:       models/account_move.py:226-263, 517-529
Clase:         AccountMove
Método:        _l10n_ar_arca_check_ready / _l10n_ar_arca_get_certificate
```
**Evidencia:** `_l10n_ar_arca_get_certificate` devuelve el certificado de la compañía y llama `_check_usable()`, que verifica revocado/expirado/activo/clave/certificado (`certificate.py:200-235`) y **nada sobre el ambiente**. `grep -n "environment" models/account_move.py` devuelve **una sola** coincidencia, la línea 314, que solo copia `certificate.environment` al `attempt`. `account.move` **no tiene campo de ambiente**.

**Impacto fiscal:** una factura con CAE de homologación no es un documento fiscal válido, pero nada en el move, el reporte ni el QR lo distingue. Una compañía que pasa a producción editando el certificado, o que restaura producción en staging y emite allí, produce documentos que **se leen como autorizados y no lo son**. Descubrirlo tarde implica re-emitir todos los comprobantes afectados.

**Test existente:** `test_homologation.py` fija su propio certificado a `environment='testing'` y lo asevera — buena guarda para CI, nada sobre el move.
**Test faltante:** una factura autorizada con certificado de testing debe quedar marcada como tal en el move y ser distinguible estructuralmente de una autorización de producción.
**Solución conceptual:** registrar la procedencia donde vive la consecuencia: almacenar el ambiente en `account.move` al autorizar (copiándolo del certificado igual que ya hace el `attempt` en :314), mostrarlo como badge en el formulario y como marca de agua visible en el impreso cuando no sea `production`.
**Dependencias:** conviene junto con H-01 (ambos tocan el reporte).

---

### Hallazgos MEDIUM destacados (consolidados, verificados)

| ID | Título | Archivo:líneas | Estado |
| --- | --- | --- | --- |
| M-01 | La reconciliación adopta cualquier comprobante en esa coordenada sin verificar `Resultado`, ni que haya CAE, ni que `ImpTotal`/`DocNro` correspondan a esta factura | `attempt.py:238-260` | confirmado |
| M-02 | `Resultado='P'` se acepta como éxito; el guard `outcome=='A' and not cae` no cubre `'P'` → factura `authorized` con CAE vacío | `l10n_ar_arca_wsfe.py:450-457` | confirmado |
| M-03 | El índice único omite `environment`: un intento de homologación colisiona con el de producción del mismo número | `attempt.py:124-128` | confirmado |
| M-04 | El índice único se scopea por `company_id` mientras el lock se scopea por CUIT emisor — dos compañías con el mismo CUIT quedan fuera del backstop que el propio comentario promete | `attempt.py:124-128` vs `account_move.py:166-176` | confirmado |
| M-05 | El grupo de facturación tiene `write`/`create` sobre el registro de auditoría fiscal; `readonly=True` no lo impide vía RPC | `security/ir.model.access.csv:5` | confirmado |
| M-06 | `action_process_certificate` y `action_generate_key_and_csr` comparten el patrón `sudo()` sin `check_access` de H-05 | `certificate.py:370-425, 294-357` | confirmado |
| M-07 | Cliente identificado reportado silenciosamente como DocTipo 99 / DocNro 0 en clase B y C | `account_move.py:636-646` | confirmado |
| M-08 | El compute del QR traga `UserError`: una factura autorizada puede imprimirse sin QR | `account_move.py:945-953` | confirmado |
| M-09 | La topología de cursor dedicado nunca se ejercita con registros ORM: todos los tests ORM toman la rama `dedicated=False` | `fiscal_transaction.py:86` | confirmado |
| M-10 | `CbtesAsoc` no verifica que el comprobante original haya sido autorizado en ARCA ni que el receptor coincida con el de la nota | `account_move.py:719-763` | confirmado |
| M-11 | El cron de reconciliación está **activo por defecto** y autentica contra el ambiente del certificado sin guarda de copia | `data/ir_cron_data.xml` | riesgo |
| M-12 | Un `zeep.Fault` en `FECAESolicitar` se clasifica como rechazo determinista, lo que contradice la doctrina de asimetría del propio módulo | `l10n_ar_arca_wsfe.py:153-159` | riesgo |

---

## G. Auditoría transaccional

**Cursor RPC (del llamador).** Nunca se commitea por el módulo. Se usa solo para leer en `_l10n_ar_arca_check_ready` y para calcular la clave de lock. Correcto: un módulo no tiene por qué decidir la atomicidad de la petición del usuario.

**Cursores independientes.** `fiscal_transaction` abre **dos** vía `env.registry.cursor()`:
- **#1, lock:** `pg_try_advisory_xact_lock(key)`. Transaction-scoped a propósito. El razonamiento del código es correcto y está probado: un lock *session-scoped* sobrevive al `ROLLBACK`, y `Cursor._close()` devuelve la conexión al pool tras un rollback — dejando una conexión viva en el pool sosteniendo un lock fiscal que nadie puede liberar (`test_a_sql_error_that_aborts_the_transaction_releases_the_lock` demuestra exactamente esto).
- **#2, trabajo:** el `Environment` sobre el que corre el protocolo, con `SET LOCAL lock_timeout='10s'` rearmado tras cada commit.

**Commits.** Solo en `FiscalTransaction.checkpoint()`, y solo sobre el cursor #2. Tres puntos: antes del envío (evidencia durable), tras clasificar el resultado, y tras el éxito. Además, `Cursor.__exit__` de Odoo commitea al salir sin excepción.

**Rollbacks.** `discard()` existe pero es **código muerto** (nadie lo llama). Los rollbacks efectivos son los implícitos de `Cursor.__exit__` ante excepción.

**Locks.** Advisory por `(CUIT emisor, punto de venta, tipo de comprobante)` — la granularidad correcta, porque ARCA numera por esa tripleta. `pg_try_advisory_xact_lock` es no bloqueante: la colisión se reporta como `ArcaSequenceBusy` → `UserError` legible, no como espera.

**Postcommit.** `_l10n_ar_arca_schedule_after_commit` registra en `cr.postcommit`. Odoo limpia estos callbacks ante rollback, así que una factura que no llega a disco tampoco llega a ARCA. El callback captura `env` del cursor RPC; como `_l10n_ar_arca_request_cae` abre sus propias conexiones, el trabajo fiscal no depende de ese cursor — pero el `browse(...).exists()` inicial sí lo usa después del commit (MEDIUM de robustez, no fiscal).

**Operación remota.** Estrictamente entre `checkpoint()` y el manejo del resultado. Ninguna operación irreversible depende del rollback del cursor principal.

**Estados inciertos.** Modelados explícitamente (`ArcaUncertain`) y bloqueantes. La clasificación de transporte es conservadora por diseño: todo lo que no sea *provablemente* falla de conexión se trata como incierto.

**Reconciliación.** Correcta y protegida en el cron; **desprotegida en el botón** (H-03).

**Veredicto transaccional:** no encontré ningún caso donde una operación remota irreversible dependa del rollback del cursor principal, ni donde un CAE autorizado se pierda por rollback. El diseño transaccional es el punto más fuerte del módulo. El defecto real es de **cobertura** (M-09): esa topología nunca se ejercita con datos ORM.

---

## H. Auditoría del payload fiscal

| Campo ARCA | Origen Odoo | Validación | Fallback | Riesgo |
| --- | --- | --- | --- | --- |
| `Auth.Token/Sign` | WSAA por certificado y servicio | cache con margen 15 min | reautentica | ✔ nunca logueado |
| `Auth.Cuit` | `company.partner_id.vat` | dígito verificador | **ninguno** (falla) | ✔ correcto: es el representado, no el titular |
| `PtoVta` | `journal.l10n_ar_afip_pos_number` | coherencia con nº de factura (289-297) | ninguno | MEDIUM: no se valida contra `FEParamGetPtosVenta` |
| `CbteTipo` | `l10n_latam_document_type_id.code` | lista blanca WSFEv1; rechaza E y FCE | ninguno | ✔ delegado a `l10n_ar` |
| `Concepto` | `l10n_ar_afip_concept` | — | **silencioso a 1 (Productos)** | MEDIUM: además saltea fechas de servicio obligatorias |
| `DocTipo`/`DocNro` | `commercial_partner_id` | clase A/M exige CUIT; solo dígitos | **99 / 0 silencioso** | MEDIUM (M-07) |
| `CbteDesde`/`Hasta` | `l10n_latam_document_number` | `_l10n_ar_arca_check_sequence` vs ARCA | ninguno | ✔ nº de Odoo, nunca derivado del contador ARCA |
| `CbteFch` | `invoice_date` | — | `context_today` | MEDIUM: sin validación de ventana de emisión |
| `FchServDesde/Hasta/VtoPago` | campos `l10n_ar_afip_service_*` | fin ≥ inicio | fecha de factura | ✔ |
| `MonId`/`MonCotiz` | `currency_id.l10n_ar_afip_code` | exige código y cotización | ninguno | ✔ invierte `invoice_currency_rate` explícitamente. MEDIUM: asume ARS si moneda = compañía |
| `ImpTotal` | suma de los cinco componentes | tolerancia 10048 real | ninguno | ✔ |
| `ImpNeto`/`ImpTotConc`/`ImpOpEx`/`ImpIVA` | `l10n_ar._l10n_ar_get_amounts` | clase C fuerza todo a ImpNeto | ninguno | ✔ delegado. MEDIUM: clase C pone en cero `ImpOpEx`/`ImpTotConc` sin plegarlos a `ImpNeto` |
| `ImpTrib` | `not_vat_taxes_amount` | — | ninguno | **HIGH — H-04: sin array `Tributos`** |
| `Iva.AlicIva` | `_get_vat` | rechaza códigos 0/1/2; agrega por alícuota (10022) | ninguno | ✔ correcto |
| `Tributos` | — | — | **NUNCA SE ENVÍA** | **HIGH — H-04** |
| `CbtesAsoc` | `reversed_entry_id` / `debit_origin_id` | tabla 10040 | exige origen (error) | MEDIUM: uno solo; sin `Cuit`; no verifica autorización del original |
| `CondicionIVAReceptorId` | `l10n_ar_afip_responsibility_type_id.code` | tabla por clase (10243) | ninguno | ✔. MEDIUM: tabla fija, no consulta `FEParamGetCondicionIvaReceptor` |
| `Opcionales`/`Actividades` | — | — | no se envían | INFO: fuera de alcance declarado (FCE) |
| resp. `CAE`/`CAEFchVto` | — | — | — | MEDIUM: `CAEFchVto` inparseable se descarta en silencio |
| resp. `Resultado` | — | `A` ok, `R` rechaza, otro incierto | — | MEDIUM (M-02): `P` aceptado sin CAE |
| resp. `Observaciones` | — | se persisten | — | ✔ |
| resp. `Eventos` | — | — | **nunca se lee** | LOW |

**Casos de importe — estado real:**

| Caso | Estado |
| --- | --- |
| Múltiples alícuotas en una factura | ✔ implementado y agregado por alícuota |
| Exento / no gravado / no alcanzado | ✔ delegado correctamente a `l10n_ar` (fue el hallazgo C5 de su propia auditoría) |
| Percepciones / impuestos internos | ⚠ **importe enviado, detalle omitido** (H-04) |
| Descuentos, líneas negativas, recargos, envío | ✔ absorbidos por `_l10n_ar_get_amounts` |
| Impuestos incluidos en precio | ✔ vía `_get_rounded_base_and_tax_lines` |
| Moneda extranjera | ✔ implementado |
| Anticipos | ⚠ no verificable sin Odoo instalado (§N) |
| Clase C | ✔ implementado (todo a `ImpNeto`, sin `Iva`) |
| Factura E / FCE / CAEA | ✔ **rechazados explícitamente** con mensaje |

---

## I. Auditoría de seguridad

*No se reproduce ningún valor sensible.*

| Secreto/dato | Almacenamiento | ACL | Logs | Exposición | Riesgo |
| --- | --- | --- | --- | --- | --- |
| Clave privada RSA | campo `Binary(attachment=True)`, PEM **sin cifrar** (`NoEncryption()`) | `groups="base.group_system"` | nunca | dump de BD / filestore | MEDIUM. Práctica habitual en el ecosistema Odoo; el vector real es el backup |
| Contraseña de la clave | no existe (clave sin passphrase) | — | — | — | LOW, consecuencia del anterior |
| Certificado X.509 | `Binary(attachment=True)` | sin `groups` | metadatos sí | vistas | ✔ correcto, es material público |
| Token / Sign WSAA | `fields.Json` en el certificado | `groups="base.group_system"` | nunca (test lo verifica) | — | ✔ bien protegido |
| Payload de request | `attempt.request_payload` (Text) | grupo facturación r/w | — | — | ✔ sin credenciales (test lo verifica). MEDIUM: escribible (M-05) |
| CUIT emisor / receptor | campos normales | grupo facturación | sí, en INFO | log | ✔ aceptable: es dato de comprobante, no secreto |
| **Acciones públicas con `sudo()`** | — | **sin `check_access`** | — | **RPC** | **HIGH — H-05** |

**Aislamiento multiempresa:** `ir.rule` global sobre `l10n_ar.arca.certificate` y `l10n_ar.arca.attempt` con `[('company_id','in',company_ids)]`. Correcto y con tests. Además, `@api.constrains` en `res.company` impide asignar el certificado de otra compañía.

**Superficies revisadas sin hallazgo:** `_logger` (nunca imprime token/sign/clave), mensajes de `ValidationError`/`UserError`, `message_post`, `request_payload`/`response_payload`, fixtures y README. La única observación es de CI: el job de homologación sube un log completo de Odoo como artefacto desde un paso que tiene la clave de homologación en el entorno (LOW, y es material de homologación, no productivo).

---

## J. Frontera con Mercado Libre

### Aislamiento del addon ARCA — verificado y limpio

Búsqueda exhaustiva sobre el árbol auditado de `meli|mercado|MELI|pack_id|order_id|access_token|fiscal_document|billing_info|api.mercadolibre` (case-insensitive): **cero coincidencias funcionales**. Los únicos aciertos son la frase "mercado interno" (término de ARCA para operaciones domésticas) en el manifest, el README y un comentario.

Se cumplen los nueve requisitos: no importa `meli_oerp`, no conoce tokens, ni order IDs, ni pack IDs, no llama a MeLi, no sube PDFs, no interpreta notificaciones, no cambia estados de órdenes y no depende de billing info. **Esto es una fortaleza, no un no-hallazgo.**

### Qué hace hoy `meli_oerp` (SHA `236dc4eb…`)

- **Sí** enriquece datos fiscales del partner: asigna `l10n_ar_afip_responsibility_type_id` (21 referencias) y `l10n_latam_identification_type_id` (35).
- **Sí** normaliza billing info legacy y v2 (`_normalize_billing_info_v2`), cubriendo el cambio de abril 2026 de `/orders/{id}/billing_info` al nuevo `/orders/billing-info/{SITE_ID}/{BILLING_INFO_ID}` con header `x-version: 2`.
- **No** asigna `l10n_latam_document_type_id` (0 referencias) — correcto: eso lo resuelve `l10n_ar`.
- **Defecto confirmado:** `sale_order.meli_create_invoice()` (`orders.py:543-549`) llama `self.action_invoice_create()`. Ese método **no existe** en `sale.order` de Odoo 19 (verificado contra `odoo/19.0/addons/sale/models/sale_order.py`: existe `_create_invoices`, no `action_invoice_create`) y `meli_oerp` no lo define. **La facturación automática desde MeLi está rota en 19.0** — falla con `AttributeError` en vez de crear una factura incorrecta.
- **Riesgo latente para el futuro bridge:** en `_normalize_billing_info_v2` (`orders.py:1710-1714`) hay un fallback silencioso: `CUIT` con `taxpayer_type` vacío ⇒ `INVOICE_TYPE = 'Factura A'`, con el comentario "RI es lo más común con CUIT". Hoy ese string **no** llega al tipo de comprobante de Odoo, así que no tiene efecto fiscal. Si un bridge futuro lo consume, emitiría Factura A a un monotributista. El propio código ya advierte, dos líneas antes, que "CUIT solo NO implica Factura A".

### Contrato oficial de Mercado Libre (leído por MCP, `es_ar`/MLA, página `cargar-factura`)

Requisitos vinculantes para un bridge futuro:

- `POST https://api.mercadolibre.com/packs/{PACK_ID}/fiscal_documents`, `multipart/form-data`, campo `fiscal_document`.
- **Máximo 1 MB**, PDF; opcionalmente XML adjunto (`application/pdf`, `application/xml`, `text/xml`). **Un solo fiscal_document por tipo y por pack.**
- Si `pack_id` viene `null` en `/orders`, usar el order ID **manteniendo el recurso `/packs`** en la URL.
- **La idempotencia se expresa como `409 conflict`**, no como éxito silencioso: reintentar una subida ya exitosa devuelve `"File Not allowed, a file already exists for the pack: $PACK_ID and seller: $SELLER_ID of the type: $FILE_TYPE"`. **El bridge debe tratar ese 409 como éxito**, no como error reintentable.
- Otros estados: `400` (archivo vacío, tipo no permitido, excede tamaño, filename vacío), `403` (no autorizado; también logística fulfillment/cross-docking, aplica a MLC y MLB, **no a MLA**), `404` (pack sin facturas), `500`.
- `DELETE /packs/{PACK_ID}/fiscal_documents` borra **todos** los archivos del pack.
- MeLi envía notificación y email al comprador al cargar la factura; si hay mensajes automáticos programados, deben cancelarse para evitar moderación.

### División de responsabilidades recomendada

| Componente | Debe hacer | Nunca debe hacer |
| --- | --- | --- |
| `meli_oerp` | Traer órdenes/packs; normalizar billing info; poblar identidad fiscal del partner; crear el `account.move` **fiscalmente completo** (arreglando `action_invoice_create` → `_create_invoices`) | Decidir el tipo de comprobante por heurística propia; llamar a ARCA |
| **Bridge futuro** (módulo nuevo, separado) | Observar `l10n_ar_arca_state == 'authorized'`; renderizar PDF/XML; subir a `/packs/{id}/fiscal_documents`; tratar 409 como éxito; persistir el `fiscal_document id`; reintentar solo fallas de canal | Solicitar CAE; modificar estado ARCA; reintentar ARCA por falla de canal |
| `l10n_ar_arca_edi` | Recibir un `account.move` ya configurado y autorizarlo en ARCA | Todo lo de Mercado Libre — **hoy cumple** |

**Invariantes que el bridge debe respetar:** una falla de subida a MeLi no puede tocar el CAE; el estado ARCA es independiente del estado de upload; la subida solo ocurre **después** de `authorized`; una falla de canal **nunca** puede disparar un nuevo `FECAESolicitar`.

### Deployment (`odoo-viarengo` @ `3cb1f71…`)

**Fortaleza:** el `Dockerfile` fija ambos addons por SHA exacto y **verifica** el checkout (`test "$(git rev-parse HEAD)" = "$SHA"`), además de comprobar que `meli_oerp` sigue pineando su SDK. Build reproducible.

**Hallazgo:** `ARG ARCA_EDI_SHA=156ab474c95a7c3a33ab5536b91ec8e93b961c58` está **6 commits por detrás** del HEAD auditado. Lo desplegado no es lo auditado. Los commits faltantes son de CI/tests/docs más `fix(homologation): keep one run to one ARCA access ticket` — ninguno es un fix fiscal, pero la divergencia debe cerrarse antes de cualquier conclusión operativa. Igualmente, `MELI_OERP_SHA=e4c9cc19…` está por detrás de `19.0` (`236dc4eb…`).

---

## K. Plan de PRs pequeños

*Regla: un hallazgo = una rama = un PR pequeño. **No se creó ninguna rama ni PR.***

```text
Orden:        1
Título:       fix(report): render the CAE due date as a date, not as a string
Repositorio:  JoaPeralta/l10n_ar_arca_edi
Branch:       fix/report-cae-due-date
Hallazgo:     H-01
Archivos:     reports/report_invoice.xml · tests/test_representation.py
Cambio mínimo: eliminar la rama len()/slicing; formatear el objeto Date.
Tests:        un test que autorice y renderice el reporte por _render_qweb_html.
NO incluir:   ningún otro cambio de estilo del reporte, ni el badge de ambiente.
Dependencias: ninguna.
Riesgo:       muy bajo.
```
```text
Orden:        2
Título:       fix(state): refuse to reset an in-flight invoice to draft
Branch:       fix/button-draft-processing
Hallazgo:     H-02
Archivos:     models/account_move.py · tests/test_authorization.py
Cambio mínimo: incluir 'processing' en la guarda de button_draft.
Tests:        test_a_processing_invoice_cannot_be_reset_to_draft sobre _crashed_invoice().
NO incluir:   la verificación de coordenadas en apply_reconciliation (PR 4).
Riesgo:       muy bajo.
```
```text
Orden:        3
Título:       fix(recover): route the reconcile button through the guarded path
Branch:       fix/reconcile-button-uses-isolated-path
Hallazgo:     H-03
Archivos:     models/account_move.py · models/l10n_ar_arca_attempt.py · tests/test_authorization.py
Cambio mínimo: action_l10n_ar_arca_reconcile → _reconcile_isolated; aplicar la ventana de
              obsolescencia también en la ruta manual; mapear ArcaSequenceBusy a UserError.
Tests:        espejo de test_the_reconciler_leaves_a_request_that_may_still_be_running, para el botón.
NO incluir:   la validación del comprobante reconciliado (PR 4).
Riesgo:       bajo.
```
```text
Orden:        4
Título:       fix(recover): verify the voucher ARCA reports before adopting its CAE
Branch:       fix/reconcile-validates-voucher
Hallazgo:     M-01 (+ M-02)
Archivos:     models/l10n_ar_arca_attempt.py · models/l10n_ar_arca_wsfe.py · tests/test_authorization.py
Cambio mínimo: en _reconcile, exigir CAE presente y Resultado aceptable, y contrastar
              ImpTotal/DocNro con el move antes de escribir; tratar 'P' sin CAE como incierto.
NO incluir:   cambios en el lock ni en el índice.
Riesgo:       bajo.
```
```text
Orden:        5
Título:       security: check the caller's rights before escalating with sudo
Branch:       security/certificate-actions-check-access
Hallazgo:     H-05 (+ M-06)
Archivos:     models/l10n_ar_arca_certificate.py · security/ir.model.access.csv · tests/test_certificate.py
Cambio mínimo: check_access('write') en las tres acciones públicas; quitar write/create del
              grupo de facturación sobre l10n_ar.arca.attempt (M-05).
Tests:        por cada acción pública, AccessError para un usuario de facturación.
NO incluir:   cifrado de la clave privada.
Riesgo:       medio — revisar que el cron y el flujo de autorización sigan funcionando.
```
```text
Orden:        6
Título:       feat(fiscal): send the Tributos array alongside ImpTrib
Branch:       feat/payload-tributos
Hallazgo:     H-04
Archivos:     models/account_move.py · models/constants.py · tests/test_payload.py
Cambio mínimo: construir Tributos desde los mismos base_lines, con aserción
              sum(Importe) == ImpTrib. Si se difiere: rechazar ImpTrib != 0 y declararlo en ROADMAP.
NO incluir:   Opcionales ni FCE.
Dependencias: mapeo de código de tributo AFIP de l10n_ar — validar con Odoo instalado.
Riesgo:       medio — es el PR que más necesita homologación real.
```
```text
Orden:        7
Título:       security: fail closed on a neutralized or restored database
Branch:       security/neutralize-fail-closed
Hallazgo:     H-06
Archivos:     data/neutralize.sql (nuevo) · __manifest__.py · models/account_move.py · tests/
Cambio mínimo: enviar neutralize.sql y una guarda única en _l10n_ar_arca_check_ready.
NO incluir:   el badge de ambiente (PR 8).
Riesgo:       bajo.
```
```text
Orden:        8
Título:       feat(audit): record the ARCA environment on the invoice
Branch:       feat/move-records-environment
Hallazgo:     H-07
Archivos:     models/account_move.py · views/account_move_views.xml · reports/report_invoice.xml · tests/
Cambio mínimo: campo de ambiente en account.move escrito al autorizar; badge y marca de agua
              cuando no sea 'production'.
Dependencias: idealmente después del PR 1 (ambos tocan el reporte).
Riesgo:       bajo.
```
```text
Orden:        9
Título:       fix(idempotency): scope the duplicate-voucher index by environment and issuer
Branch:       fix/attempt-unique-index-scope
Hallazgo:     M-03 + M-04
Archivos:     models/l10n_ar_arca_attempt.py · tests/test_concurrency.py
Cambio mínimo: incluir environment en el índice y alinear su scope con el del lock (CUIT emisor).
Riesgo:       medio — implica migración del índice; requiere revisar datos existentes.
```
```text
Orden:        10
Título:       test: exercise the dedicated-cursor topology against ORM records
Branch:       test/dedicated-cursor-with-orm
Hallazgo:     M-09
Archivos:     tests/
Cambio mínimo: al menos un test que fuerce current_test=False sobre el camino completo de
              autorización con una factura real, no solo sobre la primitiva de lock.
Riesgo:       medio — es el test más difícil del conjunto.
```
```text
Orden:        11
Título:       fix(sale): use _create_invoices instead of the removed action_invoice_create
Repositorio:  JoaPeralta/meli_oerp     Branch: fix/odoo19-create-invoices
Hallazgo:     frontera MeLi
Archivos:     models/orders.py (meli_create_invoice)
Riesgo:       medio — es el punto donde entra la factura al circuito fiscal.
```
```text
Orden:        12
Título:       build: bump the pinned ARCA EDI commit to the audited head
Repositorio:  JoaPeralta/odoo-viarengo  Branch: build/bump-arca-edi-sha
Hallazgo:     deployment
Archivos:     Dockerfile (ARCA_EDI_SHA, MELI_OERP_SHA)
Dependencias: debe ir DESPUÉS de los PRs 1-8.
Riesgo:       bajo.
```

---

## L. Secuencia recomendada

**1. Obligatorios antes de homologación** — PRs **1, 2, 3, 4**.
Sin el 1 no se puede validar el comprobante impreso, que es la mitad de la aceptación. El 2, 3 y 4 son baratos y eliminan las tres transiciones inválidas alcanzables antes de que aparezcan con datos reales.

**2. Obligatorios antes de producción** — PRs **5, 6, 7, 8, 9**, más una **sesión de homologación completa** que emita al menos: Factura A a RI, Factura B a consumidor final, Factura C, una nota de crédito con `CbtesAsoc`, y una factura con percepción IIBB (que es la que valida el PR 6).

**3. Necesarios antes de automatizar desde Mercado Libre** — PR **11**, más el bridge nuevo (no auditado aquí, no implementado) respetando los invariantes de §J, más el PR **12** para que lo desplegado sea lo auditado.

**4. Mejoras posteriores** — PR **10**, consulta de `FEParamGetPtosVenta` y `FEParamGetCondicionIvaReceptor`, `Tributos` completo si se difirió, y el reemplazo del *lease* WSAA basado en historial de GitHub Actions que el propio `AUDIT.md` reconoce como deuda aceptada.

---

## M. Veredicto

> ### **Apto para homologación con bloqueadores conocidos.**

**Justificación con evidencia.**

Lo que sostiene el "apto para homologación": la arquitectura de seguridad fiscal es genuinamente sólida y poco común. El acto irreversible está fuera de la transacción de negocio (`_post` no llama a ARCA); la evidencia durable se commitea **antes** del envío (`fiscal_transaction.py` + `account_move.py:309-329`); el estado "no sé" existe, es distinto del rechazo y **bloquea** el reintento; el lock de numeración es `pg_try_advisory_xact_lock` sobre conexión propia con la granularidad correcta (CUIT/PdV/tipo) y está probado contra PostgreSQL real, incluida la liberación tras un `SQL error`; hay un índice único parcial como backstop en base de datos; los secretos están bajo `groups="base.group_system"`; y los importes se delegan a `l10n_ar` en vez de reimplementar la clasificación fiscal. Además, el alcance no soportado se **rechaza con mensaje**, no se envía a fallar.

Lo que impide ir más allá:

1. **Nunca se emitió un CAE.** El propio `AUDIT.md` §"Verification status" declara: autenticación ejecutada y confirmada (WSAA + `FEDummy`), *"The rest of the session was not completed… No voucher has been issued."* Ninguna afirmación sobre el comportamiento real de `FECAESolicitar` está validada empíricamente.
2. **H-01** hace que ninguna factura autorizada se pueda imprimir — no se puede cerrar una aceptación end-to-end.
3. **H-04** significa que toda factura con percepción IIBB viaja incompleta; en Argentina ese es el caso común de un RI, no un borde.
4. **H-03** y **H-02** permiten dos transiciones inválidas que llevan a una factura con CAE real y datos divergentes.
5. **H-06** y **H-07** significan que no hay fail-closed ante una copia de base ni forma de distinguir en el asiento un CAE de homologación de uno real.
6. **H-05** permite a un usuario de solo lectura destruir la clave privada por RPC.

No es "no apto ni para homologación": nada de lo anterior impide correr homologación de forma controlada, y homologación es justamente donde estos defectos deben aparecer. No es "apto para producción": los puntos 1, 3, 5 y 6 son incompatibles con emitir comprobantes con valor legal.

---

## N. Límites de la auditoría

**Archivos y ramas.** Todo el árbol de `l10n_ar_arca_edi@9c41e7d` fue accesible y leído. `meli_oerp@19.0` y `odoo-viarengo@feat/arca-edi-integration` se clonaron con `--depth 50`: el historial anterior a esa profundidad no se revisó. De `meli_oerp` se auditó únicamente la frontera de integración solicitada, no el módulo completo.

**Tests no ejecutados.** Ninguno. No hay Odoo ni PostgreSQL disponibles en este entorno, y ejecutar la suite excede el mandato de solo lectura. Los conteos son estáticos y exactos: **12 archivos de test, 195 métodos** (`test_authorization` 33, `test_payload` 30, `test_certificate` 23, `test_transport` 17, `test_wsaa` 17, `test_concurrency` 15, `test_representation` 15, `test_numbering` 14, `test_scope` 14, `test_security` 9, `test_qr` 7, `test_homologation` 1). No se ejecutó ningún GitHub Action.

**No verificable sin Odoo instalado.** El comportamiento real de `_l10n_ar_get_amounts`, `_get_vat` y `_get_rounded_base_and_tax_lines` de `l10n_ar` (de los que depende toda la corrección de importes, incluido el tratamiento de clase C y anticipos); la resolución de `l10n_latam_document_type_id`; el mapeo de código de tributo AFIP necesario para H-04; y si `readonly=True` en los campos del `attempt` es efectivamente evadible por RPC en Odoo 19 (asumido según semántica ORM estándar, marcado como MEDIUM y no como confirmado-crítico).

**No verificable sin PostgreSQL.** El comportamiento del lock bajo carga real y con múltiples workers Odoo; el consumo de conexiones (hasta cinco backends concurrentes por worker cuando WSAA anida una segunda `fiscal_transaction`); y si `invalidate_recordset()` basta para reflejar el estado en el nivel de aislamiento efectivo (marcado como pregunta abierta, no como hallazgo).

**Requiere homologación ARCA real.** Si ARCA rechaza `ImpTrib` sin `Tributos` (H-04) o lo acepta silenciosamente; el comportamiento real ante `Resultado='P'`; la vigencia efectiva de las tablas de `constants.py` frente a los `FEParamGet*`; y el comportamiento real de renovación de ticket WSAA bajo concurrencia.

**Supuestos declarados.** (a) `git ls-remote` refleja el estado remoto vigente al momento de la auditoría. (b) La semántica de `cr.postcommit`, `Cursor.__exit__` y ACL/`sudo()` es la estándar de Odoo 19. (c) Las tablas de `constants.py` se contrastaron contra el manual del desarrollador ARCA v4.0 que el propio archivo cita, no contra los `FEParamGet*` en vivo. (d) La documentación de Mercado Libre se usó únicamente como autoridad sobre el contrato del canal, nunca sobre reglas fiscales argentinas.

**Sobre la clasificación.** Los 3 hallazgos propuestos como CRITICAL fueron degradados por refutadores independientes que releyeron el código con mandato de refutar. Ningún CRITICAL sobrevivió. Deliberadamente **no** inflé severidades para que el informe pareciera más contundente: el módulo no tiene, al día de hoy y en el código auditado, un defecto que emita un comprobante duplicado o pierda un CAE autorizado por sí solo.
