FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY metalerp/ ./metalerp/

WORKDIR /app/metalerp

RUN python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["gunicorn", "metalerp.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
