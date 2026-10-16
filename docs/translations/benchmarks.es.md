# Metodología y Resultados de Benchmark

🌐 **Idiomas / Languages:** [English](../benchmarks.md) | [Português (Brasil)](benchmarks.pt-BR.md) | Español

Este documento describe los procedimientos de prueba, métricas y resultados comparativos entre Git tradicional y AgGit.

---

## 1. Metodología

El benchmark evalúa la capacidad de gestionar múltiples escritores concurrentes que intentan confirmar cambios en el mismo repositorio:

1. **Git (Shared Index)**: Simula el uso estándar de Git con múltiples agentes concurrentes compartiendo el mismo árbol de trabajo y el mismo `.git/index`.
2. **Git (Worktree per Agent)**: Crea una rama y un *worktree* físico en disco para cada agente, aislando el índice al costo de sobrecarga de E/S en disco.
3. **AgGit (Python Engine)**: Utiliza staging virtual con espacios de nombres por sesión, diario append-only y reconciliación incremental en Python.
4. **AgGit (Rust Native Engine)**: Utiliza CAS acelerado por hardware con verificación previa de existencia de hash y paralelismo nativo vía Rayon.

### 1.1 Parámetros y Métricas de Ejecución
- **Carga de Trabajo (Workload)**: 20 agentes autónomos ejecutados con concurrencia de 8 a 12 trabajadores (`ProcessPoolExecutor(max_workers=8)` en Python y pool de hilos Rayon en Rust).
- **Muestras de Latencia y Percentiles**: El tiempo de ejecución de cada agente (creación del archivo, staging y confirmación) se mide individualmente en segundos. La latencia mediana ($p_{50}$) se calcula vía `statistics.median(latencies)` y el percentil 95 ($p_{95}$) vía interpolación de cuantiles `statistics.quantiles(latencies, n=20)[18]`.
- **Verificación Estricta de Contenido (`content_verified`)**: Para todas las variantes, la prueba solo se considera válida si:
  1. Todos los 20 archivos físicos esperados existen en el disco.
  2. El contenido integral en bytes de cada archivo se lee y valida contra el contenido original generado mediante SHA-256.
  3. La reconciliación de AgGit reporta exactamente 0 conflictos espurios.
  Si algún archivo falta o está corrupto, el benchmark falla y devuelve un código de salida distinto de cero.

---

## 2. Resultados en el Entorno de Prueba

Ejecutado en entorno Windows 11 (SSD NVMe PCIe Gen4, 8 hilos concurrentes, Python 3.13, Rust 1.99, Git 2.49):

| Sistema | Éxito | Tasa de Error | Tiempo Total (s) | Rendimiento (ops/s) | Latencia p50 | Latencia p95 | Contenido Verificado |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Git (Shared Index)** | 4/20 | **80.0%** | 0.52s | 7.71 | 107ms | 182ms | Sí (4 archivos) |
| **Git (Worktree por Agente)** | 20/20 | 0.0% | 1.23s | 16.27 | 359ms | 417ms | Sí (100% íntegro) |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 0.88s | 22.66 | 107ms | 490ms | Sí (100% íntegro) |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.09s** | **214.50** | **18ms** | **22ms** | Sí (100% íntegro) |

### Análisis Comparativo:
- **Git con índice compartido** sufre contención severa: el 80% de los intentos fallan de inmediato por error de bloqueo (`Unable to create '.git/index.lock': File exists`).
- **Git con worktrees** resuelve la concurrencia garantizando el 100% de éxito; no obstante, la creación de ramas y duplicación de directorios genera una sobrecarga sustancial de E/S, resultando en una latencia $p_{95}$ de 417ms.
- **AgGit (Python Engine)** opera sin bloqueos globales de índice, aislando el trabajo en archivos de staging por sesión. Incluso incluyendo el tiempo de reconciliación y materialización de 3 vías en disco, supera el rendimiento de Git Worktrees (22.66 ops/s frente a 16.27 ops/s).
- **AgGit (Rust Native Engine)** reduce drásticamente la latencia y el consumo de CPU: con CAS nativo y diario en tiempo constante ($O(1)$), alcanza **214.50 ops/s** y una latencia $p_{50}$ de apenas 18ms — **13.2x más rápido** que Git Worktrees, con verificación física estricta byte a byte.

---

## 3. Cómo Reproducir

```bash
# Ejecutar benchmark comparativo con verificación estricta:
python benchmark.py

# Ejecutar benchmark nativo con estrés de alta concurrencia:
cargo run --release --manifest-path aggit-rs/Cargo.toml -- benchmark --agents 50
```
Las métricas brutas y estructuradas se persisten automáticamente en `benchmark_results.json`.
