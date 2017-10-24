from __future__ import print_function

import json
import platform

from charms import reactive
from charmhelpers.core import hookenv

from spcharms import osi
from spcharms import service_hook
from spcharms import utils as sputils


def rdebug(s):
    sputils.rdebug(s, prefix='block-charm')


def hook_debug(hc):
    rdebug('hook information:')
    try:
        rdebug('- itself: {hc}'.format(hc=hc))
        rdebug('- name: {name}'.format(name=hc.relation_name))
        rdebug('- scope: {sc}'.format(sc=hc.scope))
        rdebug('- conversations:')
        for conv in hc.conversations():
            rdebug('   - key: {key}'.format(key=conv.key))
            rdebug('   - name: {name}'.format(name=conv.relation_name))
            rdebug('   - ids: {ids}'.format(ids=conv.relation_ids))
            rdebug('   - has config: {has}'.format(
                has=conv.get_local('storpool-config', None) is not None))
    except Exception as e:
        rdebug('could not examine the hook: {e}'.format(e=e))


@reactive.hook('leader-elected')
def we_are_the_leader():
    rdebug('looks like we have been elected leader')
    reactive.set_state('storpool-block-charm.leader')


@reactive.hook('leader-settings-changed')
def we_are_not_the_leader():
    rdebug('welp, we are not the leader')
    reactive.remove_state('storpool-block-charm.leader')


@reactive.hook('leader-deposed')
def we_are_no_longer_the_leader():
    rdebug('welp, we have been deposed as leader')
    reactive.remove_state('storpool-block-charm.leader')


@reactive.when('storpool-service.change')
@reactive.when('storpool-block-charm.leader')
def peers_change():
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


@reactive.when_not('l-storpool-config.config-network')
@reactive.when('storpool-config.available')
@reactive.when_not('storpool-block-charm.stopped')
def announce_no_config(hconfig):
    try:
        rdebug('letting the other side know that we have no config yet')
        hook_debug(hconfig)
        hconfig.configure(None, rdebug=rdebug)
    except Exception as e:
        rdebug('could not announce the lack of configuration: {e}'.format(e=e))


@reactive.when('l-storpool-config.config-network')
@reactive.when('storpool-config.available')
@reactive.when_not('storpool-block-charm.stopped')
def announce_config(hconfig):
    try:
        rdebug('letting the other side know that we have some configuration')
        hook_debug(hconfig)
        hconfig.configure(hookenv.config(),
                          extra_hostname=osi.lxd_cinder_name())
    except Exception as e:
        rdebug('could not announce the configuration to the other side: {e}'
               .format(e=e))


@reactive.when('storpool-block.block-started')
@reactive.when('storpool-osi.installed-into-lxds')
@reactive.when_not('storpool-block-charm.stopped')
def ready():
    rdebug('ready to go')
    hookenv.status_set('active', 'so far so good so what')


@reactive.hook('stop')
def stop_and_propagate():
    rdebug('a stop event was received')

    rdebug('letting storpool-openstack-integration know')
    reactive.set_state('storpool-osi.stop')
    reactive.set_state('storpool-osi.no-propagate-stop')

    rdebug('letting storpool-block know')
    reactive.set_state('storpool-block.stop')

    rdebug('done here, it seems')
    reactive.set_state('storpool-block-charm.stopped')
