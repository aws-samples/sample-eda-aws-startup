"""Resolve SSH source CIDRs without contacting an internet IP lookup service."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import subprocess
import sys


_PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def normalize_ssh_cidr(value: str | None) -> str:
    """Validate an explicit IPv4 CIDR; 'all' is an intentional /0 choice."""
    value = (value or "").strip()
    if value.lower() == "all":
        return "0.0.0.0/0"
    if not value:
        raise ValueError(
            "SSH CIDR is required here. Use the setup script for automatic "
            "source-IP detection, or specify an IPv4 CIDR or 'all'."
        )
    if "/" not in value:
        raise ValueError("Specify an IPv4 CIDR (IP/32 for one host) or 'all'.")
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise ValueError(f"Invalid SSH IPv4 CIDR: {value}") from exc
    if network.version != 4:
        raise ValueError("SSH access currently supports IPv4 CIDRs only.")
    return str(network)


def subnet_network(subnets: list[dict], vpc_id: str, subnet_id: str):
    """Use the selected subnet only, including when a VPC has several CIDRs."""
    if len(subnets) != 1:
        raise ValueError(f"Expected one subnet for {subnet_id}.")
    subnet = subnets[0]
    if subnet.get("SubnetId") != subnet_id or subnet.get("VpcId") != vpc_id:
        raise ValueError("The SSH detection subnet must belong to the selected VPC.")
    network = ipaddress.ip_network(subnet["CidrBlock"], strict=True)
    if network.version != 4 or network.num_addresses < 4:
        raise ValueError("An IPv4 subnet is required for SSH source-IP detection.")
    return network


def detect_source_cidr(network: ipaddress.IPv4Network) -> str:
    """Ask the OS for the route-selected source; UDP connect sends no payload.

    This does not prove VPN reachability or detect translation by another device.
    The first host address is used only for local route selection.
    """
    target = str(network.network_address + 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((target, 22))
        source = ipaddress.IPv4Address(probe.getsockname()[0])
    if not any(source in private for private in _PRIVATE_NETWORKS):
        raise ValueError(
            f"Route to {target} selected {source}, not an RFC1918 private source. "
            "Specify the SSH source CIDR explicitly."
        )
    return f"{source}/32"


def resolve_ssh_cidr(
    value: str | None, *, vpc_id: str, subnet_id: str, region: str
) -> tuple[str, str]:
    if (value or "").strip():
        return normalize_ssh_cidr(value), "명시적 설정"
    if not all((vpc_id, subnet_id, region)):
        raise ValueError("VPC, subnet and region are required for auto-detection.")
    result = subprocess.run(
        [
            "aws", "ec2", "describe-subnets",
            "--subnet-ids", subnet_id,
            "--region", region,
            "--output", "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    network = subnet_network(
        json.loads(result.stdout)["Subnets"], vpc_id, subnet_id
    )
    return detect_source_cidr(network), f"자동 감지: {network} 경로의 실행 머신 IP"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--value", default="")
    parser.add_argument("--vpc-id", default="")
    parser.add_argument("--subnet-id", default="")
    parser.add_argument("--region", default="")
    parser.add_argument("--setting", default="SSH_ALLOWED_CIDR")
    args = parser.parse_args()
    try:
        cidr, origin = resolve_ssh_cidr(
            args.value,
            vpc_id=args.vpc_id,
            subnet_id=args.subnet_id,
            region=args.region,
        )
    except (
        ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError
    ) as exc:
        print(
            f"{args.setting} 설정 실패: {exc}\n"
            f"{args.setting}에 실제 SSH 원본 IPv4 CIDR 또는 all을 지정하세요. "
            "전체 허용으로 자동 전환하지 않습니다.",
            file=sys.stderr,
        )
        return 1
    print(f"{args.setting}={cidr} ({origin})", file=sys.stderr)
    print(cidr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
