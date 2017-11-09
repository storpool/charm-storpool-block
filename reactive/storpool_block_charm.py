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
import os
import platform
import subprocess
import tempfile

from charms import reactive
from charmhelpers.core import hookenv, host

from spcharms import config as spconfig
from spcharms import osi
from spcharms import service_hook
from spcharms import txn
from spcharms import states as spstates
from spcharms import status as spstatus
from spcharms import utils as sputils


def block_conffile():
    """
    Return the name of the configuration file that will be generated for
    the `storpool_block` service in order to also export the block devices
    into the host's LXD containers.
    """
    return '/etc/storpool.conf.d/storpool-cinder-block.conf'


def rdebug(s):
    """
    Pass the diagnostic message string `s` to the central diagnostic logger.
    """
    sputils.rdebug(s, prefix='block-charm')


@reactive.hook('install')
def install_setup():
    """
    Note that the storpool-repo-add layer should reset the status error
    messages on "config-changed" and "upgrade-charm" hooks.
    """
    spconfig.set_meta_config(None)
    spstatus.set_status_reset_handler('storpool-repo-add')


@reactive.hook('config-changed')
def config_changed():
    """
    Fire any handlers necessary.
    """
    spstates.handle_event('upgrade-charm')


@reactive.hook('upgrade-charm')
def upgrade_setup():
    """
    Note that the storpool-repo-add layer should reset the status error
    messages on "config-changed" and "upgrade-charm" hooks.
    """
    spstatus.set_status_reset_handler('storpool-repo-add')

    # Make sure we announce our presence again if necessary and
    # when possible
    reactive.set_state('storpool-block-charm.announce-presence')

    # Also, fire state handlers as necessary.
    spstates.handle_event('upgrade-charm')


@reactive.hook('leader-elected')
def we_are_the_leader():
    """
    Make note of the fact that this unit has been elected as the leader for
    the `storpool-block` charm.  This will prompt the unit to send presence
    information to the other charms along the `storpool-presence` hook.
    """
    rdebug('have we really been elected leader?')
    if not hookenv.is_leader():
        rdebug('false alarm...')
        reactive.remove_state('storpool-block-charm.leader')
        return
    rdebug('but really?')
    try:
        hookenv.leader_set(charm_storpool_block_unit=sputils.get_machine_id())
    except Exception as e:
        rdebug('no, could not run leader_set: {e}'.format(e=e))
        reactive.remove_state('storpool-block-charm.leader')
        return
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

    reactive.set_state('storpool-block-charm.announce-presence')
    reactive.set_state('storpool-service.changed')


def ensure_our_presence():
    """
    Make sure that our node is declared as present.
    """
    rdebug('about to make sure that we are represented in the presence data')
    state = service_hook.get_present_nodes()
    rdebug('got some state: {state}'.format(state=state))

    # Let us make sure our own data is here
    sp_node = sputils.get_machine_id()
    oid = spconfig.get_our_id()
    if sp_node not in state:
        rdebug('adding our own node {sp_node}'.format(sp_node=sp_node))
        service_hook.add_present_node(sp_node, oid, 'block-p')
        rdebug('something changed, will announce (if leader): {state}'
               .format(state=service_hook.get_present_nodes()))
        reactive.set_state('storpool-block-charm.announce-presence')


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
    ensure_our_presence()
    reactive.remove_state('storpool-block-charm.announce-presence')

    cfg = hookenv.config()
    rel_ids = hookenv.relation_ids('storpool-presence')
    rdebug('- got rel_ids {rel_ids}'.format(rel_ids=rel_ids))
    for rel_id in rel_ids:
        rdebug('  - trying for {rel_id}'.format(rel_id=rel_id))
        data = json.dumps({
            'presence': service_hook.get_present_nodes(),
            'storpool_conf': cfg['storpool_conf'],
            'storpool_version': cfg['storpool_version'],
            'storpool_openstack_version': cfg['storpool_openstack_version'],
        })
        hookenv.relation_set(rel_id,
                             storpool_presence=data)
        rdebug('  - done with {rel_id}'.format(rel_id=rel_ids))
    rdebug('- done with the rel_ids')


def remove_block_conffile(confname):
    """
    Remove a previously-created storpool_block config file that
    instructs it to expose devices to LXD containers.
    """
    rdebug('no Cinder LXD containers found, checking for '
           'any previously stored configuration...')
    removed = False
    if os.path.isfile(confname):
        rdebug('- yes, {confname} exists, removing it'
               .format(confname=confname))
        try:
            os.unlink(confname)
            removed = True
        except Exception as e:
            rdebug('could not remove {confname}: {e}'
                   .format(confname=confname, e=e))
    elif os.path.exists(confname):
        rdebug('- well, {confname} exists, but it is not a file; '
               'removing it anyway'.format(confname=confname))
        subprocess.call(['rm', '-rf', '--', confname])
        removed = True
    if removed:
        rdebug('- let us try to restart the storpool_block service ' +
               '(it may not even have been started yet, so ignore errors)')
        try:
            if host.service_running('storpool_block'):
                rdebug('  - well, it does seem to be running, so ' +
                       'restarting it')
                host.service_restart('storpool_block')
            else:
                rdebug('  - nah, it was not running at all indeed')
        except Exception as e:
            rdebug('  - could not restart the service, but '
                   'ignoring the error: {e}'.format(e=e))


@reactive.when('storpool-presence.process-lxd-name')
def create_block_conffile(hk):
    """
    Instruct storpool_block to create devices in a container's filesystem.
    """
    rdebug('create_block_conffile() invoked')
    reactive.remove_state('storpool-presence.process-lxd-name')
    confname = block_conffile()
    cinder_name = osi.lxd_cinder_name()
    if cinder_name is None or cinder_name == '':
        rdebug('no Cinder containers to tell storpool_block about')
        remove_block_conffile(confname)
        return
    rdebug('- analyzing a machine name: {name}'.format(name=cinder_name))

    # Now is there actually an LXD container by that name here?
    lxc_text = sputils.exec(['lxc', 'list', '--format=json'])
    rdebug('RDBG lxc_text is {text}'.format(text=lxc_text))
    if lxc_text['res'] != 0:
        rdebug('no LXC containers at all here')
        remove_block_conffile(confname)
        return
    try:
        lxcs = json.loads(lxc_text['out'])
        pattern = '-' + cinder_name.replace('/', '-')
        found = None
        for lxc in lxcs:
            if lxc['name'].endswith(pattern):
                found = lxc
                break
        if found is None:
            rdebug('no running {pat} LXC container'.format(pat=pattern))
            remove_block_conffile(confname)
            return
        lxc_name = found['name']
    except Exception as e:
        rdebug('could not parse the output of "lxc list --format=json": {e}'
               .format(e=e))
        remove_block_conffile(confname)
        return

    rdebug('found a Cinder container at "{name}"'.format(name=lxc_name))
    try:
        rdebug('about to record the name of the Cinder LXD - "{name}" - '
               'into {confname}'
               .format(name=lxc_name, confname=confname))
        dirname = os.path.dirname(confname)
        rdebug('- checking for the {dirname} directory'
               .format(dirname=dirname))
        if not os.path.isdir(dirname):
            rdebug('  - nah, creating it')
            os.mkdir(dirname, mode=0o755)

        rdebug('- is the file there?')
        okay = False
        expected_contents = [
            '[{node}]'.format(node=platform.node()),
            'SP_EXTRA_FS=lxd:{name}'.format(name=lxc_name)
        ]
        if os.path.isfile(confname):
            rdebug('  - yes, it is... but does it contain the right data?')
            with open(confname, mode='r') as conffile:
                contents = list(map(lambda s: s.rstrip(),
                                    conffile.readlines()))
                if contents == expected_contents:
                    rdebug('   - whee, it already does!')
                    okay = True
                else:
                    rdebug('   - it does NOT: {lst}'.format(lst=contents))
        else:
            rdebug('   - nah...')
            if os.path.exists(confname):
                rdebug('     - but it still exists?!')
                subprocess.call(['rm', '-rf', '--', confname])
                if os.path.exists(confname):
                    rdebug('     - could not remove it, so leaving it '
                           'alone, I guess')
                    okay = True

        if not okay:
            rdebug('- about to recreate the {confname} file'
                   .format(confname=confname))
            with tempfile.NamedTemporaryFile(dir='/tmp',
                                             mode='w+t') as spconf:
                print('\n'.join(expected_contents), file=spconf)
                spconf.flush()
                txn.install('-o', 'root', '-g', 'root', '-m', '644', '--',
                            spconf.name, confname)
            rdebug('- looks like we are done with it')
            rdebug('- let us try to restart the storpool_block service '
                   '(it may not even have been started yet, so '
                   'ignore errors)')
            try:
                if host.service_running('storpool_block'):
                    rdebug('  - well, it does seem to be running, '
                           'so restarting it')
                    host.service_restart('storpool_block')
                else:
                    rdebug('  - nah, it was not running at all indeed')
            except Exception as e:
                rdebug('  - could not restart the service, but '
                       'ignoring the error: {e}'.format(e=e))
    except Exception as e:
        rdebug('could not check for and/or recreate the {confname} '
               'storpool_block config file adapted the "{name}" '
               'LXD container: {e}'
               .format(confname=confname, name=lxc_name, e=e))


@reactive.when('storpool-block.block-started')
@reactive.when('storpool-osi.installed')
@reactive.when_not('storpool-block-charm.stopped')
def ready():
    """
    When the StorPool block service has been installed and the OpenStack
    integration has been installed everywhere, set the unit's status to
    `active`.
    """
    rdebug('ready to go')
    ensure_our_presence()
    spstatus.set('active', 'so far so good so what')


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
