# Formato de Armazenamento do AgGit

🌐 **Idiomas / Languages:** [English](../storage-format.md) | Português (Brasil) | [Español](storage-format.es.md)

Este documento descreve os formatos de dados, schemas e contratos em disco do AgGit.

---

## 1. Content-Addressable Storage (CAS)

Localização: `.aggit/objects/<shard>/<rest>`  
Onde `<shard>` são os 2 primeiros caracteres hexadecimais do hash SHA-256 e `<rest>` os 62 caracteres restantes.

### 1.1 Formato do Objeto
O payload antes da compressão possui o cabeçalho idêntico ao modelo Git:
```
[tipo] [tamanho_em_bytes]\0[dados_brutos]
```
- `tipo`: string UTF-8 (`blob`, `manifest`, `tree`).
- `tamanho_em_bytes`: representação decimal em ASCII.
- `\0`: byte nulo delimitador.
- Compressão: zlib nível 6 (rápida).

---

## 2. Eventos do Journal (Transações)

Localização: `.aggit/journal/<timestamp_ns>_<agent_id>_<tx_id>.json`

### 2.1 Schema Version 2 (Canônico Atual)
```json
{
  "schema_version": 2,
  "tx_id": "c9bf9e57-1685-4c89-bafb-ff5af830be8a",
  "agent_id": "agent_backend",
  "session_id": "session_sprint_1",
  "base_revision": "4f2a7b1c-8e99-4d22-921a-bc015f8d9021",
  "parents": [
    "4f2a7b1c-8e99-4d22-921a-bc015f8d9021"
  ],
  "timestamp": "2026-10-14T20:55:00.123456Z",
  "timestamp_ns": 1792011300123456000,
  "message": "feat: add user authentication endpoint",
  "changes": {
    "src/auth.py": {
      "action": "modify",
      "blob_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
      "old_sha": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    },
    "docs/deprecated.txt": {
      "action": "delete"
    }
  },
  "files": {
    "src/auth.py": "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0"
  },
  "metadata": {}
}
```

---

## 3. Staging por Sessão

Localização: `.aggit/staging/<agent_safe_prefix>_<ident_hash>.json`

```json
{
  "generation": 3,
  "agent_id": "agent_backend",
  "session_id": "session_sprint_1",
  "base_revision": "4f2a7b1c-...",
  "entries": {
    "src/auth.py": {
      "action": "modify",
      "blob_sha": "a1b2c3d4...",
      "old_sha": "01234567..."
    }
  }
}
```

---

## 4. Checkpoints de Reconciliação

Localização: `.aggit/checkpoints/latest.json`

### 4.1 Schema Version 2 (Canônico Atual)
```json
{
  "version": 2,
  "tree": {
    "src/auth.py": "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
    "package.json": "887766554433221100ffeeddccbbaa99887766554433221100ffeeddccbbaa99"
  },
  "processed_tx_ids": [
    "c9bf9e57-1685-4c89-bafb-ff5af830be8a",
    "f2a7b1c8-e994-d229-21ab-c015f8d90210"
  ],
  "pending_conflicts": [
    {
      "path": "src/config.py",
      "base_sha": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "ours_sha": "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
      "theirs_sha": "f1e2d3c4b5a60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
      "ours_tx_id": "c9bf9e57-1685-4c89-bafb-ff5af830be8a",
      "theirs_tx_id": "f2a7b1c8-e994-d229-21ab-c015f8d90210",
      "conflict_type": "content_divergence",
      "description": "Conflict in src/config.py: divergent modifications"
    }
  ]
}
```

---

## 5. Ledger de Exportação para Git

Localização: `.aggit/git_export_map.json`

Mapeia o estado exportado para os commits equivalentes no Git local.

### 5.1 Formato da Chave Composta
Para garantir rastreabilidade, continuidade de branch e independência entre ramificações, a chave do ledger segue a estrutura:
```
<revision_id>::<git_branch>::<parent_git_commit>
```
Exemplo:
```json
{
  "c9bf9e57-1685-4c89-bafb-ff5af830be8a::feature/agent-sprint::": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
  "d1a2b3c4-5e6f-7a8b-9c0d-1e2f3a4b5c6d::feature/agent-sprint::4b825dc642cb6eb9a060e54bf8d69288fbee4904": "8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b"
}
```

---

## 6. Manifesto de Materialização no Disco

Localização: `.aggit/materialized_state.json`

Registra a relação de arquivos gravados no diretório de trabalho na última materialização bem-sucedida, permitindo a detecção de edições locais em 3 estados:
```json
{
  "tracked_files": {
    "src/auth.py": "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
    "package.json": "887766554433221100ffeeddccbbaa99887766554433221100ffeeddccbbaa99"
  },
  "last_reconciled_tx": "c9bf9e57-1685-4c89-bafb-ff5af830be8a"
}
```
