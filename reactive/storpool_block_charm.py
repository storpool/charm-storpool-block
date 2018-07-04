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
from charmhelpers.core import hookenv, host, unitdata

from spcharms import config as spconfig
from spcharms import error as sperror
from spcharms import kvdata
from spcharms import osi
from spcharms import service_hook
from spcharms import txn
from spcharms import status as spstatus
from spcharms import utils as sputils

from spcharms.run import storpool_block as run_block


RELATIONS = ['block-p', 'storpool-presence']


def block_conffile():
    """
    Return the name of the configuration file that will be generated for
    the `storpool_block` service in order to also export the block devices
    into the host's LXD containers.
    """
    return '/etc/storpool.conf.d/storpool-cinder-block.conf'


def rdebug(s, cond=None):
    """
    Pass the diagnostic message string `s` to the central diagnostic logger.
    """
    sputils.rdebug(s, prefix='block-charm', cond=cond)


@reactive.hook('install')
def install_setup():
    """
    Note that the storpool-repo-add layer should reset the status error
    messages on "config-changed" and "upgrade-charm" hooks.
    """
    spconfig.set_meta_config(None)
    spstatus.set_status_reset_handler('storpool-repo-add')
    run()


@reactive.hook('config-changed')
def config_changed():
    """
    Try to (re-)install everything and re-announce.
    """
    if reactive.is_state('storpool-block-charm.leader'):
        reactive.set_state('storpool-block-charm.bump-generation')
    run()


@reactive.hook('upgrade-charm')
def upgrade_setup():
    """
    Try to (re-)install everything and re-announce.
    """
    run()


@reactive.hook('start')
def start_service():
    """
    Try to (re-)install everything.
    """
    run()


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


@reactive.when('storpool-block-charm.services-started')
@reactive.when('block-p.notify')
def peers_changed(_):
    try_announce()
    update_status()


@reactive.when('storpool-block-charm.services-started')
@reactive.when('storpool-presence.notify')
def cinder_changed(_):
    try_announce()
    update_status()


def try_announce():
    try:
        announce_presence()
        # Only reset the flag afterwards so that if anything
        # goes wrong we can retry this later
        # It should be safe to reset both states at once; we always
        # fetch data on both hooks, so we can't miss anything.
        reactive.remove_state('block-p.notify')
        reactive.remove_state('block-p.notify-joined')
        reactive.remove_state('storpool-presence.notify')
        reactive.remove_state('storpool-presence.notify-joined')
    except Exception as e:
        raise  # FIXME: hookenv.log and then exit(42)?


def build_presence(current):
    current['id'] = spconfig.get_our_id()
    current['hostname'] = platform.node()

    cfg = hookenv.config()
    keys = (
        'storpool_conf',
        'storpool_repo_url',
        'storpool_version',
        'storpool_openstack_version',
    )
    missing = list(filter(lambda k: cfg.get(k) is None, keys))
    if reactive.is_state('storpool-block-charm.leader') and not missing:
        current['config'] = {
            'storpool_conf': cfg['storpool_conf'],
            'storpool_repo_url': cfg['storpool_repo_url'],
            'storpool_version': cfg['storpool_version'],
            'storpool_openstack_version': cfg['storpool_openstack_version'],
        }


def announce_presence(force=False):
    data = service_hook.fetch_presence(RELATIONS)

    mach_id = 'block:' + sputils.get_machine_id()
    our_node = data['nodes'].get(mach_id)

    announce = force
    block_joined = reactive.is_state('block-p.notify-joined')
    cinder_joined = reactive.is_state('storpool-presence.notify-joined')
    if cinder_joined or block_joined:
        announce = True

    generation = int(data['generation'])
    if generation < 0:
        generation = 0

    if reactive.is_state('storpool-block-charm.bump-generation'):
        generation = generation + 1
        announce = True

    if announce:
        our_node = {
            'generation': generation
        }
        build_presence(our_node)
        ndata = {
            'generation': generation,

            'nodes': {
                mach_id: our_node,
            },
        }
        rdebug('announcing {data}'.format(data=ndata),
               cond='announce')
        service_hook.send_presence(ndata, RELATIONS)

    reactive.remove_state('storpool-block-charm.bump-generation')

    check_for_new_presence(data)


def check_for_new_presence(data):
    found = None
    old = unitdata.kv().get(kvdata.KEY_LXD_NAME)
    our_mach_id = sputils.get_machine_id()

    for node in data['nodes']:
        if not node.startswith('cinder:'):
            continue
        mach_id = node[7:]
        parts = mach_id.split('/')
        if len(parts) == 3 and parts[1] == 'lxd':
            if parts[0] == our_mach_id:
                rdebug('found our container: {lx}'.format(lx=mach_id),
                       cond='announce')
                if found is None:
                    found = mach_id
                    if old is None or old != mach_id:
                        rdebug('setting Cinder container {mach_id}'
                               .format(mach_id=mach_id))
                        unitdata.kv().set(kvdata.KEY_LXD_NAME, mach_id)
                        reactive.set_state('storpool-block-charm.lxd')

    if not found:
        rdebug('- no Cinder containers here', cond='announce')
        if old is not None:
            rdebug('forgetting about Cinder container {old}'.format(old=old))
            unitdata.kv().set(kvdata.KEY_LXD_NAME, None)
            reactive.set_state('storpool-block-charm.lxd')


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


@reactive.when('storpool-block-charm.lxd')
def create_block_conffile():
    """
    Instruct storpool_block to create devices in a container's filesystem.
    """
    rdebug('create_block_conffile() invoked')
    reactive.remove_state('storpool-block-charm.lxd')
    confname = block_conffile()
    cinder_name = unitdata.kv().get(kvdata.KEY_LXD_NAME)
    if cinder_name is None or cinder_name == '':
        rdebug('no Cinder containers to tell storpool_block about')
        remove_block_conffile(confname)
        return
    rdebug('- analyzing a machine name: {name}'.format(name=cinder_name))

    # Now is there actually an LXD container by that name here?
    lxc_text = sputils.exec(['lxc', 'list', '--format=json'])
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


def ready():
    """
    When the StorPool block service has been installed and the OpenStack
    integration has been installed everywhere, set the unit's status to
    `active`.
    """
    rdebug('ready to go')
    try_announce()
    update_status()


def run(reraise=False):
    def reraise_or_fail():
        if reraise:
            raise
        else:
            nonlocal failed
            failed = True

    failed = False
    try:
        reactive.remove_state('storpool-block-charm.services-started')
        rdebug('Run, block, run!')
        run_block.run()
        reactive.set_state('storpool-block-charm.services-started')
        rdebug('It seems that the storpool-block setup has run its course')
        ready()
    except sperror.StorPoolNoConfigException as e_cfg:
        hookenv.log('StorPool: missing configuration: {m}'
                    .format(m=', '.join(e_cfg.missing)),
                    hookenv.INFO)
    except sperror.StorPoolPackageInstallException as e_pkg:
        hookenv.log('StorPool: could not install the {names} packages: {e}'
                    .format(names=' '.join(e_pkg.names), e=e_pkg.cause),
                    hookenv.ERROR)
        reraise_or_fail()
    except sperror.StorPoolNoCGroupsException as e_cfg:
        hookenv.log('StorPool: {e}'.format(e=e_cfg), hookenv.ERROR)
        reraise_or_fail()
    except sperror.StorPoolException as e:
        hookenv.log('StorPool installation problem: {e}'.format(e=e))
        reraise_or_fail()

    if failed:
        exit(42)


def get_status():
    inst = reactive.is_state('storpool-block-charm.services-started')
    status = {
        'node': sputils.get_machine_id(),
        'charm-config': hookenv.config(),
        'installed': inst,
        'presence': service_hook.fetch_presence(RELATIONS),
        'lxd': unitdata.kv().get(kvdata.KEY_LXD_NAME),

        'ready': False,
    }

    for name in (
      'storpool_repo_url',
      'storpool_version',
      'storpool_openstack_version',
      'storpool_conf'
    ):
        value = status['charm-config'].get(name)
        if value is None or value == '':
            status['message'] = 'No {name} in the config'.format(name=name)
            return status
    if not inst:
        status['message'] = 'Packages not installed yet'
        return status

    spstatus.set('maintenance', 'checking the StorPool configuration')
    rdebug('about to try to obtain our StorPool ID')
    try:
        out = subprocess.check_output(['storpool_showconf', '-ne', 'SP_OURID'])
        out = out.decode()
        out = out.split('\n')
        our_id = out[0]
    except Exception as e:
        status['message'] = 'Could not obtain the StorPool ID: {e}' \
                             .format(e=e)
        return status

    spstatus.set('maintenance', 'checking the Cinder and Nova processes...')
    found = False
    status['proc'] = {}
    for cmd in ('cinder-volume', 'nova-compute'):
        d = osi.check_spopenstack_processes(cmd)
        if d:
            found = True
        status['proc'][cmd] = d
        bad = sorted(filter(lambda pid: not d[pid], d.keys()))
        if bad:
            status['message'] = 'No spopenstack group: {pid}'.format(pid=bad)
            return status

    if found:
        spstatus.set('maintenance', 'checking for the spool directory')
        dirname = '/var/spool/openstack-storpool'
        if not os.path.isdir(dirname):
            status['message'] = 'No {d} directory'.format(d=dirname)
            return status
        st = os.stat(dirname)
        if not st.st_mode & 0o0020:
            status['message'] = '{d} not group-writable'.format(d=dirname)
            return status

    spstatus.set('maintenance', 'checking the StorPool services...')
    svcs = ('storpool_beacon', 'storpool_block')
    rdebug('checking for services: {svcs}'.format(svcs=svcs))
    missing = list(filter(lambda s: not host.service_running(s), svcs))
    rdebug('missing: {missing}'.format(missing=missing))
    if missing:
        status['message'] = 'StorPool services not running: {missing}' \
                            .format(missing=' '.join(missing))
        return status

    spstatus.set('maintenance', 'querying the StorPool API')
    rdebug('checking the network status of the StorPool client')
    try:
        out = subprocess.check_output(['storpool', '-jB', 'service', 'list'])
        out = out.decode()
        data = json.loads(out)
        rdebug('got API response: {d}'.format(d=data))
        if 'error' in data:
            raise Exception('API response: {d}'
                            .format(d=data['error']['descr']))
        state = data['data']['clients'][our_id]['status']
        rdebug('got our client status {st}'.format(st=state))
        if state != 'running':
            status['message'] = 'StorPool client: {st}'.format(st=state)
            return status
    except Exception as e:
        status['message'] = 'Could not query the StorPool API: {e}' \
                            .format(e=e)
        return status

    spstatus.set('maintenance', 'querying the StorPool API for client status')
    rdebug('checking the status of the StorPool client')
    try:
        out = subprocess.check_output(['storpool', '-jB', 'client', 'status'])
        out = out.decode()
        data = json.loads(out)
        rdebug('got API response: {d}'.format(d=data))
        if 'error' in data:
            raise Exception('API response: {d}'
                            .format(d=data['error']['descr']))
        int_id = int(our_id)
        found = list(filter(lambda e: e['id'] == int_id, data['data']))
        if not found:
            raise Exception('No client status reported for {our_id}'
                            .format(i=our_id))
        state = found[0]['configStatus']
        status['ready'] = state == 'ok'
        status['message'] = 'StorPool client: {st}'.format(st=state)
        return status
    except Exception as e:
        status['message'] = 'Could not query the StorPool API: {e}' \
                            .format(e=e)
        return status


@reactive.when('storpool-block-charm.sp-run')
def sp_run():
    # Yes, removing it at once, not after the fact.  If something
    # goes wrong, the action may be reissued.
    reactive.remove_state('storpool-block-charm.sp-run')
    try:
        run(reraise=True)
    except BaseException as e:
        s = 'Could not rerun the StorPool configuration: {e}'.format(e=e)
        hookenv.log(s, hookenv.ERROR)
        hookenv.action_fail(s)


@reactive.hook('update-status')
def update_status():
    try:
        status = get_status()
        spstatus.set('active' if status['ready'] else 'maintenance',
                     status['message'])
    except BaseException as e:
        s = 'Querying the StorPool status: {e}'.format(e=e)
        hookenv.log(s, hookenv.ERROR)
        spstatus.set('maintenance', s)


@reactive.when('storpool-block-charm.sp-status')
def sp_status():
    # Yes, removing it at once, not after the fact.  If something
    # goes wrong, the action may be reissued.
    reactive.remove_state('storpool-block-charm.sp-status')
    try:
        status = get_status()
        hookenv.action_set({'status': json.dumps(status)})
    except BaseException as e:
        s = 'Querying the StorPool status: {e}'.format(e=e)
        hookenv.log(s, hookenv.ERROR)
        hookenv.action_fail(s)


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

    rdebug('letting storpool-block know')
    run_block.stop()

    rdebug('done here, it seems')
    reactive.set_state('storpool-block-charm.stopped')
