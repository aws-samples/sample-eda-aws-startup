from dataclasses import replace
import pytest

import aws_cdk as cdk
from aws_cdk import assertions

from pcs.config import PcsConfig
from pcs.stack import PcsStack


ENV = cdk.Environment(account="111111111111", region="ap-northeast-2")


def config() -> PcsConfig:
    app = cdk.App(
        context={
            "pcs:vpc_id": "vpc-0123456789abcdef0",
            "pcs:subnet_id": "subnet-0123456789abcdef0",
            "pcs:ami_id": "ami-0123456789abcdef0",
            "pcs:shared_stack_prefix": "Eda",
        }
    )
    return PcsConfig.from_app(app)


def synth(config_override: PcsConfig | None = None):
    app = cdk.App()
    stack = PcsStack(
        app,
        "EdaPcs",
        config=config_override or config(),
        env=ENV,
    )
    return assertions.Template.from_stack(stack)


def test_pcs_resources_are_separate_from_parallelcluster():
    template = synth()

    template.resource_count_is("AWS::PCS::Cluster", 1)
    template.resource_count_is("AWS::PCS::ComputeNodeGroup", 2)
    template.resource_count_is("AWS::PCS::Queue", 1)
    template.resource_count_is("AWS::EC2::VPCEndpoint", 1)
    template.resource_count_is("AWS::CloudFormation::Stack", 0)

    template.has_resource_properties(
        "AWS::PCS::Cluster",
        {
            "Name": "eda-pcs-cluster",
            "Size": "SMALL",
            "Scheduler": {"Type": "SLURM", "Version": "25.11"},
            "SlurmConfiguration": {
                "ScaleDownIdleTimeInSeconds": 900,
            },
        },
    )
    template.has_resource_properties(
        "AWS::PCS::Queue",
        {
            "Name": "default-eda-queue",
            "ComputeNodeGroupConfigurations": [
                {"ComputeNodeGroupId": assertions.Match.any_value()}
            ],
        },
    )


def test_accounting_is_opt_in():
    default_template = synth()
    clusters = default_template.find_resources("AWS::PCS::Cluster")
    default_cluster = next(iter(clusters.values()))["Properties"]
    assert "Accounting" not in default_cluster["SlurmConfiguration"]

    enabled_template = synth(replace(config(), enable_accounting=True))
    enabled_template.has_resource_properties(
        "AWS::PCS::Cluster",
        {
            "SlurmConfiguration": {
                "Accounting": {
                    "Mode": "STANDARD",
                    "DefaultPurgeTimeInDays": 90,
                },
                "ScaleDownIdleTimeInSeconds": 900,
            }
        },
    )


def test_scheduler_and_job_completion_log_deliveries_are_enabled_by_default():
    template = synth()
    cluster_logical_id = next(
        iter(template.find_resources("AWS::PCS::Cluster").keys())
    )
    cluster_id = {"Fn::GetAtt": [cluster_logical_id, "Id"]}

    template.resource_count_is("AWS::Logs::LogGroup", 2)
    template.resource_count_is("AWS::Logs::DeliveryDestination", 2)
    template.resource_count_is("AWS::Logs::DeliverySource", 2)
    template.resource_count_is("AWS::Logs::Delivery", 2)

    log_groups = [
        resource["Properties"]
        for resource in template.find_resources("AWS::Logs::LogGroup").values()
    ]
    assert [resource["LogGroupName"] for resource in log_groups] == [
        {
            "Fn::Join": [
                "",
                [
                    "/aws/pcs/eda-pcs-cluster/",
                    cluster_id,
                    "/scheduler",
                ],
            ]
        },
        {
            "Fn::Join": [
                "",
                [
                    "/aws/pcs/eda-pcs-cluster/",
                    cluster_id,
                    "/job-completion",
                ],
            ]
        },
    ]
    assert all(resource["RetentionInDays"] == 30 for resource in log_groups)
    for resource in template.find_resources("AWS::Logs::LogGroup").values():
        assert resource["DeletionPolicy"] == "Retain"
        assert resource["UpdateReplacePolicy"] == "Retain"

    sources = [
        resource["Properties"]
        for resource in template.find_resources("AWS::Logs::DeliverySource").values()
    ]
    assert {resource["LogType"] for resource in sources} == {
        "PCS_SCHEDULER_LOGS",
        "PCS_JOBCOMP_LOGS",
    }
    assert [resource["Name"] for resource in sources] == [
        {
            "Fn::Join": [
                "",
                [
                    "eda-pcs-cluster-scheduler-",
                    cluster_id,
                    "-source",
                ],
            ]
        },
        {
            "Fn::Join": [
                "",
                [
                    "eda-pcs-cluster-jobs-",
                    cluster_id,
                    "-source",
                ],
            ]
        },
    ]
    destinations = [
        resource["Properties"]
        for resource in template.find_resources(
            "AWS::Logs::DeliveryDestination"
        ).values()
    ]
    assert [resource["Name"] for resource in destinations] == [
        {
            "Fn::Join": [
                "",
                [
                    "eda-pcs-cluster-scheduler-",
                    cluster_id,
                    "-destination",
                ],
            ]
        },
        {
            "Fn::Join": [
                "",
                [
                    "eda-pcs-cluster-jobs-",
                    cluster_id,
                    "-destination",
                ],
            ]
        },
    ]


def test_scheduler_audit_log_delivery_is_explicitly_opt_in():
    template = synth(replace(config(), enable_scheduler_audit_log_delivery=True))
    cluster_logical_id = next(
        iter(template.find_resources("AWS::PCS::Cluster").keys())
    )

    template.resource_count_is("AWS::Logs::LogGroup", 3)
    template.has_resource_properties(
        "AWS::Logs::DeliverySource",
        {
            "Name": {
                "Fn::Join": [
                    "",
                    [
                        "eda-pcs-cluster-audit-",
                        {"Fn::GetAtt": [cluster_logical_id, "Id"]},
                        "-source",
                    ],
                ]
            },
            "LogType": "PCS_SCHEDULER_AUDIT_LOGS",
        },
    )


def test_login_and_compute_fleets_have_distinct_scaling_contracts():
    template = synth()
    groups = template.find_resources("AWS::PCS::ComputeNodeGroup")
    properties = [resource["Properties"] for resource in groups.values()]

    login = next(item for item in properties if item["Name"] == "login-eda-pcs-cluster")
    compute = next(
        item for item in properties if item["Name"] == "compute-eda-pcs-cluster"
    )

    assert login["ScalingConfiguration"] == {
        "MinInstanceCount": 1,
        "MaxInstanceCount": 1,
    }
    assert login["InstanceConfigs"] == [{"InstanceType": "m7i.2xlarge"}]
    assert compute["ScalingConfiguration"] == {
        "MinInstanceCount": 0,
        "MaxInstanceCount": 9,
    }
    assert compute["InstanceConfigs"] == [{"InstanceType": "x8aedz.24xlarge"}]
    assert compute["PurchaseOption"] == "ONDEMAND"


def test_all_pcs_resources_use_the_configured_subnet():
    template = synth()
    subnet_id = "subnet-0123456789abcdef0"

    template.has_resource_properties(
        "AWS::PCS::Cluster",
        {"Networking": {"SubnetIds": [subnet_id]}},
    )
    groups = template.find_resources("AWS::PCS::ComputeNodeGroup")
    assert all(
        resource["Properties"]["SubnetIds"] == [subnet_id]
        for resource in groups.values()
    )
    template.has_resource_properties(
        "AWS::EC2::VPCEndpoint",
        {"SubnetIds": [subnet_id]},
    )


def test_storage_lifecycle_actions_fail_closed():
    template = synth()
    groups = template.find_resources("AWS::PCS::ComputeNodeGroup")

    for resource in groups.values():
        actions = resource["Properties"]["NodeLifecycleActions"]["Stages"][
            "NodeBootstrapped"
        ]
        mount_actions = [
            action for action in actions if action["Name"].startswith("mount-")
        ]
        assert len(mount_actions) == 3
        assert all(action["OnError"] == "TERMINATE" for action in mount_actions)
        assert all(
            action["ExecutionPolicy"] == "EVERY_BOOT" for action in mount_actions
        )
        assert (
            resource["Properties"]["NodeLifecycleActions"]["ScriptCachingPolicy"]
            == "REFRESH_ON_REBOOT"
        )

    properties = [resource["Properties"] for resource in groups.values()]
    login = next(item for item in properties if item["Name"] == "login-eda-pcs-cluster")
    compute = next(
        item for item in properties if item["Name"] == "compute-eda-pcs-cluster"
    )
    login_actions = login["NodeLifecycleActions"]["Stages"]["NodeBootstrapped"]
    compute_actions = compute["NodeLifecycleActions"]["Stages"]["NodeBootstrapped"]
    assert not any(
        action["Name"] == "prepare-local-scratch" for action in login_actions
    )
    assert any(
        action["Name"] == "prepare-local-scratch" for action in compute_actions
    )


def test_shared_foundation_defaults_use_cloudformation_exports():
    serialized = str(synth().to_json())

    assert "eda:base:KeyPairName" in serialized
    assert "eda:base:SgFsxId" in serialized
    assert "eda:storage:OpenZfsDns" in serialized
    assert "eda:license:LicenseSgId" in serialized
    assert "/eda/" not in serialized


def test_explicit_openzfs_overrides_are_used_without_ssm_storage_lookups():
    template = synth(
        replace(
            config(),
            openzfs_dns=("fs-0123456789abcdef0.fsx.ap-northeast-2.amazonaws.com"),
            openzfs_security_group_id="sg-0123456789abcdef0",
        )
    )
    groups = template.find_resources("AWS::PCS::ComputeNodeGroup")
    for resource in groups.values():
        actions = resource["Properties"]["NodeLifecycleActions"]["Stages"][
            "NodeBootstrapped"
        ]
        work_mount = next(
            action for action in actions if action["Name"] == "mount-openzfs-work"
        )
        assert work_mount["Arguments"][1] == (
            "fs-0123456789abcdef0.fsx.ap-northeast-2.amazonaws.com:/fsx/fsxz_work"
        )

    template.has_resource_properties(
        "AWS::EC2::SecurityGroupIngress",
        {"GroupId": "sg-0123456789abcdef0"},
    )


def test_openzfs_and_ontap_mounts_apply_to_login_and_compute():
    template = synth(
        replace(
            config(),
            openzfs_dns=("fs-0123456789abcdef0.fsx.ap-northeast-2.amazonaws.com"),
            openzfs_security_group_id="sg-0123456789abcdef0",
            enable_ontap_mounts=True,
            ontap_svm_nfs_dns=(
                "svm-0123456789abcdef0.fs-1123456789abcdef0.fsx."
                "ap-northeast-2.amazonaws.com"
            ),
            ontap_security_group_id="sg-1123456789abcdef0",
        )
    )
    groups = template.find_resources("AWS::PCS::ComputeNodeGroup")

    for resource in groups.values():
        actions = resource["Properties"]["NodeLifecycleActions"]["Stages"][
            "NodeBootstrapped"
        ]
        mount_actions = [
            action for action in actions if action["Name"].startswith("mount-")
        ]
        assert len(mount_actions) == 6
        ontap_work = next(
            action for action in actions if action["Name"] == "mount-ontap-work"
        )
        assert ontap_work["Arguments"] == [
            "--source",
            (
                "svm-0123456789abcdef0.fs-1123456789abcdef0.fsx."
                "ap-northeast-2.amazonaws.com:/fsxn_work"
            ),
            "--mount-point",
            "/fsxn/work",
            "--options",
            ("nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2"),
        ]

    ontap_rules = [
        resource["Properties"]
        for resource in template.find_resources(
            "AWS::EC2::SecurityGroupIngress"
        ).values()
        if resource["Properties"].get("GroupId") == "sg-1123456789abcdef0"
    ]
    assert len(ontap_rules) == 20  # Login and Compute have separate client SGs.


def test_login_node_can_be_disabled_without_changing_compute_queue():
    template = synth(replace(config(), enable_login_node=False))

    template.resource_count_is("AWS::PCS::ComputeNodeGroup", 1)
    template.resource_count_is("AWS::PCS::Queue", 1)


def test_closed_network_ssh_can_be_enabled_for_login_nodes():
    template = synth(replace(config(), ssh_cidr="0.0.0.0/0"))

    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        {
            "SecurityGroupIngress": [
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "CidrIp": "0.0.0.0/0",
                }
            ]
        },
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("192.168.20.7/32", "192.168.20.7/32"), ("all", "0.0.0.0/0")],
)
def test_ssh_source_changes_only_login_access(value, expected):
    template = synth(replace(config(), ssh_cidr=value))
    groups = template.find_resources("AWS::EC2::SecurityGroup")
    ssh_id = next(key for key in groups if key.startswith("LoginSshSecurityGroup"))
    assert groups[ssh_id]["Properties"]["SecurityGroupIngress"][0]["CidrIp"] == expected
    templates = template.find_resources("AWS::EC2::LaunchTemplate")
    for key, resource in templates.items():
        attached = resource["Properties"]["LaunchTemplateData"]["SecurityGroupIds"]
        assert ({"Ref": ssh_id} in attached) == key.startswith("LoginLaunchTemplate")
    template.has_resource_properties("AWS::EC2::SecurityGroupIngress", {
        "IpProtocol": "-1",
        "SourceSecurityGroupId": assertions.Match.any_value(),
    })


def test_no_login_node_does_not_create_an_unused_ssh_group():
    template = synth(replace(config(), ssh_cidr="all", enable_login_node=False))
    assert not any(
        key.startswith("LoginSshSecurityGroup")
        for key in template.find_resources("AWS::EC2::SecurityGroup")
    )


def test_controller_login_and_compute_have_distinct_security_groups():
    template = synth(replace(config(), ssh_cidr="10.20.30.0/24"))
    data = template.to_json()
    role_ids = {
        role: data["Outputs"][f"{role}SecurityGroupId"]["Value"]["Ref"]
        for role in ("Controller", "Login", "Compute")
    }
    assert len(set(role_ids.values())) == 3
    # Preserve the controller's old logical group ID to avoid cluster replacement.
    assert role_ids["Controller"] == "ClusterSecurityGroup"
    cluster = next(iter(template.find_resources("AWS::PCS::Cluster").values()))
    assert cluster["Properties"]["Networking"]["SecurityGroupIds"] == [
        {"Ref": role_ids["Controller"]}
    ]
    for role in ("Login", "Compute"):
        launch = next(
            resource for name, resource in
            template.find_resources("AWS::EC2::LaunchTemplate").items()
            if name.startswith(f"{role}LaunchTemplate")
        )
        assert launch["Properties"]["LaunchTemplateData"]["SecurityGroupIds"] == [
            {"Ref": role_ids[role]}
        ]
    rules = template.find_resources("AWS::EC2::SecurityGroupIngress")
    for target, source in (("Compute", "Login"), ("Login", "Compute"), ("Controller", "Compute")):
        assert any(
            r["Properties"].get("GroupId") == {"Ref": role_ids[target]}
            and r["Properties"].get("SourceSecurityGroupId") == {"Ref": role_ids[source]}
            and r["Properties"]["IpProtocol"] == "-1"
            for r in rules.values()
        )
    for rule in rules.values():
        props = rule["Properties"]
        if "License" in props.get("Description", "") or "NFS" in props.get("Description", ""):
            assert props["SourceSecurityGroupId"] in [
                {"Ref": role_ids["Login"]}, {"Ref": role_ids["Compute"]}
            ]


@pytest.mark.parametrize("login_enabled", [False, True])
@pytest.mark.parametrize("create_endpoint", [False, True])
def test_full_communication_matrix_and_bootstrap_dependencies(login_enabled, create_endpoint):
    """Check required traffic at both ends, including custom license ports."""
    openzfs_sg = "sg-3123456789abcdef0"
    ontap_sg = "sg-4123456789abcdef0"
    license_sg = "sg-5123456789abcdef0"
    template = synth(replace(
        config(),
        enable_login_node=login_enabled,
        ssh_cidr="10.20.30.0/24",
        create_pcs_vpc_endpoint=create_endpoint,
        openzfs_dns="fs-0123456789abcdef0.fsx.ap-northeast-2.amazonaws.com",
        openzfs_security_group_id=openzfs_sg,
        enable_ontap_mounts=True,
        ontap_svm_nfs_dns="svm-0123456789abcdef0.fs-0123456789abcdef0.fsx.ap-northeast-2.amazonaws.com",
        ontap_security_group_id=ontap_sg,
        license_security_group_id=license_sg,
        license_manager_port=28000,
        license_vendor_port=28020,
    ))
    data = template.to_json()
    resources = data["Resources"]
    roles = ("Controller", "Compute", "Login") if login_enabled else ("Controller", "Compute")
    group_ids = {
        role: data["Outputs"][f"{role}SecurityGroupId"]["Value"]["Ref"]
        for role in roles
    }
    rules = {
        key: rule["Properties"]
        for key, rule in template.find_resources("AWS::EC2::SecurityGroupIngress").items()
    }

    for target in roles:
        # All node types can initiate the required traffic, not just receive it.
        assert any(
            rule["IpProtocol"] == "-1" and rule.get("CidrIp") == "0.0.0.0/0"
            for rule in resources[group_ids[target]]["Properties"]["SecurityGroupEgress"]
        )
        for source in roles:
            assert any(
                rule["GroupId"] == {"Ref": group_ids[target]}
                and rule.get("SourceSecurityGroupId") == {"Ref": group_ids[source]}
                and rule["IpProtocol"] == "-1"
                for rule in rules.values()
            ), (source, target)  # Includes SSH, Slurm, srun ephemeral ports, and responses.

    clients = ("Compute", "Login") if login_enabled else ("Compute",)
    service_ports = (
        (openzfs_sg, ("tcp", "udp"), (111, 2049, 20001, 20002, 20003)),
        (ontap_sg, ("tcp", "udp"), (111, 635, 2049, 4045, 4046)),
        (license_sg, ("tcp",), (28000, 28020)),
    )
    service_rule_ids = set()
    for target, protocols, ports in service_ports:
        for client in clients:
            for protocol in protocols:
                for port in ports:
                    matching = [
                        key for key, rule in rules.items()
                        if rule["GroupId"] == target
                        and rule.get("SourceSecurityGroupId") == {"Ref": group_ids[client]}
                        and rule["IpProtocol"] == protocol
                        and rule["FromPort"] <= port <= rule["ToPort"]
                    ]
                    assert matching, (client, target, protocol, port)
                    service_rule_ids.update(matching)

    endpoints = template.find_resources("AWS::EC2::VPCEndpoint")
    if create_endpoint:
        endpoint_id, endpoint = next(iter(endpoints.items()))
        endpoint_sg_id = endpoint["Properties"]["SecurityGroupIds"][0]["Ref"]
        endpoint_ingress = resources[endpoint_sg_id]["Properties"]["SecurityGroupIngress"]
        for client in clients:
            assert any(
                rule["SourceSecurityGroupId"] == {"Ref": group_ids[client]}
                and rule["IpProtocol"] == "tcp"
                and rule["FromPort"] <= 443 <= rule["ToPort"]
                for rule in endpoint_ingress
            )
    else:
        assert not endpoints

    for cng in template.find_resources("AWS::PCS::ComputeNodeGroup").values():
        dependencies = set(cng.get("DependsOn", []))
        assert service_rule_ids <= dependencies
        if create_endpoint:
            assert endpoint_id in dependencies


def test_standalone_pcs_does_not_reference_foundation_ssm_parameters():
    standalone = replace(
        config(),
        shared_stack_prefix=None,
        key_pair_name="eda-pcs-key",
        license_security_group_id="sg-0123456789abcdef0",
        license_manager_port=27000,
        license_vendor_port=27020,
        openzfs_dns="fs-0123456789abcdef0.fsx.ap-northeast-2.amazonaws.com",
        openzfs_security_group_id="sg-1123456789abcdef0",
    )
    template = synth(standalone)
    serialized = str(template.to_json())

    assert "/eda/" not in serialized
    assert "eda-pcs-key" in serialized


def test_spot_profile_uses_explicit_allocation_strategy():
    template = synth(
        replace(
            config(),
            purchase_option="SPOT",
            spot_allocation_strategy="capacity-optimized",
        )
    )

    template.has_resource_properties(
        "AWS::PCS::ComputeNodeGroup",
        {
            "Name": "compute-eda-pcs-cluster",
            "PurchaseOption": "SPOT",
            "SpotOptions": {"AllocationStrategy": "capacity-optimized"},
        },
    )


def test_cluster_name_drives_node_group_names_and_resource_tags():
    custom = replace(
        config(),
        cluster_name="chip-poc",
    )
    template = synth(custom)

    template.has_resource_properties(
        "AWS::PCS::Cluster",
        {
            "Name": "chip-poc",
            "Tags": assertions.Match.object_like(
                {
                    "Name": "chip-poc",
                    "ClusterName": "chip-poc",
                    "Project": "eda-cluster",
                    "DeploymentModel": "AWS-PCS",
                }
            ),
        },
    )
    for name in ("login-chip-poc", "compute-chip-poc"):
        template.has_resource_properties(
            "AWS::PCS::ComputeNodeGroup",
            {
                "Name": name,
                "Tags": assertions.Match.object_like(
                    {
                        "Name": name,
                        "ClusterName": "chip-poc",
                        "Project": "eda-cluster",
                        "DeploymentModel": "AWS-PCS",
                    }
                ),
            },
        )

    launch_templates = template.find_resources("AWS::EC2::LaunchTemplate")
    template_names = {
        resource["Properties"]["LaunchTemplateName"]
        for resource in launch_templates.values()
    }
    assert template_names == {"login-chip-poc", "compute-chip-poc"}
    for resource in launch_templates.values():
        instance_tags = next(
            spec["Tags"]
            for spec in resource["Properties"]["LaunchTemplateData"][
                "TagSpecifications"
            ]
            if spec["ResourceType"] == "instance"
        )
        tag_map = {tag["Key"]: tag["Value"] for tag in instance_tags}
        assert tag_map["Name"] in template_names
        assert tag_map["ClusterName"] == "chip-poc"
        assert tag_map["Project"] == "eda-cluster"
        assert tag_map["DeploymentModel"] == "AWS-PCS"
        assert (
            not {
                "Customer",
                "ExpiresOn",
                "Environment",
                "NodeRole",
            }
            & tag_map.keys()
        )
