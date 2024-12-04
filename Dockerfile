FROM python:3.11.4-slim-buster as app

WORKDIR /slack-gpt

ENV HOST 0.0.0.0

COPY pyproject.toml .
COPY poetry.lock .
COPY *.py /slack-gpt/
# RUN mkdir .
COPY app/ /slack-gpt/app/
RUN pip install --no-cache-dir poetry
# Create virtualenv at .venv in the project instead of ~/.cache/
RUN poetry config virtualenvs.in-project true
RUN poetry install

EXPOSE 8080

# USER servicerunner
# Use Gunicorn to run the application
ENTRYPOINT poetry run gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 2 --timeout 0 main_prod:flask_app
