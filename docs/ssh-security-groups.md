[English](./ssh-security-groups.md) | [한국어](./ssh-security-groups.ko.md)

# SSH source configuration and per-node security groups

This project reuses the VPC and subnets prepared by the customer. Deployment
administrators and regular users follow the company's account policies. The
settings below specify which network sources can access SSH.

## Quick start

```bash
VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh
```

When `SSH_ALLOWED_CIDR` is empty, the setup script detects the deployment
machine's private IPv4 source address for the route to the target subnet and
uses it as `/32`. This setting applies to the ParallelCluster Head and Login
Nodes and the License Server in the shared foundation.

```bash
# Specify the corporate source network — example address
SSH_ALLOWED_CIDR=10.20.30.0/24 VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh

# Explicitly allow all IPv4 sources
SSH_ALLOWED_CIDR=all VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh

# PCS login access
PCS_SSH_CIDR=10.20.30.0/24 ./pcs/pcs-setup.sh deploy
```

| Input | Behavior |
|---|---|
| IPv4 CIDR | Use the specified range. Host bits are normalized to the network address. |
| Empty | Use the deployment machine's private source IP for the target subnet as `/32`. |
| `all` or `0.0.0.0/0` | Allow all IPv4 sources. No additional confirmation is required for this CIDR choice. |
| Invalid CIDR, IPv6, or detection failure | Print configuration instructions and exit. Never fall back to allowing all sources. |

The selected CIDR and how it was obtained appear in the setup output. Both SSH
environment variables take precedence over configuration files; an explicitly
empty environment variable requests automatic detection. Store a fixed CIDR in
an environment-specific configuration file for repeat deployments. Automatic
detection runs again on each invocation that requires it.

## Automatic detection scope

The script does not use a public IP lookup service. It reads the selected subnet's
CIDR through the AWS CLI, then checks which source address the operating system
selects for traffic to that subnet. It uses UDP socket route selection without
sending probe packets.

Automatic detection accepts only RFC1918 addresses. It does not verify VPN
connectivity, firewall access, or SNAT performed by intermediate devices. Set the
actual SSH source CIDR explicitly when address translation is involved or when
users connect from a different machine than the one running setup. Explicit
configuration also supports companies that use public address ranges internally.
A `/32` allows only the deployment machine's address; the script does not infer
the entire corporate source network.

PCS skips automatic detection for `destroy` and when the Login Node is disabled.
With `SKIP_CDK=1`, the root setup script only generates the ParallelCluster
configuration and does not change the License Server security group. Regular
PCS commands also leave the shared foundation's License Server SSH rule unchanged.

## Per-node security groups

### ParallelCluster and the shared foundation

| Target | Group attached by this project | SSH from client networks |
|---|---|---|
| Head Node | `SgHeadNodesId` | `SSH_ALLOWED_CIDR` |
| Login Node | `SgLoginNodesId` | `SSH_ALLOWED_CIDR` |
| Compute Node | `SgComputeNodesId` | No new external SSH rule |
| License Server | Dedicated `SgLicenseServer` | `SSH_ALLOWED_CIDR` |

Head, Login, and Compute Nodes also retain the default security groups created
by ParallelCluster. Each node receives only the additional group for its own
node type. FSx and the License Server allow the Head, Login, and Compute groups
as separate sources. DCV access remains controlled independently by
`DCV_ALLOWED_IPS`.

The existing `SgClusterNodes` resource is reused for Compute Nodes to avoid
replacing the physical group. The `SgClusterNodesId` output remains as a
compatibility alias for the same group as `SgComputeNodesId`. New templates use
only the role-specific outputs. Existing ONTAP port access is preserved;
adjacent ports are combined into range rules to reduce the rule count.

### PCS

| Target | Group output | SSH from client networks |
|---|---|---|
| Managed controller connection | `ControllerSecurityGroupId` | None added |
| Login Node | `LoginSecurityGroupId` | `PCS_SSH_CIDR` |
| Compute Node | `ComputeSecurityGroupId` | None added |
| License Server | Existing dedicated foundation group | Foundation configuration |

The controller, Login Nodes, and Compute Nodes use distinct groups. Rules that
reference these groups preserve existing inter-node communication, including
dynamic ports used by `srun`. This separation allows each role's rules to be
managed independently; it does not introduce user or team isolation.

PCS access to FSx, the License Server, and the PCS API endpoint allows the Login
and Compute groups as separate sources. CloudFormation dependencies ensure that
the newly created PCS API endpoint and communication rules are ready before
compute node groups (CNGs) are created.

The existing `ClusterSecurityGroup` is retained for the controller only, keeping
the controller's security group ID unchanged. `LoginSshSecurityGroup` also retains
its existing logical ID, but now handles both internal login-node communication
and SSH from client networks.

## Applying the changes to an existing deployment

This guide describes source changes and local validation. The changes are not
automatically applied to your AWS account.

1. Stop new job submissions, let running jobs finish, and review the deployment
   diff. In particular, PCS node security group changes and storage source-rule
   updates can affect existing nodes. Perform the transition during a maintenance
   window with no running jobs.
2. Deploy the shared foundation to create the new Head and Login groups and
   prepare the License Server rules. An older `outputs.json` lacks the new group
   outputs, so configuration generation with `SKIP_CDK=1` stops until these outputs
   are available.
3. For ParallelCluster, perform a separate cluster update using the newly
   generated configuration. Separation is not complete while existing nodes still
   use the old shared group. For PCS, run `diff` followed by `deploy` to apply the
   CNG launch template changes.
4. Verify the groups attached to the actual ENIs, new SSH connections, FSx mounts,
   license access, and a representative job submission before resuming user jobs.

This project does not remove unrelated security group rules added by other tools
or administrators. If broader allow rules remain, include them when evaluating
the effective access permissions.

## Running CDK directly

Automatic detection is performed by the setup scripts. When running the
foundation CDK application directly, specify
`-c eda:ssh_allowed_cidr=10.20.30.0/24` or `-c eda:ssh_allowed_cidr=all`.
Direct synthesis stops if this CIDR is empty.

For the PCS CDK application, use `-c pcs:ssh_cidr=10.20.30.0/24`.
If this value is absent during direct synthesis, no external SSH rule is created.
This differs from the automatic detection performed by `pcs-setup.sh` for an
empty setting.
