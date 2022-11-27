FROM tiangolo/uvicorn-gunicorn-fastapi:python3.8
ENV PORT 8000

# Setup App Environment
ARG ENVIRONMENT
ENV APP_ENVIRONMENT ${ENVIRONMENT}

# Install Python Requirements
ARG TOREDOCORE_PIP_INDEX_URL
RUN pip install --upgrade pip
COPY ./requirements.txt ./requirements.txt
RUN pip install -r requirements.txt --extra-index-url "${TOREDOCORE_PIP_INDEX_URL}"

# Setup App Files
COPY ./fundamental_bot /app/fundamental_bot

CMD python /app/fundamental_bot/runner.py -e $APP_ENVIRONMENT
