#!/usr/bin/python
# (c) 2019, XLAB d.o.o <www.xlab.si>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import (absolute_import, division, print_function)

__metaclass__ = type


DOCUMENTATION = r'''
---
module: s3_bucket_notification
version_added: 1.0.0
short_description: Creates, updates or deletes S3 Bucket notification for lambda
description:
    - This module allows the management of AWS Lambda function bucket event mappings via the
      Ansible framework. Use module M(community.aws.lambda) to manage the lambda function itself, M(community.aws.lambda_alias)
      to manage function aliases and M(community.aws.lambda_policy) to modify lambda permissions.
notes:
    - This module heavily depends on M(community.aws.lambda_policy) as you need to allow C(lambda:InvokeFunction)
       permission for your lambda function.

author:
    - XLAB d.o.o. (@xlab-si)
    - Aljaz Kosir (@aljazkosir)
    - Miha Plesko (@miha-plesko)
options:
  event_name:
    description:
      - Unique name for event notification on bucket.
    required: true
    type: str
  lambda_function_arn:
    description:
      - The ARN of the lambda function.
    aliases: ['function_arn']
    type: str
  bucket_name:
    description:
      - S3 bucket name.
    required: true
    type: str
  state:
    description:
      - Describes the desired state.
    default: "present"
    choices: ["present", "absent"]
    type: str
  lambda_alias:
    description:
      - Name of the Lambda function alias.
      - Mutually exclusive with I(lambda_version).
    type: str
  lambda_version:
    description:
      - Version of the Lambda function.
      - Mutually exclusive with I(lambda_alias).
    type: int
  events:
    description:
      - Events that you want to be triggering notifications. You can select multiple events to send
        to the same destination, you can set up different events to send to different destinations,
        and you can set up a prefix or suffix for an event. However, for each bucket,
        individual events cannot have multiple configurations with overlapping prefixes or
        suffixes that could match the same object key.
      - Required when I(state=present).
    choices: ['s3:ObjectCreated:*', 's3:ObjectCreated:Put', 's3:ObjectCreated:Post',
              's3:ObjectCreated:Copy', 's3:ObjectCreated:CompleteMultipartUpload',
              's3:ObjectRemoved:*', 's3:ObjectRemoved:Delete',
              's3:ObjectRemoved:DeleteMarkerCreated', 's3:ObjectRestore:Post',
              's3:ObjectRestore:Completed', 's3:ReducedRedundancyLostObject']
    type: list
    elements: str
  prefix:
    description:
      - Optional prefix to limit the notifications to objects with keys that start with matching
        characters.
    type: str
  suffix:
    description:
      - Optional suffix to limit the notifications to objects with keys that end with matching
        characters.
    type: str
requirements:
    - boto3
extends_documentation_fragment:
- amazon.aws.aws
- amazon.aws.ec2

'''

EXAMPLES = r'''
---
# Example that creates a lambda event notification for a bucket
- name: Process jpg image
  community.aws.s3_bucket_notification:
    state: present
    event_name: on_file_add_or_remove
    bucket_name: test-bucket
    function_name: arn:aws:lambda:us-east-2:526810320200:function:test-lambda
    events: ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"]
    prefix: images/
    suffix: .jpg
'''

RETURN = r'''
notification_configuration:
    description: list of currently applied notifications
    returned: success
    type: list
'''

from ansible_collections.amazon.aws.plugins.module_utils.core import AnsibleAWSModule
from ansible_collections.amazon.aws.plugins.module_utils.ec2 import camel_dict_to_snake_dict

try:
    from botocore.exceptions import ClientError, BotoCoreError
except ImportError:
    pass  # will be protected by AnsibleAWSModule


class AmazonBucket:
    def __init__(self, module, client):
        self.module = module
        self.client = client
        self.bucket_name = module.params["bucket_name"]
        self.check_mode = module.check_mode
        self._full_config_cache = None

    def full_config(self):
        if self._full_config_cache is None:
            self._full_config_cache = dict(
                QueueConfigurations=[], TopicConfigurations=[], LambdaFunctionConfigurations=[]
            )

            try:
                config_lookup = self.client.get_bucket_notification_configuration(Bucket=self.bucket_name)
            except (ClientError, BotoCoreError) as e:
                self.module.fail_json(msg=f"{e}")

            # Handle different event targets
            if config_lookup.get("QueueConfigurations"):
                for queue_config in config_lookup.get("QueueConfigurations"):
                    self._full_config_cache["QueueConfigurations"].append(Config.from_api(queue_config))

            if config_lookup.get("TopicConfigurations"):
                for topic_config in config_lookup.get("TopicConfigurations"):
                    self._full_config_cache["TopicConfigurations"].append(Config.from_api(topic_config))

            if config_lookup.get("LambdaFunctionConfigurations"):
                for function_config in config_lookup.get("LambdaFunctionConfigurations"):
                    self._full_config_cache["LambdaFunctionConfigurations"].append(Config.from_api(function_config))

        return self._full_config_cache

    def current_config(self, config_name):
        for target_configs in self.full_config():
            for config in self.full_config()[target_configs]:
                if config.raw["Id"] == config_name:
                    return config

    def apply_config(self, desired):
        configs = dict(QueueConfigurations=[], TopicConfigurations=[], LambdaFunctionConfigurations=[])

        # Iterate through existing configs then add the desired config
        for target_configs in self.full_config():
            for config in self.full_config()[target_configs]:
                if config.name != desired.raw["Id"]:
                    configs[target_configs].append(config.raw)

        if self.module.params.get("queue_arn"):
            configs["QueueConfigurations"].append(desired.raw)
        if self.module.params.get("topic_arn"):
            configs["TopicConfigurations"].append(desired.raw)
        if self.module.params.get("lambda_function_arn"):
            configs["LambdaFunctionConfigurations"].append(desired.raw)

        self._upload_bucket_config(configs)
        return configs

    def delete_config(self, desired):
        configs = dict(QueueConfigurations=[], TopicConfigurations=[], LambdaFunctionConfigurations=[])

        # Iterate through existing configs omitting specified config
        for target_configs in self.full_config():
            for config in self.full_config()[target_configs]:
                if config.name != desired.raw["Id"]:
                    configs[target_configs].append(config.raw)

        self._upload_bucket_config(configs)
        return configs

    def _upload_bucket_config(self, configs):
        api_params = dict(Bucket=self.bucket_name, NotificationConfiguration=dict())

        # Iterate through available configs
        for target_configs in configs:
            if len(configs[target_configs]) > 0:
                api_params["NotificationConfiguration"][target_configs] = configs[target_configs]

        if not self.check_mode:
            try:
                self.client.put_bucket_notification_configuration(**api_params)
            except (ClientError, BotoCoreError) as e:
                self.module.fail_json(msg=f"{e}")


class Config:
    def __init__(self, content):
        self._content = content
        self.name = content['Id']

    @property
    def raw(self):
        return self._content

    def __eq__(self, other):
        if other:
            return self.raw == other.raw
        return False

    @classmethod
    def from_params(cls, **params):
        bucket_event_params = dict(
            Id=params["event_name"],
            Events=sorted(params["events"]),
            Filter=dict(
                Key=dict(
                    FilterRules=[
                        dict(Name="Prefix", Value=params["prefix"]),
                        dict(Name="Suffix", Value=params["suffix"]),
                    ]
                )
            ),
        )

        # Handle different event targets
        if params.get("queue_arn"):
            bucket_event_params["QueueArn"] = params["queue_arn"]
        if params.get("topic_arn"):
            bucket_event_params["TopicArn"] = params["topic_arn"]
        if params.get("lambda_function_arn"):
            function_arn = params["lambda_function_arn"]

            qualifier = None
            if params["lambda_version"] > 0:
                qualifier = str(params["lambda_version"])
            elif params["lambda_alias"]:
                qualifier = str(params["lambda_alias"])
            if qualifier:
                params["lambda_function_arn"] = f"{function_arn}:{qualifier}"

            bucket_event_params["LambdaFunctionArn"] = params["lambda_function_arn"]

        return cls(bucket_event_params)

    @classmethod
    def from_api(cls, config):
        return cls(config)

def setup_module_object():
        event_types = [
            "s3:ObjectCreated:*",
            "s3:ObjectCreated:Put",
            "s3:ObjectCreated:Post",
            "s3:ObjectCreated:Copy",
            "s3:ObjectCreated:CompleteMultipartUpload",
            "s3:ObjectRemoved:*",
            "s3:ObjectRemoved:Delete",
            "s3:ObjectRemoved:DeleteMarkerCreated",
            "s3:ObjectRestore:Post",
            "s3:ObjectRestore:Completed",
            "s3:ReducedRedundancyLostObject",
        ]
    
        argument_spec = dict(
            state=dict(default="present", choices=["present", "absent"]),
            event_name=dict(required=True),
            lambda_function_arn=dict(aliases=["function_arn"]),
            queue_arn=dict(type="str"),
            topic_arn=dict(type="str"),
            bucket_name=dict(required=True),
            events=dict(type="list", default=[], choices=event_types, elements="str"),
            prefix=dict(default=""),
            suffix=dict(default=""),
            lambda_alias=dict(),
            lambda_version=dict(type="int", default=0),
        )
    
        mutually_exclusive = [
            ["queue_arn", "topic_arn", "lambda_function_arn"],
            ["lambda_alias", "lambda_version"],
        ]
    
        return AnsibleAWSModule(
            argument_spec=argument_spec,
            supports_check_mode=True,
            mutually_exclusive=mutually_exclusive,
            required_if=[["state", "present", ["events"]]],
        )
def main():
    module = setup_module_object()

    client = module.client("s3")
    bucket = AmazonBucket(module, client)
    current = bucket.current_config(module.params["event_name"])
    desired = Config.from_params(**module.params)

    notification_configs = dict(QueueConfigurations=[], TopicConfigurations=[], LambdaFunctionConfigurations=[])

    for target_configs in bucket.full_config():
        for cfg in bucket.full_config()[target_configs]:
            notification_configs[target_configs].append(camel_dict_to_snake_dict(cfg.raw))

    state = module.params["state"]
    updated_configuration = dict()
    changed = False

    if state == "present":
        if current != desired:
            updated_configuration = bucket.apply_config(desired)
            changed = True
    elif state == "absent":
        if current:
            updated_configuration = bucket.delete_config(desired)
            changed = True

    for target_configs in updated_configuration:
        notification_configs[target_configs] = []
        for cfg in updated_configuration.get(target_configs, list()):
            notification_configs[target_configs].append(camel_dict_to_snake_dict(cfg))

    module.exit_json(changed=changed, notification_configuration=camel_dict_to_snake_dict(notification_configs))


if __name__ == '__main__':
    main()
