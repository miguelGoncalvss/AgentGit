# Integração com Git e GitHub

O **AgGit** atua como um sistema de controle de versão concorrente para agentes autônomos, enquanto se integra de forma transparente com o ecossistema Git tradicional e GitHub.

---

## 1. Princípios de Segurança e Isolamento

1. **Workspace Principal Intocado**:
   - Conflitos de mesclagem nunca corrompem a árvore de trabalho do usuário. O planejamento do export é feito 100% em memória.
2. **Worktrees Git Isolados**:
   - A exportação para Git cria um diretório temporário fora do repositório (`aggit_worktree_*`) e executa o commit em uma branch dedicada (padrão: `aggit/sync`).
3. **Escopo Restrito de Arquivos**:
   - Apenas os caminhos incluídos no plano de exportação são adicionados (`git add -- <paths>`). Nunca é executado `git add .`.
4. **Idempotência**:
   - Um ledger interno (`.aggit/git_export_map.json`) mapeia revisões AgGit para hashes de commit Git. Exportar a mesma revisão repetidamente não gera commits duplicados.
5. **Autenticação Segura via GitHub CLI**:
   - O AgGit não armazena, transmite ou solicita tokens de acesso. Toda interação com o GitHub delega para o utilitário oficial `gh` e reutiliza a sessão autenticada do usuário.

---

## 2. Comandos do Fluxo de Integração

### 2.1 Prévia da Exportação (Dry-Run)
Verifica os arquivos alterados e conflitos sem criar commits nem tocar no Git:
```bash
python -m aggit.cli export-preview --branch feature/agent-sprint
```

### 2.2 Exportação Local para Git
Exporta o estado reconciliado para uma branch isolada:
```bash
python -m aggit.cli export-git --branch feature/agent-sprint -m "feat: sprint concluída por múltiplos agentes"
```

### 2.3 Publicação Explícita (Push)
O envio para o repositório remoto só ocorre por comando explícito:
```bash
python -m aggit.cli push --branch feature/agent-sprint --remote origin
```

### 2.4 Abertura de Pull Request (via GitHub CLI)
Cria o PR no GitHub ou retorna o link do PR existente se já estiver aberto:
```bash
python -m aggit.cli pr --branch feature/agent-sprint --base-branch main --title "Feature: Multi-Agent Sprint"
```

### 2.5 Importação de Estado do Git para o AgGit
Inicializa uma sessão de agentes baseando-se em um commit Git existente:
```bash
python -m aggit.cli import-git --ref HEAD
```
