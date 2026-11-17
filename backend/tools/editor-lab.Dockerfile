# 実エディタではなく、既知の診断だけを実行する使い捨てイメージ。
# アプリのソース、DB設定、Firebase秘密鍵、Dockerソケットはコピーしない。
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
# 変更するのはイメージ内の新規ディレクトリだけ。既存ボリュームにchown/chmodしない。
RUN mkdir /workspace && chown 1000:1000 /workspace && chmod 0700 /workspace
COPY tools/editor_sandbox_probe.py tools/editor_isolation_probe.py /opt/diagnostic/
LABEL org.team-progress.editor-lab="v1"
USER 1000:1000
WORKDIR /opt/diagnostic
ENTRYPOINT ["python", "-B", "/opt/diagnostic/editor_isolation_probe.py"]
