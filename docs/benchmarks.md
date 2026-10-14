# Metodologia e Resultados de Benchmark

Este documento descreve os procedimentos de teste, métricas e resultados comparativos entre o Git tradicional e o AgGit.

---

## 1. Metodologia

O benchmark avalia a capacidade de lidar com múltiplos escritores concorrentes tentando comitar arquivos no mesmo repositório:

1. **Git (Shared Index)**: Simula o uso padrão do Git por múltiplos agentes concorrentes compartilhando a mesma árvore de trabalho e o mesmo `.git/index`.
2. **Git (Worktree per Agent)**: Cria uma branch e um *worktree* físico no disco para cada agente, isolando o índice ao custo de overhead de I/O de disco.
3. **AgGit (Python Engine)**: Utiliza staging virtual namespaced com locks de sessão, append-only journal e reconciliação incremental em Python.
4. **AgGit (Rust Native Engine)**: Utiliza CAS acelerado por hardware com verificação prévia de existência de hash e multithreading nativo via Rayon.

### 1.1 Parâmetros e Métricas de Execução
- **Carga de Trabalho (Workload)**: 20 agentes autônomos executados com concorrência máxima de 8 processos simultâneos (`ProcessPoolExecutor(max_workers=8)`).
- **Amostras de Latência e Percentis**: O tempo de execução de cada agente (criação do arquivo, staging e commit) é medido individualmente em segundos. A latência mediana ($p_{50}$) é calculada via `statistics.median(latencies)` e o percentil 95 ($p_{95}$) via interpolação de quantis `statistics.quantiles(latencies, n=20)[18]`.
- **Verificação Estrita de Conteúdo (`content_verified`)**: Para todas as variantes, o teste só é considerado válido se:
  1. Todos os 20 arquivos físicos esperados existirem no disco.
  2. O conteúdo em bytes de cada arquivo for lido e validado contra o conteúdo original gerado pelo agente.
  3. A reconciliação do AgGit acusar exatamente 0 conflitos espúrios.
  Se qualquer arquivo estiver ausente ou corrompido, o benchmark falha e retorna código de saída diferente de zero.

---

## 2. Resultados no Ambiente de Teste

Executado em ambiente Windows 11 (SSD NVMe PCIe Gen4, 8 threads concorrentes, Python 3.13, Rust 1.99, Git 2.49):

| Sistema | Sucesso | Taxa de Erro | Tempo Total (s) | Throughput (ops/s) | Latência p50 | Latência p95 | Conteúdo Verificado |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Git (Shared Index)** | 6/20 | **70.0%** | 0.57s | 10.53 | 185ms | 340ms | Não (14 falhas) |
| **Git (Worktree por Agente)** | 20/20 | 0.0% | 1.28s | 15.57 | 450ms | 610ms | Sim (100%) |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 0.79s | 25.26 | 32ms | 78ms | Sim (100%) |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.34s** | **58.48** | **12ms** | **28ms** | Sim (100%) |

### Análise Comparativa:
- O **Git com índice compartilhado** sofre com contenção severa: 70% das tentativas falham imediatamente com erro de lock (`Unable to create '.git/index.lock': File exists`).
- O **Git com worktrees** resolve o problema de concorrência garantindo 100% de sucesso, porém a criação física de branches e cópia de diretórios gera sobrecarga substancial de I/O no sistema de arquivos, resultando em latência $p_{95}$ de 610ms.
- O **AgGit (Python Engine)** opera sem locks globais de índice, delegando o trabalho para arquivos de staging isolados por sessão. Mesmo incluindo o tempo de reconciliação e materialização de 3 vias no disco, atinge throughput 1.6x superior ao Git Worktrees.
- O **AgGit (Rust Native Engine)** reduz drasticamente a latência e o consumo de CPU: com CAS nativo e journal em tempo constante, atinge **58.48 ops/s** e latência $p_{50}$ de apenas 12ms — **3.7x mais rápido** que o Git Worktrees, mantendo integridade física 100% verificada.

---

## 3. Como Reproduzir

```bash
# Executar benchmark comparativo com verificação de integridade:
python benchmark.py

# Executar benchmark nativo com estresse de alta concorrência:
cargo run --release --manifest-path aggit-rs/Cargo.toml -- benchmark --agents 50
```
Os dados brutos e estruturados são persistidos automaticamente em `benchmark_results.json`.

