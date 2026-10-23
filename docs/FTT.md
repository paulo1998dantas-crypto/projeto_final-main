# Qualidade FTT — MES

Disponível em /ftt, com atalhos ao lado de O.S. / Histórico.
O perfil QUALIDADE é atribuído na administração de usuários do Estoque; permite
consultar o MES, registrar descrições de defeito e apontamentos FTT. Não concede
apontamento produtivo nem administração de O.S. ADMIN também tem acesso.

## Contagem
- Início: 26/10/2026, dia local de São Paulo.
- Unidade: entrada do veículo + área, usando a O.S. vigente. Uma revisão de O.S.
  preserva os defeitos da mesma entrada. Chassis repetidos em entradas diferentes
  (por exemplo pós-vendas) são unidades distintas.
- Produzida: etapa aplicável atualmente CONCLUÍDA, com término vigente no período.
- Com defeito: unidade com pelo menos uma ocorrência desde o início.
- Aprovada de primeira: produzida sem defeito registrado.
- FTT = aprovadas / produzidas × 100; sem denominador retorna Sem base.
- O total entre áreas é a média ponderada de passagens; não é contagem de carros
  únicos da fábrica. Percentuais não são somados nem usam média simples.
- Repetições do mesmo defeito em lançamentos diferentes contam ocorrências no Pareto,
  mas a O.S./área reprova somente uma vez no FTT. Retrabalho não apaga falha anterior.
- Pareto: data de detecção, defeitos agrupados pela descrição na planta, mostrando
  todas as áreas envolvidas; percentual sobre todas as ocorrências, não só o Top 10.
- FTT: período da conclusão da etapa. Uma falha registrada depois permanece ligada
  à O.S./área daquela conclusão. Ocorrências em etapas ainda pendentes aparecem no
  Pareto e aguardam conclusão para integrar o denominador/numerador FTT.
- Referências visuais iniciais: >=95 Classe Mundial; >=90 Atenção; <90 Crítico.
  São convenções locais, não uma certificação.
- Relatórios atualizam após salvar, ao voltar à aba e a cada 60 segundos visíveis.
- Liberação mantém seu papel de encerrar a O.S., mas não é uma área calculada no
  FTT. O filtro semanal preenche automaticamente o intervalo de datas.
- Excel exporta Pareto, FTT por área e os apontamentos do período selecionado.

## Persistência e segurança
Dados aditivos nas tabelas erp_quality_defects, erp_quality_reports e
erp_quality_issues. Não altera O.S., etapas, apontamentos produtivos ou estoque.
Data, usuário, O.S., área e descrição do defeito são persistidos. A tela não
expõe classificação, refugo ou rastreabilidade; eventuais registros legados são
tratados como defeito, sem duplicar a mesma unidade/área.
UUID idempotente evita duplicação no reenvio da mesma operação; reutilização
com dados diferentes é rejeitada. Catálogo normaliza acentos/caixa para impedir
descrições equivalentes duplicadas. Escritas exigem permissão e requisição da
mesma origem; tabelas possuem RLS e acesso exclusivo do backend.

## Publicação
1. Aplicar migrations/20261022_mes_quality_ftt.sql e
   migrations/20261023_mes_quality_ftt_simplify.sql ao banco compartilhado.
   A segunda remove somente os dez textos de catálogo fornecidos pelo sistema
   que ainda não tenham ocorrência vinculada.
2. Publicar o MES com quality_ftt.py, quality_routes.py, tela e script.
3. Publicar a alteração de estoque_app/auth.py no Estoque para exibir QUALIDADE
   no cadastro de acessos. Não atribui o novo perfil automaticamente a ninguém.
4. ADMIN atribui QUALIDADE às contas desejadas.
Antes de 26/10/2026 o lançamento de ocorrências fica bloqueado; descrições de
defeito podem ser cadastradas antes. Não há backfill de defeitos antigos.

## Validação isolada
python -m unittest test_quality_ftt -v
node --check static/quality_ftt.js
node tests/quality_ftt_postgres.cjs
O último teste requer @electric-sql/pglite em ambiente de teste.

