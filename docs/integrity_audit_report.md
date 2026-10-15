# Relatório de Auditoria de Integridade, Concorrência e Exportação (AgGit)

**Data:** 15 de Outubro de 2026  
**Ambiente de Execução:** Windows 11 Pro x64, AMD/Intel Multi-Core, SSD NVMe PCIe Gen4, Python 3.13.7, Rust 1.99.0, Git 2.49.0  
**Branch de Trabalho:** `fix/aggit-deep-integrity-windows-and-export`  
**Referência Comparativa:** Commit `309df61c45a36357adc0d7326aa4836143e2586e`

---

## 1. Análise Comparativa: HEAD vs. `309df61c45a36357adc0d7326aa4836143e2586e`

| Área Auditada | Estado em `309df61c45a36357adc0d7326aa4836143e2586e` | Estado no HEAD Atual (`fix/aggit-deep-integrity-windows-and-export`) |
| :--- | :--- | :--- |
| **Validação de Caminhos no Windows** | `_validate_safe_path` entrava em loop infinito ao inspecionar junctions/symlinks porque `os.path.dirname("C:\\") == "C:\\"`. Verificação de prefixo (`target.startswith(base)`) permitia escape para diretórios irmãos (ex: `/work_external` aceito a partir de `/work`). | Implementado `is_path_contained` com `os.path.commonpath` e `os.path.normcase`, garantindo verificação estrita por componentes de diretório. Adicionada guarda explícita de terminação na raiz do sistema de arquivos (`parent == cur`). |
| **Matriz de Exportação Git (3-Way)** | Cenários de divergência humana na branch de exportação falhavam: AgGit deletando arquivo modificado por humano apagava o arquivo sem conflito; AgGit modificando arquivo deletado por humano recriava o arquivo silenciosamente; adições concorrentes divergentes eram sobrescritas; e exportações sucessivas revertiam alterações humanas previamente integradas. | Implementada matriz completa de divergência de 3 vias em `prepare_export_plan`: detecta conflitos `modify_delete`, `delete_modify` e `add_add_divergence`. Exportações sucessivas realizam merge de 3 vias usando `last_aggit_bytes` (base), `cur_git_bytes` (ponta Git com edições integradas) e `cur_aggit_bytes` (novo estado AgGit). |
| **Identidade e Ciclo de Vida de Conflitos** | Conflitos utilizavam UUID aleatório ou SHA sem ordenação determinística de versões. Resolução de um conflito antigo podia limpar indevidamente um conflito concorrente recém-adicionado no mesmo arquivo. | Identidade calculada deterministicamente via SHA-256 (`file_path::internal_path::conflict_type::base_sha::sorted(a_sha, b_sha)`). `resolve_conflict` recebe `target_conflict_ids` e a reconciliação filtra estritamente os conflitos direcionados, preservando conflitos não resolvidos de terceiros agentes. |
| **Auto-reparação de Índices e Dry-Run Estrito** | Se `journal_index.json` estivesse corrompido, defasado ou ausente, novas instâncias do engine falhavam em carregar o histórico. Chamadas em dry-run (`preview_export`) causavam mutações no disco ao regenerar índices ou persistir checkpoints. | Implementado modo `read_only` no `Journal`. `_load_index` detecta automaticamente ausência ou defasagem do índice em relação aos arquivos de transação e auto-repara em memória. Chamadas em dry-run garantem **zero mutações no disco** (sem gravação de blobs CAS, checkpoints ou índices). |
| **Escalabilidade da Reconciliação** | Reconciliação de transações consecutivas recalculava a árvore completa a partir da raiz percorrendo todo o histórico de transações ($O(N^2)$ em acessos a disco). | Implementada memoização de revisões em memória e caminho rápido incremental via delta do commit pai imediato em `get_revision_tree`. Leituras repetidas do histórico durante a reconciliação foram eliminadas. |
| **Hardening de CI Workflows** | Workflow do GitHub Actions executava arquivos de teste individuais codificados manualmente, não descobria novos testes de integridade e não possuía timeouts, permitindo travamentos silenciosos. | CI atualizado com `timeout-minutes: 15` no job e nos steps de execução. Runner configurado para descoberta automática completa via `python -m unittest discover -s tests -v`. |
| **Verificação Estrita de Benchmarks** | `benchmark.py` utilizava verificações superficiais (`exists`, `startswith`) e mantinha fallback para o binário estático `aggit.exe` na raiz do repositório. O benchmark Rust nativo não validava conteúdo em bytes. | Verificação estrita byte-a-byte por SHA-256 de todas as saídas físicas em disco em todos os motores (Python, Rust e Git worktrees). Fallback para executável versionado removido; binário Rust é estritamente compilado a partir dos fontes (`aggit-rs/target/release/aggit.exe`). |

---

## 2. Arquivos Modificados e Novas Implementações

1. **`aggit/path_utils.py`**:
   - Adicionada função `is_path_contained(parent_dir, target_path)` utilizando `os.path.realpath`, `os.path.normcase` e `os.path.commonpath`.
   - Endurecida `sanitize_repo_path` contra caminhos UNC, dispositivos reservados Windows e prefixos de drive.
2. **`aggit/git_bridge.py`**:
   - `_validate_safe_path`: adicionada quebra explícita de loop na raiz (`parent == curr`) e validação via `is_path_contained`.
   - `prepare_export_plan`: implementada matriz de 3 vias cobrindo cenários A, B e C, e preservação de edições humanas integradas em exportações sucessivas.
   - Cálculo de hash compatível com CAS (`blob <len>\0...`) ao referenciar árvores de exportação.
   - Proteção de `journal.read_only = True` durante `preview_export` e `prepare_export_plan`.
3. **`aggit/semantic_merge.py`**:
   - Atualizado `MergeConflict.compute_conflict_id` para computação estritamente determinística com shas de versão ordenados.
   - `merge_files`: atribuição de `base_sha`, `a_sha`, `b_sha` e recálculo de identidade para todas as divergências.
4. **`aggit/engine.py`**:
   - `resolve_conflict`: suporte a `target_conflict_ids` com gravação nos metadados de resolução.
   - `reconcile_state`: filtragem granular por `target_conflict_ids`, retenção de conflitos concorrentes no mesmo caminho.
   - Memoização em `get_revision_tree` e fast-path incremental de pai para linearizar o custo de reconciliação.
5. **`aggit/journal.py`**:
   - Suporte a `read_only`.
   - Auto-reparação do índice (`_load_index`) ao detectar divergência entre a lista de arquivos de transação e `journal_index.json`.
6. **`aggit-rs/src/semantic.rs`**:
   - Implementado `compute_conflict_id` e `recompute_id` com hash SHA-256 e ordenação determinística de versões.
   - Anexação de metadados de versões em todos os ramos de divergência (`modify_delete`, `binary`, `json_key`, texto).
7. **`aggit-rs/src/engine.rs`**:
   - `conflicts_by_path` migrado para `HashMap<String, Vec<MergeConflict>>`.
   - `resolve_conflict` atualizado com parâmetro `target_conflict_ids`.
   - `materialize`: sanitização de caminhos antigos e novos antes de operações no sistema de arquivos.
8. **`aggit-rs/src/main.rs`**:
   - Subcomando `benchmark`: validação física estrita dos bytes esperados em cada arquivo de agente.
9. **`benchmark.py`**:
   - Gravação em modo binário (`wb`) para neutralizar conversão CRLF/LF no Windows.
   - Validação estrita byte-a-byte para Python, Rust e Git worktrees.
   - Remoção de fallback para executável versionado; compilação e execução estrita a partir dos fontes.
10. **`.github/workflows/ci.yml`**:
    - Substituição de chamadas manuais por `python -m unittest discover -s tests -v`.
    - Adição de `timeout-minutes: 15`.
11. **`tests/test_deep_integrity_v2.py`** *(novo)*:
    - 10 cenários de regressão e estresse cobrindo:
      - Terminação em junctions/symlinks no Windows.
      - Bloqueio de escapes para diretórios irmãos com prefixo compartilhado.
      - Cenário A: AgGit del vs Human mod -> conflito `modify_delete`.
      - Cenário B: AgGit mod vs Human del -> conflito `modify_delete`.
      - Cenário C: Add vs Add divergente -> conflito `add_add_divergence`.
      - Exportações sucessivas preservando alterações humanas integradas.
      - Identidade determinística de conflito e rejeição de resolução obsoleta.
      - Auto-recuperação de índices de transação ausentes ou defasados.
      - Isolamento estrito de dry-run (zero escritas em disco com índice ausente).
      - Reconciliação com escala linear.

---

## 3. Metodologia e Resultados dos Benchmarks

Executado em ambiente Windows 11 (SSD NVMe PCIe Gen4, 8 threads concorrentes, Python 3.13, Rust 1.99, Git 2.49):

| Sistema Testado | Sucesso | Taxa de Erro | Tempo Total (s) | Throughput (ops/s) | Latência p50 | Latência p95 | Conteúdo Verificado |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Git (Shared Index)** | 4/20 | **80.0%** | 0.52s | 7.71 | 107ms | 182ms | Sim (4 arquivos) |
| **Git (Worktree por Agente)** | 20/20 | 0.0% | 1.23s | 16.27 | 359ms | 417ms | Sim (100% íntegro) |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 0.88s | 22.66 | 107ms | 490ms | Sim (100% íntegro) |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.09s** | **214.50** | **18ms** | **22ms** | Sim (100% íntegro) |

### Destaques de Desempenho:
- O **Git com índice compartilhado** falhou em 80% das requisições devido à contenção de trava no arquivo `.git/index.lock`.
- O **Git com worktrees dedicados** concluiu todas as operações, porém a sobrecarga de criação de branches e I/O de diretórios resultou em latência $p_{95}$ de 417ms.
- O **AgGit (Python Engine)** superou o Git worktrees com 22.66 ops/s e 0.88s de tempo total (incluindo commit, reconciliação e materialização física no disco).
- O **AgGit (Rust Native Engine)** atingiu **214.50 ops/s** e latência $p_{50}$ de apenas **18ms** (**13.2x mais rápido** que o Git worktrees), com verificação estrita de conteúdo 100% aprovada.

---

## 4. Cobertura da Suíte de Testes Automatizados

- **Suíte Python Completa (`python -m unittest discover -s tests -v`):**
  - **50 testes executados, 50 aprovados** (1 teste de symlinks externos ignorado por falta de privilégio elevado de desenvolvedor no Windows).
  - Tempo total de execução: ~22.7 segundos.
- **Suíte Rust Nativa (`cargo test --manifest-path aggit-rs/Cargo.toml`):**
  - **13 testes de integração executados, 13 aprovados**.
  - Tempo total de execução: ~0.33 segundos.
