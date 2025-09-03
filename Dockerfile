FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py /app/app.py
COPY .env /app/.env
ENV PORT=8089
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8089"]
