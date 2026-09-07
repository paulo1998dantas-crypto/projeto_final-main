# Conclusão técnica e materiais — 07/09/2026

## Escopo

Nova conclusão técnica disparada por Suprimentos (Gestão de O.S. ou histórico do documento). Não executa fechamento retroativo em massa, não finaliza/entrega veículo e não altera o backflush de entrada ou de O.P.

## Regras

1. Saldo de cada empenho vinculado = quantidade empenhada menos baixas ATIVAS relacionadas ao seu ID. Empenhos cancelados e baixas canceladas não entram na conta. Apenas esse saldo pode gerar nova BAIXA.
2. Antes dos candidatos, abater da necessidade a cobertura dos empenhos da O.S. e das baixas diretas anteriores. Não contar novamente a baixa de um empenho já considerado.
3. Um SKU pai com B.O.M. é item fantasma para a O.S.: a necessidade e a expedição exibem somente as folhas da árvore. A B.O.M. serve para cobertura (um empenho/baixa histórica do pai pode cobrir as folhas), mas a conclusão técnica não gera BAIXA do pai nem dos filhos; só candidatos de SKU folha podem ser consumidos. Um SKU sem B.O.M. continua aparecendo e sendo baixável normalmente.
4. Candidatos são empenhos ativos sem O.S., de SKU folha ativo, com saldo e cobertura da necessidade remanescente. Reservas `PRODUCTION_ORDER` não são saldo livre: pertencem ao fluxo de fabricação interna.
5. Usar uma única necessidade residual ao distribuir candidatos. Priorizar conjuntos sobre filhos e FIFO por SKU. O empenho compartilhado permanece sem O.S.; somente sua BAIXA recebe o vínculo. Não consumir excedente; quantidades fracionárias são truncadas à precisão de estoque (0,001).
6. Necessidade sem candidato é encerrada administrativamente e registrada na auditoria. Não inventar baixa, entrada ou empenho para zerá-la.
7. Saldo físico negativo exige HTTP 409, lista de SKUs/saldos e confirmação explícita com token do plano. A confirmação se invalida quando mudam os consumos ou o saldo negativo apurado. Sem confirmação, nenhuma baixa/conclusão é persistida.
8. Baixa direta sem ID de empenho + empenho aberto do mesmo SKU na O.S. é ambíguo. Bloquear para revisão do vínculo, sem presumir identidade, alterar movimentos históricos ou debitar novamente.

## Atomicidade e rastreabilidade

- `erp_service.technical_close_work_order` mantém a transação que grava baixas, saldos, histórico, auditoria, status técnico e documento de Suprimentos. Erro causa rollback de tudo.
- Bloqueio da O.S. `FOR NO KEY UPDATE`; movimentos pais ordenados por ID; saldos ordenados por SKU. O saldo pendente é consultado em uma instrução nova depois de obter o bloqueio do empenho, como no consumo manual de Estoque.
- Deadlock/conflito de serialização/timeout de bloqueio do PostgreSQL aborta a transação. A API retorna conflito operacional após rollback, sem refazer movimentos automaticamente. Inclui concorrência com o sequenciamento WIP existente.
- Repetição após sucesso retorna `replayed=true`, sem novos movimentos, inclusive se surgiram candidatos depois da conclusão.
- Responsável é o usuário ativo recebido do backend de Suprimentos, não o autor do empenho original.
- Cada nova BAIXA conserva `related_movement_id`, `work_order_id`, operação UUID e chave idempotente; `source_type=TECHNICAL_CLOSE_AUTO_BAIXA`.
- Auditoria `CONCLUSAO_TECNICA` inclui IDs das baixas, candidatos consumidos, quantidades, pendências encerradas e eventual confirmação de negativo.
- Reabertura não estorna estoque. Restaura apenas o documento modificado pela conclusão registrada. Uma nova conclusão usa os saldos ainda pendentes.
- O relatório de necessidade de Estoque já exclui `technical_status=CONCLUIDA`; o documento também passa a `concluido` para as consultas documentais.

## Arquivos relacionados

MES: `erp_stock_closure.py`, `erp_service.py`, `main.py`, `test_mes_technical_close_commitments.py`, `test_mes_technical_close_api.py`.

Estoque: `estoque_app/services/work_order_needs_service.py`, `estoque_app/services/erp_service.py`, `estoque_app/services/estoque_service.py`, `tests/test_work_order_needs.py`.

Suprimentos (repositório irmão `modulo-suprimentos`): `compras_app/app.py`, `compras_app/static/technical_close.js`, templates `erp_gestao_os.html` e `index.html`, testes `test_technical_close_flow.py` e `technical_close.test.cjs`.

Não há migration, variável de ambiente ou permissão nova. Permanecem os requisitos existentes de integração MES/Estoque, autenticação compartilhada e permissão `suprimentos.work_order.technical_close`.

## Verificação e publicação

- Testes transacionais locais com SQLite: backflush prévio, baixa parcial/total, baixas diretas anteriores, candidatos compartilhados/FIFO, cobertura multinível, arredondamento, reservas de O.P., confirmação negativa, reabertura, repetição e rollback.
- Contratos HTTP de MES/Suprimentos e testes JavaScript da confirmação, cancelamento, mudança de plano, erro de rede e duplo clique.
- Esquema de produção conferido por consultas somente de leitura no Supabase. Nenhuma baixa real ou conclusão foi executada para testar.
- SQLite não valida os bloqueios concorrentes do PostgreSQL. A homologação concorrente em PostgreSQL isolado continua necessária antes da publicação; o Docker local não disponibilizou seu mecanismo Linux nesta sessão.
- Não publicar somente Suprimentos: o MES precisa ter a versão correspondente. Depois de homologar, publicar MES e em seguida Suprimentos. Confirmar com O.S. de teste em ambiente isolado e consultar auditoria, saldos e relatórios.
- Estado desta entrega: código local, sem commit/push/deploy desta alteração.
