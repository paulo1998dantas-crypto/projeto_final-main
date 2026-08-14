-- Permite substituir uma O.S. cancelada sem perder seu historico.
-- ITEM e numero_os permanecem iguais; o UUID e revision_number distinguem
-- cada ocorrencia. Somente uma revisao pode ser corrente por entrada/numero.

begin;

alter table public.erp_work_orders
    add column if not exists revision_number integer not null default 1,
    add column if not exists is_current boolean not null default true,
    add column if not exists supersedes_work_order_id uuid null;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid='public.erp_work_orders'::regclass
          and conname='erp_work_orders_supersedes_work_order_id_fkey'
    ) then
        alter table public.erp_work_orders
            add constraint erp_work_orders_supersedes_work_order_id_fkey
            foreign key (supersedes_work_order_id)
            references public.erp_work_orders(id)
            on delete restrict;
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid='public.erp_work_orders'::regclass
          and conname='ck_erp_work_orders_revision_number_positive'
    ) then
        alter table public.erp_work_orders
            add constraint ck_erp_work_orders_revision_number_positive
            check (revision_number > 0);
    end if;
end $$;

alter table public.erp_work_orders
    drop constraint if exists erp_work_orders_vehicle_entry_id_key,
    drop constraint if exists erp_work_orders_numero_os_key;

create unique index if not exists ux_erp_work_orders_current_entry
    on public.erp_work_orders(vehicle_entry_id)
    where is_current=true;

create unique index if not exists ux_erp_work_orders_current_number
    on public.erp_work_orders(numero_os)
    where is_current=true;

create unique index if not exists ux_erp_work_orders_entry_revision
    on public.erp_work_orders(vehicle_entry_id,revision_number);

create unique index if not exists ux_erp_work_orders_number_revision
    on public.erp_work_orders(numero_os,revision_number);

create index if not exists ix_erp_work_orders_supersedes
    on public.erp_work_orders(supersedes_work_order_id)
    where supersedes_work_order_id is not null;

commit;
