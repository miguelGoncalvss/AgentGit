# AgGit: Version Control System para Sistemas Multi-Agentes de IA

[![Build & Test](https://github.com/miguelGoncalvss/AgentGit/actions/workflows/ci.yml/badge.svg)](https://github.com/miguelGoncalvss/AgentGit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Rust: 1.80+](https://img.shields.io/badge/Rust-1.80%2B-orange.svg)](https://www.rust-lang.org)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org)

🌐 **Outras Traduções / Languages:** [English](../../README.md) | [Português (Brasil)](README.pt-BR.md) | [Español](README.es.md)

O **AgGit** é um sistema de controle de versão moderno projetado especificamente para **Sistemas Multi-Agentes (MAS)**. Ele substitui a trava exclusiva monolítica do Git (`.git/index.lock`) por uma arquitetura de diário append-only concorrente, áreas de staging isoladas por sessão, reconciliação de três vias com preservação semântica e integração segura com Git e GitHub.

---

## 📑 Sumário

- [Visão Geral e Conceitos](#-visão-geral-e-conceitos)
- [Como o AgGit Funciona e Fluxo de Arquitetura](#-como-o-aggit-funciona-e-fluxo-de-arquitetura)
- [Garantias de Concorrência e Persistência](#-garantias-de-concorrência-e-persistência)
- [Requisitos e Instalação](#-requisitos-e-instalação)
- [Guia de Uso Rápido](#-guia-de-uso-rápido)
- [Resolução de Conflitos e Ciclo de Vida](#-resolução-de-conflitos-e-ciclo-de-vida)
- [Materialização Segura em 3 Estados](#-materialização-segura-em-3-estados)
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

## 🔄 Como o AgGit Funciona e Fluxo de Arquitetura

> [!IMPORTANT]
> **O AgGit é uma camada de versionamento e sincronização projetada para receber e coordenar alterações de múltiplos agentes.**  
> **Ele NÃO executa nem gerencia os agentes de IA diretamente:** seus agentes autônomos, processos em background ou orquestradores (ex: LangChain, CrewAI, AutoGen, scripts customizados) simplesmente chamam a API em Python/Rust ou a CLI do projeto para registrar e comitar seu trabalho.

<p align="center">
  <img src="../img/mermaid-diagram.png" alt="Fluxo de Trabalho do AgGit" width="460" />
</p>

### Ciclo Prático de Trabalho Multi-Agente:
1. **Agentes e sessões**: Vários agentes de IA operam simultaneamente em contextos de sessão isolados sem travar ou concorrer pelo mesmo arquivo de índice.
2. **Staging separado**: Cada par `(agent_id, session_id)` grava em sua própria área virtual de staging (`.aggit/staging/`), eliminando totalmente as disputas por lock no `.git/index`.
3. **Conteúdo e transações em `.aggit`**: Blobs de conteúdo imutáveis (CAS) e transações em diário append-only (`.aggit/journal/`) registram o progresso atomicamente sem bloqueios globais.
4. **Reconciliação**: A reconciliação semântica de 3 vias periódica ou sob demanda mescla edições de texto ortogonais e chaves JSON independentes sem intervenção humana.
5. **Resolução explícita de conflitos (`Conflitos` ➔ `Resolução explícita`)**: Se houver edições concorrentes na mesma linha ou exclusões conflitantes, os conflitos recebem identidade determinística e permanecem pendentes no checkpoint até resolução via `aggit resolve`.
6. **Exportação para branch Git**: Estados reconciliados e limpos são exportados em worktrees isolados para branches dedicadas do Git sem interferir no workspace local do desenvolvedor.
7. **Push e PR no GitHub**: Publicação explícita de branches e abertura ou atualização de Pull Requests via GitHub CLI (`gh`).

---

## 🔒 Garantias de Concorrência e Persistência

- **Publicação Atômica**: Os objetos no CAS e os eventos no diário são gravados em arquivos temporários exclusivos (`.tmp`) com `fsync` explícito e renomeados atomicamente pelo sistema operacional (`os.replace` / `fs::rename`).
- **Leitura Segura**: Arquivos em processo de escrita nunca são tratados como transações concluídas por processos concorrentes.
- **Sem Perda de Atualização no Staging**: As áreas de staging utilizam um contador de geração (`generation`) e locks de sessão (`<path>.lock`). Commits anteriores não removem arquivos adicionados por operações mais recentes.
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
# Simulação sem aplicar alterações (Dry-Run):
python -m aggit.cli reconcile --dry-run

# Reconciliação e materialização segura com pré-validação em 3 estados:
python -m aggit.cli reconcile

# Forçar materialização sobrescrevendo alterações locais caso necessário:
python -m aggit.cli reconcile --force
```

### 6. Resolução Explícita de Conflitos
Conflitos não resolvidos persistem nos checkpoints e bloqueiam materializações e exportações até serem resolvidos:
```bash
# Resolver conflito apontando o conteúdo resolvido:
python -m aggit.cli resolve src/config.py --content "PORT = 8080\nDEBUG = False" -m "fix: resolve port conflict" --agent agent_lead

# Ou resolver a partir de um arquivo físico no disco:
python -m aggit.cli resolve src/config.py --file src/config_resolved.py -m "fix: resolved config"
```

---

## ⚖️ Resolução de Conflitos e Ciclo de Vida

O motor de 3 vias do AgGit classifica e trata conflitos de forma determinística e persistente:

1. **Alterações Ortogonais**:
   - Se dois agentes modificam arquivos diferentes ou seções distintas de um arquivo de texto, o merge é limpo e 100% automático.
2. **Arquivos JSON**:
   - Chaves independentes adicionadas por agentes distintos (ex: `{"porta": 3000}` e `{"debug": true}`) são unidas automaticamente no objeto base sem gerar conflito.
   - Objetos aninhados são mesclados recursivamente.
3. **Persistência de Conflitos Pendentes**:
   - Conflitos de conteúdo, modificação/exclusão (`modify/delete`) ou divergência binária são persistidos no Checkpoint Schema v2 (`pending_conflicts`).
   - Repetir `reconcile`, `status` ou `export-preview` **mantém os conflitos pendentes** até uma resolução explícita.
   - Materialização no disco e exportação para Git são **estritamente bloqueadas** enquanto houver conflitos pendentes.
4. **Resolução Rastreável**:
   - A operação `aggit resolve` grava uma transação formal no diário que unifica as versões divergentes no grafo causal e libera o pipeline.

---

## 🛡️ Materialização Segura em 3 Estados

Para proteger o trabalho do desenvolvedor humano, a materialização realiza uma validação trifásica prévia (*pre-flight validation*):
- Compara: (1) Último estado materializado registrado, (2) Conteúdo físico atual no disco, e (3) Estado desejado reconciliado.
- Detecta:
  - **Edições locais**: se o arquivo no disco foi alterado manualmente após a última materialização, a escrita é abortada.
  - **Exclusões locais**: se o usuário apagou o arquivo localmente, a substituição é prevenida.
  - **Arquivos não rastreados**: impede que a materialização sobrescreva arquivos locais não gerenciados pelo AgGit.
- Escrita atômica: atualizações são gravadas em arquivos temporários exclusivos (`.tmp_mat_<pid>_<thread>_<uuid>.tmp`) com `fsync` e substituição atômica (`os.replace`). O manifesto só é atualizado após a confirmação no disco.

---

## 🐙 Integração com Git e GitHub

O AgGit permite sincronizar o trabalho dos agentes com o Git tradicional sem poluir a branch de trabalho ou o workspace principal:

```bash
# 1. Prévia em memória (Dry-Run): lista arquivos e conflitos sem alterar o Git ou checkpoints
python -m aggit.cli export-preview --branch feature/agent-sprint

# 2. Exportação isolada: cria um commit limpo em worktree temporário preservando continuidade
python -m aggit.cli export-git --branch feature/agent-sprint -m "feat: autonomous sprint sync"

# 3. Publicação explícita (Push):
python -m aggit.cli push --branch feature/agent-sprint --remote origin

# 4. Criar ou reutilizar Pull Request via GitHub CLI:
python -m aggit.cli pr --branch feature/agent-sprint --base-branch main

# 5. Importar um commit do Git como ponto de partida no AgGit:
python -m aggit.cli import-git --ref HEAD
```

- **Continuidade de Branch**: Múltiplos exports sequenciais para a mesma branch mantêm a genealogia de commits (`commit 1 -> commit 2`) sem reset destrutivo.
- **Preservação de Arquivos Git**: Arquivos pré-existentes no repositório Git (como `keep.md`, `README.md`, `.github/`) são estritamente preservados; apenas arquivos expressamente marcados com `action: "delete"` pelo AgGit são removidos.
- **Repositórios Vazios**: Suporte a exportação inicial mesmo em repositórios Git recém-criados sem commits, utilizando plumbing Git de árvore vazia sem interferir no índice do usuário.

---

## 📊 Benchmarks

Comparativo executado com **20 agentes de IA concorrentes** sob carga real em Windows 11 (SSD NVMe, 8 threads concorrentes):

| Sistema | Sucessos | Taxa de Erro | Tempo Total | Throughput (ops/s) | Latência p50 | Latência p95 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Git (Índice Compartilhado)** | 6/20 | **70.0%** | 0.57s | 10.53 | 185ms | 340ms |
| **Git (Worktree por Agente)** | 20/20 | 0.0% | 1.28s | 15.57 | 450ms | 610ms |
| **AgGit (Python Engine)** | 20/20 | **0.0%** | 0.79s | 25.26 | 32ms | 78ms |
| **AgGit (Rust Native Engine)** | 20/20 | **0.0%** | **0.34s** | **58.48** | **12ms** | **28ms** |

*Todas as execuções do AgGit incluem verificação de integridade criptográfica SHA-256 de 100% dos arquivos gerados.*

### ⚡ Concorrência Extrema: 70 Agentes Simultâneos (Rust Engine)
Sob estresse multiagente com **70 agentes autônomos comitando concorrentemente em 70 threads dedicadas do sistema operacional** (avaliação empírica de 10 rodadas, 700 transações atômicas no total):
- **Taxa de Sucesso**: **100.0%** (700/700 commits com sucesso, 0 erros de contenção, taxa de erro de 0.00%).
- **Throughput (Vazão)**: **153.31 ops/sec** (commit de ponta a ponta + reconciliação do DAG + materialização em disco).
- **Latência**: $p_{50} = \mathbf{168.4ms}$, $p_{95} = \mathbf{205.1ms}$, $p_{99} = \mathbf{209.0ms}$.
- **Integridade Byte-a-Byte**: **100% Verificado** em todos os 700 arquivos físicos gravados no disco.

Para reproduzir os testes com cálculo preciso de percentis e verificação de integridade:
```bash
# Benchmark comparativo em Python:
python benchmark.py

# Benchmark de 70 agentes em Rust:
cargo run --release --manifest-path aggit-rs/Cargo.toml -- benchmark --agents 70 --threads 70 --rounds 10
```
Mais detalhes em [docs/benchmarks.md](../benchmarks.md).

---

## 🧪 Suíte de Testes

O projeto conta com 50 cenários de testes automatizados em Python e 14 testes nativos de integração em Rust cobrindo sanitização e contenção de caminhos no Windows, integridade de CAS, concorrência no staging, escalabilidade sob 70 agentes simultâneos, integridade de reconciliação linear, ciclo de vida e identidade de conflitos, matriz de exportação Git de 3 vias, pré-validação trifásica e recuperação automática de índices:

```bash
# Executar todos os testes de regressão e integridade em Python:
python -m unittest discover -s tests -v

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

- [Arquitetura Completa](../architecture.md)
- [Formato de Armazenamento e Schemas](../storage-format.md)
- [Integração Detalhada com Git & GitHub](../github-integration.md)
- [Relatório e Metodologia de Benchmarks](../benchmarks.md)
- [Guia de Migração de Formatos](../migration.md)
