"""Local-only disposable preview: python -m tests.quality_ftt_preview.

No production imports, credentials, connections or persisted data.
"""
from datetime import date
from unittest.mock import patch
from uuid import uuid4
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
import uvicorn
import quality_ftt as q
import quality_routes
from test_quality_ftt import FTTTests


def preview():
    fixture = FTTTests()
    fixture.setUp()
    fixture.clock.stop()
    with patch("quality_ftt.today", return_value=date(2026,11,3)):
        fixture.save(fixture.payload(audit_complete=True))
        other=str(uuid4())
        with fixture.engine.begin() as c:
            c.execute(text("insert into erp_vehicles values('v2','TESTE-00000002')"))
            c.execute(text("insert into erp_vehicle_entries values('e2','v2',9998)"))
            c.execute(text("insert into erp_work_orders values(:id,'9998','e2','ATIVA',true)"),{"id":other})
            for stage in q.AREA_NAMES:
                c.execute(text("insert into erp_work_order_stages values(:id,:stage,'PENDENTE',true,null)"),{"id":other,"stage":stage})
        qdata=dict(idempotency_key=str(uuid4()),work_order_id=other,observed_on="2026-11-03",audit_complete=True,issues=[])
        with fixture.engine.begin() as c:
            q.create_report(c,qdata,fixture.user,list(q.AREA_NAMES))
    app=FastAPI()
    class DB:
        def close(self):pass
    templates=Jinja2Templates(directory="templates")
    templates.env.globals["preview_mode"]=True
    quality_routes.register(app,fixture.engine,DB,lambda request,db:fixture.user,
        templates,[(code,index,[]) for index,code in enumerate(q.AREA_NAMES)])
    app.mount("/static",StaticFiles(directory="static"),name="static")
    return app


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=4192)
    uvicorn.run(preview(),host="127.0.0.1",port=parser.parse_args().port)
