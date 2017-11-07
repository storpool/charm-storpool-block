#!/usr/bin/python

"""
Simulate the layer-storpool-helper's utility classes.
"""

import mock


class SPStatus(object):
    """
    Simulate the status setting helper.
    """

    def __init__(self):
        """
        Initialize a SPStatus object: no reset handler set.
        """
        self.status_reset_handler = None

    def set_status_reset_handler(self, name):
        """
        Simulate setting the name of the layer that is allowed to reset
        a persistent error status.
        """
        self.status_reset_handler = name


class SPOpenStackIntegration(object):
    """
    Simulate the OpenStack Cinder container name helper.
    """
    def __init__(self, cinder_name=None):
        """
        Initialize an object: nothing really.
        """
        self.cinder_name = cinder_name

    def lxd_cinder_name(self):
        """
        Return the name of the Cinder container found.
        """
        return self.cinder_name

    def r_set_lxd_cinder_name(self, name):
        """
        For testing purposes, set the Cinder container name.
        """
        self.cinder_name = name


class SPServiceHook(object):
    """
    Simulate the service-hook layer and interface.
    """
    def __init__(self):
        """
        Initialize a service hook object with no presence data.
        """
        self.data = set()
        self.relation_name = None

    def r_get_relation_name(self):
        """
        For testing purposes, get the last relation name set by
        the add_present_node() method.
        """
        return self.relation_name

    def r_set_present_nodes(self, data):
        """
        For testing purposes, overwrite the presence data completely.
        """
        self.data = data

    def get_present_nodes(self):
        """
        Get the nodes presence data.
        """
        return set(self.data)

    def add_present_node(self, node, rel_name):
        """
        Add a node to the presence data, simulate sending it along
        the specified relation.
        """
        self.data.add(node)
        self.relation_name = rel_name


repo = mock.Mock()
utils = mock.Mock()
utils.MACHINE_ID = '42'
utils.get_machine_id.return_value = utils.MACHINE_ID

osi = SPOpenStackIntegration()
service_hook = SPServiceHook()
status = SPStatus()
