import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from trade import handle_signal

app = FastAPI()


@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(req: Request):
    try:
        payload = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "reason": "bad json"}, status_code=400)

    try:
        result = await handle_signal(payload)
        status = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=status)
    except Exception as e:
        # 안전 로그
        return JSONResponse({"ok": False, "reason": f"exception: {type(e).__name__}"}, status_code=500)