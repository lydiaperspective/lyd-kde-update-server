FROM docker.io/library/python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir Flask==3.1.1 gunicorn==23.0.0
COPY app.py /app/app.py
EXPOSE 5055
CMD ["gunicorn", "--bind", "0.0.0.0:5055", "--workers", "2", "--threads", "4", "--timeout", "60", "app:app"]
