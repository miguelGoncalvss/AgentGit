# AgGit: Arquitetura do Sistema

O **AgGit** é um sistema de controle de versão nativo para agentes autônomos de IA que elimina o ponto único de bloqueio do Git tradicional (`.git/index.lock`), fornecendo concorrência segura, merge semântico de três vias e integração isolada com Git/GitHub.

---

## 1. Visão Geral da Arquitetura

O sistema é dividido em cinco camadas ortogonais e desacopladas:

```
┌─────────────────────────────────────────────────────────────┐
│                       Agentes de IA                         │
└──────────────┬──────────────────────────────┬───────────────┘
               │                              │
               ▼                              ▼
┌──────────────────────────────┐┌──────────────────────────────┐
│ Staging Virtual por Sessão   ││ Content-Addressable Storage  │
│ (.aggit/staging/*_<hash>.json)││ (.aggit/objects/xx/yy...)   │
└──────────────┬───────────────┘└──────────────▲───────────────┘
               │                               │
               ▼                               │
┌──────────────────────────────┐               │
│ Append-Only Event Journal    ├───────────────┘
│ (.aggit/journal/*.json)      │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Motor de Merge Semântico     │
│ (3-Way: Python, JSON, Texto) │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐┌──────────────────────────────┐
│ Materialização Incremental   ││ Git / GitHub Bridge          │
│ (.aggit/checkpoints/latest)  ││ (Worktrees & Branches Isoladas)│
└──────────────────────────────┘└──────────────────────────────┘
```

---

## 2. Subsistemas

### 2.1 Content-Addressable Storage (CAS)
- **Hashing**: SHA-256 calculado sobre o cabeçalho `[tipo] [tamanho]\0` e payload.
- **Otimização**: A existência do objeto no disco é checada **antes** da compressão, economizando ciclos de CPU em objetos já presentes.
- **Atomicidade**: A gravação utiliza arquivos temporários exclusivos por processo, thread e UUID (`.tmp_obj_<pid>_<uuid>.tmp`), com `fsync` e substituição atômica (`os.replace` / `fs::rename`).
- **Verificação**: Na leitura, valida o hash SHA-256 e impõe limite máximo de descompressão (200 MB) para prevenir ataques de descompressão.

### 2.2 Staging Virtual Namespaced
- Em vez de um arquivo único compartilhado (`.git/index`), cada par `(agent_id, session_id)` possui seu próprio arquivo de staging, protegido contra colisões por hash SHA-256 no nome do arquivo.
- **Controle de Geração**: Cada escrita incrementa um contador `generation`. Quando um commit é finalizado, a área de staging só é limpa se o `generation` atual for menor ou igual ao momento do commit, impedindo que edições concorrentes mais recentes sejam perdidas.
- **Exclusões Explícitas**: Suporta adições, modificações e exclusões (`stage_delete`).

### 2.3 Append-Only Journal & Índice Derivado
- Cada commit gera um evento imutável com UUID v4 completo e timestamp em nanossegundos.
- Escritas usam arquivos temporários terminados em `.tmp` para garantir que leitores concorrentes de `.json` nunca leiam arquivos em progresso.
- Mantém um índice derivado (`.aggit/journal_index.json`) que permite consultas em $O(1)$ sem re-ler todo o diretório de eventos. Se o índice for perdido, ele é reconstruído a partir dos dados duráveis.

### 2.4 Motor de Reconciliação e Merge de Três Vias
- **Base Real**: Sempre utiliza a base comum real (`base_revision` ou ancestral do grafo), nunca uma base vazia.
- **JSON**: Preserva adições independentes de chaves recursivamente (ex: `{"porta": 3000}` e `{"debug": true}` mesclados limpos sem conflito), respeita exclusões e detecta conflitos sem corromper o JSON.
- **Python e Texto**: Utiliza merge de 3 vias baseado em diff3 / `git merge-file`, preservando comentários, docstrings de módulo, variáveis globais, condicionais e decorators.
- **Arquivos Binários**: Detecta alterações divergentes e sinaliza conflitos explicitamente (sem last-write-wins silencioso).

### 2.5 Checkpoints e Materialização Incremental
- `.aggit/checkpoints/latest.json` mantém a árvore consolidada e o conjunto de transações incorporadas.
- Uma nova reconciliação processa **apenas** as transações que ainda não foram incorporadas ao checkpoint.
- `.aggit/materialized_state.json` monitora os arquivos presentes no diretório de trabalho. A materialização regrava apenas arquivos cujos blobs mudaram e remove arquivos deletados. Se nada mudou, 0 arquivos são gravados.

### 2.6 Ponte Git e GitHub
- Prepara o plano de exportação em memória. Se houver conflitos, aborta sem alterar o diretório de trabalho do usuário.
- Utiliza um *worktree* Git temporário isolado e uma branch dedicada (ex: `aggit/sync`).
- Inclui apenas os arquivos do plano (nunca `git add .`).
- Mantém um ledger de mapeamento (`.aggit/git_export_map.json`) que garante idempotência: exportar a mesma revisão para o mesmo destino não duplica commits nem PRs.
- Integração com GitHub CLI (`gh`) verifica autenticação e diagnostica ausência de credenciais sem armazenar tokens.
