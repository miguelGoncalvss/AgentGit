# Formato de Almacenamiento de AgGit

🌐 **Idiomas / Languages:** [English](../storage-format.md) | [Português (Brasil)](storage-format.pt-BR.md) | Español

Este documento describe los formatos de datos, esquemas y contratos en disco de AgGit.

---

## 1. Content-Addressable Storage (CAS)

Ubicación: `.aggit/objects/<shard>/<rest>`  
Donde `<shard>` son los 2 primeros caracteres hexadecimales del hash SHA-256 y `<rest>` los 62 caracteres restantes.

### 1.1 Formato del Objeto
El payload antes de comprimirse posee un encabezado idéntico al modelo de Git:
```
[tipo] [tamaño_en_bytes]\0[datos_brutos]
```
- `tipo`: cadena UTF-8 (`blob`, `manifest`, `tree`).
- `tamaño_en_bytes`: representación decimal en ASCII.
- `\0`: byte nulo delimitador.
- Compresión: zlib nivel 6 (rápida).

---

## 2. Eventos del Journal (Transacciones)

Ubicación: `.aggit/journal/<timestamp_ns>_<agent_id>_<tx_id>.json`

### 2.1 Schema Version 2 (Canónico Actual)
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

## 3. Staging por Sesión

Ubicación: `.aggit/staging/<agent_safe_prefix>_<ident_hash>.json`

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

## 4. Checkpoints de Reconciliación

Ubicación: `.aggit/checkpoints/latest.json`

### 4.1 Schema Version 2 (Canónico Actual)
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

## 5. Ledger de Exportación a Git

Ubicación: `.aggit/git_export_map.json`

Asigna el estado exportado a los commits equivalentes en el Git local.

### 5.1 Formato de la Clave Compuesta
Para asegurar trazabilidad, continuidad de ramas e independencia entre ramificaciones, la clave sigue la estructura:
```
<revision_id>::<git_branch>::<parent_git_commit>
```
Ejemplo:
```json
{
  "c9bf9e57-1685-4c89-bafb-ff5af830be8a::feature/agent-sprint::": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
  "d1a2b3c4-5e6f-7a8b-9c0d-1e2f3a4b5c6d::feature/agent-sprint::4b825dc642cb6eb9a060e54bf8d69288fbee4904": "8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b"
}
```

---

## 6. Manifiesto de Materialización en Disco

Ubicación: `.aggit/materialized_state.json`

Registra la lista de archivos escritos en el directorio de trabajo durante la última materialización exitosa, permitiendo la detección de ediciones locales en 3 estados:
```json
{
  "tracked_files": {
    "src/auth.py": "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0",
    "package.json": "887766554433221100ffeeddccbbaa99887766554433221100ffeeddccbbaa99"
  },
  "last_reconciled_tx": "c9bf9e57-1685-4c89-bafb-ff5af830be8a"
}
```
