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

## 3. Escala de Alta Capacidade: Benchmark com 70 Agentes Simultâneos (Rust Native Engine)

Para testar a capacidade do AgGit sob estresse de dezenas de agentes simultâneos, o motor nativo em Rust foi avaliado com uma média de **70 agentes autônomos comitando concorrentemente em 70 threads dedicadas do sistema operacional**.

O teste executou uma avaliação empírica multi-rodadas (10 rodadas, totalizando 700 transações atômicas) mensurando velocidade de commit, reconciliação de DAG, materialização em disco, vazão, percentis de latência ($p_{50}, p_{95}, p_{99}$) e integridade estrita byte-a-byte:

| Métrica | Resultado (70 Agentes Concorrentes / 70 Threads) |
| :--- | :---: |
| **Total de Commits Executados (10 Rodadas)** | **700 / 700 (100.0% de Sucesso)** |
| **Falhas de Transação / Conflitos de Lock** | **0 (0.00% de Taxa de Erro)** |
| **Tempo Médio da Fase de Commit** | **0.211s** |
| **Tempo Médio de Reconciliação e Materialização** | **0.258s** |
| **Tempo Total Médio Ponta a Ponta** | **0.468s** |
| **Throughput (Vazão) Empírico** | **153.31 ops/sec** |
| **Latência $p_{50}$ (Mediana)** | **168.4ms** (0.2100s) |
| **Latência $p_{95}$** | **205.1ms** (0.2051s) |
| **Latência $p_{99}$** | **209.0ms** (0.2090s) |
| **Verificação Estrita de Integridade** | **100% Verificado** (todos os 700 arquivos conferidos via SHA-256) |

### Fatores Arquiteturais que Viabilizam Concorrência com mais de 70 Agentes:
1. **Journal de Transações Lock-Free**: As transações são persistidas como arquivos JSON imutáveis independentes (`.aggit/journal/*.json`), eliminando mutexes e locks globais de staging/commit.
2. **Armazenamento CAS Particionado (Sharded)**: Blobs indexados por hash são organizados com prefixos de dois caracteres (`.aggit/objects/xx/yy`), permitindo que 70 threads gravem arquivos simultaneamente sem contenção de diretório.
3. **Reconciliação e Materialização Paralela**: O pool de threads Rayon paraleliza a validação SHA-256 e a escrita física no disco, mantendo a latência total ponta a ponta abaixo de 0.5s para 70 agentes.

---

## 4. Como Reproduzir

```bash
# Executar benchmark comparativo com verificação de integridade (Python):
python benchmark.py

# Executar benchmark de 70 agentes em Rust (1 rodada):
cargo run --release --manifest-path aggit-rs/Cargo.toml -- benchmark --agents 70

# Executar média empírica com 70 agentes e 70 threads (10 rodadas):
cargo run --release --manifest-path aggit-rs/Cargo.toml -- benchmark --agents 70 --threads 70 --rounds 10

# Exportar métricas estruturadas em JSON:
cargo run --release --manifest-path aggit-rs/Cargo.toml -- benchmark --agents 70 --rounds 5 --json
```
Os dados brutos e estruturados são persistidos automaticamente em `benchmark_results.json`.

