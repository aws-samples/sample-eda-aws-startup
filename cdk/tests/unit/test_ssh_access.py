import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from eda_common import ssh_access


ROOT = Path(__file__).resolve().parents[3]
SUBNET = {
    "SubnetId": "subnet-0123456789abcdef0",
    "VpcId": "vpc-0123456789abcdef0",
    "CidrBlock": "10.40.8.0/24",
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.20.30.0/24", "10.20.30.0/24"),
        ("10.20.30.15/32", "10.20.30.15/32"),
        ("10.20.30.15/24", "10.20.30.0/24"),
        ("all", "0.0.0.0/0"),
        (" ALL ", "0.0.0.0/0"),
        ("0.0.0.0/0", "0.0.0.0/0"),
    ],
)
def test_explicit_values_need_no_network_or_aws_lookup(monkeypatch, value, expected):
    def forbidden(*args, **kwargs):
        pytest.fail("Explicit SSH CIDRs must not trigger address detection")

    monkeypatch.setattr(ssh_access.subprocess, "run", forbidden)
    monkeypatch.setattr(ssh_access.socket, "socket", forbidden)
    assert ssh_access.resolve_ssh_cidr(
        value, vpc_id="", subnet_id="", region=""
    ) == (expected, "명시적 설정")


@pytest.mark.parametrize("value", ["", None, "not-cidr", "0.0.0.0", "::/0", "10.0.0.0/33"])
def test_invalid_direct_cdk_values_do_not_become_allow_all(value):
    with pytest.raises(ValueError):
        ssh_access.normalize_ssh_cidr(value)


def route_socket(monkeypatch, address):
    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def connect(self, destination):
            assert destination == ("10.40.8.1", 22)

        def getsockname(self):
            return (address, 45000)

    def create(family, kind):
        assert family == socket.AF_INET
        assert kind == socket.SOCK_DGRAM
        return Probe()

    monkeypatch.setattr(ssh_access.socket, "socket", create)


def test_empty_value_uses_selected_subnet_route_not_an_internet_service(monkeypatch):
    def describe(args, **kwargs):
        assert args == [
            "aws", "ec2", "describe-subnets",
            "--subnet-ids", SUBNET["SubnetId"],
            "--region", "ap-northeast-2", "--output", "json",
        ]
        return subprocess.CompletedProcess(args, 0, json.dumps({"Subnets": [SUBNET]}))

    monkeypatch.setattr(ssh_access.subprocess, "run", describe)
    route_socket(monkeypatch, "192.168.20.7")
    cidr, origin = ssh_access.resolve_ssh_cidr(
        " ", vpc_id=SUBNET["VpcId"], subnet_id=SUBNET["SubnetId"],
        region="ap-northeast-2",
    )
    assert cidr == "192.168.20.7/32"
    assert "10.40.8.0/24" in origin


@pytest.mark.parametrize(
    "address", ["0.0.0.0", "127.0.0.1", "169.254.10.1", "8.8.8.8", "224.0.0.1"]
)
def test_auto_detection_rejects_non_private_sources(monkeypatch, address):
    route_socket(monkeypatch, address)
    with pytest.raises(ValueError, match="private source"):
        ssh_access.detect_source_cidr(ipaddress.ip_network(SUBNET["CidrBlock"]))


def test_subnet_must_match_requested_vpc():
    with pytest.raises(ValueError, match="selected VPC"):
        ssh_access.subnet_network([SUBNET], "vpc-other", SUBNET["SubnetId"])


def test_detection_failure_exits_without_a_cidr(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError("No route to target")

    monkeypatch.setattr(ssh_access, "resolve_ssh_cidr", fail)
    monkeypatch.setattr(sys, "argv", ["ssh_access.py", "--setting", "PCS_SSH_CIDR"])
    assert ssh_access.main() == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "PCS_SSH_CIDR" in out.err
    assert "전체 허용으로 자동 전환하지 않습니다" in out.err


def test_cli_all_outputs_only_canonical_cidr():
    result = subprocess.run(
        [sys.executable, str(ROOT / "eda_common/ssh_access.py"), "--value", "all"],
        check=True, capture_output=True, text=True,
    )
    assert result.stdout == "0.0.0.0/0\n"
    assert "명시적 설정" in result.stderr


@pytest.mark.parametrize("login", ["0", "1"])
@pytest.mark.parametrize("cidr", ["192.168.20.7/32", "0.0.0.0/0"])
def test_actual_template_renderer_limits_entry_nodes_only(tmp_path, login, cidr):
    """Run the real sed/block-rendering section with synthetic foundation outputs."""
    setup = (ROOT / "setup.sh").read_text()
    start = setup.index('sed \\\n  -e "s|\\${REGION}')
    end = setup.index("# Check for unresolved placeholders", start)
    destination = tmp_path / "cluster.yaml"
    env = {
        **os.environ,
        "REGION": "ap-northeast-2",
        "SSH_ALLOWED_CIDR": cidr,
        "DCV_ALLOWED_IPS": "10.20.40.0/24",
        "LOGIN_NODE_INSTANCE_TYPE": "g6.4xlarge",
        "SUBNET_ID": SUBNET["SubnetId"],
        "SG_HEAD": "sg-1123456789abcdef0",
        "SG_LOGIN": "sg-2123456789abcdef0",
        "SG_COMPUTE": "sg-3123456789abcdef0",
        "KEY_PAIR_NAME": "admin-key",
        "TEMPLATE": str(ROOT / "pcluster/pcluster-config-template.yaml"),
        "CONFIG": str(destination),
        "ENABLE_LOGIN_NODE": login,
        "ENABLE_DCV": "1",
        "ENABLE_OPENZFS": "1",
        "ENABLE_ONTAP": "0",
        "ENABLE_SSM": "0",
    }
    for name in ("VOL_TOOLS", "VOL_WORK", "VOL_SCRATCH"):
        env[name] = "fsvol-0123456789abcdef0"
    for name in ("ONTAP_TOOLS", "ONTAP_WORK", "ONTAP_SCRATCH"):
        env[name] = ""
    subprocess.run(
        ["bash", "-c", "set -eu\ninfo() { :; }\nwarn() { :; }\n" + setup[start:end]],
        env=env, check=True, capture_output=True, text=True,
    )
    rendered = destination.read_text()
    assert "${" not in "\n".join(
        line for line in rendered.splitlines() if not line.lstrip().startswith("#")
    )
    assert rendered.count(f"AllowedIps: {cidr}") == 1 + int(login)
    assert ("AllowedIps: 10.20.40.0/24" in rendered) == (login == "1")
    assert "AllowedIps" not in rendered.split("Scheduling:", 1)[1]
    assert "sg-1123456789abcdef0" in rendered.split("HeadNode:", 1)[1]
    assert "sg-3123456789abcdef0" in rendered.split("Scheduling:", 1)[1]
    if login == "1":
        assert "sg-2123456789abcdef0" in rendered.split("LoginNodes:", 1)[1].split("SharedStorage:")[0]


@pytest.mark.parametrize("value", ["all", "10.20.30.0/24", ""])
def test_pcs_ssh_environment_override_survives_config_loading(tmp_path, value):
    """Exercise the shell's actual config loader without executing AWS commands."""
    script = (ROOT / "pcs/pcs-setup.sh").read_text()
    start = script.index('ACTION="${1:-deploy}"')
    end = script.index("for command in aws node npm python3;")
    custom = tmp_path / "pcs.env"
    custom.write_text('PCS_SSH_CIDR="10.99.0.0/16"\n')
    result = subprocess.run(
        ["bash", "-c", 'set -eu\n' + script[start:end] + '\nprintf "%s" "$PCS_SSH_CIDR"'],
        env={
            **os.environ,
            "PROJECT_DIR": str(ROOT),
            "PCS_DIR": str(ROOT / "pcs"),
            "PCS_CONFIG": str(custom),
            "PCS_SSH_CIDR": value,
        },
        capture_output=True, text=True, check=True,
    )
    assert result.stdout == value
