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

### 2.2 Staging Virtual Namespaced e Concorrência de Sessão
- Em vez de um arquivo único compartilhado (`.git/index`), cada par `(agent_id, session_id)` possui seu próprio arquivo de staging, protegido contra colisões por hash SHA-256 no nome do arquivo (`.aggit/staging/<agent>_<hash>.json`).
- **Locks de Sessão Multiplataforma**: Operações de staging utilizam travas exclusivas por sessão (`<path>.lock`) com timeout configurável e backoff exponencial, implementadas com `msvcrt` no Windows e `fcntl` em ambientes POSIX (`aggit/lock_utils.py`).
- **Escrita Segura com Tempfiles Exclusivos**: Atualizações de staging gravam em arquivos temporários exclusivos (`.tmp_stg_<pid>_<thread>_<uuid>.tmp`) seguidos de substituição atômica (`os.replace`), prevenindo erros de concorrência e violações de compartilhamento (`[WinError 32]`).
- **Controle de Geração**: Cada escrita incrementa um contador `generation`. Quando um commit é finalizado, a área de staging só é limpa se o `generation` atual for menor ou igual ao momento do commit, impedindo que edições concorrentes mais recentes sejam perdidas.
- **Exclusões Explícitas**: Suporta adições, modificações e exclusões (`stage_delete`).

### 2.3 Append-Only Journal & Índice Derivado
- Cada commit gera um evento imutável com UUID v4 completo e timestamp em nanossegundos.
- Escritas usam arquivos temporários terminados em `.tmp` para garantir que leitores concorrentes de `.json` nunca leiam arquivos em progresso.
- O índice derivado (`.aggit/journal_index.json`) sincroniza acessos concorrentes sob trava de arquivo. Se o índice for corrompido ou apresentar divergência com o diretório durável `.aggit/journal/`, ele é reconstruído automaticamente usando o diário como fonte única da verdade.

### 2.4 Motor de Reconciliação, DAG e Ciclo de Vida de Conflitos
- **Ordenação Causal por DAG**: O histórico de eventos é ordenado topologicamente respeitando as relações de ancestralidade (`parents`), sem depender da sincronização de relógios físicos entre agentes.
- **Base Real**: Sempre utiliza a base comum real (`base_revision` ou ancestral comum mais recente no DAG), nunca uma base vazia.
- **JSON Semântico**: Preserva adições independentes de chaves recursivamente (ex: `{"porta": 3000}` e `{"debug": true}` mesclados limpos sem conflito), respeita exclusões e detecta conflitos sem corromper o JSON.
- **Python e Texto**: Utiliza merge de 3 vias baseado em diff3 / `git merge-file`, preservando comentários, docstrings de módulo, variáveis globais, condicionais e decorators.
- **Ciclo de Vida Persistente de Conflitos**:
  - Conflitos de conteúdo, modificação/exclusão (`modify/delete`) ou divergência binária são registrados estruturadamente no checkpoint (`pending_conflicts`).
  - Execuções subsequentes de `reconcile`, `status` ou `export-preview` preservam os conflitos pendentes sem transformá-los em sucesso espúrio.
  - **Dry-run Puro**: O modo `--dry-run` calcula o plano estritamente em memória, sem persistir checkpoints, alterar o journal ou tocar o working tree.
  - **Bloqueio de Pipeline**: Materializações no disco e exportações para Git são estritamente bloqueadas enquanto houver conflitos pendentes.
  - **Resolução Explícita (`resolve_conflict`)**: A resolução cria uma transação de reconciliação que substitui as revisões divergentes no DAG, persiste o conteúdo acordado no CAS e remove o conflito da lista de pendências.

### 2.5 Materialização Segura em 3 Estados
- **Validação Trifásica Prévia (Pre-flight Validation)**:
  Antes de gravar ou remover qualquer arquivo no diretório de trabalho, o motor avalia três estados: (1) Último estado registrado no manifesto (`materialized_state.json`), (2) Conteúdo físico atual no disco, e (3) Estado reconciliado desejado.
  - Detecta **edições locais do usuário**: se o hash atual no disco divergir do manifesto gravado anteriormente, a materialização é abortada com erro explícito para evitar perda de dados.
  - Detecta **exclusões locais**: se o arquivo foi removido manualmente no disco, a recriação automática é suspensa a menos que explicitamente solicitada.
  - Detecta **arquivos não rastreados**: impede que o AgGit sobrescreva arquivos pré-existentes criados fora do controle do sistema.
  - Flag `--force`: permite contornar a proteção caso o usuário intencionalmente deseje descartar alterações locais.
- **Publicação Atômica**:
  As gravações no disco são feitas em arquivos temporários exclusivos (`.tmp_mat_<pid>_<thread>_<uuid>.tmp`), com `fsync` e substituição atômica (`os.replace`). O manifesto físico só é atualizado após a confirmação no disco.

### 2.6 Ponte Git e GitHub
- **Isolamento Total**: O workspace principal do usuário permanece intocado durante a exportação. A preparação do commit ocorre em um *worktree* Git temporário isolado fora da pasta de trabalho (`aggit_worktree_*`), limpo de forma determinística em blocos `finally`.
- **Continuidade de Branches**: Ao exportar sequencialmente para uma branch Git existente, o AgGit reutiliza a ponta da branch (`git checkout <branch>`) sem flags destrutivas (`-B`), garantindo que o primeiro commit seja o ancestral legítimo do segundo commit.
- **Preservação de Arquivos Git Não Relacionados**: A exportação adiciona apenas arquivos gerenciados pelo AgGit. Arquivos existentes na branch Git (ex: `keep.md`, `README.md`, `.github/`) são estritamente preservados, removendo apenas caminhos explicitamente marcados como excluídos no AgGit (`action: "delete"`).
- **Repositórios Git Vazios**: Inicializa o commit raiz em repositórios Git recém-criados utilizando comandos de plumbing (`git commit-tree 4b825dc...` com o hash de árvore vazia do Git) e `git update-ref`, sem colocar arquivos não comitados do usuário no índice.
- **Ledger Idempotente de Exportação**: O arquivo `.aggit/git_export_map.json` armazena mapeamentos indexados por chave composta `revision::branch::parent_commit`, garantindo que exportações repetidas para branches distintas mantenham rastreabilidade sem duplicação indevida.
- **Automação Segura de Pull Requests**: Reutiliza a sessão local do GitHub CLI (`gh`), validando autenticação e detectando PRs já existentes para a branch sem duplicar requisições.

### 2.7 Confinamento e Segurança de Caminhos
- **Prevenção de Path Traversal**: Rejeita caminhos absolutos e sequências de navegação com diretórios pai (`..`).
- **Resolução de Links Simbólicos**: Valida o caminho real canônico (`os.path.realpath`) para garantir que o destino não aponte para fora do diretório do repositório.
- **Nomes Reservados em Sistemas Operacionais**: Rejeita nomes de dispositivos reservados do Windows (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`) em qualquer componente de caminho.
