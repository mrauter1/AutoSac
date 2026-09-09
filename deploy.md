# Deploy do AutoSac

## Ambiente e regras

- Repositório de desenvolvimento: `/home/rauter/AutoSac`.
- Checkout de produção: `/home/rauter/prod/AutoSac`, branch `main`.
- Remoto autorizado: `git@github.com:mrauter1/AutoSac.git`.
- Serviços systemd: `autosac-web` e `autosac-worker`, executados como `rauter`.
- Web de produção: porta `8001`; a porta `9000` é desenvolvimento.
- Configuração local: `/home/rauter/prod/AutoSac/.env`.
- Python de produção: `/home/rauter/prod/AutoSac/.venv/bin/python`.
- Backups: `/home/rauter/prod/backups/`.

Confirme esses valores antes de agir: o ambiente pode mudar. Deploy exige autorização
do usuário; não faça push, alterações de configuração ou migrações fora desse escopo.
Não exponha `.env`, senhas, tokens ou ambientes dos processos nos logs.

Não use `git reset --hard`, `git clean`, force-push ou cópia de arquivos por cima do
checkout para resolver divergências. Preserve `.env`, `.venv`, uploads e sessões Codex.
Não execute o instalador de serviços, bootstrap de workspace ou setup de PostgreSQL
como parte de uma atualização comum: eles podem alterar configuração existente.

## 1. Preparar e validar a versão

No desenvolvimento, confirme que as mudanças foram revisadas, testadas, commitadas
e enviadas para `origin/main`. Não publique uma árvore de trabalho suja.

```bash
cd /home/rauter/AutoSac
git status --short --branch
git fetch origin
git log -1 --oneline origin/main
.venv/bin/python -m pytest -q
```

Para alterações de interface, execute também o teste de navegador, usando um binário
Chromium disponível na máquina:

```bash
CHROME_BIN=/home/rauter/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome \
  node scripts/check_ticket_filters_browser.mjs
```

Os testes acima verificam o checkout local. Confirme que ele corresponde ao commit
que será implantado; não rode a suíte de testes contra o ambiente de produção.

## 2. Inspecionar a produção sem alterá-la

Execute os comandos seguintes na mesma sessão de shell. Falhas devem interromper
a sequência; não continue até entender e resolver a causa.

```bash
set -euo pipefail
PROD_REPO=/home/rauter/prod/AutoSac
PROD_EXEC=(/home/rauter/AutoSac/.venv/bin/python /home/rauter/AutoSac/scripts/prod_exec.py)
cd "$PROD_REPO"
git status --short --branch
git remote -v
test "$(git branch --show-current)" = main
test -z "$(git status --porcelain)"
git fetch origin
PREVIOUS_COMMIT=$(git rev-parse HEAD)
DEPLOY_TARGET=$(git rev-parse origin/main)
git merge-base --is-ancestor HEAD "$DEPLOY_TARGET"
git log --oneline HEAD.."$DEPLOY_TARGET"
git diff --stat HEAD "$DEPLOY_TARGET"
git diff HEAD "$DEPLOY_TARGET" -- requirements.txt .env.example shared/migrations/versions
systemctl show autosac-web autosac-worker \
  -p WorkingDirectory -p ExecStart -p User -p ActiveState -p MainPID
curl --fail --silent http://127.0.0.1:8001/readyz
"${PROD_EXEC[@]}" -- -m alembic current
```

Se houver arquivos modificados, não descarte nem commite automaticamente mudanças de
produção. Investigue com o usuário. Se `main` divergiu, pare: não crie um merge local
de produção. Se aparecer uma revisão de banco desconhecida, compare-a com as migrações
do commit alvo; não use `alembic stamp` para mascarar a divergência.

Revise novas variáveis obrigatórias e a compatibilidade das migrações. Não substitua
`.env` por `.env.example`. Preserve os defaults de modelo/esforço e as flags Codex.
Não suponha que desenvolvimento e produção usam bancos distintos: confirme a
identidade do banco e coordene outros processos que possam escrever nele.

`prod_exec.py` consulta as propriedades efetivas carregadas no systemd para web e
worker, incluindo overrides. Executa o Python de produção em um serviço temporário,
com o usuário e diretório de produção, ambiente independente do shell e os mesmos
`EnvironmentFiles`, interpretados pelo próprio systemd. Valores de `Environment=`
passam por arquivo temporário privado, nunca por argumentos contendo segredos.
O retorno do comando é propagado; o executor não inicia nem para os serviços AutoSac.

O suporte é deliberadamente limitado à configuração convencional deste servidor.
Ambientes divergentes (exceto `PORT`), arquivos opcionais, specifiers e mecanismos
dinâmicos não suportados causam recusa. Não remova essa validação para prosseguir.
O executor não clona todo o sandbox do serviço: namespaces, credenciais systemd ou
outras personalizações de execução exigem revisão antes do uso. Não altere unidades,
drop-ins ou arquivos de ambiente durante o deploy. Se houver mudanças pendentes no
systemd, coordene sua aplicação e repita a validação antes da manutenção.

Criar serviços temporários pode exigir privilégio adicional ao de reiniciar AutoSac.
Teste o executor **antes de parar o web**. Se houver recusa de autorização, solicite
ao operador executar o procedimento ou autorizar o uso de `sudo -n` no executor:

```bash
# Somente quando autorizado e necessário:
PROD_EXEC=(sudo -n /home/rauter/AutoSac/.venv/bin/python /home/rauter/AutoSac/scripts/prod_exec.py)
"${PROD_EXEC[@]}" -- -c 'print("Production executor OK")'
```

Não faça fallback para executar Alembic ou backup diretamente no shell. O caminho
do executor é o checkout de desenvolvimento revisado, permitindo usá-lo mesmo antes
de atualizar uma produção que ainda não tenha o script. Não altere esse script
durante o procedimento. Comandos chamados pelo executor também não devem imprimir
configuração, tokens ou senhas.

Verifique execuções de IA antes da manutenção, sem imprimir conteúdo dos tickets:

```bash
"${PROD_EXEC[@]}" -- - <<'PY'
from sqlalchemy import create_engine, text
from shared.config import get_settings
with create_engine(get_settings().database_url).connect() as connection:
    print(connection.execute(text(
        "SELECT status, count(*) FROM ai_runs "
        "WHERE status IN ('running', 'pending') GROUP BY status"
    )).all())
PY
```

Essa consulta é apenas uma inspeção inicial, não uma garantia de drenagem. Identifique
todos os produtores: outras instâncias web, integrações, automações, workers e
ambientes de desenvolvimento que compartilhem o banco. Coordene a suspensão de novas
entradas; não pare serviços externos ao escopo sem autorização. Se não for possível
controlar os produtores, adie a manutenção ou solicite autorização para interrupção.
Nunca cancele tickets ou altere registros de execução para facilitar o deploy.

## 3. Abrir a janela de manutenção e criar backups

Primeiro registre o estado inicial e prepare a recuperação. O procedimento comum
abaixo pressupõe que os dois serviços estavam ativos. Se algum já estava parado,
investigue antes de continuar e não o inicie automaticamente na recuperação.

```bash
umask 077
systemctl is-active --quiet autosac-web
systemctl is-active --quiet autosac-worker
DEPLOY_BACKUP=$(mktemp -d /home/rauter/prod/backups/autosac-deploy-XXXXXX)
git rev-parse HEAD > "$DEPLOY_BACKUP/previous-commit.txt"
git rev-parse origin/main > "$DEPLOY_BACKUP/target-commit.txt"
systemctl --no-ask-password stop autosac-web
test "$(systemctl show autosac-web -p ActiveState --value)" = inactive
"${PROD_EXEC[@]}" -- - <<'PY'
import time
from sqlalchemy import create_engine, text
from shared.config import get_settings
engine = create_engine(get_settings().database_url, connect_args={"connect_timeout": 10})
deadline = time.monotonic() + 600
while True:
    # A new transaction on each poll observes completed/queued work correctly.
    with engine.connect() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '10s'"))
        count = connection.execute(text(
            "SELECT count(*) FROM ai_runs WHERE status IN ('running', 'pending')"
        )).scalar_one()
    if count == 0:
        print('AI queue drained')
        break
    if time.monotonic() >= deadline:
        raise SystemExit('Drain timeout; do not stop the worker without a decision')
    print('Waiting for AI queue to drain', flush=True)
    time.sleep(5)
PY
systemctl --no-ask-password stop autosac-worker
systemctl show autosac-web autosac-worker -p ActiveState -p SubState
test "$(systemctl show autosac-web -p ActiveState --value)" = inactive
test "$(systemctl show autosac-worker -p ActiveState --value)" = inactive
```

O limite de drenagem é dez minutos. Durante a espera, monitore se o worker permanece
saudável. Fila vazia só é suficiente porque novos produtores foram previamente
controlados. Se surgir trabalho novo, não prossiga até resolver a origem. Se a espera
expirar ou o deploy for abandonado **antes de modificar código, dependências ou banco**,
restabeleça os serviços que este procedimento parou: confirme o worker saudável e
reabra o web. Não deixe a produção indisponível por causa de um comando abortado.
Solicite decisão entre adiar ou interromper; não faça correções manuais na fila.

Com os serviços parados, faça os backups:

```bash
tar --exclude='./.venv' --exclude='__pycache__' \
  -czf "$DEPLOY_BACKUP/files.tar.gz" -C "$PROD_REPO" .
"${PROD_EXEC[@]}" -- - "$DEPLOY_BACKUP/database.dump" <<'PY'
import os
import subprocess
import sys
from sqlalchemy.engine import make_url
from shared.config import get_settings
url = make_url(get_settings().database_url)
env = {
    **os.environ,
    'PGHOST': url.host or 'localhost',
    'PGPORT': str(url.port or 5432),
    'PGUSER': url.username or '',
    'PGPASSWORD': url.password or '',
    'PGDATABASE': url.database or '',
}
subprocess.run([
    'pg_dump', '--format=custom',
    '--file=' + sys.argv[1],
], env=env, check=True)
PY
test -s "$DEPLOY_BACKUP/files.tar.gz"
test -s "$DEPLOY_BACKUP/database.dump"
pg_restore --list "$DEPLOY_BACKUP/database.dump" >/dev/null
```

O exemplo de backup pressupõe PostgreSQL com conexão TCP comum. Se a configuração
usar parâmetros adicionais de TLS, serviço PostgreSQL ou socket, adapte o `pg_dump`
à conexão real, sem remover suas garantias de segurança. Use cliente compatível
com a versão do servidor. Um backup do banco não cobre anexos ou artefatos externos:
preserve seus diretórios configurados e inclua-os se a mudança puder afetá-los.

Se o systemd exigir autenticação, solicite ao operador executar os comandos com
`sudo`; não contorne a autorização matando processos ou lançando serviços paralelos.
Confirme que os dois serviços estão inativos antes de continuar. Se uma etapa falhar
após a parada, comunique a indisponibilidade e retome a versão anterior quando seguro.

## 4. Atualizar o checkout e as dependências

```bash
git merge --ff-only "$DEPLOY_TARGET"
test "$(git rev-parse HEAD)" = "$DEPLOY_TARGET"
```

`fetch` seguido desse fast-forward é o equivalente controlado de um
`git pull --ff-only origin main`: implanta exatamente o commit revisado no passo 2,
mesmo se o remoto receber novos commits durante a manutenção.

Se `requirements.txt` mudou, preserve a `.venv` antes da instalação e instale as
dependências com o Python de produção:

```bash
if ! git diff --quiet "$PREVIOUS_COMMIT" HEAD -- requirements.txt; then
  tar -czf "$DEPLOY_BACKUP/venv.tar.gz" -C "$PROD_REPO" .venv
  "${PROD_EXEC[@]}" -- -m pip install -r requirements.txt
fi
"${PROD_EXEC[@]}" -- -m pip check
```

Não atualize pacotes indiscriminadamente nem altere a configuração dos serviços se
a versão não exigir isso. Preserve a `.env` existente.

## 5. Migrar e validar antes de iniciar

```bash
"${PROD_EXEC[@]}" -- -m alembic upgrade head
"${PROD_EXEC[@]}" -- -m alembic current
"${PROD_EXEC[@]}" --service web -- scripts/run_web.py --check
"${PROD_EXEC[@]}" -- scripts/run_worker.py --check
```

Todos os comandos devem concluir com sucesso. O check do worker verifica contratos,
autenticação persistente e caminhos configurados; não deve iniciar uma análise de
ticket. Não trate falhas como avisos nem execute bootstrap automaticamente para
silenciá-las. Revise previamente migrações destrutivas ou incompatíveis com rollback.

Em instalações antigas que ainda precisem do backfill de histórico de execução,
revise `scripts/backfill_ai_run_steps.py` e execute-o com o Python de produção após
a migração, conforme o README. Esse é um procedimento de dados: confirme que a falha
de prontidão realmente corresponde ao histórico ausente, em vez de executá-lo como
tentativa genérica de recuperação.

## 6. Iniciar e verificar o processo real

```bash
systemctl --no-ask-password start autosac-worker
systemctl show autosac-worker -p ActiveState -p SubState -p MainPID -p NRestarts
systemctl is-active --quiet autosac-worker
```

Observe o worker por alguns ciclos, sem aumento de `NRestarts` ou falhas de startup.
Confirme atividade de heartbeat e os logs de inicialização sem divulgar conteúdo
privado. Se não estiver estável, mantenha o web fechado e investigue. Só então:

```bash
systemctl --no-ask-password start autosac-web
systemctl show autosac-web autosac-worker \
  -p ActiveState -p SubState -p MainPID -p NRestarts
curl --fail --silent --retry 5 --retry-connrefused --retry-delay 1 \
  http://127.0.0.1:8001/healthz
curl --fail --silent http://127.0.0.1:8001/readyz
git status --short --branch
git log -1 --oneline
```

Confirme `active/running` para os dois serviços e respostas `ok` e `ready`. Observe
novamente os serviços após alguns ciclos do worker para detectar reinícios repetidos.
Inspecione os logs recentes quando necessário, sem compartilhar credenciais ou
conteúdo privado. Verifique login e as telas alteradas; não crie tickets, envie
mensagens nem dispare IA em produção sem autorização adicional.

`readyz` confirma a prontidão web, não substitui a verificação do worker. Informe ao
usuário commit implantado, revisão do banco, saúde dos dois serviços e caminho do backup.
Não apague backups automaticamente.

## Recuperação em caso de falha

1. Se nada foi alterado além da parada de serviços, restaure o estado inicial como
   descrito na etapa 3. Caso contrário, identifique a etapa que falhou e mantenha os serviços parados se houver risco de
   executar código incompatível com o banco. Não continue uma sequência parcialmente
   falha sem avaliar o estado real.
2. Para rollback somente de código, confirme árvore limpa e compatibilidade do
   banco/dependências. Use `git switch --detach "$PREVIOUS_COMMIT"` para selecionar
   a versão anterior sem reescrever `main`; restaure dependências compatíveis se
   necessário, valide e inicie os serviços. Informe explicitamente o HEAD destacado.
3. Não faça downgrade Alembic nem restaure o banco automaticamente: isso pode apagar
   dados criados após o backup, inclusive por outros ambientes que compartilhem o
   banco. Obtenha aprovação e um plano específico de recuperação de dados.
4. Depois de corrigir a falha, retorne a `main`, confirme o commit desejado e refaça
   as validações. Nunca faça force-push da produção.

## Histórico da conversão para Git

Em 2026-09-09, a produção foi convertida de cópia de distribuição para checkout Git
de `main`, com `origin` apontando para `mrauter1/AutoSac`. A versão de referência era
`c1177fe`. A conversão associa histórico e índice aos arquivos já existentes; não
exige substituir `.env`, `.venv`, configurações ou reiniciar serviços.
