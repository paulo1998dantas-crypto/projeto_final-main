# FTT do MES: produção e auditoria separadas

## Regra da Qualidade

- Um novo apontamento confirmado como auditoria completa avalia todas as áreas
  aplicáveis da O.S., exceto Liberação. As áreas são fotografadas no registro.
- É possível registrar auditoria sem defeitos. Não há liberação automática de
  produção, apontamento de etapa, baixa ou movimentação de estoque.
- A unidade é a entrada do veículo no MES (`vehicle_entry_id`), não o texto do
  chassi. Revisões de O.S. conservam a unidade; retornos do mesmo chassi não são
  fundidos com a passagem anterior.
- A primeira auditoria completa define a data de entrada na base FTT. Reauditorias
  aparecem no histórico e na lista diária, mas não duplicam o denominador.
- Falhas conhecidas desde 26/10/2026 até a data final selecionada reprovam a
  primeira passagem do veículo/área. Uma reinspeção aprovada não apaga a falha.
- FTT por área = aprovadas de primeira / auditadas nessa área × 100. A produção
  continua visível em coluna própria, pela data de conclusão produtiva vigente.
- O cartão de veículos usa veículos distintos; o total da matriz usa passagens
  por área. Não são a mesma grandeza. Sem auditorias, o indicador é “Sem base”,
  nunca 100% presumido.
- O Pareto continua por data de detecção. Um defeito posterior pode constar no
  Pareto do mês de detecção e reprovar uma coorte auditada em mês anterior.

## Registros anteriores

Os registros existentes não comprovavam aprovação de todas as áreas. Por isso a
migração mantém `audit_complete=false`: preserva defeitos, datas e responsáveis,
sem inventar aprovações retroativas. Os defeitos permanecem no Pareto/histórico e
são considerados quando existir auditoria completa daquele veículo. Nenhum dado
de produção nem arquivo/modelo do Power BI foi alterado. A API legada `/api/ftt`
conserva a regra anterior; a tela MES usa `/api/ftt/quality`.

## Histórico e correções

- “O.S. apontadas no dia” filtra a data da auditoria, não a data de digitação.
  Mostra registros sem defeitos, reinspeções e apontamentos antigos identificados.
- Ao selecionar O.S. no formulário, seu histórico abre automaticamente. Há
  também busca independente para usuários com acesso de leitura.
- “Corrigir área” exige permissão `mes.quality.write`, mesma origem, versão atual
  e motivo. Guarda área anterior/nova, falha, usuário, data e versão em tabela
  própria, sem apagar o registro original ou alterar produção.
- “Editar apontamento” permite corrigir data, defeitos, setores e confirmação de
  auditoria completa. É possível remover a atribuição de um dos setores mantendo
  os demais. As linhas anteriores permanecem inativas e os valores antes/depois
  são preservados, junto com motivo, versão, data e responsável.
- “Excluir apontamento” exige confirmação e motivo. A exclusão é lógica: não
  conta no FTT, Pareto, listas ativas ou exportações, mas pode ser consultada ao
  marcar “Incluir apontamentos excluídos no histórico”. Não há exclusão física de
  evidências nem alteração de O.S./produção/estoque.
- Repetir o envio com a mesma chave é idempotente. Versão desatualizada é rejeitada
  antes de escrever. A transação engloba alteração e evidência da correção.
- Indicadores, lista diária e histórico atualizam após gravações/correções,
  ao retornar à aba e a cada 60 segundos enquanto visível.
- Excel do MES contém Pareto, matriz, defeitos e auditorias/histórico, inclusive
  auditorias sem defeitos. Datas e filtros são preservados.

## Publicação segura

1. Backup e conferência de contagens das tabelas de Qualidade.
2. Aplicar `migrations/20261103195306_mes_quality_audits.sql` e depois
   `migrations/20261103200911_mes_quality_report_editing.sql` no banco do MES.
   É aditiva: dois campos no cabeçalho e duas tabelas novas; sem backfill de
   aprovação, exclusão de dados ou alterações em O.S./estoque/BI.
3. Publicar o código. A migração vem antes do deploy: sem ela, a nova funcionalidade
   responde 503 com instrução explícita, sem gravar registros parciais.
4. Validar com uma auditoria real autorizada pela Qualidade. Não inserir dados
   fictícios em produção.
5. Em rollback, retornar apenas o código anterior e preservar as novas tabelas e
   campos; não excluir auditorias/correções que já tenham sido registradas.

## Verificação isolada

```
python -m unittest test_quality_ftt test_quality_audits test_quality_editing -v
node tests/quality_ftt_postgres.cjs
node --check static/quality_ftt.js
```

O teste PostgreSQL usa PGlite local e valida migração, FKs, índices, RLS e grants
por coluna. Nenhum teste depende de banco operacional. Para revisão visual com
dados descartáveis: `python -m tests.quality_ftt_preview` (127.0.0.1:4192/ftt).
