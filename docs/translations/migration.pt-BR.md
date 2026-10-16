# Guia de Compatibilidade e Migração de Formatos

🌐 **Idiomas / Languages:** [English](../migration.md) | Português (Brasil) | [Español](migration.es.md)

O AgGit foi projetado para manter retrocompatibilidade com repositórios e eventos criados em versões anteriores.

---

## 1. Evolução do Formato de Eventos

| Campo | Schema v1 (Legado) | Schema v2 (Canônico Atual) |
| :--- | :--- | :--- |
| **Identificador (`tx_id`)** | 8 caracteres truncados | UUID v4 completo (36 caracteres) |
| **Registro de Arquivos** | Dicionário plano `files: {path: sha}` | Dicionário estruturado `changes: {path: {action, blob_sha, old_sha}}` |
| **Exclusões** | Ausência implícita de chave | Ação explícita `"action": "delete"` |
| **Revisão Base** | Ausente (herdava HEAD linear) | `base_revision: "uuid"` explícito |
| **Identidade de Sessão** | Apenas `agent_id` | `session_id` desacoplado de `agent_id` |
| **Carimbo de Tempo** | Float em segundos | String ISO-8601 UTC + `timestamp_ns` (i64) |

---

## 2. Como a Migração Ocorre

1. **Leitura Transparente**:
   Ao ler um evento sem o campo `"schema_version"` ou com `"schema_version": 1`, os leitores Python (`TransactionRecord.from_dict`) e Rust (`Transaction::get_effective_changes`) convertem automaticamente o dicionário `files` em registros de modificação (`action="modify"`).
2. **Preservação de Dados Duráveis**:
   Eventos legados no journal `.aggit/journal/` não são reescritos nem destruídos. Novos commits geram eventos no schema v2.
3. **Compatibilidade dos Blobs CAS**:
   O formato dos objetos armazenados no CAS (`.aggit/objects/`) permanece 100% idêntico entre v1 e v2: cabeçalho `[tipo] [tamanho]\0` comprimido com zlib e endereçado por SHA-256.

---

## 3. Evolução dos Checkpoints e Auto-recuperação

### 3.1 Checkpoints Legados (Schema v1)
No schema v1 de checkpoints (`.aggit/checkpoints/latest.json`), o arquivo registrava apenas `tree` e `processed_tx_ids`. Se um conflito ocorresse em execuções antigas, ele não era persistido estruturadamente, arriscando perda de contexto em reinicializações de processo.

### 3.2 Migração para Checkpoint Schema v2
- **Compatibilidade de Leitura**: Ao carregar um checkpoint sem o campo `"pending_conflicts"`, o motor atribui automaticamente uma lista vazia `[]`, mantendo compatibilidade direta.
- **Detecção e Preservação**: Novos conflitos detectados durante a reconciliação são persistidos estruturadamente no schema v2 com metadados completos (`path`, `base_sha`, `ours_sha`, `theirs_sha`, `conflict_type`).
- **Auto-recuperação Baseada no Journal**: Caso um checkpoint antigo tenha sido gravado de forma inconsistente ou corrompido, a recuperação utiliza o append-only journal (`.aggit/journal/`) como fonte imutável da verdade. Ao apagar ou invalidar o arquivo de checkpoint, a execução de `aggit reconcile` reprocessa o histórico causal segundo o DAG de transações, restabelecendo a árvore unificada e re-detectando quaisquer conflitos não resolvidos.
