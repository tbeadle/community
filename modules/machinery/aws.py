from __future__ import absolute_import
import logging
import time
from typing import Literal

import boto3
from sqlalchemy.exc import SQLAlchemyError

from lib.cuckoo.common.abstracts import Machinery
from lib.cuckoo.common.config import Config
from lib.cuckoo.common.exceptions import CuckooMachineError

logging.getLogger("boto3").setLevel(logging.CRITICAL)
logging.getLogger("botocore").setLevel(logging.CRITICAL)
log = logging.getLogger(__name__)


class AWS(Machinery):
    """Virtualization layer for AWS."""

    # VM states.
    PENDING = "pending"
    STOPPING = "stopping"
    RUNNING = "running"
    POWEROFF = "poweroff"
    ERROR = "machete"

    AUTOSCALE_CUCKOO = "AUTOSCALE_CUCKOO"

    def __init__(self):
        super(AWS, self).__init__()

    """override Machinery method"""

    def _initialize_check(self):
        """
        Looking for all EC2 machines that match aws.conf and load them into EC2_MACHINES dictionary.
        """
        self.ec2_machines = {}
        self.dynamic_machines_sequence = 0
        self.dynamic_machines_count = 0
        log.info("connecting to AWS: %s", self.options.aws.region_name)
        self.ec2_resource = boto3.resource(
            "ec2",
            region_name=self.options.aws.region_name,
            aws_access_key_id=self.options.aws.aws_access_key_id,
            aws_secret_access_key=self.options.aws.aws_secret_access_key,
        )

        # Iterate over all instances with tag that has a key of AUTOSCALE_CUCKOO
        for instance in self.ec2_resource.instances.filter(
            Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped", "stopping"]}]
        ):
            if self._is_autoscaled(instance):
                log.info("Terminating autoscaled instance %s", instance.id)
                instance.terminate()

        instance_ids = self._list()
        machines = self.machines()
        for machine in machines:
            if machine.label not in instance_ids:
                continue
            self.ec2_machines[machine.label] = self.ec2_resource.Instance(machine.label)
            if self._status(machine.label) != AWS.POWEROFF:
                self.stop(label=machine.label)

        self._start_or_create_machines()

    def _start_next_machines(self, num_of_machines_to_start: int):
        """
        pull from DB the next machines in queue and starts them
        the whole idea is to prepare x machines on, so once a task will arrive - the machine will be ready with windows
        already launched.
        :param num_of_machines_to_start: how many machines(first in queue) will be started
        """
        for machine in self.db.get_available_machines():
            if num_of_machines_to_start <= 0:
                break
            if self._status(machine.label) in {AWS.POWEROFF, AWS.STOPPING}:
                self.ec2_machines[machine.label].start()  # not using self.start() to avoid _wait_ method
                num_of_machines_to_start -= 1

    def _delete_machine_form_db(self, label: str):
        """
        cuckoo's DB class does not implement machine deletion, so we made one here
        :param label: the machine label
        """
        session = self.db.Session()
        try:
            from lib.cuckoo.core.database import Machine

            machine = session.query(Machine).filter_by(label=label).first()
            if machine:
                session.delete(machine)
                session.commit()
        except SQLAlchemyError as e:
            log.debug("Database error removing machine: %s", e)
            session.rollback()
        finally:
            session.close()

    def _allocate_new_machine(self) -> bool:
        """
        allocating/creating new EC2 instance(autoscale option)
        """
        # read configuration file
        machinery_options = self.options.get("aws")
        autoscale_options = self.options.get("autoscale")
        # If configured, use specific network interface for this
        # machine, else use the default value.
        interface = autoscale_options.get("interface") or machinery_options.get("interface")
        resultserver_ip = autoscale_options.get("resultserver_ip") or Config("cuckoo:resultserver:ip")
        if autoscale_options.get("resultserver_port"):
            resultserver_port = autoscale_options["resultserver_port"]
        else:
            # The ResultServer port might have been dynamically changed,
            # get it from the ResultServer singleton. Also avoid import
            # recursion issues by importing ResultServer here.
            from lib.cuckoo.core.resultserver import ResultServer

            resultserver_port = ResultServer().port

        log.info("All machines are busy, allocating new machine")
        self.dynamic_machines_sequence += 1
        self.dynamic_machines_count += 1
        new_machine_name = f"cuckoo_autoscale_{self.dynamic_machines_sequence:03d}"
        instance = self._create_instance(
            tags=[{"Key": "Name", "Value": new_machine_name}, {"Key": self.AUTOSCALE_CUCKOO, "Value": "True"}]
        )
        if instance is None:
            return False

        self.ec2_machines[instance.id] = instance
        #  sets "new_machine" object in configuration object to avoid raising an exception
        setattr(self.options, new_machine_name, {})
        # add machine to DB
        self.db.add_machine(
            name=new_machine_name,
            label=instance.id,
            ip=instance.private_ip_address,
            platform=autoscale_options["platform"],
            options=autoscale_options["options"],
            tags=autoscale_options["tags"],
            interface=interface,
            snapshot=None,
            resultserver_ip=resultserver_ip,
            resultserver_port=resultserver_port,
        )
        return True

    """override Machinery method"""

    def acquire(self, machine_id=None, platform=None, tags=None):
        """
        override Machinery method to utilize the auto scale option
        """
        base_class_return_value = super().acquire(machine_id, platform, tags)
        self._start_or_create_machines()  # prepare another machine
        return base_class_return_value

    def _start_or_create_machines(self):
        """
        checks if x(according to "gap" in aws config) machines can be immediately started.
        If autoscale is enabled and less then x can be started - > create new instances to complete the gap
        :return:
        """

        # read configuration file
        machinery_options = self.options.get("aws")
        autoscale_options = self.options.get("autoscale")

        current_available_machines = self.db.count_machines_available()
        running_machines_gap = machinery_options.get("running_machines_gap", 0)
        dynamic_machines_limit = autoscale_options["dynamic_machines_limit"]

        self._start_next_machines(num_of_machines_to_start=min(current_available_machines, running_machines_gap))
        # if no sufficient machines left, launch a new machine
        while autoscale_options["autoscale"] and current_available_machines < running_machines_gap:
            if self.dynamic_machines_count >= dynamic_machines_limit:
                log.debug("Reached dynamic machines limit - %d machines", dynamic_machines_limit)
                break
            if not self._allocate_new_machine():
                break
            current_available_machines += 1

    """override Machinery method"""

    def _list(self) -> list:
        """
        :return: A list of all instance ids under the AWS account
        """
        instances = self.ec2_resource.instances.filter(
            Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped", "stopping"]}]
        )
        return [instance.id for instance in instances]

    """override Machinery method"""

    def _status(self, label) -> Literal:
        """
        Gets current status of a vm.
        @param label: virtual machine label.
        @return: status string.
        """
        try:
            self.ec2_machines[label].reload()
            state = self.ec2_machines[label].state["Name"]
            states = {
                "running": AWS.RUNNING,
                "stopped": AWS.POWEROFF,
                "pending": AWS.PENDING,
                "stopping": AWS.STOPPING,
            }
            status = states.get(state, AWS.ERROR)
            log.info("instance state: %s", status)
            return status
        except Exception as e:
            log.exception("can't retrieve the status: %s", e)
            return AWS.ERROR

    """override Machinery method"""

    def start(self, label):
        """
        Start a virtual machine.
        @param label: virtual machine label.
        @raise CuckooMachineError: if unable to start.
        """
        log.debug("Starting vm %s", label)

        if not self._is_autoscaled(self.ec2_machines[label]):
            self.ec2_machines[label].start()
            self._wait_status(label, AWS.RUNNING)

    """override Machinery method"""

    def stop(self, label):
        """
        Stops a virtual machine.
        If the machine has initialized from autoscaled component, then terminate it.
        @param label: virtual machine label.
        @raise CuckooMachineError: if unable to stop.
        """
        log.debug("Stopping vm %s", label)

        status = self._status(label)

        if status == AWS.POWEROFF:
            raise CuckooMachineError(f"Trying to stop an already stopped VM: {label}")

        if self._is_autoscaled(self.ec2_machines[label]):
            self.ec2_machines[label].terminate()
            self._delete_machine_form_db(label)
            self.dynamic_machines_count -= 1
        else:
            self.ec2_machines[label].stop(Force=True)
            self._wait_status(label, AWS.POWEROFF)
            self._restore(label)

    """override Machinery method"""

    def release(self, label=None):
        """
        we override it to have the ability to run start_or_create_machines() after unlocking the last machine
        Release a machine.
        @param label: machine label.
        """
        super().release(label)
        self._start_or_create_machines()

    def _create_instance(self, tags):
        """
        create a new instance
        :param tags: tags to attach to instance
        :return: the instance id
        """

        autoscale_options = self.options.get("autoscale")
        response = self.ec2_resource.create_instances(
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"DeleteOnTermination": True, "VolumeType": "gp2"}}],
            ImageId=autoscale_options["image_id"],
            InstanceType=autoscale_options["instance_type"],
            MaxCount=1,
            MinCount=1,
            NetworkInterfaces=[
                {
                    "DeviceIndex": 0,
                    "SubnetId": autoscale_options["subnet_id"],
                    "Groups": autoscale_options["security_groups"],
                }
            ],
            TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
        )
        new_instance = response[0]
        new_instance.modify_attribute(SourceDestCheck={"Value": False})
        log.debug("Created %s\n%s", new_instance.id, repr(response))
        return new_instance

    def _is_autoscaled(self, instance) -> bool:
        """
        checks if the instance has a tag that indicates that it was created as a result of autoscaling
        :param instance: instance object
        :return: true if the instance in "autoscaled"
        """
        if instance.tags:
            for tag in instance.tags:
                if tag.get("Key") == self.AUTOSCALE_CUCKOO:
                    return True
        return False

    def _restore(self, label):
        """
        restore the instance according to the configured snapshot(aws.conf)
        This method detaches and deletes the current volume, then creates a new one and attaches it.
        :param label: machine label
        """
        log.info("restoring machine: %s", label)
        vm_info = self.db.view_machine_by_label(label)
        snap_id = vm_info.snapshot
        instance = self.ec2_machines[label]
        state = self._status(label)
        if state != AWS.POWEROFF:
            raise CuckooMachineError(f"Instance '{label}' state '{state}' is not poweroff")
        volumes = list(instance.volumes.all())
        if len(volumes) != 1:
            raise CuckooMachineError(f"Instance '{label}' has wrong number of volumes {len(volumes)}")
        old_volume = volumes[0]

        log.debug("Detaching %s", old_volume.id)
        resp = instance.detach_volume(VolumeId=old_volume.id, Force=True)
        log.debug("response: %s", resp)
        while True:
            old_volume.reload()
            if old_volume.state != "in-use":
                break
            time.sleep(1)

        log.debug("Old volume %s in state %s", old_volume.id, old_volume.state)
        if old_volume.state != "available":
            raise CuckooMachineError(f"Old volume turned into state {old_volume.state} instead of 'available'")
        log.debug("Deleting old volume")
        volume_type = old_volume.volume_type
        old_volume.delete()

        log.debug("Creating new volume")
        new_volume = self.ec2_resource.create_volume(
            SnapshotId=snap_id, AvailabilityZone=instance.placement["AvailabilityZone"], VolumeType=volume_type
        )
        log.debug("Created new volume %s", new_volume.id)
        while True:
            new_volume.reload()
            if new_volume.state != "creating":
                break
            time.sleep(1)
        log.debug("new volume %s in state %s", new_volume.id, new_volume.state)
        if new_volume.state != "available":
            state = new_volume.state
            new_volume.delete()
            raise CuckooMachineError(f"New volume turned into state {state} instead of 'available'")

        log.debug("Attaching new volume")
        resp = instance.attach_volume(VolumeId=new_volume.id, Device="/dev/sda1")
        log.debug("response %s", resp)
        while True:
            new_volume.reload()
            if new_volume.state != "available":
                break
            time.sleep(1)
        log.debug("new volume %s in state %s", new_volume.id, new_volume.state)
        if new_volume.state != "in-use":
            new_volume.delete()
            raise CuckooMachineError(f"New volume turned into state {old_volume.state} instead of 'in-use'")
