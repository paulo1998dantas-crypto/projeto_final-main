-- Exclusao controlada de entradas MES lancadas por engano.
-- Somente entradas sem O.S., planejamento, compras ou apontamentos podem ser removidas.
begin;

insert into public.erp_permissions (code, module, description)
values (
    'mes.vehicle_entries.delete',
    'MES',
    'Excluir entradas de veiculo sem O.S. e sem vinculos operacionais.'
)
on conflict (code) do update
set module = excluded.module,
    description = excluded.description;

insert into public.erp_role_permissions (role_code, permission_code)
values ('PCP', 'mes.vehicle_entries.delete')
on conflict do nothing;

commit;
