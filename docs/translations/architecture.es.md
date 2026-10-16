# AgGit: Arquitectura del Sistema

🌐 **Idiomas / Languages:** [English](../architecture.md) | [Português (Brasil)](architecture.pt-BR.md) | Español

**AgGit** es un sistema de control de versiones nativo para agentes autónomos de IA que elimina el cuello de botella del bloqueo tradicional de Git (`.git/index.lock`), proporcionando concurrencia segura, fusión semántica de tres vías e integración aislada con Git/GitHub.

---

## 1. Visión General de la Arquitectura

El sistema se divide en cinco capas ortogonales y desacopladas:

```
┌─────────────────────────────────────────────────────────────┐
│                       Agentes de IA                         │
└──────────────┬──────────────────────────────┬───────────────┘
               │                              │
               ▼                              ▼
┌──────────────────────────────┐┌──────────────────────────────┐
│ Staging Virtual por Sesión   ││ Content-Addressable Storage  │
│ (.aggit/staging/*_<hash>.json)││ (.aggit/objects/xx/yy...)   │
└──────────────┬───────────────┘└──────────────▲───────────────┘
               │                               │
               ▼                               │
┌──────────────────────────────┐               │
│ Append-Only Event Journal    ├───────────────┘
│ (.aggit/journal/*.json)      │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Motor de Merge Semántico     │
│ (3-Way: Python, JSON, Texto) │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐┌──────────────────────────────┐
│ Materialización Incremental  ││ Git / GitHub Bridge          │
│ (.aggit/checkpoints/latest)  ││ (Worktrees y Ramas Aisladas) │
└──────────────────────────────┘└──────────────────────────────┘
```

---

## 2. Subsistemas

### 2.1 Content-Addressable Storage (CAS)
- **Hashing**: SHA-256 calculado sobre el encabezado `[tipo] [tamaño]\0` y el contenido.
- **Optimización**: Se verifica la existencia del objeto en disco **antes** de comprimir, ahorrando ciclos de CPU en objetos previamente almacenados.
- **Atomicidad**: La escritura utiliza archivos temporales exclusivos por proceso, hilo y UUID (`.tmp_obj_<pid>_<uuid>.tmp`), con `fsync` y reemplazo atómico (`os.replace` / `fs::rename`).
- **Verificación**: Al leer, valida la suma de comprobación SHA-256 e impone un límite máximo de descompresión (200 MB) para prevenir ataques de descompresión.

### 2.2 Staging Virtual Namespaced y Concurrencia de Sesión
- En lugar de un archivo único compartido (`.git/index`), cada par `(agent_id, session_id)` cuenta con su propio archivo de staging, protegido contra colisiones mediante un hash SHA-256 en el nombre (`.aggit/staging/<agent>_<hash>.json`).
- **Bloqueos de Sesión Multiplataforma**: Las operaciones de staging emplean bloqueos exclusivos por sesión (`<path>.lock`) con tiempo de espera configurable y reintento exponencial, implementados con `msvcrt` en Windows y `fcntl` en entornos POSIX (`aggit/lock_utils.py`).
- **Escritura Segura con Archivos Temporales Exclusivos**: Las actualizaciones se escriben en archivos temporales exclusivos (`.tmp_stg_<pid>_<thread>_<uuid>.tmp`) seguidos de reemplazo atómico (`os.replace`), evitando errores de concurrencia y violaciones de uso compartido (`[WinError 32]`).
- **Control de Generación**: Cada escritura incrementa un contador `generation`. Al finalizar una confirmación, el área de staging solo se limpia si la generación actual es menor o igual a la del momento del commit, asegurando que adiciones concurrentes más recientes no se descarten.
- **Eliminaciones Explícitas**: Soporta altas, modificaciones y eliminaciones explícitas (`stage_delete`).

### 2.3 Append-Only Journal e Índice Derivado
- Cada confirmación genera un evento inmutable con UUID v4 completo y marca de tiempo en nanosegundos.
- Las escrituras utilizan archivos temporales con sufijo `.tmp` para asegurar que lectores concurrentes de `.json` nunca lean archivos en progreso.
- El índice derivado (`.aggit/journal_index.json`) sincroniza accesos concurrentes bajo bloqueo de archivo. Si el índice se corrompe o presenta divergencia con el directorio `.aggit/journal/`, se reconstruye automáticamente utilizando el diario como fuente de verdad.

### 2.4 Motor de Reconciliación, DAG y Ciclo de Vida de Conflictos
- **Orden Causal por DAG**: El historial de eventos se ordena topológicamente respetando relaciones de ancestros (`parents`), sin depender de relojes físicos entre agentes.
- **Base Real**: Siempre utiliza la base común real (`base_revision` o el ancestro común más cercano en el DAG), nunca una base vacía.
- **JSON Semántico**: Conserva adiciones independientes de claves de forma recursiva (ej: `{"puerto": 3000}` y `{"debug": true}` fusionados limpiamente), respeta eliminaciones y detecta conflictos sin corromper la estructura JSON.
- **Python y Texto**: Utiliza fusión de 3 vías basada en diff3 / `git merge-file`, preservando comentarios, docstrings, variables globales, condicionales y decoradores.
- **Ciclo de Vida Persistente de Conflictos**:
  - Los conflictos de contenido, modificación/eliminación (`modify/delete`) o divergencia binaria se registran de forma estructurada en el checkpoint (`pending_conflicts`).
  - Las ejecuciones subsiguientes de `reconcile`, `status` o `export-preview` preservan los conflictos pendientes sin marcarlos erróneamente como éxito.
  - **Dry-run Puro**: El modo `--dry-run` calcula el plan estrictamente en memoria, sin escribir checkpoints ni alterar el diario o árbol de trabajo.
  - **Bloqueo del Pipeline**: La materialización en disco y exportación a Git se bloquean estrictamente mientras existan conflictos pendientes.
  - **Resolución Explícita (`resolve_conflict`)**: La resolución genera una transacción que sustituye las revisiones divergentes en el DAG, persiste el contenido acordado en el CAS y elimina el conflicto de la lista de pendientes.

### 2.5 Materialización Segura en 3 Estados
- **Validación Previa Trifásica (Pre-flight Validation)**:
  Antes de escribir o eliminar cualquier archivo en el directorio de trabajo, el motor evalúa tres estados: (1) Último estado registrado en el manifiesto (`materialized_state.json`), (2) Contenido físico actual en disco, y (3) Estado reconciliado deseado.
  - Detecta **ediciones locales del usuario**: si el hash actual en disco difiere del manifiesto previo, la materialización se cancela con error para evitar pérdida de datos.
  - Detecta **eliminaciones locales**: si el archivo fue eliminado manualmente en disco, su recreación automática se omite a menos que se fuerce.
  - Detecta **archivos no rastreados**: impide que AgGit sobreescriba archivos preexistentes creados fuera del control del sistema.
  - Indicador `--force`: permite omitir la protección si el usuario desea descartar intencionalmente los cambios locales.
- **Publicación Atómica**:
  Las escrituras en disco se realizan mediante archivos temporales exclusivos (`.tmp_mat_<pid>_<thread>_<uuid>.tmp`), con `fsync` y reemplazo atómico (`os.replace`). El manifiesto físico solo se actualiza tras la confirmación en disco.

### 2.6 Puente con Git y GitHub
- **Aislamiento Total**: El espacio de trabajo principal del usuario no se toca durante la exportación. La preparación del commit se realiza en un *worktree* temporal de Git fuera del repositorio (`aggit_worktree_*`), limpiado determinísticamente en bloques `finally`.
- **Continuidad de Ramas**: Al exportar secuencialmente a una rama existente de Git, AgGit reutiliza la punta de la rama (`git checkout <branch>`) sin banderas destructivas (`-B`), garantizando linaje directo de commits.
- **Preservación de Archivos Git No Relacionados**: Solo se tocan los archivos gestionados por AgGit. Los archivos existentes en la rama de Git (ej: `keep.md`, `README.md`, `.github/`) se preservan estrictamente, eliminando solo las rutas con acción explícita de borrado (`action: "delete"`).
- **Repositorios Git Vacíos**: Inicializa el commit raíz en repositorios vacíos mediante comandos plumbing (`git commit-tree 4b825dc...` con el hash de árbol vacío) y `git update-ref`, evitando indexar archivos no confirmados del usuario.
- **Ledger Idempotente de Exportación**: El archivo `.aggit/git_export_map.json` almacena asignaciones indexadas por clave compuesta `revision::branch::parent_commit`, garantizando trazabilidad sin duplicaciones innecesarias.
- **Automatización Segura de Pull Requests**: Reutiliza la sesión local de GitHub CLI (`gh`), validando autenticación y detectando PRs existentes sin duplicar solicitudes.

### 2.7 Confinamiento y Seguridad de Rutas
- **Prevención de Path Traversal**: Rechaza rutas absolutas y secuencias de navegación hacia directorios padre (`..`).
- **Resolución de Enlaces Simbólicos**: Valida la ruta canónica real (`os.path.realpath`) asegurando que el destino no apunte fuera del repositorio.
- **Nombres Reservados del Sistema Operativo**: Rechaza nombres de dispositivos reservados de Windows (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`) en cualquier componente de ruta.
