# Guía de Compatibilidad y Migración de Formatos

🌐 **Idiomas / Languages:** [English](../migration.md) | [Português (Brasil)](migration.pt-BR.md) | Español

AgGit está diseñado para mantener compatibilidad hacia atrás con repositorios y eventos creados en versiones previas.

---

## 1. Evolución del Formato de Eventos

| Campo | Schema v1 (Legado) | Schema v2 (Canónico Actual) |
| :--- | :--- | :--- |
| **Identificador (`tx_id`)** | 8 caracteres truncados | UUID v4 completo (36 caracteres) |
| **Registro de Archivos** | Diccionario plano `files: {path: sha}` | Diccionario estructurado `changes: {path: {action, blob_sha, old_sha}}` |
| **Eliminaciones** | Ausencia implícita de clave | Acción explícita `"action": "delete"` |
| **Revisión Base** | Ausente (heredaba HEAD lineal) | `base_revision: "uuid"` explícito |
| **Identidad de Sesión** | Únicamente `agent_id` | `session_id` desacoplado de `agent_id` |
| **Marca de Tiempo** | Float en segundos | Cadena ISO-8601 UTC + `timestamp_ns` (i64) |

---

## 2. Cómo se Ejecuta la Migración

1. **Lectura Transparente**:
   Al leer un evento sin `"schema_version"` o con `"schema_version": 1`, los lectores en Python (`TransactionRecord.from_dict`) y Rust (`Transaction::get_effective_changes`) transforman automáticamente el diccionario `files` en registros de modificación (`action="modify"`).
2. **Preservación de Datos Duraderos**:
   Los eventos previos en `.aggit/journal/` no se reescriben ni descartan. Nuevas confirmaciones generan eventos en schema v2.
3. **Compatibilidad de Blobs CAS**:
   El formato de los objetos en CAS (`.aggit/objects/`) permanece 100% idéntico: encabezado `[tipo] [tamaño]\0` comprimido con zlib y direccionado por SHA-256.

---

## 3. Evolución de Checkpoints y Auto-Recuperación

### 3.1 Checkpoints Legados (Schema v1)
En el schema v1 de checkpoints (`.aggit/checkpoints/latest.json`), el archivo registraba únicamente `tree` y `processed_tx_ids`. Los conflictos no se guardaban de forma estructurada, arriesgando pérdida de contexto al reiniciar.

### 3.2 Migración a Checkpoint Schema v2
- **Compatibilidad de Lectura**: Al cargar un checkpoint sin el campo `"pending_conflicts"`, el motor asigna automáticamente una lista vacía `[]`, manteniendo compatibilidad directa.
- **Detección y Preservación**: Los nuevos conflictos detectados durante la reconciliación se persisten estructuradamente en el schema v2 con metadatos completos (`path`, `base_sha`, `ours_sha`, `theirs_sha`, `conflict_type`).
- **Auto-Recuperación basada en el Journal**: Si un checkpoint previo se grabó de forma inconsistente o resultó dañado, la recuperación recurre al append-only journal (`.aggit/journal/`) como fuente inmutable de verdad. Al borrar o invalidar el archivo de checkpoint, la ejecución de `aggit reconcile` procesa el historial causal según el DAG de transacciones, restaurando el árbol unificado y redescubriendo los conflictos no resueltos.
