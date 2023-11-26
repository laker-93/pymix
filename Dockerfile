FROM tiangolo/uvicorn-gunicorn-fastapi:python3.11
ENV PORT 8002

# Setup App Environment
ARG ENVIRONMENT
ENV APP_ENVIRONMENT ${ENVIRONMENT}

RUN pip install --upgrade pip
COPY ./ToredoCore /app/toredocore
RUN pip install -e /app/toredocore

COPY ./pymix/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt


# Setup App Files
COPY ./pymix/pymix /app/pymix
COPY ./pymix/pymix /app/pymix

CMD python /app/pymix/runner.py -e $APP_ENVIRONMENT
