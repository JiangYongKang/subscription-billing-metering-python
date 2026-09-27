import logging

import uvicorn
from fastapi import FastAPI

from app.api import billing_error_handler, router
from app.models import BillingError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title="订阅计费与用量计量服务（本地模拟）")
app.add_exception_handler(BillingError, billing_error_handler)
app.include_router(router)


@app.get("/")
def read_root():
    return {"service": "subscription-billing-metering", "status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
