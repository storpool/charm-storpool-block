"""
A Juju charm that installs the StorPool block (client initiator) service and
provides the configuration needed for other StorPool charms.

Subordinate hooks to the master:
- juju-info: attach to another charm (e.g. `nova-compute` or `cinder-storpool`)
  as a subordinate

Configuration hooks:
- storpool-presence: announce to other charms when the StorPool client service
  has been installed and configured on the Juju nodes

Internal hooks:
- block-p: announce to the other `storpool-block` units that this unit has
  been installed and configured
"""

from __future__ import print_function

import json
import platform

from charms import reactive
from charmhelpers.core import hookenv

from spcharms import osi
from spcharms import service_hook
from spcharms import utils as sputils


def rdebug(s):
    """
    Pass the diagnostic message string `s` to the central diagnostic logger.
    """
    sputils.rdebug(s, prefix='block-charm')


@reactive.hook('leader-elected')
def we_are_the_leader():
    """
    Make note of the fact that this unit has been elected as the leader for
    the `storpool-block` charm.  This will prompt the unit to send presence
    information to the other charms along the `storpool-presence` hook.
    """
    rdebug('looks like we have been elected leader')
    reactive.set_state('storpool-block-charm.leader')


@reactive.hook('leader-settings-changed')
def we_are_not_the_leader():
    """
    Make note of the fact that this unit is no longer the leader for
    the `storpool-block` charm, so no longer attempt to send presence data.
    """
    rdebug('welp, we are not the leader')
    reactive.remove_state('storpool-block-charm.leader')


@reactive.hook('leader-deposed')
def we_are_no_longer_the_leader():
    """
    Make note of the fact that this unit is no longer the leader for
    the `storpool-block` charm, so no longer attempt to send presence data.
    """
    rdebug('welp, we have been deposed as leader')
    reactive.remove_state('storpool-block-charm.leader')


@reactive.when('storpool-service.change')
@reactive.when('storpool-block-charm.leader')
def peers_change():
    """
    Handle a presence data change reported along the internal `block-p` hook.
    """
    rdebug('whee, got a storpool-service.change notification')
    reactive.remove_state('storpool-service.change')

    state = service_hook.get_present_nodes()
    rdebug('got some state: {state}'.format(state=state))

    # Let us make sure our own data is here
    sp_node = platform.node()
    if sp_node not in state:
        rdebug('adding our own node {sp_node}'.format(sp_node=sp_node))
        service_hook.add_present_node(sp_node, 'block-p')
    lxd_cinder = osi.lxd_cinder_name()
    if lxd_cinder is not None and lxd_cinder not in state:
        rdebug('adding the Cinder LXD node {name}'.format(name=lxd_cinder))
        service_hook.add_present_node(lxd_cinder, 'block-p')
    rdebug('just for kicks, the current state: {state}'
           .format(state=service_hook.get_present_nodes()))

    reactive.set_state('storpool-block-charm.announce-presence')
    reactive.set_state('storpool-service.changed')


@reactive.when('storpool-block-charm.announce-presence')
@reactive.when('storpool-block.block-started')
@reactive.when('storpool-presence.notify')
@reactive.when('storpool-block-charm.leader')
def announce_peers(hk):
    """
    If this unit is the leader, send the collected presence data to other
    charms along the `storpool-presence` hook.
    """
    rdebug('about to announce our presence to the StorPool Cinder thing')
    rel_ids = hookenv.relation_ids('storpool-presence')
    rdebug('- got rel_ids {rel_ids}'.format(rel_ids=rel_ids))
    for rel_id in rel_ids:
        rdebug('  - trying for {rel_id}'.format(rel_id=rel_id))
        data = json.dumps(service_hook.get_present_nodes())
        hookenv.relation_set(rel_id,
                             storpool_presence=data)
        rdebug('  - done with {rel_id}'.format(rel_id=rel_ids))
    rdebug('- done with the rel_ids')


@reactive.when('storpool-block.block-started')
@reactive.when('storpool-osi.installed-into-lxds')
@reactive.when_not('storpool-block-charm.stopped')
def ready():
    """
    When the StorPool block service has been installed and the OpenStack
    integration has been installed everywhere, set the unit's status to
    `active`.
    """
    rdebug('ready to go')
    hookenv.status_set('active', 'so far so good so what')


@reactive.hook('stop')
def stop_and_propagate():
    """
    Propagate a `stop` action to the lower layers; in particular, let
    the `storpool-openstack-integration` layer know that it does not need to
    propagate the `stop` action by itself.

    Also set the "storpool-block-charm.stopped" state so that no further
    presence or status updates are sent to other units or charms.
    """
    rdebug('a stop event was received')

    rdebug('letting storpool-openstack-integration know')
    reactive.set_state('storpool-osi.stop')
    reactive.set_state('storpool-osi.no-propagate-stop')

    rdebug('letting storpool-block know')
    reactive.set_state('storpool-block.stop')

    rdebug('done here, it seems')
    reactive.set_state('storpool-block-charm.stopped')
