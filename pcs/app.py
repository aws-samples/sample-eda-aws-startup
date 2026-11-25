#!/usr/bin/env python3
"""Independent CDK entry point for the AWS PCS deployment model."""

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aws_cdk as cdk  # noqa: E402

from pcs.config import PcsConfig  # noqa: E402
from pcs.stack import PcsStack  # noqa: E402


app = cdk.App()
config = PcsConfig.from_app(app)

region = (
    os.getenv("AWS_REGION")
    or os.getenv("AWS_DEFAULT_REGION")
    or os.getenv("CDK_DEFAULT_REGION")
)
if not region:
    raise ValueError("Set AWS_REGION or AWS_DEFAULT_REGION before synthesizing")

PcsStack(
    app,
    config.stack_name,
    config=config,
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=region,
    ),
)

app.synth()
