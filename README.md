# AgGit: Version Control System para Sistemas Multi-Agentes de IA

[![Build & Test](https://github.com/miguelGoncalvss/AgentGit/actions/workflows/ci.yml/badge.svg)](https://github.com/miguelGoncalvss/AgentGit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Rust: 1.80+](https://img.shields.io/badge/Rust-1.80%2B-orange.svg)](https://www.rust-lang.org)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org)

O **AgGit** é um sistema de controle de versão moderno projetado especificamente para **Sistemas Multi-Agentes (MAS)**. Ele substitui a trava exclusiva monolítica do Git (`.git/index.lock`) por uma arquitetura de diário append-only concorrente, áreas de staging isoladas por sessão, reconciliação de três vias com preservação semântica e integração segura com Git e GitHub.

---

## 📑 Sumário

- [Visão Geral e Conceitos](#-visão-geral-e-conceitos)
- [Garantias de Concorrência e Persistência](#-garantias-de-concorrência-e-persistência)
- [Requisitos e Instalação](#-requisitos-e-instalação)
- [Guia de Uso Rápido](#-guia-de-uso-rápido)
- [Resolução de Conflitos](#-resolução-de-conflitos)
- [Integração com Git e GitHub](#-integração-com-git-e-github)
- [Benchmarks](#-benchmarks)
- [Suíte de Testes](#-suíte-de-testes)
- [Limitações Conhecidas](#-limitações-conhecidas)
- [Documentação Técnica Adicional](#-documentação-técnica-adicional)

---

## 💡 Visão Geral e Conceitos

Para evitar as ambiguidades comuns em sistemas distribuídos, o AgGit adota termos formais:

- **Agente (`agent_id`)**: A identidade lógica do agente de IA (ex: `agent_backend`, `agent_qa`).
- **Sessão (`session_id`)**: O contexto de execução atual do agente. Permite que o mesmo agente execute tarefas paralelas sem sobrescrever seu próprio staging.
- **Revisão (`tx_id`)**: Um identificador imutável (UUID v4) de uma transação publicada no journal, contendo metadados, autores, base e alterações de arquivos.
- **Base de Revisão (`base_revision`)**: O identificador exato da revisão a partir da qual o agente iniciou seu trabalho. Impede que merges assumam bases vazias ou ancestrais incorretos.
- **Checkpoint**: Um snapshot derivado e recuperável (`.aggit/checkpoints/latest.json`) que registra a fronteira causal incorporada até o momento, permitindo reconciliação incremental em tempo constante.

---

## 🔒 Garantias de Concorrência e Persistência

- **Publicação Atômica**: Os objetos no CAS e os eventos no diário são gravados em arquivos temporários exclusivos (`.tmp`) com `fsync` explícito e renomeados atomicamente pelo sistema operacional (`os.replace` / `fs::rename`).
- **Leitura Segura**: Arquivos em processo de escrita nunca são tratados como transações concluídas por processos concorrentes.
- **Sem Perda de Atualização no Staging**: As áreas de staging utilizam um contador de geração (`generation`). Commits anteriores não removem arquivos adicionados por operações mais recentes.
- **Integridade Criptográfica**: Todos os objetos do CAS têm seu hash SHA-256 recalculado e validado durante a leitura.

---

## 🛠️ Requisitos e Instalação

### Requisitos
- **Git** 2.30+ instalado no sistema.
- **Python** 3.10+ (para execução via CLI Python).
- **Rust** 1.80+ e Cargo (caso deseje compilar o binário nativo).
- **GitHub CLI (`gh`)** (opcional, necessário apenas para abertura automatizada de Pull Requests).

### Compilação do Binário Nativo em Rust
```bash
cd aggit-rs
cargo build --release
# O executável será gerado em: target/release/aggit (ou aggit.exe no Windows)
```

---

## 🚀 Guia de Uso Rápido

### 1. Inicializar Repositório
```bash
# Via executável compilado:
./aggit init

# Ou via módulo Python:
python -m aggit.cli init
```

### 2. Preparação (Staging) e Commit
```bash
# Agente A prepara arquivos no seu namespace de sessão
./aggit stage src/auth.py --agent agent_backend --session sprint_1

# Agente B prepara arquivos simultaneamente sem colisão
./aggit stage src/ui.py --agent agent_frontend --session sprint_1

# Agente A comita informando mensagem e base
./aggit commit -m "feat: user authentication endpoint" --agent agent_backend --session sprint_1

# Agente B comita no mesmo instante
./aggit commit -m "feat: login interface" --agent agent_frontend --session sprint_1
```

### 3. Exclusão de Arquivos no Staging
```bash
./aggit stage-delete src/legacy.py --agent agent_backend
./aggit commit -m "chore: remove deprecated file" --agent agent_backend
```

### 4. Consultar Status e Histórico
```bash
./aggit status
./aggit log -n 5
```

### 5. Reconciliar e Materializar no Diretório de Trabalho
A reconciliação incremental processa apenas eventos novos desde o último checkpoint e grava somente arquivos modificados:
```bash
./aggit reconcile
```

---

## ⚖️ Resolução de Conflitos

O motor de 3 vias do AgGit classifica e trata conflitos de forma determinística:

1. **Alterações Ortogonais**:
   - Se dois agentes modificam arquivos diferentes ou seções distintas de um arquivo de texto, o merge é limpo e 100% automático.
2. **Arquivos JSON**:
   - Chaves independentes adicionadas por agentes distintos (ex: `{"porta": 3000}` e `{"debug": true}`) são unidas automaticamente no objeto base sem gerar conflito.
   - Objetos aninhados são mesclados recursivamente.
3. **Detecção Explícita de Incompatibilidades**:
   - Conflitos de exclusão contra modificação (`modify/delete`), sobreposição de linhas em texto e divergências em binários são registrados em uma estrutura própria de conflitos e reportados ao usuário/agente.
   - A reconciliação **nunca sobrescreve silenciosamente** nem inventa código concatenado.

---

## 🐙 Integração com Git e GitHub

O AgGit permite sincronizar o trabalho dos agentes com o Git tradicional sem poluir a branch de trabalho ou o workspace principal:

```bash
# 1. Prévia em memória (Dry-Run): lista arquivos e conflitos sem alterar o Git
python -m aggit.cli export-preview --branch feature/agent-sprint

# 2. Exportação isolada: cria um commit limpo em worktree temporário
python -m aggit.cli export-git --branch feature/agent-sprint -m "feat: autonomous sprint sync"

# 3. Publicação explícita (Push):
python -m aggit.cli push --branch feature/agent-sprint --remote origin

# 4. Criar ou reutilizar Pull Request via GitHub CLI:
python -m aggit.cli pr --branch feature/agent-sprint --base-branch main

# 5. Importar um commit do Git como ponto de partida no AgGit:
python -m aggit.cli import-git --ref HEAD
```

---

## 📊 Benchmarks

Comparativo executado com **20 agentes de IA concorrentes** sob carga real:

| Sistema | Sucessos | Taxa de Erro | Tempo Total | Throughput (ops/s) |
| :--- | :---: | :---: | :---: | :---: |
| **Git (Índice Compartilhado)** | 6/20 | **70.0%** | 0.57s | 10.53 |
| **Git (Worktree por Agente)** | 20/20 | 0.0% | 1.28s | 15.57 |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 0.79s | 25.26 |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.34s** | **58.48** |

Para reproduzir os testes com verificação de integridade:
```bash
python benchmark.py
```
Mais detalhes em [docs/benchmarks.md](docs/benchmarks.md).

---

## 🧪 Suíte de Testes

O projeto conta com mais de 25 cenários automatizados cobrindo sanitização de caminhos, integridade de CAS, merge recursivo de JSON, concorrência, checkpoints e integração com Git local:

```bash
# Executar testes em Python:
python tests/test_all_regressions.py -v

# Executar testes nativos em Rust:
cd aggit-rs && cargo test
```

---

## ⚠️ Limitações Conhecidas

1. **Atributos de Arquivo do Sistema Operacional**: Permissões POSIX avançadas (ex: modo executável `chmod +x`) e symlinks ainda não são serializados nos blobs do CAS atual.
2. **Sincronização Bidirecional**: O AgGit suporta importação inicial de commits Git e exportação de branches AgGit para Git. Atualizações contínuas do Git remoto exigem comandos explícitos de `import-git` ou rebase assistido.
3. **GitHub CLI**: A automação de Pull Requests depende da presença do utilitário `gh` autenticado. Em ambientes sem `gh`, a exportação para Git e push continuam funcionando normalmente.

---

## 📚 Documentação Técnica Adicional

- [Arquitetura Completa](docs/architecture.md)
- [Formato de Armazenamento e Schemas](docs/storage-format.md)
- [Integração Detalhada com Git & GitHub](docs/github-integration.md)
- [Relatório e Metodologia de Benchmarks](docs/benchmarks.md)
- [Guia de Migração de Formatos](docs/migration.md)
