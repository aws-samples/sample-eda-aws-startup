[English](./ssh-security-groups.md) | [한국어](./ssh-security-groups.ko.md)

# SSH 원본 설정과 노드별 보안 그룹

고객이 준비한 VPC·서브넷을 재사용합니다. 배포 관리자와 일반 사용자 계정은
회사 정책을 따르며, 이 설정은 네트워크에서 허용할 SSH 원본을 지정합니다.

## 빠른 설치

```bash
VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh
```

`SSH_ALLOWED_CIDR`을 비워 두면 실행 머신에서 대상 서브넷으로 향하는 경로의
사내 IPv4 주소를 감지해 `/32`로 사용합니다. 적용 범위는 ParallelCluster
Head·Login Node와 공통 기반의 License Server입니다.

```bash
# 회사 접속망 지정 — 주소는 예시
SSH_ALLOWED_CIDR=10.20.30.0/24 VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh

# 관리자가 전체 IPv4 허용을 명시적으로 선택
SSH_ALLOWED_CIDR=all VPC_ID=vpc-xxx SUBNET_ID=subnet-xxx ./setup.sh

# PCS 로그인 경로
PCS_SSH_CIDR=10.20.30.0/24 ./pcs/pcs-setup.sh deploy
```

| 입력 | 동작 |
|---|---|
| IPv4 CIDR | 지정한 범위 사용. 호스트 비트는 네트워크 주소로 정규화 |
| 빈 값 | 대상 서브넷 경로의 실행 머신 사내 IP를 `/32`로 사용 |
| `all` 또는 `0.0.0.0/0` | 전체 IPv4 허용. CIDR 선택에 대한 별도 확인 질문 없음 |
| 잘못된 CIDR, IPv6, 자동 감지 실패 | 설정 방법을 안내하고 종료. 전체 허용으로 전환하지 않음 |

CIDR과 설정 출처는 실행 로그에 표시됩니다. 두 SSH 환경변수는 설정 파일보다
우선하며, 빈 환경변수도 자동 감지 요청입니다. 반복 배포에서 고정할 CIDR은
환경별 설정 파일에 보관하세요. 자동 감지는 새로 실행할 때마다 수행합니다.

## 자동 감지의 범위

공용 IP 조회 서비스를 사용하지 않습니다. AWS CLI로 선택한 서브넷의 CIDR을
읽고, 운영체제가 그 서브넷으로 패킷을 보낼 때 선택하는 원본 주소를 확인합니다.
UDP 소켓의 경로 선택만 사용하며 검사 패킷을 전송하지 않습니다.

자동 감지는 RFC1918 주소만 허용합니다. VPN 연결 성공, 방화벽 통과 또는
중간 장비의 SNAT 여부까지 검증하는 기능은 아닙니다. 주소 변환이 있거나
설치 머신과 실제 접속 머신이 다르면 실제 SSH 원본 CIDR을 직접 지정하세요.
회사에서 공인 주소를 내부적으로 사용하는 경우에도 직접 지정할 수 있습니다.
`/32`는 설치 머신 한 대만 허용하며 회사 전체 접속망을 자동 추정하지 않습니다.

PCS의 `destroy` 또는 Login Node 비활성화 시에는 자동 감지를 수행하지 않습니다.
`SKIP_CDK=1`은 ParallelCluster 설정만 생성하므로 License Server의 SG를
변경하지 않습니다. 일반 PCS 명령도 공통 기반의 License Server SSH 규칙을
변경하지 않습니다.

## 노드별 보안 그룹

### ParallelCluster와 공통 기반

| 대상 | 프로젝트가 연결하는 그룹 | 외부 SSH |
|---|---|---|
| Head Node | `SgHeadNodesId` | `SSH_ALLOWED_CIDR` |
| Login Node | `SgLoginNodesId` | `SSH_ALLOWED_CIDR` |
| Compute Node | `SgComputeNodesId` | 새 외부 SSH 규칙을 추가하지 않음 |
| License Server | 전용 `SgLicenseServer` | `SSH_ALLOWED_CIDR` |

Head·Login·Compute에는 ParallelCluster가 자동 생성하는 노드별 기본 그룹도
유지됩니다. 각 노드에는 자신의 타입에 해당하는 추가 그룹만 연결합니다.
FSx와 License Server는 Head·Login·Compute 그룹을 각각 원본으로 허용합니다.
DCV 허용 범위는 기존 `DCV_ALLOWED_IPS`로 별도 관리합니다.

기존 `SgClusterNodes` 리소스는 물리 그룹 교체를 피하기 위해 Compute용으로
재사용합니다. `SgClusterNodesId` 출력은 이전 환경과의 호환용 별칭이며
`SgComputeNodesId`와 같은 그룹입니다. 새 템플릿은 역할별 출력만 사용합니다.
ONTAP의 기존 허용 포트는 유지하며, 인접 포트를 범위 규칙으로 묶어 중복을 줄입니다.

### PCS

| 대상 | 그룹 출력 | 외부 SSH |
|---|---|---|
| 관리형 컨트롤러 연결 | `ControllerSecurityGroupId` | 추가하지 않음 |
| Login Node | `LoginSecurityGroupId` | `PCS_SSH_CIDR` |
| Compute Node | `ComputeSecurityGroupId` | 추가하지 않음 |
| License Server | 기존 공통 기반의 전용 그룹 | 공통 기반 설정에 따름 |

컨트롤러·Login·Compute는 서로 다른 그룹을 사용합니다. `srun`의 동적 포트 등
기존 클러스터 내부 통신은 그룹 간 참조 규칙으로 유지합니다. 각 역할의 규칙을
독립적으로 관리하기 위한 분리이며 사용자·팀별 격리를 추가하는 변경은 아닙니다.

PCS의 FSx·라이선스·API 엔드포인트 접근은 Login·Compute 그룹을 각각 허용합니다.
새로 생성하는 PCS API 엔드포인트와 통신 규칙은 CNG 생성보다 먼저 준비되도록
CloudFormation 의존성을 설정합니다.
기존 `ClusterSecurityGroup`은 컨트롤러 전용으로 유지하여 컨트롤러의 SG ID를
바꾸지 않습니다. `LoginSshSecurityGroup`도 기존 논리 ID를 유지하지만 이제
로그인 노드의 내부 통신과 외부 SSH를 함께 담당합니다.

## 기존 배포 환경에 적용

이 문서는 소스 변경과 로컬 검증 범위를 설명합니다. 계정에 자동 적용되지는 않습니다.

1. 기존 실행 작업을 완료하고 신규 제출을 멈춘 뒤 변경 차이를 확인합니다.
   특히 PCS는 노드 SG 변경과 스토리지 원본 규칙 갱신 중 기존 노드의 통신에
   영향을 줄 수 있으므로 작업을 비운 유지보수 시점에 전환합니다.
2. 공통 기반을 배포해 새 Head·Login 그룹과 License Server의 규칙을 준비합니다.
   이전 `outputs.json`에는 새 그룹 출력이 없으므로 이 상태로 `SKIP_CDK=1`을
   사용하면 설정 생성이 중단됩니다.
3. ParallelCluster는 새로 생성된 설정으로 별도의 클러스터 업데이트를 수행합니다.
   기존 노드가 과거 공통 그룹을 계속 사용하는 상태에서는 분리가 완료된 것이
   아닙니다. PCS도 `diff` 후 `deploy`로 CNG의 Launch Template 변경을 반영합니다.
4. 실제 ENI에 연결된 그룹, 신규 SSH 연결, FSx 마운트, 라이선스 접근과 대표
   작업 제출을 확인한 뒤 사용자 작업을 재개합니다.

다른 도구나 관리자가 추가한 SG 규칙은 이 프로젝트가 임의로 삭제하지 않습니다.
기존 규칙과 별도로 광범위한 허용이 남아 있으면 실제 접근 범위는 그 규칙까지
포함하여 평가해야 합니다.

## CDK 직접 실행

자동 감지는 배포 스크립트가 수행합니다. 공통 기반 CDK를 직접 실행할 때는
`-c eda:ssh_allowed_cidr=10.20.30.0/24` 또는 `-c eda:ssh_allowed_cidr=all`을
명시해야 합니다. 직접 실행 시 CIDR이 비어 있으면 합성을 중단합니다.

PCS CDK 직접 실행은 `-c pcs:ssh_cidr=10.20.30.0/24`를 사용합니다.
직접 합성에서 이 값이 없으면 외부 SSH 규칙을 생성하지 않습니다.
`pcs-setup.sh`의 빈 설정 자동 감지와 구분하세요.
