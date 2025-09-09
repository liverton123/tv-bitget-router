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
    # 어떤 경우에도 500이 나가지 않도록 방어
    try:
        payload = await req.json()
    except Exception as e:
        return JSONResponse({"ok": False, "reason": f"bad json: {type(e).__name__}"}, status_code=400)

    try:
        result = await handle_signal(payload)
        # 내부에서 오류가 나도 여기서는 200/400으로만 응답하게
        return JSONResponse(result, status_code=(200 if result.get("ok") else 400))
    except Exception as e:
        # 최종 방어막
        return JSONResponse({"ok": False, "reason": f"unhandled: {type(e).__name__}"}, status_code=400)