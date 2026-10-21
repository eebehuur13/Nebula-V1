"""The single `Nebula` stack: VPC, Aurora, S3, secrets, ECS (api + worker), ALB, CloudFront.

Request path:  Claude -> CloudFront (HTTPS, *.cloudfront.net) -> ALB :80 (only from the
CloudFront origin-facing prefix list, and only with the X-Nebula-Origin header) -> api
tasks :8000. Tasks sit in public subnets with public IPs because there is no NAT gateway.
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import (
    Acknowledgment,
    CfnOutput,
    Duration,
    IgnoreMode,
    RemovalPolicy,
    Stack,
    Validations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_rds as rds,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Mirrors ../.dockerignore (CDK also reads that file). Keeps the asset hash stable, so
# editing infra/ or docs doesn't rebuild the image, and keeps cdk.out out of the context.
DOCKER_EXCLUDES = [
    ".venv",
    "**/.venv",
    ".data",
    ".env",
    ".env.*",
    ".git",
    ".gitignore",
    "**/.DS_Store",
    "infra",
    "**/__pycache__",
    "**/*.py[oc]",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".coverage",
    ".coverage.*",
    "htmlcov",
    "tests",
    "build",
    "dist",
    "wheels",
    "*.egg-info",
    "*.md",
    "!README.md",
]

# Latest 17.x Aurora PostgreSQL in us-east-2 on 2026-10-16
# (`aws rds describe-db-engine-versions --engine aurora-postgresql`); not yet a CDK enum.
AURORA_PG_FULL_VERSION = "17.11"
AURORA_PG_MAJOR_VERSION = "17"

CLOUDFRONT_ORIGIN_FACING_PREFIX_LIST = "com.amazonaws.global.cloudfront.origin-facing"
ORIGIN_HEADER = "X-Nebula-Origin"

APP_PORT = 8000
DB_NAME = "nebula"
TASK_CPU = 1024  # 1 vCPU
TASK_MEMORY_MIB = 6144  # local e5-large (~2.2 GB) + int8 reranker (~0.6 GB) + parsing headroom

# Fields of the nebula/app secret that start as placeholders and are set by hand.
APP_SECRET_PLACEHOLDER_FIELDS = [
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "NOTION_TOKEN",
    "GRANOLA_API_KEY",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
]
APP_SECRET_GENERATED_FIELD = "JWT_SIGNING_KEY"

EMBED_PROVIDERS = {"bedrock-cohere", "bedrock-titan", "local"}
RERANK_PROVIDERS = {"bedrock-cohere", "local", "none"}


class NebulaStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        cfg = self._read_config()

        # ------------------------------------------------------------------ network
        vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name="nebula",
            ip_addresses=ec2.IpAddresses.cidr("10.40.0.0/16"),
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
            gateway_endpoints={
                # Free; image layers (ECR stores them in S3) and app S3 traffic skip the internet.
                "S3": ec2.GatewayVpcEndpointOptions(service=ec2.GatewayVpcEndpointAwsService.S3),
            },
        )

        alb_sg = ec2.SecurityGroup(
            self, "AlbSg", vpc=vpc, description="Nebula ALB: HTTP from CloudFront only", allow_all_outbound=False
        )
        tasks_sg = ec2.SecurityGroup(
            self,
            "TasksSg",
            vpc=vpc,
            description="Nebula ECS tasks (api + worker)",
            allow_all_outbound=True,  # Google, Notion, Granola, Bedrock, ECR, Secrets Manager
        )
        db_sg = ec2.SecurityGroup(
            self, "DbSg", vpc=vpc, description="Nebula Aurora: 5432 from ECS tasks only", allow_all_outbound=False
        )
        db_sg.add_ingress_rule(tasks_sg, ec2.Port.tcp(5432), "Postgres from Nebula ECS tasks")

        cloudfront_prefix_list = ec2.PrefixList.from_lookup(
            self, "CloudFrontOriginFacing", prefix_list_name=CLOUDFRONT_ORIGIN_FACING_PREFIX_LIST
        )
        alb_sg.add_ingress_rule(
            ec2.Peer.prefix_list(cloudfront_prefix_list.prefix_list_id),
            ec2.Port.tcp(80),
            "HTTP from CloudFront origin-facing servers",
        )

        # ------------------------------------------------------------------ storage
        bucket = s3.Bucket(
            self,
            "Files",
            bucket_name=f"nebula-files-{self.account}-{self.region}",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="cleanup",
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                    # The app keeps only current copies of synced files; this is just a short undo window.
                    noncurrent_version_expiration=Duration.days(7),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------ secrets
        app_secret = secretsmanager.Secret(
            self,
            "AppSecret",
            secret_name="nebula/app",
            description="Nebula app credentials (set the 'unset' fields by hand; JWT_SIGNING_KEY is generated)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                # Don't edit this template after the first deploy: CloudFormation would
                # regenerate the whole secret and wipe the values set by hand.
                secret_string_template=json.dumps({k: "unset" for k in APP_SECRET_PLACEHOLDER_FIELDS}),
                generate_string_key=APP_SECRET_GENERATED_FIELD,
                password_length=64,
                exclude_punctuation=True,
                include_space=False,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Shared secret between CloudFront and the ALB listener rule, so only our
        # distribution (not any CloudFront distribution) can reach the ALB. Generated once
        # at creation and stable across deploys; resolved by CloudFormation dynamic refs.
        origin_secret = secretsmanager.Secret(
            self,
            "OriginVerifySecret",
            secret_name="nebula/origin-verify",
            description=f"Value of the {ORIGIN_HEADER} header CloudFront adds and the ALB requires",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=48, exclude_punctuation=True, include_space=False
            ),
        )
        origin_token = origin_secret.secret_value.unsafe_unwrap()

        # ------------------------------------------------------------------ database
        db = rds.DatabaseCluster(
            self,
            "Database",
            cluster_identifier="nebula",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.of(AURORA_PG_FULL_VERSION, AURORA_PG_MAJOR_VERSION)
            ),
            credentials=rds.Credentials.from_generated_secret("nebula", secret_name="nebula/db"),
            default_database_name=DB_NAME,
            writer=rds.ClusterInstance.serverless_v2("Writer", publicly_accessible=False),
            serverless_v2_min_capacity=0.5,
            serverless_v2_max_capacity=4,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[db_sg],
            port=5432,
            storage_encrypted=True,
            # Windows in UTC: backup 02:30-03:30 IST, maintenance Mon 03:30-04:30 IST.
            backup=rds.BackupProps(retention=Duration.days(7), preferred_window="21:00-22:00"),
            preferred_maintenance_window="Sun:22:00-Sun:23:00",
            removal_policy=RemovalPolicy.SNAPSHOT,
            deletion_protection=False,
        )
        # aws-cdk-lib's bundled CloudFormation schema (2.273) lists engine versions only up
        # to 17.10. 17.11 is available in us-east-2 (checked with describe-db-engine-versions)
        # and CloudFormation passes the value straight to RDS, so the stale check is
        # acknowledged here. To be conservative, set AURORA_PG_FULL_VERSION = "17.10" instead.
        Validations.of(db).acknowledge(
            Acknowledgment(
                id="CloudFormation-Validate::E9006",
                reason=f"Aurora PostgreSQL {AURORA_PG_FULL_VERSION} is newer than the bundled CFN schema",
            )
        )
        db_secret = db.secret
        assert db_secret is not None

        # ------------------------------------------------------------------ load balancer
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=vpc,
            internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=alb_sg,
            idle_timeout=Duration.seconds(120),
        )
        listener = alb.add_listener(
            "Http",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,  # ingress comes only from the CloudFront prefix list rule above
            default_action=elbv2.ListenerAction.fixed_response(
                403, content_type="text/plain", message_body="Forbidden"
            ),
        )

        # ------------------------------------------------------------------ CloudFront
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment="Nebula",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.HttpOrigin(
                    alb.load_balancer_dns_name,
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                    http_port=80,
                    read_timeout=Duration.seconds(60),
                    keepalive_timeout=Duration.seconds(60),
                    custom_headers={ORIGIN_HEADER: origin_token},
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                # Preserve the public hostname and Authorization header through the ALB.
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                compress=True,
            ),
            price_class=cloudfront.PriceClass.PRICE_CLASS_200,  # includes India edge locations
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
        )
        # Dependency direction: tasks -> distribution -> ALB. The ALB never references the
        # services (the target group does, via the listener rule), so there is no cycle.
        public_url = "https://" + distribution.distribution_domain_name

        # ------------------------------------------------------------------ IAM
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Nebula api + worker containers",
        )
        bucket.grant_read_write(task_role)
        app_secret.grant_read(task_role)
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="Bedrock",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Rerank",
                    "bedrock:GetInferenceProfile",
                ],
                # Cross-region inference profiles route to models in other US regions.
                resources=["*"],
            )
        )
        execution_role = iam.Role(
            self,
            "ExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Nebula ECS task execution (image pull, logs, secret injection)",
        )
        # ECR pull, log writes and secret reads are granted by CDK as the image,
        # log driver and ecs.Secret entries are attached below.

        # ------------------------------------------------------------------ ECS
        cluster = ecs.Cluster(self, "Cluster", cluster_name="nebula", vpc=vpc)

        image_asset = ecr_assets.DockerImageAsset(
            self,
            "Image",
            directory=str(PROJECT_ROOT),
            platform=ecr_assets.Platform.LINUX_ARM64,
            build_args={"BAKE_MODELS": "1"},
            exclude=DOCKER_EXCLUDES,
            ignore_mode=IgnoreMode.DOCKER,
        )
        image = ecs.ContainerImage.from_docker_image_asset(image_asset)

        environment = {
            "NEBULA_ENV": "prod",
            "NEBULA_PUBLIC_URL": public_url,
            "NEBULA_AWS_REGION": self.region,
            "NEBULA_AUTH_MODE": "google",
            # Plain env, not from the secret: Credentials.from_generated_secret's secret only
            # holds username/password (+ host/port/engine once attached), never `dbname`, and
            # ECS refuses to start a task whose secret JSON key is missing.
            "NEBULA_DB_NAME": DB_NAME,
            "NEBULA_S3_BUCKET": bucket.bucket_name,
            "NEBULA_ALLOWED_EMAIL_DOMAINS": cfg["allowed_email_domains"],
            "NEBULA_PUBLIC_SIGNUP": cfg["public_signup"],
            "NEBULA_EMBED_PROVIDER": cfg["embed_provider"],
            "NEBULA_RERANK_PROVIDER": cfg["rerank_provider"],
        }
        # Empty values are left out rather than passed as "" (the app defaults to "").
        if cfg["allowed_emails"]:
            environment["NEBULA_ALLOWED_EMAILS"] = cfg["allowed_emails"]
        if cfg["admin_emails"]:
            environment["NEBULA_ADMIN_EMAILS"] = cfg["admin_emails"]

        secrets = {
            "NEBULA_DB_HOST": ecs.Secret.from_secrets_manager(db_secret, "host"),
            "NEBULA_DB_PORT": ecs.Secret.from_secrets_manager(db_secret, "port"),
            "NEBULA_DB_USER": ecs.Secret.from_secrets_manager(db_secret, "username"),
            "NEBULA_DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
        }
        for field in [*APP_SECRET_PLACEHOLDER_FIELDS, APP_SECRET_GENERATED_FIELD]:
            secrets[f"NEBULA_{field}"] = ecs.Secret.from_secrets_manager(app_secret, field)

        runtime_platform = ecs.RuntimePlatform(
            cpu_architecture=ecs.CpuArchitecture.ARM64,
            operating_system_family=ecs.OperatingSystemFamily.LINUX,
        )

        api_logs = logs.LogGroup(
            self,
            "ApiLogs",
            log_group_name="/nebula/api",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        worker_logs = logs.LogGroup(
            self,
            "WorkerLogs",
            log_group_name="/nebula/worker",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- api
        api_task = ecs.FargateTaskDefinition(
            self,
            "ApiTask",
            family="nebula-api",
            cpu=TASK_CPU,
            memory_limit_mib=TASK_MEMORY_MIB,
            runtime_platform=runtime_platform,
            task_role=task_role,
            execution_role=execution_role,
        )
        api_task.add_container(
            "api",
            container_name="api",
            image=image,
            command=["nebula", "serve"],
            port_mappings=[ecs.PortMapping(container_port=APP_PORT, protocol=ecs.Protocol.TCP)],
            environment=environment,
            secrets=secrets,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="api", log_group=api_logs),
            health_check=ecs.HealthCheck(
                # The slim image has no curl; urlopen raises on 4xx/5xx and refused connections.
                command=[
                    "CMD",
                    "python",
                    "-c",
                    "import sys, urllib.request as u; "
                    f"sys.exit(u.urlopen('http://127.0.0.1:{APP_PORT}/healthz', timeout=4).status != 200)",
                ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(120),
            ),
        )
        api_service = ecs.FargateService(
            self,
            "ApiService",
            service_name="nebula-api",
            cluster=cluster,
            task_definition=api_task,
            # desired_count is left unset on purpose: CloudFormation starts 1 task on create
            # and later deploys don't reset whatever autoscaling has chosen.
            assign_public_ip=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[tasks_sg],
            enable_execute_command=True,
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            min_healthy_percent=100,
            max_healthy_percent=200,
            health_check_grace_period=Duration.seconds(180),
        )
        scaling = api_service.auto_scale_task_count(min_capacity=1, max_capacity=3)
        scaling.scale_on_cpu_utilization(
            "Cpu",
            target_utilization_percent=60,
            scale_out_cooldown=Duration.seconds(60),
            scale_in_cooldown=Duration.seconds(300),
        )

        listener.add_targets(
            "Api",
            priority=10,
            conditions=[elbv2.ListenerCondition.http_header(ORIGIN_HEADER, [origin_token])],
            port=APP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[api_service.load_balancer_target(container_name="api", container_port=APP_PORT)],
            health_check=elbv2.HealthCheck(
                path="/healthz",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
            deregistration_delay=Duration.seconds(30),
        )

        # --- worker
        worker_task = ecs.FargateTaskDefinition(
            self,
            "WorkerTask",
            family="nebula-worker",
            cpu=TASK_CPU,
            memory_limit_mib=TASK_MEMORY_MIB,
            runtime_platform=runtime_platform,
            task_role=task_role,
            execution_role=execution_role,
        )
        worker_task.add_container(
            "worker",
            container_name="worker",
            image=image,
            command=["nebula", "worker"],
            environment=environment,
            secrets=secrets,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="worker", log_group=worker_logs),
        )
        worker_service = ecs.FargateService(
            self,
            "WorkerService",
            service_name="nebula-worker",
            cluster=cluster,
            task_definition=worker_task,
            desired_count=1,
            assign_public_ip=True,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[tasks_sg],
            enable_execute_command=True,
            circuit_breaker=ecs.DeploymentCircuitBreaker(enable=True, rollback=True),
            min_healthy_percent=100,
            max_healthy_percent=200,
        )

        # ------------------------------------------------------------------ outputs
        outputs = {
            "PublicUrl": public_url,
            "McpUrl": public_url + "/mcp",
            "GoogleRedirectUri": public_url + "/auth/callback",
            "BucketName": bucket.bucket_name,
            "DbEndpoint": db.cluster_endpoint.hostname,
            "DbSecretArn": db_secret.secret_arn,
            "AppSecretArn": app_secret.secret_arn,
            "ClusterName": cluster.cluster_name,
            "ApiServiceName": api_service.service_name,
            "WorkerServiceName": worker_service.service_name,
            "ApiLogGroup": api_logs.log_group_name,
            "WorkerLogGroup": worker_logs.log_group_name,
        }
        for name, value in outputs.items():
            CfnOutput(self, name, value=value)

    def _read_config(self) -> dict[str, str]:
        """Plain-text app settings from cdk.json context (override with `-c nebula:key=value`)."""

        def ctx(key: str, default: str = "") -> str:
            value = self.node.try_get_context(f"nebula:{key}")
            return default if value is None else str(value).strip()

        cfg = {
            "public_signup": ctx("publicSignup", "false").lower(),
            "allowed_email_domains": ctx("allowedEmailDomains", "example.org"),
            "allowed_emails": ctx("allowedEmails"),
            "admin_emails": ctx("adminEmails"),
            "embed_provider": ctx("embedProvider", "local"),
            "rerank_provider": ctx("rerankProvider", "local"),
        }
        if cfg["public_signup"] not in {"true", "false"}:
            raise ValueError("nebula:publicSignup must be true or false")
        if cfg["embed_provider"] not in EMBED_PROVIDERS:
            raise ValueError(f"nebula:embedProvider must be one of {sorted(EMBED_PROVIDERS)}")
        if cfg["rerank_provider"] not in RERANK_PROVIDERS:
            raise ValueError(f"nebula:rerankProvider must be one of {sorted(RERANK_PROVIDERS)}")
        return cfg
