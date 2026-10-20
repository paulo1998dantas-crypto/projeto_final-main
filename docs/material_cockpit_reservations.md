# Reserva de planejamento por sequência — MES

## Regra

O cockpit passa a consultar as O.S. anteriores na fila WIP completa, sem os
filtros de cliente, setor, pesquisa ou paginação da tela. Usa a ordenação dos
cards: sequência persistida, data de entrega, ITEM e UUID como desempate final.
O.S. encerradas, não vigentes ou tecnicamente concluídas não disputam material.

Por SKU folha:

1. Saldo livre global = saldo físico menos empenhos/saídas ainda não baixados
   de todas as ordens, inclusive ordens posteriores na fila.
2. Demanda anterior residual = necessidade vigente das ordens anteriores menos
   a cobertura de seus próprios empenhos e baixas. Um empenho consumido continua
   atendendo sua O.S.; sua baixa não é uma segunda cobertura.
3. Reserva = saldo livre atribuído a essas necessidades anteriores, na ordem
   dos cards. Não passa do saldo físico livre e nunca usa trânsito como estoque.
4. Disponível na sequência = máximo entre saldo livre global e zero, menos a reserva.
5. Falta da O.S. = máximo entre necessidade menos cobertura própria menos saldo
   disponível na sequência e zero.

Exemplo: 30 O.S. precisam de uma unidade cada; há cinco unidades físicas, uma
empenhada para a primeira O.S. A primeira já está atendida, as próximas quatro
disputam as quatro unidades livres e a sexta apresenta falta de uma unidade.

## Integração e compatibilidade

- A operação é apenas leitura; não cria empenhos, reservas físicas, baixas,
  movimentações ou novas tabelas. Não altera O.S., critérios ou sequência.
- Composição pelo documento de Suprimentos vinculado por UUID; vínculo direto
  tem precedência sobre número legado. Usa a versão mais recente desse vínculo.
- Normalização de B.O.M. reutilizada de `erp_stock_closure`: conjuntos e seus
  descendentes já renderizados não duplicam a necessidade; a cobertura de
  conjuntos empenhados/baixados é projetada nos componentes.
- Documentos anteriores são consultados em lote, não por chamada recursiva ao
  cockpit. Apenas os componentes relevantes para a O.S. aberta são alocados.
- `saldo_disponivel` continua sendo o saldo global do contrato anterior. Os novos
  campos `reserva`, `saldo_disponivel_sequencia`, `demanda_anterior`,
  `reservas_anteriores` e `reserva_sequencia` expõem a projeção e sua rastreabilidade.
- Baixas diretas e apropriação de empenhos compartilhados abatem a necessidade
  da O.S. que recebeu o vínculo, sem duplicar a cobertura do mesmo empenho.
- Compras em trânsito continuam sendo previsão geral, sem reserva por sequência.
  Sua data/quantidade e a explosão dos conjuntos em componentes são preservadas.
- É uma fotografia consultiva, recalculada ao abrir/atualizar o cockpit. Um
  empenho operacional simultâneo pode mudar a disponibilidade na próxima consulta;
  a projeção não substitui a confirmação transacional do Estoque.

## Falhas visíveis

Documento anterior ausente, cancelado, vazio, inválido ou com B.O.M. cíclica
marca a reserva como incompleta. O cockpit lista as O.S. para revisão e não
apresenta o material ainda não atendido como disponibilidade confirmada.
O valor de falta, nesse caso, é o mínimo identificado nas composições válidas.
Empenhos/baixas que já atendem integralmente a própria O.S. continuam protegidos.

Sem a estrutura de sequenciamento, permanece a consulta global compatível com
aviso explícito de reserva indisponível. O.S. fora do WIP não toma a fila de
outra ordem e exibe reserva não aplicável.

## Verificação

Testes: `test_mes_material_reservations.py`, `test_mes_material_cockpit.py`,
`test_mes_sequencing.py`, `test_mes_technical_close_commitments.py` e
`test_mes_material_reservations.cjs`. As bases dos testes são SQLite isolado;
nenhum movimento real ou alteração de dados de produção é feito para testar.

Resultado local: 267 testes Python da suíte completa aprovados, mais testes
JavaScript de renderização do cockpit. A base usada para importar a aplicação
nos testes é SQLite em arquivo isolado, com auto-DDL legado desabilitado.
Publicação pelo repositório do MES na branch `main`, sem migration ou alteração
de configuração. Confirmar a revisão em execução no endpoint `/healthz`.

Não há migration nem configuração nova. Publicação exige apenas o MES, com o
sequenciamento WIP existente. Reversão: voltar a versão anterior do código;
não há dados novos a estornar.
