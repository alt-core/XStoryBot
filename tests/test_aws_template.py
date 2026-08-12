from pathlib import Path
import re
import unittest

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / 'template.aws.yaml'


class CloudFormationLoader(yaml.SafeLoader):
    """CloudFormation組み込みtagを構造検証用の値として読み込む。"""


def _construct_unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                'mappingを構築中', node.start_mark,
                f'重複したkeyです: {key}', key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


def _construct_cloudformation_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {tag_suffix: value}


CloudFormationLoader.add_multi_constructor(
    '!', _construct_cloudformation_tag)
CloudFormationLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class AwsTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE_PATH.read_text(encoding='utf-8')
        cls.template = yaml.load(cls.source, Loader=CloudFormationLoader)
        cls.resources = cls.template['Resources']

    def test_最小構成だけを定義する(self):
        resource_types = {
            resource['Type'] for resource in self.resources.values()
        }
        required = {
            'AWS::ApiGatewayV2::Api',
            'AWS::ApiGatewayV2::Integration',
            'AWS::ApiGatewayV2::Route',
            'AWS::ApiGatewayV2::Stage',
            'AWS::Serverless::Function',
            'AWS::DynamoDB::Table',
            'AWS::S3::Bucket',
            'AWS::CloudFront::Distribution',
            'AWS::CloudFront::OriginAccessControl',
            'AWS::SQS::Queue',
            'AWS::Scheduler::ScheduleGroup',
            'AWS::ECS::Cluster',
            'AWS::ECS::TaskDefinition',
            'AWS::CloudWatch::Alarm',
        }
        self.assertTrue(required.issubset(resource_types))
        self.assertNotIn('AWS::EC2::NatGateway', resource_types)
        self.assertNotIn('AWS::WAFv2::WebACL', resource_types)
        self.assertNotIn('AWS::ECR::Repository', resource_types)
        self.assertNotIn('AWS::SSM::Parameter', resource_types)

    def test_YAMLの重複keyを拒否する(self):
        with self.assertRaises(yaml.constructor.ConstructorError):
            yaml.load(
                'RedrivePolicy:\n'
                '  deadLetterTargetArn: first\n'
                '  deadLetterTargetArn: second\n',
                Loader=CloudFormationLoader,
            )

    def test_DynamoDBの耐久性とcache_TTLを分ける(self):
        for name in ('StateTable', 'GroupTaskTable'):
            table = self.resources[name]
            self.assertEqual('Retain', table['DeletionPolicy'])
            self.assertEqual('Retain', table['UpdateReplacePolicy'])
            self.assertEqual(
                'PAY_PER_REQUEST', table['Properties']['BillingMode'])
            self.assertTrue(table['Properties'][
                'PointInTimeRecoverySpecification'][
                    'PointInTimeRecoveryEnabled'])
            self.assertTrue(
                table['Properties']['DeletionProtectionEnabled'])

        cache = self.resources['CacheTable']
        self.assertNotIn('DeletionPolicy', cache)
        self.assertNotIn('PointInTimeRecoverySpecification',
                         cache['Properties'])
        self.assertEqual(
            {'AttributeName': 'expire_at', 'Enabled': True},
            cache['Properties']['TimeToLiveSpecification'],
        )

    def test_S3は公開せず固定日数削除を行わない(self):
        for name in ('PrivateBucket', 'MediaBucket'):
            bucket = self.resources[name]
            self.assertEqual('Retain', bucket['DeletionPolicy'])
            properties = bucket['Properties']
            self.assertEqual(
                {
                    'BlockPublicAcls': True,
                    'BlockPublicPolicy': True,
                    'IgnorePublicAcls': True,
                    'RestrictPublicBuckets': True,
                },
                properties['PublicAccessBlockConfiguration'],
            )
            self.assertEqual(
                'BucketOwnerEnforced',
                properties['OwnershipControls']['Rules'][0][
                    'ObjectOwnership'],
            )
            self.assertEqual(
                'AES256',
                properties['BucketEncryption'][
                    'ServerSideEncryptionConfiguration'][0][
                        'ServerSideEncryptionByDefault']['SSEAlgorithm'],
            )
            rules = properties['LifecycleConfiguration']['Rules']
            self.assertEqual(1, len(rules))
            self.assertEqual(
                7,
                rules[0]['AbortIncompleteMultipartUpload'][
                    'DaysAfterInitiation'],
            )
            self.assertNotIn('ExpirationInDays', rules[0])

        distribution = self.resources['MediaDistribution']['Properties'][
            'DistributionConfig']
        origin = distribution['Origins'][0]
        self.assertEqual(
            {'GetAtt': 'MediaOriginAccessControl.Id'},
            origin['OriginAccessControlId'],
        )

    def test_SQS_workerの制御値を固定する(self):
        expected = {
            'ActionWorkerFunction': (60, 1024, 10, 'ActionMessages'),
            'GroupWorkerFunction': (900, 2048, 2, 'GroupMessages'),
        }
        for name, (timeout, memory, concurrency, event_name) in expected.items():
            properties = self.resources[name]['Properties']
            self.assertEqual('Image', properties['PackageType'])
            self.assertEqual(timeout, properties['Timeout'])
            self.assertEqual(memory, properties['MemorySize'])
            event = properties['Events'][event_name]['Properties']
            self.assertEqual(1, event['BatchSize'])
            self.assertEqual(
                ['ReportBatchItemFailures'], event['FunctionResponseTypes'])
            self.assertEqual(
                concurrency, event['ScalingConfig']['MaximumConcurrency'])
            environment = properties['Environment']['Variables']
            self.assertEqual('/healthz',
                             environment['AWS_LWA_READINESS_CHECK_PATH'])
            self.assertEqual('/events',
                             environment['AWS_LWA_PASS_THROUGH_PATH'])
            self.assertEqual('500-599',
                             environment['AWS_LWA_ERROR_STATUS_CODES'])

        self.assertEqual(
            'Image', self.resources['ApiFunction']['Properties'][
                'PackageType'])
        self.assertNotIn(
            'PackageType', self.template['Globals']['Function'])

        self.assertEqual(
            360, self.resources['ActionQueue']['Properties'][
                'VisibilityTimeout'])
        self.assertEqual(
            5400, self.resources['GroupQueue']['Properties'][
                'VisibilityTimeout'])

        for queue_name in ('ActionQueue', 'GroupQueue'):
            queue = self.resources[queue_name]
            self.assertNotIn('DeletionPolicy', queue)
            self.assertNotIn('UpdateReplacePolicy', queue)
            self.assertNotIn('FifoQueue', queue['Properties'])
            self.assertEqual(
                {'GetAtt': 'DeadLetterQueue.Arn'},
                queue['Properties']['RedrivePolicy'][
                    'deadLetterTargetArn'],
            )

        dead_letter_queue = self.resources['DeadLetterQueue']
        self.assertEqual('Retain', dead_letter_queue['DeletionPolicy'])
        self.assertEqual('Retain', dead_letter_queue['UpdateReplacePolicy'])

    def test_PrivateBucketの不存在を404として判定できる権限を持つ(self):
        for role_name in ('ApiRole', 'WorkerRole', 'BuildTaskRole'):
            statements = self.resources[role_name]['Properties'][
                'Policies'][0]['PolicyDocument']['Statement']
            statement = next(
                item for item in statements
                if item['Action'] == 's3:ListBucket')
            self.assertEqual(
                {'GetAtt': 'PrivateBucket.Arn'}, statement['Resource'])
            self.assertNotIn('Condition', statement)

    def test_Fargateはpublic_subnet二つで秘密値名だけを受け取る(self):
        subnet_count = sum(
            resource['Type'] == 'AWS::EC2::Subnet'
            for resource in self.resources.values())
        self.assertEqual(2, subnet_count)
        resource_types = {
            resource['Type'] for resource in self.resources.values()
        }
        self.assertNotIn('AWS::EC2::NatGateway', resource_types)

        task = self.resources['BuildTaskDefinition']['Properties']
        self.assertEqual('1024', task['Cpu'])
        self.assertEqual('2048', task['Memory'])
        self.assertEqual(
            {
                'OperatingSystemFamily': 'LINUX',
                'CpuArchitecture': 'X86_64',
            },
            task['RuntimePlatform'],
        )
        container = task['ContainerDefinitions'][0]
        self.assertNotIn('Secrets', container)
        names = {
            item['Name'] for item in container['Environment']
        }
        self.assertIn('XSBOT_AWS_RUNTIME_SECRETS_PARAMETER', names)
        self.assertNotIn('XSBOT_RUNTIME_SECRETS_JSON', names)
        self.assertEqual(
            [],
            self.resources['BuildSecurityGroup']['Properties'][
                'SecurityGroupIngress'],
        )

    def test_秘密値をtemplateへ受け取らない(self):
        parameters = self.template['Parameters']
        self.assertEqual(
            {
                'ImageUri',
                'EnvironmentName',
                'SheetId',
                'GoogleSheetsCredentialParameterName',
                'AdminAuthParameterName',
                'RuntimeSecretsParameterName',
            },
            set(parameters),
        )
        self.assertNotIn('Default', parameters['SheetId'])
        self.assertEqual(1, parameters['SheetId']['MinLength'])
        self.assertNotIn('{{resolve:ssm-secure:', self.source)
        self.assertNotRegex(
            self.source,
            re.compile(r'^\s*(LINE_ACCESS_TOKEN|LINE_CHANNEL_SECRET|'
                       r'TWILIO_AUTH_TOKEN|PUSHER_APP_SECRET):', re.MULTILINE),
        )
        for name in (
                'GoogleSheetsCredentialParameterName',
                'AdminAuthParameterName',
                'RuntimeSecretsParameterName'):
            self.assertEqual(
                r'^/[a-zA-Z0-9_.\-/]+$',
                parameters[name]['AllowedPattern'],
            )
            self.assertIn(
                f'parameter${{{name}}}', self.source)

    def test_API_access_logへ入力値を含めない(self):
        access_log = self.resources['HttpApiStage']['Properties'][
            'AccessLogSettings']['Format']
        self.assertIn('$context.requestId', access_log)
        self.assertIn('$context.status', access_log)
        self.assertIn('$context.routeKey', access_log)
        self.assertIn('$context.responseLatency', access_log)
        self.assertIn('$context.integrationLatency', access_log)
        for forbidden in (
                'path', 'query', 'header', 'body', 'sourceIp',
                'integrationErrorMessage'):
            self.assertNotIn(forbidden, access_log)

    def test_HTTP_APIをLambdaへ明示的に接続する(self):
        integration = self.resources['HttpApiIntegration']['Properties']
        self.assertEqual('AWS_PROXY', integration['IntegrationType'])
        self.assertEqual('POST', integration['IntegrationMethod'])
        self.assertEqual('2.0', integration['PayloadFormatVersion'])
        integration_uri, substitutions = integration['IntegrationUri']['Sub']
        self.assertIn(
            ':apigateway:${AWS::Region}:lambda:path/2015-03-31/functions/',
            integration_uri,
        )
        self.assertTrue(integration_uri.endswith('/invocations'))
        self.assertEqual(
            {'GetAtt': 'ApiFunction.Arn'}, substitutions['FunctionArn'])

        permission = self.resources['HttpApiInvokePermission']['Properties']
        self.assertEqual('apigateway.amazonaws.com', permission['Principal'])
        self.assertEqual('lambda:InvokeFunction', permission['Action'])
        self.assertEqual({'Ref': 'ApiFunction'}, permission['FunctionName'])
        self.assertIn('${HttpApi}/*', permission['SourceArn']['Sub'])

    def test_Scheduler_roleを専用groupへ制限する(self):
        role = self.resources['SchedulerRole']['Properties']
        trust = role['AssumeRolePolicyDocument']['Statement'][0]
        self.assertEqual(
            {'Ref': 'AWS::AccountId'},
            trust['Condition']['StringEquals']['aws:SourceAccount'],
        )
        self.assertEqual(
            {'GetAtt': 'SchedulerGroup.Arn'},
            trust['Condition']['StringEquals']['aws:SourceArn'],
        )
        target_resources = role['Policies'][0]['PolicyDocument'][
            'Statement'][0]['Resource']
        self.assertEqual(
            {
                ('GetAtt', 'ActionQueue.Arn'),
                ('GetAtt', 'GroupQueue.Arn'),
                ('GetAtt', 'DeadLetterQueue.Arn'),
            },
            {(next(iter(item)), next(iter(item.values())))
             for item in target_resources},
        )

    def test_SSM権限をParameter単位へ限定する(self):
        expected_parameters = {
            'ApiRole': {
                'AdminAuthParameterName',
                'RuntimeSecretsParameterName',
            },
            'WorkerRole': {'RuntimeSecretsParameterName'},
            'BuildTaskRole': {
                'GoogleSheetsCredentialParameterName',
                'RuntimeSecretsParameterName',
            },
        }
        for role_name, expected in expected_parameters.items():
            statements = self.resources[role_name]['Properties'][
                'Policies'][0]['PolicyDocument']['Statement']
            ssm_statement = next(
                statement for statement in statements
                if statement['Action'] == 'ssm:GetParameter')
            resources = ssm_statement['Resource']
            if not isinstance(resources, list):
                resources = [resources]
            self.assertEqual(
                expected,
                {
                    name
                    for resource in resources
                    for name in expected
                    if f'parameter${{{name}}}' in resource['Sub']
                },
            )
            self.assertTrue(all(
                resource['Sub'].startswith(
                    'arn:${AWS::Partition}:ssm:${AWS::Region}:'
                    '${AWS::AccountId}:parameter${')
                for resource in resources
            ))

        self.assertNotIn('ssm:GetParameters', self.source)
        build_policy = self.resources['BuildTaskRole']['Properties'][
            'Policies'][0]['PolicyDocument']
        self.assertNotIn('GroupTaskTable', str(build_policy))

    def test_ログは30日でstack削除時に残さない(self):
        for name in (
                'ApiLogGroup', 'ApiAccessLogGroup',
                'ActionWorkerLogGroup', 'GroupWorkerLogGroup',
                'BuildLogGroup'):
            resource = self.resources[name]
            self.assertEqual(30, resource['Properties']['RetentionInDays'])
            self.assertNotIn('DeletionPolicy', resource)
            self.assertNotIn('UpdateReplacePolicy', resource)

    def test_Sheet_IDを全computeへ渡す(self):
        globals_environment = self.template['Globals']['Function'][
            'Environment']['Variables']
        self.assertEqual({'Ref': 'SheetId'}, globals_environment['SHEETS_ID'])

        build_environment = {
            item['Name']: item['Value']
            for item in self.resources['BuildTaskDefinition']['Properties'][
                'ContainerDefinitions'][0]['Environment']
        }
        self.assertEqual({'Ref': 'SheetId'}, build_environment['SHEETS_ID'])

    def test_固定費を増やす同時実行予約を行わない(self):
        self.assertNotIn('ReservedConcurrentExecutions', self.source)
        self.assertNotIn('ProvisionedConcurrencyConfig', self.source)

    def test_明示的なresource依存に循環がない(self):
        resource_names = set(self.resources)
        parameter_names = set(self.template['Parameters'])

        def collect_dependencies(value):
            dependencies = set()
            if isinstance(value, list):
                for item in value:
                    dependencies.update(collect_dependencies(item))
                return dependencies
            if not isinstance(value, dict):
                return dependencies
            if set(value) == {'Ref'} and value['Ref'] in resource_names:
                dependencies.add(value['Ref'])
            if set(value) == {'GetAtt'}:
                target = value['GetAtt']
                if isinstance(target, str):
                    target = target.split('.', 1)[0]
                elif isinstance(target, list):
                    target = target[0]
                if target in resource_names:
                    dependencies.add(target)
            if set(value) == {'Sub'}:
                substitution = value['Sub']
                text = substitution[0] if isinstance(
                    substitution, list) else substitution
                for target in re.findall(r'\$\{([A-Za-z0-9]+)', text):
                    if (target in resource_names
                            and target not in parameter_names):
                        dependencies.add(target)
            for item in value.values():
                dependencies.update(collect_dependencies(item))
            return dependencies

        graph = {}
        global_function_dependencies = collect_dependencies(
            self.template['Globals']['Function'])
        for name, resource in self.resources.items():
            dependencies = collect_dependencies(resource)
            if resource['Type'] == 'AWS::Serverless::Function':
                dependencies.update(global_function_dependencies)
            explicit = resource.get('DependsOn', [])
            if isinstance(explicit, str):
                explicit = [explicit]
            dependencies.update(explicit)
            graph[name] = dependencies & resource_names

        visiting = set()
        visited = set()

        def visit(name):
            if name in visiting:
                self.fail(f'resource依存が循環しています: {name}')
            if name in visited:
                return
            visiting.add(name)
            for dependency in graph[name]:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in graph:
            visit(name)


if __name__ == '__main__':
    unittest.main()
