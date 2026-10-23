-- FTT: início controlado dos modos de falha e regra única de defeito.
-- Não altera O.S., etapas, apontamentos produtivos ou saldo.
begin;
set local lock_timeout = '5s';

-- Os dez textos eram apenas sugestões de catálogo. Como ainda não receberam
-- ocorrências, saem da seleção para o cadastro dos modos de falha reais.
-- Se uma base tiver algum deles já referenciado, a restrição preserva-o.
delete from public.erp_quality_defects d
where d.created_by = 'SISTEMA'
  and d.id in (
    '00000000-0000-4000-8000-000000000001',
    '00000000-0000-4000-8000-000000000002',
    '00000000-0000-4000-8000-000000000003',
    '00000000-0000-4000-8000-000000000004',
    '00000000-0000-4000-8000-000000000005',
    '00000000-0000-4000-8000-000000000006',
    '00000000-0000-4000-8000-000000000007',
    '00000000-0000-4000-8000-000000000008',
    '00000000-0000-4000-8000-000000000009',
    '00000000-0000-4000-8000-000000000010'
  )
  and not exists (
    select 1 from public.erp_quality_issues i where i.defect_id = d.id
  );

commit;
