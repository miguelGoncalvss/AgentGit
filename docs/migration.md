# Guia de Compatibilidade e Migração de Formatos

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
