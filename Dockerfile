#FROM python:3.11-slim
#WORKDIR /app
#COPY requirements.txt .
#RUN pip install --no-cache-dir -r requirements.txt
#COPY . .
#ENV PORT=10000
#EXPOSE 10000
CMD ["python", "apm.py"]
# CMD sh -c "python server.py & python apm.py --no-http"
