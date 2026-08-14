"""Generate safe e-commerce quant-service incident logs for Tencent Cloud CLS."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from super_ai.aiops.fixtures import (
    generate_java_ecommerce_incident_logs,
    generate_quant_incident_logs,
)
from super_ai.evaluation.live.cls_evidence import put_cls_records
from super_ai.project_config import project_config_section, required_int, required_str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=("java-ecommerce", "quant"),
        default="java-ecommerce",
    )
    parser.add_argument("--count", type=int, default=None)
    args = parser.parse_args()
    target = project_config_section("clsLogUpload")
    credentials = project_config_section("clsMcpServer")
    if args.profile == "java-ecommerce":
        if args.count is not None:
            raise SystemExit("--count is only supported with --profile quant")
        generated_logs = generate_java_ecommerce_incident_logs(
            now=datetime.now(timezone.utc)
        )
        filename = "java-ecommerce-microservices.log"
    else:
        count = args.count or required_int(target, "defaultCount")
        max_count = required_int(target, "maxCount")
        if not 1 <= count <= max_count:
            raise SystemExit(f"--count must be between 1 and {max_count}")
        generated_logs = generate_quant_incident_logs(
            count=count,
            now=datetime.now(timezone.utc),
        )
        filename = "ecommerce-quant-risk-service.log"

    for fields in generated_logs:
        fields["region"] = required_str(target, "region")
        fields.setdefault("host", f"{fields['service']}-01")

    put_cls_records(
        endpoint=required_str(target, "endpoint"),
        topic_id=required_str(target, "topicId"),
        secret_id=required_str(credentials, "secretId"),
        secret_key=required_str(credentials, "secretKey"),
        records=generated_logs,
        filename=filename,
    )
    print(f"Uploaded {len(generated_logs)} {args.profile} CLS logs.")


if __name__ == "__main__":
    main()
