FROM python:3.11-slim
COPY requirements.txt .
COPY . .
CMD ["python", "apm.py"]
