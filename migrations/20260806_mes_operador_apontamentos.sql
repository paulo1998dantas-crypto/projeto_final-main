-- Operador: permitir somente o apontamento produtivo no MES.
-- Esta migration e aditiva/idempotente: nao altera O.S., etapas, estoques,
-- usuarios, historicos ou qualquer permissao administrativa.
begin;

insert into public.erp_role_permissions (role_code, permission_code)
values ('OPERADOR', 'mes.stage.write')
on conflict do nothing;

commit;
