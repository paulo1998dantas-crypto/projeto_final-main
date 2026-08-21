# projeto_final-main

MES industrial em FastAPI. O fluxo ERP novo recebe O.S. ativadas pelo
Suprimentos e mantém o upload legado disponível durante a transição.

## Fluxo operacional local

1. Registre a entrada, gere o ITEM e abra a O.S. em
   `http://127.0.0.1:5001/erp/gestao-os`.
2. Ao salvar a O.S., as 12 etapas são criadas com o código `?` e o MES
   apresenta `AG. PARAMETRIZAÇÃO`.
3. Em `http://127.0.0.1:8010/gestao-os`, defina cada etapa como:
   `?` (não parametrizada), `P` (parcial), `N` (pendente), `S` (concluída)
   ou `N/A` (não aplicável). A ativação é bloqueada enquanto existir `?`.
4. Execute e finalize a O.S. no mesmo endereço.
5. `FINALIZADA`, `ENTREGUE` e `RETIRADA` saem do quadro ativo, mas continuam
   disponíveis na visão histórica com etapas, programações e eventos.

## Relatório diário

O botão **Exportar controle diário** e a rota
`/exportar_controle_producao` geram um XLSX com:

- Controle de Produção e todas as 12 etapas.
- `DATA COMERCIAL` calculada pelo prazo padrão da linha: 30 dias para LB/LAB
  e 45 dias para LE/LAE, sem ser alterada por reprogramações.
- `INÍCIO REAL DE PRODUÇÃO`, `TÉRMINO PRODUÇÃO` e `DATA SAÍDA` separados.
- `DATA 1` com a primeira promessa e `REPROGRAMA 1` com a data vigente.
- Aba normalizada com o histórico ilimitado de reprogramações.
- Processos em branco quando ainda não houve O.S.; `?` apenas para O.S.
  aguardando parametrização, além da legenda dos códigos operacionais.

A migração aditiva necessária é
`ModuloEstoque/supabase/migrations/20260729_mes_stage_parameterization.sql`.
Ela não altera apontamentos existentes nem qualquer saldo de estoque.

O arquivo `.env.local` deve apontar somente para o PostgreSQL Docker local.
Não altere Supabase ou Render para validar este fluxo.

## Autenticação compartilhada e perfis

O modo padrão continua sendo `MES_AUTH_MODE=legacy`, permitindo rollback sem
alterar as contas antigas. Depois de aplicar e validar as tabelas RBAC
compartilhadas, use `MES_AUTH_MODE=shared_users`. Nesse modo:

- `public.users` é a fonte única de usuário, senha e estado ativo.
- Os vários perfis vêm de `erp_user_roles` e suas permissões de
  `erp_role_permissions`, com exceções por usuário.
- O MES grava somente o hash do token em `erp_app_sessions` e revoga a sessão
  automaticamente quando `users.auth_version` muda.
- `/usuarios` redireciona para o gerenciamento central do Estoque.
- todas as tabelas e colunas do contrato RBAC são obrigatórias; qualquer falta
  bloqueia login/sessão e faz `/healthz` responder `503`, sem reativar usuários
  legados.

Ordem segura no Render: mantenha `MES_AUTH_MODE=legacy`, aplique e reconcilie as
migrations no banco compartilhado, publique o código, altere a `DATABASE_URL`
do MES e, por último, mude para `MES_AUTH_MODE=shared_users`. O rollback volta
somente o modo para `legacy`; não remova tabelas nem vínculos.

As aplicações continuam com cookies próprios porque são serviços Render
independentes; a credencial e os perfis, porém, são os mesmos.

### Login central pelo Portal Operacional

Com `ERP_PORTAL_SSO_ENABLED=1`, uma tentativa de abrir uma rota do MES sem
sessão é encaminhada ao Portal Operacional e retorna à rota originalmente
solicitada. O Portal emite um comprovante de curta duração e o MES valida de
novo o usuário e suas permissões antes de gravar seu cookie local. Configure o
mesmo `ERP_PORTAL_SSO_SECRET` no Portal, Cadastro, Estoque e Suprimentos; use
`ERP_PORTAL_URL=https://ji-portal-operacional.onrender.com`. Enquanto a chave
estiver em `0`, o login próprio do MES permanece como contingência.

O endpoint interno
`/api/erp/internal/work-order-options?q=...&limit=20`, protegido pelo token de
backend, fornece O.S. ativas para seletores de chassi/O.S. em outros módulos.

## Reconciliação do MES legado para o ERP compartilhado

Antes de qualquer migração, execute `mes_legacy_reconciliation.py`. Ele é
estritamente somente leitura: compara os 27 veículos/cartões do MES legado com
as O.S. existentes em `suprimentos_documentos` no ERP compartilhado e gera uma
matriz de aprovação em JSON. Uma coincidência de chassi é apenas sugestão; não
é importada automaticamente porque um mesmo veículo pode retornar em outra
O.S.

No PowerShell de uma máquina de staging, obtenha as duas connection strings
seguras no painel Supabase e defina-as apenas naquela sessão:

```powershell
$env:MES_LEGACY_DATABASE_URL = 'postgresql+...'
$env:ERP_TARGET_DATABASE_URL = 'postgresql+...'
python .\mes_legacy_reconciliation.py --report .\artifacts\mes_reconciliation.json
```

O relatório não usa o Docker local, não lê planilhas e não escreve nos dois
Supabase. A próxima etapa só poderá consumir uma matriz revisada/aprovada,
backup restaurável e execução prévia em staging.

Como alternativa mais segura no Windows, execute
`executar_reconciliacao_mes_segura.ps1`. Ele solicita as duas `DATABASE_URL`
completas sem ecoá-las nem salvá-las em arquivo e as mantém somente na memória
do processo. Isso evita erro de dupla codificação em senhas com caracteres
especiais:

```powershell
powershell -ExecutionPolicy Bypass -File .\executar_reconciliacao_mes_segura.ps1
```

## Perfil Produção e tempos por etapa

O perfil `PRODUCAO` possui somente `mes.dashboard.read` e
`mes.stage.write`. Ao acessar o MES, esse usuário é direcionado para
`/producao`, uma interface simplificada com busca de veículo, seleção da etapa
e os comandos **Iniciar**, **Parar**, **Interromper** e **Finalizar**. Usuários
que também tenham o perfil PCP continuam na interface completa.

Cada novo início cria uma sessão produtiva independente. Por isso:

- o primeiro início da etapa permanece como início canônico;
- a conclusão mais recente passa a ser o término canônico;
- o tempo produtivo total é a soma das sessões finalizadas;
- paradas e interrupções são somadas separadamente e nunca entram no tempo
  produtivo;
- uma etapa concluída pode voltar para ajuste sem apagar o apontamento
  anterior;
- veículos aguardando O.S. e O.S. finalizadas aceitam apontamentos; entregues
  e retirados permanecem bloqueados.

A estrutura necessária está na migration aditiva
`migrations/20260820_mes_production_profile_stage_pauses.sql`. Antes de
ativá-la em produção:

1. confirme backup restaurável e valide a migration em staging;
2. aplique a migration sem executar reset, seed, drop ou truncate;
3. publique o MES e o Estoque com o novo catálogo do perfil;
4. atribua `PRODUCAO` somente aos operadores autorizados;
5. teste iniciar, parar, retomar, finalizar e reabrir uma etapa concluída;
6. confira no histórico que o primeiro início foi preservado e que os totais
   produtivo e parado estão separados.

O rollback operacional não exige remover tabelas: retire temporariamente o
perfil dos usuários ou volte a versão da aplicação. As tabelas aditivas podem
permanecer no banco, preservando o histórico já registrado.
