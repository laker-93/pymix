FROM ubuntu:24.04

RUN apt update
RUN apt install ca-certificates curl gnupg -y
RUN install -m 0755 -d /etc/apt/keyrings
RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
RUN chmod a+r /etc/apt/keyrings/docker.gpg

# Add the repository to Apt sources:
RUN echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null
RUN apt update && \
    apt-get install -y software-properties-common
RUN apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin -y
RUN add-apt-repository ppa:deadsnakes/ppa
RUN apt install python3.11 python3-pip python3.11-dev python3.11-venv -y
#RUN apt install docker.io -y
#RUN apt install docker-compose-plugin -y
#FROM tiangolo/uvicorn-gunicorn-fastapi:python3.11
ENV PORT 8002

# Setup App Environment
ENV PYTHONPATH "${PYTHONPATH}:/app"
#RUN python3.11 -m pip install --upgrade pip
RUN python3.11 -m venv venv
COPY ./ToredoCore /app/toredocore
RUN venv/bin/pip install -e /app/toredocore

COPY ./pymix/requirements.txt ./requirements.txt
RUN venv/bin/pip install -r requirements.txt


# Setup App Files
COPY ./pymix/pymix /app/pymix

CMD venv/bin/python /app/pymix/runner.py -e $APP_ENVIRONMENT
