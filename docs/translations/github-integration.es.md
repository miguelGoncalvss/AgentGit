# Integración con Git y GitHub

🌐 **Idiomas / Languages:** [English](../github-integration.md) | [Português (Brasil)](github-integration.pt-BR.md) | Español

**AgGit** funciona como un sistema de control de versiones concurrente para agentes autónomos mientras se integra de forma transparente con el ecosistema tradicional de Git y GitHub.

---

## 1. Principios de Seguridad y Aislamiento

1. **Espacio de Trabajo Principal Intacto**:
   - Los conflictos de fusión nunca corrompen el árbol de trabajo del usuario. La planificación de la exportación se realiza 100% en memoria.
2. **Worktrees de Git Aislados**:
   - La exportación a Git crea un directorio temporal fuera del repositorio (`aggit_worktree_*`) y realiza la confirmación en una rama dedicada (predeterminada: `aggit/sync`).
3. **Alcance Restringido de Archivos**:
   - Solo se agregan las rutas explícitamente incluidas en el plan de exportación (`git add -- <paths>`). Nunca se ejecuta `git add .`.
4. **Idempotencia**:
   - Un libro mayor interno (`.aggit/git_export_map.json`) mapea revisiones de AgGit a hashes de confirmación de Git. Exportar la misma revisión repetidamente no genera confirmaciones duplicadas.
5. **Autenticación Segura mediante GitHub CLI**:
   - AgGit no almacena, transmite ni solicita tokens de acceso. Toda interacción con GitHub delega en la herramienta oficial `gh` y reutiliza la sesión autenticada del usuario.
6. **Continuidad de Ramas sin Reset**:
   - Al enviar múltiples exportaciones sucesivas a la misma rama, AgGit reutiliza la confirmación existente como ancestro directo (`commit 1 -> commit 2`), evitando reinicios forzados (`-B`) que destruirían el linaje de la rama.
7. **Preservación de Archivos Preexistentes de Git**:
   - La exportación respeta los archivos existentes en la rama de Git no administrados por AgGit (por ejemplo: `.github/workflows/`, `keep.md`, `README.md`). Únicamente se eliminan las rutas marcadas explícitamente con acción de borrado (`action: "delete"`).
8. **Plumbing Seguro en Repositorios Vacíos**:
   - Si el repositorio local de Git carece de confirmación raíz, AgGit crea la confirmación inicial mediante comandos de bajo nivel (`git commit-tree 4b825dc...` con el hash SHA-1 de árbol vacío estándar) y `git update-ref`, asegurando que archivos modificados o sin confirmar en el directorio de trabajo no se capturen en la raíz.

---

## 2. Comandos del Flujo de Integración

### 2.1 Vista Previa de la Exportación (Dry-Run)
Verifica los archivos modificados y conflictos sin crear confirmaciones ni alterar Git:
```bash
python -m aggit.cli export-preview --branch feature/agent-sprint
```

### 2.2 Exportación Local a Git
Exporta el estado reconciliado a una rama aislada:
```bash
python -m aggit.cli export-git --branch feature/agent-sprint -m "feat: sprint completado por múltiples agentes"
```

### 2.3 Publicación Explícita (Push)
El envío al repositorio remoto solo ocurre mediante comando explícito:
```bash
python -m aggit.cli push --branch feature/agent-sprint --remote origin
```

### 2.4 Apertura de Pull Request (vía GitHub CLI)
Crea el PR en GitHub o devuelve el enlace al PR existente si ya se encuentra abierto:
```bash
python -m aggit.cli pr --branch feature/agent-sprint --base-branch main --title "Feature: Multi-Agent Sprint"
```

### 2.5 Importación del Estado de Git hacia AgGit
Inicializa una sesión de agentes basándose en una confirmación existente de Git:
```bash
python -m aggit.cli import-git --ref HEAD
```
