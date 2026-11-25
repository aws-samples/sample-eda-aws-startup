"""Cross-stack wiring checks against concrete synthesized security group rules."""

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
import pytest

from cdk.base_stack import BaseStack
from cdk.license_server_stack import LicenseServerStack
from cdk.storage_stack import StorageStack


@pytest.mark.parametrize("reuse_license", [False, True])
def test_foundation_storage_and_license_wiring(monkeypatch, reuse_license):
    class NetworkLookup:
        def describe_subnets(self, **kwargs):
            return {"Subnets": [{
                "AvailabilityZone": "ap-northeast-2a",
                "CidrBlock": "10.40.8.0/24",
            }]}

        def describe_route_tables(self, **kwargs):
            return {"RouteTables": [{"RouteTableId": "rtb-0123456789abcdef0"}]}

    monkeypatch.setattr("cdk.base_stack.boto3.client", lambda *a, **k: NetworkLookup())
    monkeypatch.setattr(ec2.Vpc, "from_lookup", staticmethod(
        lambda scope, name, **kwargs: ec2.Vpc.from_vpc_attributes(
            scope, name, vpc_id="vpc-0123456789abcdef0",
            vpc_cidr_block="10.40.0.0/16", availability_zones=["ap-northeast-2a"],
        )
    ))
    context = {
        "eda:vpc_id": "vpc-0123456789abcdef0",
        "eda:subnet_id": "subnet-0123456789abcdef0",
        "eda:enable_vpc_endpoints": False,  # Endpoint creation is covered separately.
        "eda:enable_openzfs": True,
        "eda:enable_ontap": True,
        "eda:ssh_allowed_cidr": "10.20.30.0/24",
        "eda:license_ami_id": "ami-0123456789abcdef0",
        "eda:license_manager_port": 28000,
        "eda:license_vendor_port": 28020,
    }
    if reuse_license:
        context.update({
            "eda:license_eni_id": "eni-0123456789abcdef0",
            "eda:license_sg_id": "sg-9123456789abcdef0",
        })
    app = cdk.App(context=context)
    env = cdk.Environment(account="111111111111", region="ap-northeast-2")
    base = BaseStack(app, "EdaBase", env=env)
    StorageStack(
        app, "EdaStorage", vpc=base.vpc, sg_fsx=base.sg_fsx,
        sg_ontap=base.sg_ontap, primary_subnet=base.primary_subnet, env=env,
    )
    LicenseServerStack(
        app, "EdaLicenseServer", vpc=base.vpc, primary_subnet=base.primary_subnet,
        client_security_groups=list(base.node_security_groups.values()), env=env,
    )
    assembly = app.synth()  # Includes real cross-stack export/import dependency checks.
    templates = {stack.stack_name: stack.template for stack in assembly.stacks}
    exports = {
        output["Export"]["Name"]: output["Value"]
        for template in templates.values()
        for output in template.get("Outputs", {}).values()
        if "Export" in output
    }

    def group_id(value):
        if isinstance(value, str):
            return value
        if "Fn::ImportValue" in value:
            return group_id(exports[value["Fn::ImportValue"]])
        if "Fn::GetAtt" in value:
            return value["Fn::GetAtt"][0]
        return value["Ref"]

    base_template = templates["EdaBase"]
    groups = {
        role: group_id(base_template["Outputs"][f"Sg{role}NodesId"]["Value"])
        for role in ("Head", "Login", "Compute")
    }
    resources = {
        name: resource
        for template in templates.values()
        for name, resource in template["Resources"].items()
    }
    fsx_groups = {
        kind: group_id(base_template["Outputs"][output]["Value"])
        for kind, output in (("OPENZFS", "SgFsxId"), ("ONTAP", "SgOntapId"))
    }
    # Verify the file systems actually attach the groups that receive the rules.
    for resource in templates["EdaStorage"]["Resources"].values():
        if resource["Type"] == "AWS::FSx::FileSystem":
            properties = resource["Properties"]
            assert [group_id(value) for value in properties["SecurityGroupIds"]] == [
                fsx_groups[properties["FileSystemType"]]
            ]

    license_template = templates["EdaLicenseServer"]
    license_group = group_id(license_template["Outputs"]["LicenseSgId"]["Value"])
    instances = [
        r["Properties"] for r in license_template["Resources"].values()
        if r["Type"] == "AWS::EC2::Instance"
    ]
    assert len(instances) == 1
    eni_id = group_id(instances[0]["NetworkInterfaces"][0]["NetworkInterfaceId"])
    if reuse_license:
        assert eni_id == "eni-0123456789abcdef0"
        assert license_group == "sg-9123456789abcdef0"
    else:
        assert [group_id(value) for value in resources[eni_id]["Properties"]["GroupSet"]] == [
            license_group
        ]

    ingress = [
        r["Properties"] for r in resources.values()
        if r["Type"] == "AWS::EC2::SecurityGroupIngress"
    ]
    for key, resource in resources.items():
        if resource["Type"] == "AWS::EC2::SecurityGroup":
            for rule in resource["Properties"].get("SecurityGroupIngress", []):
                ingress.append({"GroupId": key, **rule})

    for role, source in groups.items():
        outbound = resources[source]["Properties"]["SecurityGroupEgress"]
        assert any(r["IpProtocol"] == "-1" and r.get("CidrIp") == "0.0.0.0/0" for r in outbound)
        for target, protocols, ports in (
            (fsx_groups["OPENZFS"], ("tcp", "udp"), (111, 2049, 20001, 20002, 20003)),
            (fsx_groups["ONTAP"], ("tcp", "udp"), (111, 635, 2049, 4045, 4046)),
            (license_group, ("tcp",), (28000, 28020)),
        ):
            for protocol in protocols:
                for port in ports:
                    assert any(
                        group_id(rule["GroupId"]) == target
                        and "SourceSecurityGroupId" in rule
                        and group_id(rule["SourceSecurityGroupId"]) == source
                        and rule["IpProtocol"] == protocol
                        and rule["FromPort"] <= port <= rule["ToPort"]
                        for rule in ingress
                    ), (role, target, protocol, port)
