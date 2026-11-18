"""gVisor検証レシピと安全設定をファイル読取りだけで検査する。build/run/probeはしない。"""

import hashlib
import json

from app.editor_contracts import EditorBinding
from gvisor_lab.policy import Profile, plan_for, validate_plan
from tools.editor_foundation_check import ROOT, SafeParser, bounded_read, check_runtime_source, exact, load_json, shape

# レシピに危険なCOPY/USER/ENTRYPOINT等が追加されたら静的合格にしない。LF差だけ正規化。
# 更新時は実行/保存/秘密情報の扱いを再レビューし、単に期待hashを追従させない。
RECIPE_HASHES = {
    "Dockerfile": "a83a6b88e32e1343181387a5dc7b2d51585a525b085bddadbc58c8c79ae10625",
    ".dockerignore": "0bf1c0c8bc03caf91dfb2fb53fe0a6d53330c559b0a99afdbdb5e34ddd3c7426",
    "disabled_entrypoint.py": "5fab281f6e30712d68751b43b2ecd8719380d8173f6b571197360c445e01d0cf",
    "code-server.example.yaml": "72709c8ea4fe1122f85f07691945b1d1c236015c7af02ad135f29665448f34bd",
}


def evaluate(root=ROOT):
    """保護ソースとレシピを検査する。manifest申告が増えても配備許可へ変換しない。"""
    report = {"status": "DENIED", "launch_enabled": False, "deployment_allowed": False,
              "connectable": False, "real_runsc": "NOT_RUN", "image_build": "NOT_RUN", "browser": "NOT_RUN",
              "runtime_source_unchanged": False}
    try:
        check_runtime_source(root)
        report["runtime_source_unchanged"] = True
        directory = root / "artifacts/gvisor-editor"
        for name, expected in RECIPE_HASHES.items():
            content = bounded_read(directory / name).replace(b"\r\n", b"\n")
            if hashlib.sha256(content).hexdigest() != expected:
                return report
        manifest = load_json(directory / "manifest.example.json")
        shape(manifest, ("kind", "base_image", "built_image_digest", "architectures", "image_build", "runtime_test", "browser_test", "launch_enabled"))
        for key, expected in (("kind", "GVISOR_LAB_IMAGE_RECIPE"), ("architectures", ["amd64", "arm64"]),
                              ("image_build", "NOT_RUN"), ("runtime_test", "NOT_RUN"), ("browser_test", "NOT_RUN"), ("launch_enabled", False)):
            exact(manifest[key], expected, "invalid_recipe")
        # 現段階は承認済みdigestを未取得。架空digestをbuild済み証跡として登録しない。
        if manifest["base_image"] is not None or manifest["built_image_digest"] is not None:
            return report
        binding = EditorBinding(team_id=1, project_id=1, user_id=1)
        profile = Profile(2, 2048)
        fake_digest = "sha256:" + "1" * 64
        validate_plan(plan_for(binding, profile, fake_digest), binding, profile, fake_digest)
        report["status"] = "STATIC_CONSISTENT_ONLY"
    except Exception:
        pass  # raw例外/入力値をログへ反射せず、既定の拒否結果を返す。
    return report


def main(argv=None):
    """CLIは静的検査だけ。--build/--run/接続先指定等の実操作引数は存在しない。"""
    parser = SafeParser(description="gVisorレシピの静的検査だけ。実機はNOT_RUN。")
    try:
        parser.parse_args(argv)
        result = evaluate()
    except Exception:
        result = {"status": "DENIED", "launch_enabled": False, "deployment_allowed": False, "connectable": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "STATIC_CONSISTENT_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
