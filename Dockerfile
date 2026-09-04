FROM python:3.11-slim
CMD ["python", "apm.py", "--run", "python server.py"]
