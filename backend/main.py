# main.py
from fastapi import FastAPI

# Create an instance of the FastAPI class
# 这是一个核心对象，你的所有API都将通过它来注册
app = FastAPI()

# Define a path operation decorator
# @app.get("/") 告诉FastAPI，任何对根路径 ("/") 的GET请求都应由下面的函数处理
@app.get("/")
def read_root():
    """
    Root endpoint, returns a welcome message.
    This is useful for health checks.
    """
    return {"message": "Welcome to the Compliance Monitoring System API"}