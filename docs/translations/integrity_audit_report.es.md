# Informe de Auditoría de Integridad, Concurrencia y Exportación (AgGit)

🌐 **Idiomas / Languages:** [English](../integrity_audit_report.md) | [Português (Brasil)](integrity_audit_report.pt-BR.md) | Español

**Fecha:** 15 de Octubre de 2026  
**Entorno de Ejecución:** Windows 11 Pro x64, AMD/Intel Multi-Core, SSD NVMe PCIe Gen4, Python 3.13.7, Rust 1.99.0, Git 2.49.0  
**Rama de Trabajo:** `fix/aggit-deep-integrity-windows-and-export`  
**Referencia Comparativa:** Commit `309df61c45a36357adc0d7326aa4836143e2586e`

---

## 1. Análisis Comparativo: HEAD vs. `309df61c45a36357adc0d7326aa4836143e2586e`

| Área Auditada | Estado en `309df61c45a36357adc0d7326aa4836143e2586e` | Estado en HEAD Actual (`fix/aggit-deep-integrity-windows-and-export`) |
| :--- | :--- | :--- |
| **Validación de Rutas en Windows** | `_validate_safe_path` entraba en bucle infinito al inspeccionar uniones/symlinks porque `os.path.dirname("C:\\") == "C:\\"`. La comprobación por prefijo (`target.startswith(base)`) permitía escapes a directorios hermanos (ej: `/work_external` desde `/work`). | Se implementó `is_path_contained` con `os.path.commonpath` y `os.path.normcase`, garantizando verificación estricta por componentes de directorio. Se agregó guarda de terminación en la raíz (`parent == cur`). |
| **Matriz de Exportación Git (3-Way)** | Los escenarios de divergencia humana en la rama de exportación fallaban: AgGit borrando un archivo modificado por humano borraba el archivo sin conflicto; AgGit modificando un archivo borrado por humano recreaba el archivo en silencio; adiciones concurrentes divergentes se sobreescribían; y exportaciones sucesivas revertían cambios humanos integrados. | Se implementó la matriz completa de 3 vías en `prepare_export_plan`: detecta conflictos `modify_delete`, `delete_modify` y `add_add_divergence`. Las exportaciones sucesivas realizan fusión de 3 vías usando `last_aggit_bytes` (base), `cur_git_bytes` (punta de Git con cambios integrados) y `cur_aggit_bytes` (nuevo estado de AgGit). |
| **Identidad y Ciclo de Vida de Conflictos** | Los conflictos usaban UUID aleatorio o SHA sin ordenación determinista de versiones. La resolución de un conflicto previo podía eliminar indebidamente un conflicto nuevo en el mismo archivo. | Identidad calculada de forma determinista mediante SHA-256 (`file_path::internal_path::conflict_type::base_sha::sorted(a_sha, b_sha)`). `resolve_conflict` recibe `target_conflict_ids` y la reconciliación filtra estrictamente los conflictos indicados, conservando conflictos no resueltos de terceros agentes. |
| **Auto-reparación de Índices y Dry-Run Estricto** | Si `journal_index.json` estaba ausente o desfasado, nuevas instancias del motor fallaban al cargar el historial. Las ejecuciones en dry-run (`preview_export`) causaban escrituras en disco al regenerar índices o guardar checkpoints. | Se implementó el modo `read_only` en `Journal`. `_load_index` detecta automáticamente desincronizaciones con los archivos de transacciones y se repara en memoria. En dry-run se garantizan **cero escrituras en disco** (sin blobs CAS temporales, checkpoints ni índices). |
| **Escalabilidad de la Reconciliación** | La reconciliación de transacciones consecutivas recalculaba todo el árbol recorriendo el historial completo ($O(N^2)$ en lecturas de disco). | Se implementó memoización de revisiones y un atajo incremental mediante delta del commit padre inmediato en `get_revision_tree`. Se eliminaron las lecturas repetidas del historial durante la reconciliación. |
| **Hardening de Flujos CI** | El flujo de GitHub Actions ejecutaba archivos de prueba codificados manualmente, no descubría nuevas suites de integridad y carecía de timeouts, arriesgando bloqueos silenciosos. | CI actualizado con `timeout-minutes: 15` en el trabajo y pasos. Runner configurado para descubrimiento automático completo vía `python -m unittest discover -s tests -v`. |
| **Verificación Estricta de Benchmarks** | `benchmark.py` empleaba comprobaciones superficiales (`exists`, `startswith`) y mantenía fallback a un ejecutable desactualizado en la raíz. El benchmark en Rust no validaba contenido en bytes. | Verificación física estricta byte a byte mediante SHA-256 de todas las salidas en disco en todos los motores. Se eliminó el fallback al binario estático; el ejecutable en Rust se compila estrictamente desde el código fuente (`aggit-rs/target/release/aggit.exe`). |

---

## 2. Archivos Modificados y Nuevas Implementaciones

1. **`aggit/path_utils.py`**:
   - Función `is_path_contained(parent_dir, target_path)` con `os.path.realpath`, `os.path.normcase` y `os.path.commonpath`.
   - Protección en `sanitize_repo_path` contra rutas UNC, dispositivos reservados de Windows y prefijos de unidad.
2. **`aggit/git_bridge.py`**:
   - `_validate_safe_path`: rotura explícita de bucle en raíz (`parent == curr`) y validación mediante `is_path_contained`.
   - `prepare_export_plan`: matriz de 3 vías cubriendo escenarios A, B y C, y preservación de ediciones humanas en exportaciones sucesivas.
   - Cálculo de hash compatible con CAS (`blob <len>\0...`) para el árbol de exportación.
   - Modo `journal.read_only = True` durante `preview_export` y `prepare_export_plan`.
3. **`aggit/semantic_merge.py`**:
   - `MergeConflict.compute_conflict_id` con cálculo determinista y hashes de versión ordenados.
   - Asignación de `base_sha`, `a_sha`, `b_sha` y recálculo de identidad en todas las divergencias.
4. **`aggit/engine.py`**:
   - `resolve_conflict`: soporte para `target_conflict_ids` en metadatos.
   - `reconcile_state`: filtrado granular por `target_conflict_ids`.
   - Memoización en `get_revision_tree` para reconciliación en tiempo lineal.
5. **`aggit/journal.py`**:
   - Soporte para `read_only`.
   - Auto-reparación de `_load_index` ante discrepancias en archivos de transacción.
6. **`aggit-rs/src/semantic.rs`**:
   - Implementación de `compute_conflict_id` y `recompute_id` con SHA-256 y versiones ordenadas.
   - Incorporación de metadatos de versiones en todas las ramas de divergencia (`modify_delete`, `binary`, `json_key`, texto).
7. **`aggit-rs/src/engine.rs`**:
   - Migración de `conflicts_by_path` a `HashMap<String, Vec<MergeConflict>>`.
   - Soporte para `target_conflict_ids` en `resolve_conflict`.
   - Sanitización de rutas en `materialize` antes de interactuar con el sistema de archivos.
8. **`aggit-rs/src/main.rs`**:
   - Subcomando `benchmark`: validación física de bytes esperados para cada agente.
9. **`benchmark.py`**:
   - Escritura binaria (`wb`) para evitar conversiones CRLF/LF en Windows.
   - Validación estricta byte a byte para Python, Rust y Git worktrees.
   - Compilación y ejecución forzosa desde fuentes en `aggit-rs/target/release/`.
10. **`.github/workflows/ci.yml`**:
    - Descubrimiento automático con `python -m unittest discover -s tests -v`.
    - Límite de tiempo `timeout-minutes: 15`.
11. **`tests/test_deep_integrity_v2.py`** *(nuevo)*:
    - 10 escenarios de regresión que validan terminación en symlinks, bloqueo de escapes a carpetas hermanas, matriz de exportación A/B/C, auto-reparación de índices, dry-run puro y escala lineal.

---

## 3. Metodología y Resultados de los Benchmarks

Ejecutado en Windows 11 (SSD NVMe PCIe Gen4, 8 hilos concurrentes, Python 3.13, Rust 1.99, Git 2.49):

| Sistema Probado | Éxito | Tasa de Error | Tiempo Total (s) | Rendimiento (ops/s) | Latencia p50 | Latencia p95 | Contenido Verificado |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Git (Shared Index)** | 4/20 | **80.0%** | 0.52s | 7.71 | 107ms | 182ms | Sí (4 archivos) |
| **Git (Worktree por Agente)** | 20/20 | 0.0% | 1.23s | 16.27 | 359ms | 417ms | Sí (100% íntegro) |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 0.88s | 22.66 | 107ms | 490ms | Sí (100% íntegro) |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.09s** | **214.50** | **18ms** | **22ms** | Sí (100% íntegro) |

### Aspectos Destacados:
- **Git con índice compartido** falló en el 80% de las operaciones debido a contención en `.git/index.lock`.
- **Git con worktrees** completó las confirmaciones, pero la sobrecarga de E/S elevó la latencia $p_{95}$ a 417ms.
- **AgGit (Python Engine)** superó a Git worktrees con 22.66 ops/s y 0.88s en total.
- **AgGit (Rust Native Engine)** alcanzó **214.50 ops/s** y latencia $p_{50}$ de **18ms** (**13.2x más rápido** que Git worktrees), con verificación física de contenido 100% aprobada.

---

## 4. Cobertura de la Suite de Pruebas

- **Suite Completa en Python (`python -m unittest discover -s tests -v`):**
  - **50 pruebas ejecutadas, 50 aprobadas** (1 prueba de enlaces simbólicos externos omitida por falta de privilegios elevados de desarrollador en Windows).
  - Tiempo total de ejecución: ~22.7 segundos.
- **Suite Nativa en Rust (`cargo test --manifest-path aggit-rs/Cargo.toml`):**
  - **13 pruebas de integración ejecutadas, 13 aprobadas**.
  - Tiempo total de ejecución: ~0.33 segundos.
