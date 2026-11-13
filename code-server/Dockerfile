
FROM codercom/code-server:latest

USER root

# Python・Git・Node.js・npmをインストール
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3 \
      python3-pip \
      python3-venv \
      git \
      nodejs \
      npm \
      build-essential \
      ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# code-serverの通常ユーザーに戻す
USER coder

WORKDIR /home/coder/project
