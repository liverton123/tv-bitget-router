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
    except Exception as e:
        print(f"[WEBHOOK] bad json: {type(e).__name__}")
        return JSONResponse({"ok": False, "reason": "bad_json"}, status_code=400)

    try:
        result = await handle_signal(payload)
        # 본문 요약 로그
        print(f"[WEBHOOK] result: {result}")
        return JSONResponse(result, status_code=(200 if result.get("ok") else 400))
    except Exception as e:
        print(f"[WEBHOOK] unhandled: {type(e).__name__}")
        return JSONResponse({"ok": False, "reason": "unhandled"}, status_code=400)