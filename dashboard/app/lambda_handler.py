"""Entry point for AWS Lambda behind a Function URL.

Mangum translates the Function URL event into an ASGI request for FastAPI.
Handler setting: ``app.lambda_handler.handler``.
"""
from mangum import Mangum

from .main import app

handler = Mangum(app, lifespan="off")
