# Deuda técnica — ARCA: `holder_cuit` e `issuer_cuit` en el wizard (2026-11-20)

**Estado:** abierta · **Prioridad:** media · **Alcance:** wizard de creación de certificados; no bloquea por sí sola la separación production/testing ni autoriza emisión fiscal.

## Evidencia

En los logs de `Odoo-Homologacion` (base `arca_homologacion`, Odoo 19) aparecen las advertencias del ORM:

> `l10n_ar.arca.create.certificate.wizard: inconsistent 'store' for computed fields, accessing issuer_cuit may recompute and update holder_cuit. Use distinct compute methods for stored and non-stored fields.`

> `l10n_ar.arca.create.certificate.wizard: inconsistent 'compute_sudo' for computed fields holder_cuit, issuer_cuit. Either set 'compute_sudo' to the same value on all those fields, or use distinct compute methods for sudoed and non-sudoed fields.`

Código observado en la revisión de `486ac5e3fb290f519cee880418f965ea176238be`:
`wizards/l10n_ar_arca_create_certificate_wizard.py`, `holder_cuit` (compute compartido, `store=True`, editable) e `issuer_cuit` (compute compartido, no almacenado), ambos `_compute_holder_cuit`.

## Comportamiento deseado

- `holder_cuit`: CUIT de quien tramita el certificado; inicializarlo con el CUIT de la compañía, pero admitir que un representante autorizado use uno distinto.
- `issuer_cuit`: CUIT de la compañía emisora; no debe sobrescribir ni recalcular silenciosamente un `holder_cuit` editado por el usuario.
- Para **VIARENGO S.A.S.**, el titular y emisor confirmados son **30-71786594-0**; no generalizar esta igualdad a otras compañías.

## Tarea pendiente

1. Separar los métodos de cómputo y dependencias de ambos campos, o implementar una solución de Odoo 19 que mantenga la semántica indicada.
2. Eliminar ambos warnings de `store` / `compute_sudo`.
3. Añadir pruebas de: valores por defecto, edición manual del titular, cambio de compañía, lectura del emisor sin mutación del titular, ACL y creación correcta del registro.
4. Validar sobre base de prueba sintética y revisar diff antes de proponer el upgrade.

**Restricciones:** no generar/regenerar claves privadas, CSR ni certificados; no tocar registros fiscales existentes; no modificar el ambiente ARCA ni publicar una imagen productiva en virtud de esta deuda.

## Seguimiento

Este repositorio tiene GitHub Issues deshabilitado (API HTTP 410 al intentar crear el ticket). Se conserva esta deuda en el repositorio bajo `docs/technical-debt/` hasta su resolución.
