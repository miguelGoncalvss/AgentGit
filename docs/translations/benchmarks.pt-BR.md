# Metodologia e Resultados de Benchmark

🌐 **Idiomas / Languages:** [English](../benchmarks.md) | Português (Brasil) | [Español](benchmarks.es.md)

Este documento descreve os procedimentos de teste, métricas e resultados comparativos entre o Git tradicional e o AgGit.

---

## 1. Metodologia

O benchmark avalia a capacidade de lidar com múltiplos escritores concorrentes tentando comitar arquivos no mesmo repositório:

1. **Git (Shared Index)**: Simula o uso padrão do Git por múltiplos agentes concorrentes compartilhando a mesma árvore de trabalho e o mesmo `.git/index`.
2. **Git (Worktree per Agent)**: Cria uma branch e um *worktree* físico no disco para cada agente, isolando o índice ao custo de overhead de I/O de disco.
3. **AgGit (Python Engine)**: Utiliza staging virtual namespaced com locks de sessão, append-only journal e reconciliação incremental em Python.
4. **AgGit (Rust Native Engine)**: Utiliza CAS acelerado por hardware com verificação prévia de existência de hash e multithreading nativo via Rayon.

### 1.1 Parâmetros e Métricas de Execução
- **Carga de Trabalho (Workload)**: 20 agentes autônomos executados com concorrência de 8 a 12 workers (`ProcessPoolExecutor(max_workers=8)` no Python e pool de threads Rayon no Rust).
- **Amostras de Latência e Percentis**: O tempo de execução de cada agente (criação do arquivo, staging e commit) é medido individualmente em segundos. A latência mediana ($p_{50}$) é calculada via `statistics.median(latencies)` e o percentil 95 ($p_{95}$) via interpolação de quantis `statistics.quantiles(latencies, n=20)[18]`.
- **Verificação Estrita de Conteúdo (`content_verified`)**: Para todas as variantes, o teste só é considerado válido se:
  1. Todos os 20 arquivos físicos esperados existirem no disco.
  2. O conteúdo integral em bytes de cada arquivo for lido e validado contra o payload original gerado pelo agente via SHA-256.
  3. A reconciliação do AgGit acusar exatamente 0 conflitos espúrios.
  Se qualquer arquivo estiver ausente ou corrompido, o benchmark falha e retorna código de saída diferente de zero.

---

## 2. Resultados no Ambiente de Teste

Executado em ambiente Windows 11 (SSD NVMe PCIe Gen4, 8 threads concorrentes, Python 3.13, Rust 1.99, Git 2.49):

| Sistema | Sucesso | Taxa de Erro | Tempo Total (s) | Throughput (ops/s) | Latência p50 | Latência p95 | Conteúdo Verificado |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Git (Shared Index)** | 4/20 | **80.0%** | 0.52s | 7.71 | 107ms | 182ms | Sim (4 arquivos) |
| **Git (Worktree por Agente)** | 20/20 | 0.0% | 1.23s | 16.27 | 359ms | 417ms | Sim (100% íntegro) |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 0.88s | 22.66 | 107ms | 490ms | Sim (100% íntegro) |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.09s** | **214.50** | **18ms** | **22ms** | Sim (100% íntegro) |

### Análise Comparativa:
- O **Git com índice compartilhado** sofre com contenção severa: 80% das tentativas falham imediatamente com erro de lock (`Unable to create '.git/index.lock': File exists`).
- O **Git com worktrees** resolve o problema de concorrência garantindo 100% de sucesso, porém a criação física de branches e cópia de diretórios gera sobrecarga substancial de I/O no sistema de arquivos, resultando em latência $p_{95}$ de 417ms.
- O **AgGit (Python Engine)** opera sem locks globais de índice, delegando o trabalho para arquivos de staging isolados por sessão. Mesmo incluindo o tempo de reconciliação e materialização de 3 vias no disco, atinge throughput superior ao Git Worktrees (22.66 ops/s vs 16.27 ops/s).
- O **AgGit (Rust Native Engine)** reduz drasticamente a latência e o consumo de CPU: com CAS nativo e journal em tempo constante ($O(1)$), atinge **214.50 ops/s** e latência $p_{50}$ de apenas 18ms — **13.2x mais rápido** que o Git Worktrees, mantendo integridade física 100% verificada byte-a-byte.

---

## 3. Como Reproduzir

```bash
# Executar benchmark comparativo com verificação de integridade:
python benchmark.py

# Executar benchmark nativo com estresse de alta concorrência:
cargo run --release --manifest-path aggit-rs/Cargo.toml -- benchmark --agents 50
```
Os dados brutos e estruturados são persistidos automaticamente em `benchmark_results.json`.
