#!/usr/bin/python3

"""
A set of unit tests for the storpool-block charm.
"""

import os
import sys
import unittest

import mock

from charmhelpers.core import hookenv

root_path = os.path.realpath('.')
if root_path not in sys.path:
    sys.path.insert(0, root_path)

lib_path = os.path.realpath('unit_tests/lib')
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)


from spcharms import service_hook as spservice
from spcharms import status as spstatus
from spcharms import utils as sputils


class MockReactive(object):
    def r_clear_states(self):
        self.states = set()

    def __init__(self):
        self.r_clear_states()

    def set_state(self, name):
        self.states.add(name)

    def remove_state(self, name):
        if name in self.states:
            self.states.remove(name)

    def is_state(self, name):
        return name in self.states

    def r_get_states(self):
        return set(self.states)

    def r_set_states(self, states):
        self.states = set(states)


initializing_config = None


class MockConfig(object):
    def r_clear_config(self):
        global initializing_config
        saved = initializing_config
        initializing_config = self
        self.override = {}
        self.changed_attrs = {}
        self.config = {}
        initializing_config = saved

    def __init__(self):
        self.r_clear_config()

    def r_set(self, key, value, changed):
        self.override[key] = value
        self.changed_attrs[key] = changed

    def get(self, key, default):
        return self.override.get(key, self.config.get(key, default))

    def changed(self, key):
        return self.changed_attrs.get(key, False)

    def __getitem__(self, name):
        # Make sure a KeyError is actually thrown if needed.
        if name in self.override:
            return self.override[name]
        else:
            return self.config[name]

    def __getattr__(self, name):
        return self.config.__getattribute__(name)

    def __setattr__(self, name, value):
        if initializing_config == self:
            return super(MockConfig, self).__setattr__(name, value)

        raise AttributeError('Cannot override the MockConfig '
                             '"{name}" attribute'.format(name=name))


r_state = MockReactive()
r_config = MockConfig()

# Do not give hookenv.config() a chance to run at all
hookenv.config = lambda: r_config


def mock_reactive_states(f):
    def inner1(inst, *args, **kwargs):
        @mock.patch('charms.reactive.set_state', new=r_state.set_state)
        @mock.patch('charms.reactive.remove_state', new=r_state.remove_state)
        @mock.patch('charms.reactive.helpers.is_state', new=r_state.is_state)
        def inner2(*args, **kwargs):
            return f(inst, *args, **kwargs)

        return inner2()

    return inner1


from reactive import storpool_block_charm as testee


LEADER_STATE = 'storpool-block-charm.leader'
PRESENCE_STATE = 'storpool-block-charm.announce-presence'


class TestStorPoolBlock(unittest.TestCase):
    def setUp(self):
        super(TestStorPoolBlock, self).setUp()
        r_state.r_clear_states()
        r_config.r_clear_config()
        spstatus.set_status_reset_handler(None)

    def do_test_hook_install(self, tested_function, is_upgrade):
        """
        Test the install hook: set the status reset handler, change nothing.
        """
        states = r_state.r_get_states()
        tested_function()
        self.assertEquals('storpool-repo-add', spstatus.status_reset_handler)
        if is_upgrade:
            self.assertEquals(states.union(set([PRESENCE_STATE])),
                              r_state.r_get_states())
        else:
            self.assertEquals(states, r_state.r_get_states())

    def do_test_we_are_the_leader(self, h_is_leader, h_leader_set):
        """
        Test the handling of the two possible false alarms when Juju
        signals us that our unit is the charm leader.
        """
        states = r_state.r_get_states()
        r_state.remove_state(LEADER_STATE)
        no_leader = r_state.r_get_states()
        r_state.set_state(LEADER_STATE)
        leader = r_state.r_get_states()
        self.assertNotEquals(no_leader, leader)
        self.assertEquals(no_leader.union(set([LEADER_STATE])),
                          leader)

        is_leader_call_count = h_is_leader.call_count
        leader_set_call_count = h_leader_set.call_count
        # is_leader() fails
        h_is_leader.return_value = False
        testee.we_are_the_leader()
        self.assertEquals(no_leader, r_state.r_get_states())
        self.assertEquals(is_leader_call_count + 1, h_is_leader.call_count)
        self.assertEquals(leader_set_call_count + 0, h_leader_set.call_count)

        def raise_fail(*args, **kwargs):
            """
            Simulate a leader_set() failure.
            """
            raise Exception('oops')

        # is_leader() succeeds, but leader_set() fails
        h_is_leader.return_value = True
        h_leader_set.side_effect = raise_fail
        testee.we_are_the_leader()
        self.assertEquals(no_leader, r_state.r_get_states())
        self.assertEquals(is_leader_call_count + 2, h_is_leader.call_count)
        self.assertEquals(leader_set_call_count + 1, h_leader_set.call_count)

        self.lset_args = None
        self.lset_kwargs = None

        def record_leader_set_args(*args, **kwargs):
            """
            Make sure leader_set() was invoked with the correct parameters.
            """
            self.lset_args = args
            self.lset_kwargs = kwargs

        # ...and now it all works out
        h_is_leader.return_value = True
        h_leader_set.side_effect = record_leader_set_args
        testee.we_are_the_leader()
        self.assertEquals(leader, r_state.r_get_states())
        self.assertEquals(is_leader_call_count + 3, h_is_leader.call_count)
        self.assertEquals(leader_set_call_count + 2, h_leader_set.call_count)
        self.assertEquals((), self.lset_args)
        self.assertEquals({'charm_storpool_block_unit': sputils.MACHINE_ID},
                          self.lset_kwargs)

        r_state.r_set_states(states)

    def do_test_ensure_our_presence(self):
        """
        Make sure ensure_our_presence() really adds our node and also
        triggers an announcement if necessary.
        """
        states = r_state.r_get_states()
        presence = spservice.get_present_nodes()

        r_state.set_state(PRESENCE_STATE)
        do_announce = r_state.r_get_states()
        r_state.remove_state(PRESENCE_STATE)
        no_announce = r_state.r_get_states()
        self.assertNotEquals(do_announce, no_announce)
        self.assertEquals(no_announce.union(set([PRESENCE_STATE])),
                          do_announce)

        sp_node = sputils.get_machine_id()
        other_nodes = {
            'not-' + sp_node: '17',
            'neither-' + sp_node: '18',
        }
        with_ours = {
            **other_nodes,
            sp_node: '16',
        }

        # No change if our node is there.
        r_state.r_set_states(no_announce)
        spservice.r_set_present_nodes(with_ours)
        testee.ensure_our_presence()
        self.assertEquals(no_announce, r_state.r_get_states())
        self.assertEquals(with_ours, spservice.get_present_nodes())

        # Add just our node if it's not there.
        r_state.r_set_states(no_announce)
        spservice.r_set_present_nodes(other_nodes)
        testee.ensure_our_presence()
        self.assertEquals(do_announce, r_state.r_get_states())
        self.assertEquals(with_ours, spservice.get_present_nodes())

        r_state.r_set_states(states)
        spservice.r_set_present_nodes(presence)

    @mock_reactive_states
    def test_hook_install(self):
        """
        Run the test for the install hook.
        """
        self.do_test_hook_install(testee.install_setup, False)

    @mock_reactive_states
    def test_hook_upgrade(self):
        """
        Run the test for the install hook.
        """
        self.do_test_hook_install(testee.upgrade_setup, True)

    @mock_reactive_states
    @mock.patch('charmhelpers.core.hookenv.leader_set')
    @mock.patch('charmhelpers.core.hookenv.is_leader')
    def test_hook_leader_elected(self, h_is_leader, h_leader_set):
        """
        Test the possible false alarms when we would be the leader.
        """
        self.do_test_we_are_the_leader(h_is_leader, h_leader_set)

    @mock_reactive_states
    def test_ensure_our_presence(self):
        """
        Test that ensure_our_presence() works properly.
        """
        self.do_test_ensure_our_presence()

    @mock_reactive_states
    @mock.patch('charmhelpers.core.hookenv.leader_set')
    @mock.patch('charmhelpers.core.hookenv.is_leader')
    def test_full_lifecycle(self, h_is_leader, h_leader_set):
        """
        Test the full lifecycle of the storpool-block charm.
        """
        self.do_test_hook_install(testee.install_setup, False)
        self.do_test_hook_install(testee.upgrade_setup, True)
        self.do_test_we_are_the_leader(h_is_leader, h_leader_set)
        self.do_test_ensure_our_presence()
