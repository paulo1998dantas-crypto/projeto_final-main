-- Consulta geral do MES: somente PCP e ADMIN mantem comandos operacionais.
-- Esta migration altera exclusivamente a matriz de permissoes; nao toca em O.S.,
-- veiculos, etapas, apontamentos, estoque ou historico operacional.
begin;

insert into public.erp_role_permissions (role_code, permission_code)
values ('FINANCEIRO', 'mes.dashboard.read')
on conflict do nothing;

delete from public.erp_role_permissions
where role_code in ('OPERADOR', 'COMPRADOR', 'FINANCEIRO', 'ENGENHARIA')
  and permission_code in (
    'mes.stage.write',
    'mes.work_orders.manage',
    'mes.vehicle_entries.create',
    'mes.schedule.manage',
    'mes.finalize',
    'mes.legacy.import',
    'mes.users.manage'
  );

commit;
