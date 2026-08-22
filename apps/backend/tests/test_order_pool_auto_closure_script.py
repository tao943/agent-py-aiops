from pathlib import Path


def test_acceptance_script_uses_real_alert_path_without_secret_output() -> None:
    root = Path(__file__).resolve().parents[3]
    script = (root / "scripts" / "run_order_pool_auto_closure.ps1").read_text(
        encoding="utf-8"
    )

    assert "--auto-closure" in script
    assert "publish_alertmanager" not in script
    assert "user.project.json" not in script
    assert "ground_truth" not in script
    assert "Get-Content $ConfigPath" not in script
    assert "--strategy" in script
    assert '"single"' in script
    assert "live-eval-order-api" in script
    assert "prometheus" in script
