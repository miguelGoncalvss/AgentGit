# Relatório de Auditoria de Integridade, Concorrência e Exportação (AgGit)

**Data:** 15 de Outubro de 2026  
**Ambiente de Execução:** Windows 11 Pro x64, AMD/Intel Multi-Core, SSD NVMe PCIe Gen4, Python 3.13.7, Rust 1.99.0, Git 2.49.0  
**Branch de Trabalho:** `fix/aggit-data-integrity-and-export`  
**Referência Comparativa:** Commit `281fd09539f213fc6f9d08a7da83ed90fbf9684c`

---

## 1. Análise Comparativa: HEAD vs. `281fd09539f213fc6f9d08a7da83ed90fbf9684c`

| Área Auditada | Estado em `281fd09539f213fc6f9d08a7da83ed90fbf9684c` | Estado no HEAD Atual (`fix/aggit-data-integrity-and-export`) |
| :--- | :--- | :--- |
| **Concorrência no Staging** | Lock files (`.lock`) eram removidos (`os.remove`) na liberação, causando *races* de compartilhamento (`WinError 32`) e *lost updates* em concorrência alta (16 threads). O commit limpava todo o staging, descartando arquivos adicionados concorrentemente. | Lock files permanecem abertos/persistidos no disco sob travas nativas do SO (`LockFileEx` no Windows, `flock` no Unix). Staging snapshot captura geração estrita (`get_staged_snapshot`) e `consume_committed_changes` preserva arquivos adicionados concorrentemente. |
| **Ciclo de Vida de Conflitos** | Um terceiro agente fazendo commit comum em arquivo em conflito limpava a pendência silenciosamente. Resoluções por arquivo não limpavam conflitos com chave aninhada. Checkpoints legados sem `pending_conflicts` eram tratados como limpos. | Conflitos só são limpos por transações com metadados explícitos de resolução (`is_explicit_res`). Identidade de conflito separa caminho físico de tokens de caminho interno (`internal_path`). Checkpoint v1/legado dispara reconstrução segura pelo journal. |
| **Semântica de Merge JSON** | Base `{}`, A adiciona `{"key": null}`, B adiciona `{"key": 42}` resolvia silenciosamente para `42` sem conflito (confundia chave ausente com valor nulo). | Introduzido sentinela `_ABSENT` em Python e `Option<&Value>` em Rust. Adição de `null` vs `42` em chave ausente produz conflito estrito de tipo/valor. |
| **Exportação Git Delimitada** | Exportava a árvore completa, sobrescrevendo alterações externas feitas diretamente na branch Git por humanos. Arquivos criados e depois excluídos no AgGit não eram removidos se não existissem na branch base. | Exportação baseada em delta estrito contra `last_aggit_tree` da branch. Arquivos inalterados pelo AgGit nunca são tocados. Deleções são executadas via `git rm -f`. Mudanças concorrentes na mesma chave sofrem 3-way merge ou reportam conflito. |
| **Segurança no Export Worktree** | Não havia validação de *path traversal* contra diretórios superiores ou symlinks maliciosos. | Implementada função `_validate_safe_path` que valida `os.path.realpath`, rejeita escapes de worktree (`..`) e symlinks direcionados para fora do repositório. |
| **Atomicidade de Exportação (CAS)** | `git commit` no worktree atualizava a branch sem verificar se a ponta da branch havia se movido concorrentemente. | Verificação *Compare-And-Swap* (CAS): lê `current_branch_tip` e confirma antes do commit que a branch de destino não avançou concorrentemente. |
| **Execução Dry-Run Zero-Write** | `reconcile_state(dry_run=True)` e `export-preview` gravavam blobs no CAS em `.aggit/objects` ao realizar merges de 3 vias. | Blobs gerados em dry-run ficam estritamente em memória em `virtual_blobs: Dict[str, bytes]`. **Zero bytes** são gravados no disco em dry-run. |
| **Contrato de Hash e Materialização** | A implementação Rust gravava o hash CAS (`blob <len>\0...`) em `materialized_state.json`, enquanto o Python gravava o hash bruto do conteúdo, quebrando a paridade. Deleções locais eram restauradas silenciosamente. | Contrato unificado: ambos gravam `sha256(raw_bytes)`. Deleções manuais locais de arquivos inalterados por agentes são preservadas (`preserved_local_deletions`). Erros de remoção/gravação são propagados. |
| **Desempenho no Append do Journal** | Cada chamada a `append_transaction` executava `_rebuild_index()`, relendo todo o diretório do journal ($O(N)$ em arquivos lidos). | Append incremental sob lock exclusivo no índice: atualiza `by_id`, `all_tx_ids` e `head_tx_id` em tempo constante $O(1)$. |
| **CLI e Ferramentas de Benchmark** | Comando `resolve` não suportava `--content` inline e anunciava sucesso mesmo com conflito remanescente. `benchmark.py` usava `onexc` incompatível com Python 3.11 e resolvia binário para pasta `aggit/`. | CLI suporta `--content`, valida status pós-resolução e sai com código 1 se houver conflito pendente. `safe_rmtree` suporta Python 3.11 (`onerror`) e 3.12+ (`onexc`). Resolução de binário valida `os.path.isfile`. |

---

## 2. Arquivos Modificados e Finalidade das Alterações

1. **`aggit/lock_utils.py`**:
   - Elimina a exclusão de arquivos de lock na liberação, evitando o erro `WinError 32` no Windows e condições de corrida de inodes desvinculados no Unix.
2. **`aggit/staging.py`**:
   - Implementa `get_staged_snapshot()` e `consume_committed_changes()`, garantindo que arquivos inseridos concorrentemente durante o commit de uma transação anterior sejam preservados para o próximo commit.
3. **`aggit/semantic_merge.py`**:
   - Adiciona `file_path` e `internal_path: Optional[List[str]]` em `MergeConflict`.
   - Adiciona `_ABSENT` para diferenciar chaves ausentes de `null`, `false`, `0` e `{}` em merges JSON recursivos.
4. **`aggit/engine.py`**:
   - Adiciona exceção `IncompatibleCheckpointError` no nível do módulo.
   - Atualiza `_load_latest_checkpoint()` para validar o schema v2 (`pending_conflicts`), acionando reconstrução íntegra do journal se legado.
   - Implementa `virtual_blobs` em `reconcile_state(dry_run=True)` para garantir **zero escritas** no CAS.
   - Protege o ciclo de vida dos conflitos: commits comuns não limpam pendências; apenas resoluções explícitas (`is_explicit_res`) com metadados correspondentes podem limpar conflitos.
   - Em `materialize()`, preserva deleções locais de arquivos inalterados e propaga todas as exceções de I/O de disco.
5. **`aggit/git_bridge.py`**:
   - Redesenha a exportação Git com cálculo de delta delimitado contra `last_aggit_tree` por branch.
   - Preserva arquivos externos e commits humanos na branch de destino em caminhos não tocados pelo AgGit.
   - Aplica 3-way merge ou reporta conflito quando AgGit e Git alteram concorrentemente o mesmo arquivo.
   - Implementa `_validate_safe_path` para impedir *path traversal* e escapes por symlinks.
   - Adiciona verificação CAS antes de comitar no worktree.
6. **`aggit/journal.py`**:
   - Substitui `_rebuild_index()` no append por atualização incremental em $O(1)$ sob lock no `index.json`.
7. **`aggit/cli.py`**:
   - Adiciona opção `--content` ao comando `resolve`.
   - Verifica se os conflitos foram realmente eliminados; se persistirem, exibe erro e encerra com código `1`.
8. **`benchmark.py`**:
   - Adiciona compatibilidade de callback `safe_rmtree` para Python 3.11 (`onerror`) e 3.12+ (`onexc`).
   - Garante que a resolução do executável nativo Rust verifique `os.path.isfile`, evitando colisão com o diretório `aggit/`.
9. **`aggit-rs/src/lock_utils.rs`** *(novo)*:
   - Implementa travas de arquivo multiplataforma no Rust (`LockFileEx` no Windows, `flock` no Unix).
10. **`aggit-rs/src/staging.rs`**:
    - Integra locks de sessão `<path>.lock` e método `consume_committed_changes()`.
11. **`aggit-rs/src/semantic.rs`**:
    - Suporta `internal_path` e `file_path` em `MergeConflict`.
    - Implementa `merge_json_recursive` com `Option<&Value>` diferenciando chave ausente de `null`.
12. **`aggit-rs/src/engine.rs`**:
    - Unifica contrato de hash em `materialized_state.json` com `sha256(raw_bytes)`.
    - Preserva deleções locais de arquivos intocados e propaga erros de remoção.
    - Suporta `virtual_blobs` em `reconcile_state(dry_run=true)`.
    - Bloqueia limpeza de conflito por commits comuns sem metadados de resolução.
13. **`tests/test_deep_integrity_regressions.py`** *(novo)*:
    - 10 cenários de teste de regressão cobrindo todas as falhas reportadas.

---

## 3. Metodologia e Resultados dos Benchmarks Reproduzíveis

### 3.1 Metodologia
- **Carga de Teste:** 20 agentes concorrentes gerando e commitando arquivos simultaneamente sob concorrência de 8 a 12 workers.
- **Validação de Conteúdo:** Cada arquivo gerado tem seu conteúdo integral em bytes conferido contra o disco via SHA-256 após materialização e reconciliação.
- **Métricas:** Tempo total decorrido, taxa de erro (locks falhos), vazão efetiva (ops/s) e percentis de latência ($p_{50}$ e $p_{95}$).

### 3.2 Tabela de Resultados Medidos

| Sistema Testado | Sucesso | Taxa de Erro | Tempo Total (s) | Throughput (ops/s) | Latência p50 | Latência p95 | Conteúdo Verificado |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Git (Shared Index)** | 7/20 | **65.0%** | 0.56s | 12.61 | 128ms | 187ms | Não (13 falhas de lock) |
| **Git (Worktree por Agente)** | 20/20 | 0.0% | 1.26s | 15.83 | 377ms | 389ms | Sim (100% íntegro) |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 1.12s | 17.89 | 66ms | 283ms | Sim (100% íntegro) |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.10s** | **207.99** | **19ms** | **31ms** | Sim (100% íntegro) |

### 3.3 Limitações Observadas
- No Windows, sistemas de arquivos NTFS impõem overhead na abertura e fechamento repetido de processos subprocess (`git.exe`). O motor nativo em Rust elimina esse gargalo mantendo as operações em memória e disco direto, atingindo 207.99 ops/s.

---

## 4. Resumo das Garantias de Integridade Comprovadas por Testes

1. **Concorrência no Staging (`test_01` e `test_02`)**:
   - 16 threads escrevendo na mesma sessão simultaneamente não geram perda de arquivos (*zero lost updates*).
   - Inserir arquivos no staging durante a execução de um commit preserva os novos arquivos para o commit seguinte.
2. **Ciclo de Vida de Conflitos e JSON Merge (`test_03`, `test_04`, `test_05`, `test_06`)**:
   - Commit regular de um 3º agente sobre arquivo com conflito mantém a pendência ativa.
   - Resolução explícita de `config.json` elimina com precisão conflitos em chaves internas simples e aninhadas.
   - Merge JSON diferencia chave ausente de valor `null`, produzindo conflito ao invés de aceitar valor arbitrário.
   - Checkpoints antigos/legados disparam reconstrução completa a partir do journal detectando conflitos.
3. **Exportação Delimitada para Git (`test_07`, `test_08`)**:
   - Ciclo de adição e posterior exclusão no AgGit remove o arquivo da branch Git de exportação mesmo se ausente na base.
   - Edições humanas manuais em arquivos não alterados pelo AgGit na branch de exportação são estritamente preservadas.
4. **Isolamento Dry-Run (`test_09`)**:
   - Reconciliação em modo `dry_run=True` executa merges de 3 vias em memória gravando exatamente **zero** arquivos no CAS.
5. **Eficiência no Append (`test_10`)**:
   - Adição de transações opera em tempo constante $O(1)$, eliminando a varredura quadrática do diretório de histórico.
