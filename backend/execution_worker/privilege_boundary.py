"""将来の権限分離計画を静的検査する。通信・設定変更・実行許可は一切行わない。"""

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class BoundaryPlan:
    """配備前の設計値。ホストから収集した証拠や、実行可能という証明ではない。

    rootless_dedicatedを推奨値とするが、rootfulの比較候補も表現できる。
    ソケットパス・秘密・任意Docker引数は受け取らず、APIへの接続設定にも使わない。
    """

    strategy: str = "rootless_dedicated"
    dedicated_host: bool = True
    dedicated_daemon: bool = True
    api_root: bool = False
    socket_mount: bool = False
    generic_api_forwarding: bool = False
    broad_sudo: bool = False
    host_code_execution: bool = False
    broker_required: bool = True
    transport: str = "mtls"
    peer_bound_identity: bool = True
    operations: tuple[str, ...] = ("RUN_PYTHON",)
    runtime: str = "runsc"
    runtime_fallback: bool = False
    ownership_ledger: bool = True
    unknown_holds_slots: bool = True
    max_concurrent: int = 4
    execution_enabled: bool = False


@dataclass(frozen=True)
class DesignReview:
    """静的な矛盾の有無だけを示す結果。DESIGN_ONLYでも実接続は禁止のままである。"""

    status: str
    findings: tuple[str, ...]
    # 実機証跡がないことを明示する。入力側の自己申告でPASSへ変更しない。
    not_run: tuple[str, ...] = (
        "LINUX_FCNTL", "ROOTLESS_CHAIN", "RUNSC_ISOLATION", "RESOURCE_ENFORCEMENT",
        "BROKER_AUTHENTICATION", "DAEMON_AUTHORIZATION", "INDEPENDENT_WATCHDOG",
    )

    @property
    def execution_enabled(self):
        """どの静的検査結果でもFalseを返す。既存の実行無効ゲートへ接続しない。"""
        return False


def review_plan(plan):
    """計画の危険な値を固定コードで列挙する純粋関数。実環境の安全性は判定しない。

    不明な型・方式は拒否し、入力値を例外やログへ出さない。環境変数、ファイル、
    socket、Docker contextを参照しないため、呼び出しても配備や実行は始まらない。
    """
    if type(plan) is not BoundaryPlan:
        return DesignReview("REJECTED", ("PLAN_TYPE",))
    # boolはintの派生型なのでisinstanceではなく完全一致で検査する。
    # 文字列の"false"や1を安全な設定と誤認しないための静的入力検査である。
    defaults = BoundaryPlan()
    if any(type(getattr(plan, field.name)) is not type(getattr(defaults, field.name))
           for field in fields(BoundaryPlan)):
        return DesignReview("REJECTED", ("FIELD_TYPE",))
    findings = []
    if plan.strategy not in ("rootless_dedicated", "rootful_broker", "daemon_authz"):
        findings.append("STRATEGY")
    required_true = ("dedicated_host", "dedicated_daemon", "broker_required",
                     "peer_bound_identity", "ownership_ledger", "unknown_holds_slots")
    required_false = ("api_root", "socket_mount", "generic_api_forwarding", "broad_sudo",
                      "host_code_execution", "runtime_fallback", "execution_enabled")
    for name in required_true:
        if getattr(plan, name) is not True:
            findings.append(name.upper())
    for name in required_false:
        if getattr(plan, name) is not False:
            findings.append(name.upper())
    if plan.transport != "mtls":
        findings.append("TRANSPORT")
    if plan.operations != ("RUN_PYTHON",):
        findings.append("OPERATIONS")
    if plan.runtime != "runsc":
        findings.append("RUNTIME")
    if plan.max_concurrent != 4:
        findings.append("MAX_CONCURRENT")
    return DesignReview("REJECTED" if findings else "DESIGN_ONLY", tuple(findings))
