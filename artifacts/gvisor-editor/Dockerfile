# 検証用レシピのみ。今回はbuildしない。承認済み公式multiarch digestの指定が必須。
# build contextはこのディレクトリだけに限定し、リポジトリrootを渡さない。
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root
# このrootはイメージ構築時のみ。実行時はUSERとrunsc側設定の両方で非rootに固定する。
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv gcc libc6-dev make nodejs npm git ca-certificates \
    && apt-get purge -y sudo \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} +

COPY disabled_entrypoint.py /opt/gvisor-lab/disabled_entrypoint.py
COPY code-server.example.yaml /opt/gvisor-lab/code-server.example.yaml

# writable HOMEは実行系のサイズ制限付きtmpfs。永続データは本人専用/workspaceだけ。
ENV HOME=/home/coder XDG_DATA_HOME=/workspace/.editor/data XDG_CONFIG_HOME=/workspace/.editor/config
USER 1000:1000
WORKDIR /workspace
# EXPOSE/port公開/資格情報ENVを設定しない。誤って起動してもエディタを実行しない。
ENTRYPOINT ["python3", "/opt/gvisor-lab/disabled_entrypoint.py"]
