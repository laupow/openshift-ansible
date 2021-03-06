"""Check that there is enough disk space in predefined paths."""

import os.path
import tempfile

from openshift_checks import OpenShiftCheck, OpenShiftCheckException


class DiskAvailability(OpenShiftCheck):
    """Check that recommended disk space is available before a first-time install."""

    name = "disk_availability"
    tags = ["preflight"]

    # Values taken from the official installation documentation:
    # https://docs.openshift.org/latest/install_config/install/prerequisites.html#system-requirements
    recommended_disk_space_bytes = {
        '/var': {
            'masters': 40 * 10**9,
            'nodes': 15 * 10**9,
            'etcd': 20 * 10**9,
        },
        # Used to copy client binaries into,
        # see roles/openshift_cli/library/openshift_container_binary_sync.py.
        '/usr/local/bin': {
            'masters': 1 * 10**9,
            'nodes': 1 * 10**9,
            'etcd': 1 * 10**9,
        },
        # Used as temporary storage in several cases.
        tempfile.gettempdir(): {
            'masters': 1 * 10**9,
            'nodes': 1 * 10**9,
            'etcd': 1 * 10**9,
        },
    }

    # recommended disk space for each location under an upgrade context
    recommended_disk_upgrade_bytes = {
        '/var': {
            'masters': 10 * 10**9,
            'nodes': 5 * 10 ** 9,
            'etcd': 5 * 10 ** 9,
        },
    }

    def is_active(self):
        """Skip hosts that do not have recommended disk space requirements."""
        group_names = self.get_var("group_names", default=[])
        active_groups = set()
        for recommendation in self.recommended_disk_space_bytes.values():
            active_groups.update(recommendation.keys())
        has_disk_space_recommendation = bool(active_groups.intersection(group_names))
        return super(DiskAvailability, self).is_active() and has_disk_space_recommendation

    def run(self):
        group_names = self.get_var("group_names")
        ansible_mounts = self.get_var("ansible_mounts")
        ansible_mounts = {mount['mount']: mount for mount in ansible_mounts}

        user_config = self.get_var("openshift_check_min_host_disk_gb", default={})
        try:
            # For backwards-compatibility, if openshift_check_min_host_disk_gb
            # is a number, then it overrides the required config for '/var'.
            number = float(user_config)
            user_config = {
                '/var': {
                    'masters': number,
                    'nodes': number,
                    'etcd': number,
                },
            }
        except TypeError:
            # If it is not a number, then it should be a nested dict.
            pass

        # TODO: as suggested in
        # https://github.com/openshift/openshift-ansible/pull/4436#discussion_r122180021,
        # maybe we could support checking disk availability in paths that are
        # not part of the official recommendation but present in the user
        # configuration.
        for path, recommendation in self.recommended_disk_space_bytes.items():
            free_bytes = self.free_bytes(path, ansible_mounts)
            recommended_bytes = max(recommendation.get(name, 0) for name in group_names)

            config = user_config.get(path, {})
            # NOTE: the user config is in GB, but we compare bytes, thus the
            # conversion.
            config_bytes = max(config.get(name, 0) for name in group_names) * 10**9
            recommended_bytes = config_bytes or recommended_bytes

            # if an "upgrade" context is set, update the minimum disk requirement
            # as this signifies an in-place upgrade - the node might have the
            # required total disk space, but some of that space may already be
            # in use by the existing OpenShift deployment.
            context = self.get_var("r_openshift_health_checker_playbook_context", default="")
            if context == "upgrade":
                recommended_upgrade_paths = self.recommended_disk_upgrade_bytes.get(path, {})
                if recommended_upgrade_paths:
                    recommended_bytes = config_bytes or max(recommended_upgrade_paths.get(name, 0)
                                                            for name in group_names)

            if free_bytes < recommended_bytes:
                free_gb = float(free_bytes) / 10**9
                recommended_gb = float(recommended_bytes) / 10**9
                msg = (
                    'Available disk space in "{}" ({:.1f} GB) '
                    'is below minimum recommended ({:.1f} GB)'
                ).format(path, free_gb, recommended_gb)

                # warn if check failed under an "upgrade" context
                # due to limits imposed by the user config
                if config_bytes and context == "upgrade":
                    msg += ('\n\nMake sure to account for decreased disk space during an upgrade\n'
                            'due to an existing OpenShift deployment. Please check the value of\n'
                            '  openshift_check_min_host_disk_gb={}\n'
                            'in your Ansible inventory, and lower the recommended disk space availability\n'
                            'if necessary for this upgrade.').format(config_bytes)

                return {
                    'failed': True,
                    'msg': (
                        'Available disk space in "{}" ({:.1f} GB) '
                        'is below minimum recommended ({:.1f} GB)'
                    ).format(path, free_gb, recommended_gb)
                }

        return {}

    @staticmethod
    def free_bytes(path, ansible_mounts):
        """Return the size available in path based on ansible_mounts."""
        mount_point = path
        # arbitry value to prevent an infinite loop, in the unlike case that '/'
        # is not in ansible_mounts.
        max_depth = 32
        while mount_point not in ansible_mounts and max_depth > 0:
            mount_point = os.path.dirname(mount_point)
            max_depth -= 1

        try:
            free_bytes = ansible_mounts[mount_point]['size_available']
        except KeyError:
            known_mounts = ', '.join('"{}"'.format(mount) for mount in sorted(ansible_mounts)) or 'none'
            msg = 'Unable to determine disk availability for "{}". Known mount points: {}.'
            raise OpenShiftCheckException(msg.format(path, known_mounts))

        return free_bytes
