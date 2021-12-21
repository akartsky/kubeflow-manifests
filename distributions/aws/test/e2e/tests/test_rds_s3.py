import os
import json

import pytest
import boto3


from e2e.utils.constants import DEFAULT_USER_NAMESPACE
from e2e.utils.utils import (
    wait_for,
    rand_name,
    WaitForCircuitBreakerError,
    unmarshal_yaml,
)
from e2e.utils.config import metadata, configure_env_file

from e2e.conftest import region, get_accesskey, get_secretkey, keep_successfully_created_resource

from e2e.fixtures.cluster import cluster
from e2e.fixtures.kustomize import kustomize
from e2e.fixtures.clients import (
    kfp_client,
    port_forward,
    session_cookie,
    host,
    login,
    password,
    client_namespace,
    cfn_client,
    ec2_client,
    s3_client,
    create_k8s_admission_registration_api_client,
    create_mysql_client,
)

from e2e.utils import mysql

from e2e.utils.cloudformation_resources import (
    create_cloudformation_fixture,
    get_stack_outputs,
)
from e2e.utils.custom_resources import (
    create_katib_experiment_from_yaml,
    get_katib_experiment,
    delete_katib_experiment,
)

from kfp_server_api.exceptions import ApiException as KFPApiException
from kubernetes.client.exceptions import ApiException as K8sApiException

RDS_S3_KUSTOMIZE_MANIFEST_PATH = "../../examples/rds-s3/"
RDS_S3_CLOUDFORMATION_TEMPLATE_PATH = "./resources/cloudformation-templates/rds-s3.yaml"
CUSTOM_RESOURCE_TEMPLATES_FOLDER = "./resources/custom-resource-templates"
DISABLE_PIPELINE_CACHING_PATCH_FILE = (
    "./resources/custom-resource-templates/patch-disable-pipeline-caching.yaml"
)


@pytest.fixture(scope="class")
def kustomize_path():
    return RDS_S3_KUSTOMIZE_MANIFEST_PATH


@pytest.fixture(scope="class")
def cfn_stack(metadata, cluster, cfn_client, ec2_client, request):
    stack_name = rand_name("test-e2e-rds-stack-")

    resp = ec2_client.describe_vpcs(
        Filters=[
            {
                "Name": "tag:alpha.eksctl.io/cluster-name",
                "Values": [cluster],
            },
        ],
    )
    vpc_id = resp["Vpcs"][0]["VpcId"]

    resp = ec2_client.describe_subnets(
        Filters=[
            {
                "Name": "tag:alpha.eksctl.io/cluster-name",
                "Values": [cluster],
            },
            {
                "Name": "tag:aws:cloudformation:logical-id",
                "Values": ["SubnetPublic*"],
            },
        ],
    )
    public_subnets = [s["SubnetId"] for s in resp["Subnets"]]

    resp = ec2_client.describe_instances(
        Filters=[
            {
                "Name": "tag:eks:cluster-name",
                "Values": [cluster],
            }
        ],
    )
    instances = [i for r in resp["Reservations"] for i in r["Instances"]]
    security_groups = [i["SecurityGroups"][0]["GroupId"] for i in instances]

    return create_cloudformation_fixture(
        metadata=metadata,
        request=request,
        cfn_client=cfn_client,
        template_path=RDS_S3_CLOUDFORMATION_TEMPLATE_PATH,
        stack_name=stack_name,
        metadata_key="test_rds_s3_cfn_stack",
        create_timeout=10 * 60,
        delete_timeout=10 * 60,
        params={
            "VpcId": vpc_id,
            "Subnets": ",".join(public_subnets),
            "SecurityGroupId": security_groups[0],
            "DBUsername": rand_name("admin"),
            "DBPassword": rand_name("Kubefl0w"),
        },
    )


KFP_MANIFEST_FOLDER = "../../../../apps/pipeline/upstream/env/aws"
KATIB_MANIFEST_FOLDER = (
    "../../../../apps/katib/upstream/installs/katib-external-db-with-kubeflow"
)

KFP_MINIO_ARTIFACT_SECRET_PATCH_ENV_FILE = (
    KFP_MANIFEST_FOLDER + "/minio-artifact-secret-patch.env"
)
KFP_PARAMS_ENV_FILE = KFP_MANIFEST_FOLDER + "/params.env"
KFP_SECRET_ENV_FILE = KFP_MANIFEST_FOLDER + "/secret.env"

KATIB_SECRETS_ENV_FILE = KATIB_MANIFEST_FOLDER + "/secrets.env"


@pytest.fixture(scope="class")
def configure_manifests(cfn_client, cfn_stack, region, request):
    stack_outputs = get_stack_outputs(cfn_client, cfn_stack["stack_name"])

    configure_env_file(
        env_file_path=KFP_MINIO_ARTIFACT_SECRET_PATCH_ENV_FILE,
        env_dict={
            "accesskey": get_accesskey(request),
            "secretkey": get_secretkey(request),
        },
    )

    configure_env_file(
        env_file_path=KFP_PARAMS_ENV_FILE,
        env_dict={
            "dbHost": stack_outputs["RDSEndpoint"],
            "bucketName": stack_outputs["S3BucketName"],
            "minioServiceHost": "s3.amazonaws.com",
            "minioServiceRegion": region,
        },
    )

    configure_env_file(
        env_file_path=KFP_SECRET_ENV_FILE,
        env_dict={
            "username": cfn_stack["params"]["DBUsername"],
            "password": cfn_stack["params"]["DBPassword"],
        },
    )

    configure_env_file(
        env_file_path=KATIB_SECRETS_ENV_FILE,
        env_dict={
            "KATIB_MYSQL_DB_DATABASE": "kubeflow",
            "KATIB_MYSQL_DB_HOST": stack_outputs["RDSEndpoint"],
            "KATIB_MYSQL_DB_PORT": "3306",
            "DB_USER": cfn_stack["params"]["DBUsername"],
            "DB_PASSWORD": cfn_stack["params"]["DBPassword"],
        },
    )

@pytest.fixture(scope="class")
def delete_s3_bucket_contents(cfn_client, cfn_stack, request):
    """
    Hack to clean out s3 objects since CFN does not allow deleting non-empty buckets
    nor provides a way to empty a bucket.
    """

    yield

    if keep_successfully_created_resource(request):
        return

    stack_outputs = get_stack_outputs(cfn_client, cfn_stack["stack_name"])

    s3 = boto3.resource('s3')
    bucket = s3.Bucket(stack_outputs['S3BucketName'])
    bucket.objects.delete()

PIPELINE_NAME = "[Demo] XGBoost - Iterative model training"
KATIB_EXPERIMENT_FILE = "katib-experiment-random.yaml"


def wait_for_run_succeeded(kfp_client, run, job_name, pipeline_id):
    def callback():
        resp = kfp_client.get_run(run.id)

        assert resp.run.name == job_name
        assert resp.run.pipeline_spec.pipeline_id == pipeline_id

        if "Failed" == resp.run.status:
            print(resp.run)
            raise WaitForCircuitBreakerError("Pipeline run Failed")

        assert resp.run.status == "Succeeded"

        return resp

    return wait_for(callback)


def wait_for_katib_experiment_succeeded(cluster, region, namespace, name):
    def callback():
        resp = get_katib_experiment(cluster, region, namespace, name)

        assert resp["kind"] == "Experiment"
        assert resp["metadata"]["name"] == name
        assert resp["metadata"]["namespace"] == namespace

        assert resp["status"]["completionTime"] != None
        condition_types = {
            condition["type"] for condition in resp["status"]["conditions"]
        }

        if "Failed" in condition_types:
            print(resp)
            raise WaitForCircuitBreakerError("Katib experiment Failed")

        assert "Succeeded" in condition_types

    wait_for(callback)


class TestRDSS3:
    @pytest.fixture(scope="class")
    def setup(self, metadata, port_forward, cluster, region, delete_s3_bucket_contents):

        # Disable caching in KFP
        # By default KFP will cache previous pipeline runs and subsequent runs will skip cached steps
        # This prevents artifacts from being uploaded to s3 for subsequent runs
        patch_body = unmarshal_yaml(DISABLE_PIPELINE_CACHING_PATCH_FILE)
        k8s_admission_registration_api_client = (
            create_k8s_admission_registration_api_client(cluster, region)
        )
        k8s_admission_registration_api_client.patch_mutating_webhook_configuration(
            "cache-webhook-kubeflow", patch_body
        )

        metadata_file = metadata.to_file()
        print(metadata.params)  # These needed to be logged
        print("Created metadata file for TestRDSS3", metadata_file)

    def test_kfp_experiment(self, setup, kfp_client, cfn_stack, cfn_client):
        stack_outputs = get_stack_outputs(cfn_client, cfn_stack["stack_name"])

        name = rand_name("experiment-")
        description = rand_name("description-")
        experiment = kfp_client.create_experiment(
            name, description=description, namespace=DEFAULT_USER_NAMESPACE
        )

        assert name == experiment.name
        assert description == experiment.description
        assert DEFAULT_USER_NAMESPACE == experiment.resource_references[0].key.id

        mysql_client = create_mysql_client(
            user=cfn_stack["params"]["DBUsername"],
            password=cfn_stack["params"]["DBPassword"],
            host=stack_outputs["RDSEndpoint"],
            database="mlpipeline",
        )

        resp = mysql.query(
            mysql_client, f"select * from experiments where Name='{name}'"
        )
        assert len(resp) == 1
        assert resp[0]["Name"] == experiment.name
        assert resp[0]["Description"] == experiment.description
        assert resp[0]["Namespace"] == experiment.resource_references[0].key.id

        resp = kfp_client.get_experiment(
            experiment_id=experiment.id, namespace=DEFAULT_USER_NAMESPACE
        )

        assert name == resp.name
        assert description == resp.description
        assert DEFAULT_USER_NAMESPACE == resp.resource_references[0].key.id

        kfp_client.delete_experiment(experiment.id)

        resp = mysql.query(
            mysql_client, f"select * from experiments where Name='{name}'"
        )
        assert len(resp) == 0

        try:
            kfp_client.get_experiment(
                experiment_id=experiment.id, namespace=DEFAULT_USER_NAMESPACE
            )
            raise AssertionError("Expected KFPApiException Not Found")
        except KFPApiException as e:
            assert "Not Found" == e.reason

        mysql_client.close()

    def test_run_pipeline(self, setup, kfp_client, cfn_stack, cfn_client, s3_client):
        stack_outputs = get_stack_outputs(cfn_client, cfn_stack["stack_name"])

        experiment_name = rand_name("experiment-")
        experiment_description = rand_name("description-")
        experiment = kfp_client.create_experiment(
            experiment_name,
            description=experiment_description,
            namespace=DEFAULT_USER_NAMESPACE,
        )

        pipeline_id = kfp_client.get_pipeline_id(PIPELINE_NAME)
        job_name = rand_name("run-")

        run = kfp_client.run_pipeline(
            experiment.id, job_name=job_name, pipeline_id=pipeline_id
        )

        assert run.name == job_name
        assert run.pipeline_spec.pipeline_id == pipeline_id
        assert run.status == None

        resp = wait_for_run_succeeded(kfp_client, run, job_name, pipeline_id)

        workflow_manifest_json = resp.pipeline_runtime.workflow_manifest
        workflow_manifest = json.loads(workflow_manifest_json)

        s3_artifact_keys = []
        for _, node in workflow_manifest["status"]["nodes"].items():
            if "outputs" not in node:
                continue
            if "artifacts" not in node["outputs"]:
                continue

            for artifact in node["outputs"]["artifacts"]:
                if "s3" not in artifact:
                    continue
                s3_artifact_keys.append(artifact["s3"]["key"])

        bucket_objects = s3_client.list_objects_v2(Bucket=stack_outputs["S3BucketName"])
        content_keys = {content["Key"] for content in bucket_objects["Contents"]}

        assert f"pipelines/{pipeline_id}" in content_keys
        for key in s3_artifact_keys:
            assert key in content_keys

        mysql_client = create_mysql_client(
            user=cfn_stack["params"]["DBUsername"],
            password=cfn_stack["params"]["DBPassword"],
            host=stack_outputs["RDSEndpoint"],
            database="mlpipeline",
        )

        resp = mysql.query(
            mysql_client, f"select * from run_details where UUID='{run.id}'"
        )

        assert len(resp) == 1
        assert resp[0]["DisplayName"] == job_name
        assert resp[0]["PipelineId"] == pipeline_id
        assert resp[0]["Conditions"] == "Succeeded"

        kfp_client.delete_experiment(experiment.id)

    def test_katib_experiment(self, setup, cluster, region, cfn_stack, cfn_client):
        stack_outputs = get_stack_outputs(cfn_client, cfn_stack["stack_name"])

        filepath = os.path.abspath(
            os.path.join(CUSTOM_RESOURCE_TEMPLATES_FOLDER, KATIB_EXPERIMENT_FILE)
        )

        name = rand_name("katib-random-")
        namespace = DEFAULT_USER_NAMESPACE
        replacements = {"NAME": name, "NAMESPACE": namespace}

        resp = create_katib_experiment_from_yaml(
            cluster, region, filepath, namespace, replacements
        )

        assert resp["kind"] == "Experiment"
        assert resp["metadata"]["name"] == name
        assert resp["metadata"]["namespace"] == namespace

        wait_for_katib_experiment_succeeded(cluster, region, namespace, name)

        mysql_client = create_mysql_client(
            user=cfn_stack["params"]["DBUsername"],
            password=cfn_stack["params"]["DBPassword"],
            host=stack_outputs["RDSEndpoint"],
            database="kubeflow",
        )

        resp = mysql.query(
            mysql_client,
            f"select count(*) as count from observation_logs where trial_name like '{name}%'",
        )

        assert resp[0]["count"] > 0

        resp = delete_katib_experiment(cluster, region, namespace, name)

        assert resp["kind"] == "Experiment"
        assert resp["metadata"]["name"] == name
        assert resp["metadata"]["namespace"] == namespace

        try:
            get_katib_experiment(cluster, region, namespace, name)
            raise AssertionError("Expected K8sApiException Not Found")
        except K8sApiException as e:
            assert "Not Found" == e.reason
