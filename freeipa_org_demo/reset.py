import time
import logging
import boto3

from .utils import yes_no
from .config import ec2_configuration, demo_configuration

def reset(debug, unattended, rebuild, eip, instance_type=ec2_configuration['instance_type']):
    print("Called with", debug, unattended, rebuild, eip, instance_type)
    logger = logging.getLogger('demo1.freeipa.org')
    logger.setLevel(logging.WARNING)
    if debug:
        logger.setLevel(logging.DEBUG)
        level = logging.DEBUG
    else:
        level = logging.WARNING

    handler = logging.StreamHandler()
    handler.setLevel(level)
    formatter = logging.Formatter('%(levelname)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    ec2 = boto3.resource('ec2', region_name=ec2_configuration['ec2_region'])
    ec2_client = boto3.client('ec2', region_name=ec2_configuration['ec2_region'])
    sts = boto3.client('sts')
    ec2_client_id = sts.get_caller_identity().get('Account')

    instances = ec2.instances.all()
    logger.debug("INSTANCES")
    for instance in instances:
        logger.debug("- {} [{}]: AMI {}, key {}, state {}".format(
            instance.id,
            instance.instance_type,
            instance.image_id,
            instance.key_name,
            instance.state,
            ))

    images = ec2.images.filter(Owners=[ec2_client_id])
    logger.debug("Available images")
    for image in images:
        logger.debug("- {}: {}".format(image.id, image.name))

    # Select source image
    images = list(ec2.images.filter(
        Owners=[ec2_client_id],
        Filters=[{'Name': 'name', 'Values':[ec2_configuration['instance_image_name']]}]))

    if not images:
        error = "Cannot find instance with filter '{}'".format(ec2_configuration['instance_image_name'])
        logger.critical(error)
        raise RuntimeError(error)
    image_map = dict((i.name, i) for i in images)
    image_map_names = list(image_map.keys())
    image_map_names.sort()
    logger.debug("Filtered images (last one will be selected): {}".format(image_map_names))
    instance_image = image_map[image_map_names[-1]]
    logger.debug("TARGET IMAGE: {} ({})".format(instance_image.id, instance_image.name))

    response = ec2_client.run_instances(
        ImageId=instance_image.id,
        InstanceType=instance_type,
        MaxCount=1,
        MinCount=1,
        Monitoring={'Enabled': False},
        SecurityGroups=ec2_configuration['instance_security_groups'],
        KeyName=ec2_configuration['instance_ssh_key'],
        IamInstanceProfile={'Name': 'freeipa-org-demo-iam'},
        )

    new_instance_id = response['Instances'][0]['InstanceId']
    logger.debug("NEW INSTANCE: {}".format(new_instance_id))
    instance = ec2.Instance(new_instance_id)

    try:
        logger.debug("Waiting on public IP...")
        while True:
            if instance.public_ip_address:
                logger.debug("Public IP: {}".format(instance.public_ip_address))
                break
            instance.reload()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.debug("... continue, interrupted")

    if eip:
        logger.debug("Allocate Elastic IP address")
        eip_addrs = ec2_client.describe_addresses(PublicIps=[ec2_configuration['instance_elastic_ip']])
        old_instance_id = eip_addrs['Addresses'][0].get('InstanceId')

        if old_instance_id:
            logger.info("Instance {} is pointing at EIP {}".format(old_instance_id, ec2_configuration['instance_elastic_ip']))
            logger.debug("Remove old instance pointed from EIP?")
            reply = yes_no('Terminate {}?'.format(old_instance_id), unattended)
            if reply:
                ec2_client.disassociate_address(PublicIp=ec2_configuration['instance_elastic_ip'])
                ec2_client.terminate_instances(InstanceIds=[old_instance_id])
            else:
                logger.debug("Skipping")

        # FIXME: can fail with botocore.exceptions.ClientError: An error
        # occurred (InvalidInstanceID) when calling the AssociateAddress
        # operation: The pending instance 'i-07b9d9187311e650d' is not in
        # a valid state for this operation.
        response = ec2_client.associate_address(
            InstanceId=new_instance_id,
            PublicIp=ec2_configuration['instance_elastic_ip'],
            AllowReassociation=False,
        )
        logger.debug("New public (elastic) IP: {}".format(ec2_configuration['instance_elastic_ip']))
        instance.reload()
    else:
        logger.debug("Skipping EIP allocation")

    try:
        logger.debug("Waiting on fully initialized")
        while True:
            instance_status_obj = ec2_client.describe_instance_status(InstanceIds=[new_instance_id])
            instance_status = instance_status_obj['InstanceStatuses'][0]['InstanceStatus']['Status']
            if instance_status == 'initializing':
                pass
            elif instance_status == 'ok':
                logger.debug("Instance ready")
                break
            else:
                raise RuntimeError("Instance health check failed!")

            time.sleep(5)
    except KeyboardInterrupt:
        logger.debug("... continue, interrupted")

    reply = yes_no('Start FreeIPA?', unattended)
    if reply:
        logger.debug("Starting FreeIPA")
        ssm_client = boto3.client('ssm')
        commands = ['systemctl start ipa.service']
        resp = ssm_client.send_command(
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': commands},
            InstanceIds=[new_instance_id],
            TimeoutSeconds=60,
            Comment="Starting FreeIPA"
        )
        logger.debug("Result: {}".format(resp))

    else:
        logger.debug("Skipping")

    reply = yes_no('Terminate current instance?', unattended, default='n')
    if reply:
        logger.debug("Terminating")
        ec2_client.terminate_instances(InstanceIds=[instance.id])
    else:
        logger.debug("Skipping instance termination")
