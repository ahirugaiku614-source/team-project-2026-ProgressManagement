from fastapi import FastAPI

app = FastAPI(title="チーム進捗管理API")

@app.get("/")
def read_root():
    return {"status": "ok", "message": "FastAPI サーバーが正常に起動しています！"}

@app.get("/healthcheck")
def healthcheck():
   return {"status": "ok"} 