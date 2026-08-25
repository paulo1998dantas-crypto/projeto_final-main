-- Evita ITEMs duplicados quando o navegador repete a entrada por oscilação de rede.
alter table public.erp_vehicle_entries
    add column if not exists idempotency_key text;

create unique index if not exists erp_vehicle_entries_idempotency_key_ux
    on public.erp_vehicle_entries(idempotency_key)
    where idempotency_key is not null;

comment on column public.erp_vehicle_entries.idempotency_key is
    'Chave estável da requisição de entrada; retentativas devolvem o ITEM já criado.';
