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
- `DATA 1` e uma coluna dinâmica para cada `REPROGRAMA n`.
- Aba normalizada com o histórico ilimitado de reprogramações.
- Legenda dos códigos operacionais.

A migração aditiva necessária é
`ModuloEstoque/supabase/migrations/20260729_mes_stage_parameterization.sql`.
Ela não altera apontamentos existentes nem qualquer saldo de estoque.

O arquivo `.env.local` deve apontar somente para o PostgreSQL Docker local.
Não altere Supabase ou Render para validar este fluxo.

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
