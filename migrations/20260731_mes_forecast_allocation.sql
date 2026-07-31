-- A Forecast may be allocated to one real vehicle entry / O.S. only.
-- The application also locks the row; these indexes protect direct writes.
create unique index if not exists suprimentos_forecasts_one_vehicle_entry_idx
    on public.suprimentos_forecasts (vehicle_entry_id)
    where vehicle_entry_id is not null;

create unique index if not exists suprimentos_forecasts_one_work_order_idx
    on public.suprimentos_forecasts (work_order_id)
    where work_order_id is not null;
