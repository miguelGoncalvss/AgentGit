# Metodologia e Resultados de Benchmark

Este documento descreve os procedimentos de teste, métricas e resultados comparativos entre o Git tradicional e o AgGit.

---

## 1. Metodologia

O benchmark avalia a capacidade de lidar com múltiplos escritores concorrentes tentando comitar arquivos no mesmo repositório:

1. **Git (Shared Index)**: Simula o uso padrão do Git por múltiplos agentes compartilhando a mesma árvore de trabalho e o mesmo `.git/index`.
2. **Git (Worktree per Agent)**: Cria uma branch e um *worktree* físico no disco para cada agente, isolando o index.
3. **AgGit (Python Engine)**: Utiliza staging virtual e append-only journal com reconciliação incremental em Python.
4. **AgGit (Rust Native Engine)**: Utiliza CAS acelerado por hardware e Rayon multithreading em Rust nativo.

Todas as rodadas do AgGit passam por **verificação rigorosa de conteúdo**: se qualquer arquivo gerado estiver corrompido, incompleto ou ausente, o benchmark falha.

---

## 2. Resultados no Ambiente de Teste

Executado em Windows 11 com SSD NVMe e 8 threads de CPU concorrentes (workload: 20 agentes):

| Sistema | Sucesso | Taxa de Erro | Tempo Total (s) | Throughput (ops/s) |
| :--- | :---: | :---: | :---: | :---: |
| **Git (Shared Index)** | 6/20 | **70.0%** | 0.57s | 10.53 |
| **Git (Worktree por Agente)** | 20/20 | 0.0% | 1.28s | 15.57 |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 0.79s | 25.26 |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.34s** | **58.48** |

### Observações:
- O **Git com índice compartilhado** falha a maioria das operações devido à trava pessimista `.git/index.lock`.
- O **Git com worktrees** elimina as colisões de lock, mas impõe alta sobrecarga de I/O de disco para criar diretórios físicos e metadados de checkout para cada agente.
- O **AgGit** elimina tanto as travas quanto a duplicação física de arquivos, alcançando throughput até **3.7x superior** ao Git Worktrees com integridade completa verificada.

---

## 3. Como Reproduzir

```bash
# Benchmark comparativo com validação de conteúdo:
python benchmark.py

# Benchmark nativo em Rust para estresse em larga escala (ex: 50 a 100 agentes):
.\aggit.exe benchmark --agents 50
```
Os dados brutos são gravados em `benchmark_results.json`.
