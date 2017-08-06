from __future__ import print_function

import json
import subprocess
import time

from charms import reactive
from charmhelpers.core import hookenv

from spcharms import repo as sprepo
from spcharms import config as spconfig
from spcharms import txn

def rdebug(s):
	with open('/tmp/storpool-charms.log', 'a') as f:
		print('{tm} [block-charm] {s}'.format(tm=time.ctime(), s=s), file=f)

@reactive.when('storpool-block.block-started')
@reactive.when('storpool-osi.installed-into-lxds')
def whee():
	rdebug('wheeeeeee')

	hookenv.status_set('maintenance', 'checking our storpool-repo-add installation')
	policy = sprepo.apt_pkg_policy(['txn-install', 'storpool-config', 'meowmeow'])
	rdebug('got some kind of policy: {p}'.format(p=policy))

	hookenv.status_set('maintenance', 'checking our storpool-config installation')
	lines_b = subprocess.check_output(['/usr/sbin/storpool_confshow', '-n', 'SP_OURID'])
	lines = lines_b.decode().split('\n')
	rdebug('got some kind of output from storpool_confshow -n SP_OURID: {out}'.format(out=lines))

	hookenv.status_set('maintenance', 'checking the network configuration')
	cfg = spconfig.get_dict()
	rdebug('got {len} keys in the spconfig dict'.format(len=len(cfg)))
	ifaces = cfg['SP_IFACE'].split(',')
	rdebug('got interfaces: {ifaces}'.format(ifaces=ifaces))
	for iface in ifaces:
		hookenv.status_set('maintenance', 'checking the network configuration: {iface}'.format(iface=iface))
		out_b = subprocess.check_output(['ip', 'link', 'show', 'dev', iface])
		out = out_b.decode().split('\n')
		rdebug('got {len} lines for interface {iface}'.format(len=len(out), iface=iface))
		if len(out) < 1:
			hookenv.status_set('error', 'no configuration fetched for interface {iface}'.format(iface=iface))
			return
		line = out[0]
		rdebug('first line: {line}'.format(line=line))
		if line.find('state UP') == -1:
			hookenv.status_set('error', 'interface {iface} does not seem to be up: {line}'.format(iface=iface, line=line))
			return
		if line.find('mtu 9000') == -1:
			hookenv.status_set('error', 'interface {iface} does not seem to have an MTU of 9000: {line}'.format(iface=iface, line=line))
			return
	rdebug('looks like the network interfaces check out')

	hookenv.status_set('maintenance', 'trying to figure out which StorPool services are running')
	out_b = subprocess.check_output(['storpool', '-Bj', 'service', 'list'])
	out = json.loads(out_b.decode())
	rdebug('storpool service list returned {out}'.format(out=out))

	hookenv.status_set('maintenance', 'trying to obtain the StorPool disk IDs through Python')
	cfg = spconfig.get_dict()
	out_b = subprocess.check_output(['python2', '-c', 'from storpool import spapi; a = spapi.Api(host="{host}", port={port}, auth="{auth}"); print str.join(" ", map(str, sorted(a.disksList().keys())))'.format(host=cfg['SP_API_HTTP_HOST'], port=cfg['SP_API_HTTP_PORT'], auth=cfg['SP_AUTH_TOKEN'])])
	rdebug('disks found: {disks}'.format(disks=out_b.decode().split('\n')[0]))

	hookenv.status_set('maintenance', 'trying to build a list of LXC objects')
	lst = list(txn.LXD.list_all())
	rdebug('LXD.list_all() returned {lst}'.format(lst=lst))
	objs = list(txn.LXD.construct_all())
	rdebug('LXD.construct_all() returned a list of {ln} objects:'.format(ln=len(objs)))
	for obj in objs:
		rdebug('- name "{name}" prefix "{prefix}"'.format(name=obj.name, prefix=obj.txn.prefix))

	hookenv.status_set('active', 'so far so good so what')
