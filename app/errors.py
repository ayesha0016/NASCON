from fastapi.responses import JSONResponse

def err(status, code, message="", details=None):
    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": message or code, "details": details or {}}
    })
