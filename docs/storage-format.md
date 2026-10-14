# Formato de Armazenamento do AgGit

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

```json
{
  "version": 1,
  "tree": {
    "src/auth.py": "a1b2c3d4...",
    "package.json": "88776655..."
  },
  "processed_tx_ids": [
    "tx-1",
    "tx-2",
    "tx-3"
  ]
}
```
