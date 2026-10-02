-- Normalize the three O.S. fields to the fixed choices used by Suprimentos/MES.
-- Every changed record gets a before/after audit event. Re-running is idempotent.
WITH normalized AS MATERIALIZED (
    SELECT
        w.id,
        w.acessibilidade AS old_acessibilidade,
        w.acessorio AS old_acessorio,
        w.plotagem AS old_plotagem,
        CASE upper(trim(coalesce(w.acessibilidade, '')))
            WHEN '' THEN 'NÃO'
            WHEN '-' THEN 'NÃO'
            WHEN 'N/A' THEN 'NÃO'
            WHEN 'DPM ELEVITA' THEN 'DPM ELEVITTA'
            WHEN 'FOCA' THEN 'DPM FOCA'
            WHEN 'PTA ABERTA' THEN 'BI PARTIDA'
            WHEN 'ABERTA' THEN 'BI PARTIDA'
            WHEN 'PTA BI PARTIDA' THEN 'BI PARTIDA'
            WHEN 'PTA FECHADA' THEN 'FECHADA'
            WHEN 'SIM' THEN 'FECHADA'
            ELSE upper(trim(w.acessibilidade))
        END AS new_acessibilidade,
        CASE upper(trim(coalesce(w.acessorio, '')))
            WHEN '' THEN 'NÃO'
            WHEN '-' THEN 'NÃO'
            WHEN 'N/A' THEN 'NÃO'
            WHEN 'INSTAL TECH' THEN 'INSTALL TECH'
            WHEN 'INSTALL-TECH' THEN 'INSTALL TECH'
            WHEN 'OUTRO' THEN 'OUTROS'
            ELSE upper(trim(w.acessorio))
        END AS new_acessorio,
        CASE upper(trim(coalesce(w.plotagem, '')))
            WHEN '' THEN 'NÃO'
            WHEN '-' THEN 'NÃO'
            WHEN 'N/A' THEN 'NÃO'
            WHEN 'NAO' THEN 'NÃO'
            WHEN 'N' THEN 'NÃO'
            WHEN 'S' THEN 'SIM'
            ELSE upper(trim(w.plotagem))
        END AS new_plotagem
    FROM public.erp_work_orders AS w
), updated AS (
    UPDATE public.erp_work_orders AS w
       SET acessibilidade = n.new_acessibilidade,
           acessorio = n.new_acessorio,
           plotagem = n.new_plotagem,
           updated_at = now(),
           version = coalesce(w.version, 0) + 1
      FROM normalized AS n
     WHERE w.id = n.id
       AND (w.acessibilidade IS DISTINCT FROM n.new_acessibilidade
         OR w.acessorio IS DISTINCT FROM n.new_acessorio
         OR w.plotagem IS DISTINCT FROM n.new_plotagem)
    RETURNING w.id
), audited AS (
    INSERT INTO public.erp_audit_events (
        entity_type, entity_id, action, actor, origin,
        before_data, after_data, reason
    )
    SELECT
        'WORK_ORDER', u.id, 'PADRONIZACAO_OPCOES_OS', 'MIGRACAO', 'MES',
        jsonb_build_object(
            'acessibilidade', n.old_acessibilidade,
            'acessorio', n.old_acessorio,
            'plotagem', n.old_plotagem
        ),
        jsonb_build_object(
            'acessibilidade', n.new_acessibilidade,
            'acessorio', n.new_acessorio,
            'plotagem', n.new_plotagem
        ),
        'Padronização das opções de O.S. conforme lista definida pelo usuário.'
    FROM updated AS u
    JOIN normalized AS n ON n.id = u.id
    RETURNING id
)
SELECT
    (SELECT count(*) FROM updated) AS registros_atualizados,
    (SELECT count(*) FROM audited) AS eventos_de_auditoria;
