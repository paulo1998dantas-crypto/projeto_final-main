"""FTT endpoints registered without changing production pointing routes."""
from io import BytesIO
from urllib.parse import urlsplit
import logging
from fastapi import Request, Body
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from openpyxl import Workbook
import quality_ftt as ftt
import authz

log = logging.getLogger(__name__)

def register(app, engine, get_db, require_login, templates, stages):
    codes = [code for code, _, _ in stages]
    def principal(request, db, write=False):
        user = require_login(request, db)
        if not user:
            raise PermissionError("Entre no MES para acessar o FTT.")
        if not user.can("mes.quality.write" if write else "mes.quality.read"):
            raise PermissionError("Seu perfil não possui acesso a esta ação do FTT.")
        if write:
            origin = request.headers.get("origin")
            if request.headers.get("X-FTT-Request") != "1" or (origin and urlsplit(origin).netloc != request.headers.get("host")):
                raise PermissionError("Sessão de apontamento inválida. Atualize a página.")
        return user

    async def execute(request, action, write=False):
        db = get_db()
        try:
            user = principal(request, db, write)
            with engine.begin() as conn:
                result = action(conn, user)
            return JSONResponse(jsonable_encoder({"ok":True, **result}), headers={"Cache-Control":"no-store"})
        except PermissionError as exc:
            return JSONResponse({"ok":False,"error":str(exc)},status_code=403)
        except ValueError as exc:
            return JSONResponse({"ok":False,"error":str(exc)},status_code=503 if "migração" in str(exc) else 400)
        except Exception:
            log.exception("Falha no FTT: %s",request.url.path)
            return JSONResponse({"ok":False,"error":"Não foi possível concluir a operação FTT. Atualize e confira o histórico."},status_code=500)
        finally:
            db.close()

    @app.get("/ftt")
    async def screen(request:Request):
        db = get_db()
        try:
            user = require_login(request,db)
            if not user:
                return RedirectResponse("/login?next=/ftt",status_code=303)
            if not user.can("mes.quality.read"):
                return JSONResponse({"error":"Acesso FTT não autorizado."},status_code=403)
            return templates.TemplateResponse(request,"quality_ftt.html",
                {"request":request,"current_user":user,"can_write":user.can("mes.quality.write")})
        finally:
            db.close()

    @app.get("/api/ftt")
    async def report(request:Request):
        return await execute(request,lambda conn,user: ftt.dashboard(conn,codes,request.query_params.get("from"),request.query_params.get("to")))

    @app.get("/api/ftt/options")
    async def options(request:Request):
        def load(conn,user):
            ftt.require_ready(conn)
            return {"defects":[dict(r) for r in conn.execute(select(ftt.defects).order_by(ftt.defects.c.description)).mappings()],
                    "stages":codes,"work_orders":ftt.work_options(conn,request.query_params.get("q",""))}
        return await execute(request,load)

    @app.post("/api/ftt/defects")
    async def add_defect(request:Request, data:dict=Body(...)):
        return await execute(request,lambda conn,user:{"defect":ftt.create_defect(conn,data.get("description"),user.username)},True)

    @app.post("/api/ftt/reports")
    async def add_report(request:Request, data:dict=Body(...)):
        return await execute(request,lambda conn,user:ftt.create_report(conn,data,user,codes),True)

    @app.get("/api/ftt/export.xlsx")
    async def export(request:Request):
        db = get_db()
        try:
            principal(request,db)
            with engine.connect() as conn:
                data = ftt.dashboard(conn,codes,request.query_params.get("from"),request.query_params.get("to"))
            book=Workbook()
            for index,(name,headers,rows) in enumerate([
                ("Pareto",["Rank","Descrição do defeito","Área","Ocorrências","% acumulado"],
                 [[r[k] for k in ("rank","description","area","frequency","accumulated")] for r in data["pareto"]]
                 + [["","TOTAL DE FALHAS","",data["total_failures"],None]]),
                ("FTT por área",["Área","Produzidas","Com defeito","Refugadas","Aprovadas de primeira","FTT (%)","Situação"],
                 [[ftt.AREA_NAMES.get(r["area"],r["area"])]+[r[k] for k in ("produced","defective","scrapped","first_pass","ftt","status")] for r in data["areas"]]
                 + [["TOTAL / PASSAGENS"]+[data["totals"][k] for k in ("produced","defective","scrapped","first_pass","ftt")]+["Média ponderada"]]),
                ("Apontamentos",["O.S.","Chassi","Data de detecção","Defeito","Área","Classificação","Responsável","Registrado em"],
                 [[str(r.get(k) or "") if k!="stage_code" else ftt.AREA_NAMES.get(r[k],r[k]) for k in ("numero_os","chassi","observed_on","description","stage_code","disposition","actor","created_at")] for r in data["records"]])
            ]):
                sheet=book.active if index==0 else book.create_sheet()
                sheet.title=name
                sheet.append(headers)
                for row in rows:
                    # Text supplied by users must remain text, not Excel formulas.
                    sheet.append([("'" + v) if isinstance(v,str) and v.startswith(("=","+","-","@")) else v for v in row])
                sheet.freeze_panes="A2"
                sheet.auto_filter.ref=sheet.dimensions
                for column in sheet.columns:
                    sheet.column_dimensions[column[0].column_letter].width=min(60,max(16,max(len(str(c.value or "")) for c in column)+2))
            output=BytesIO()
            book.save(output)
            output.seek(0)
            return StreamingResponse(output,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition":'attachment; filename="Indicador_FTT.xlsx"',"Cache-Control":"no-store"})
        except (ValueError,PermissionError) as exc:
            return JSONResponse({"error":str(exc)},status_code=403 if isinstance(exc,PermissionError) else 400)
        finally:
            db.close()

