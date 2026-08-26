-- Permite ao Financeiro exportar somente o controle diario do MES.
-- Logs e tempos continuam protegidos por mes.exports.read.
begin;

insert into public.erp_permissions (code, module, description)
values (
    'mes.control_export.read',
    'MES',
    'Exportar o relatorio de controle diario.'
)
on conflict (code) do update
set module = excluded.module,
    description = excluded.description;

insert into public.erp_role_permissions (role_code, permission_code)
values ('FINANCEIRO', 'mes.control_export.read')
on conflict do nothing;

commit;
